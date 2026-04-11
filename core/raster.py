# core/raster.py

"""


Low-level raster access layer for RockMorph.
Wraps GDAL to provide clean numpy arrays and coordinate utilities.
No UI logic — pure computation.
"""

import numpy as np # type: ignore
from osgeo import gdal  # type: ignore 
from qgis.core import QgsRasterLayer, QgsProject  # type: ignore
from PyQt5.QtCore import QCoreApplication  # type: ignore


def tr(message):
    return QCoreApplication.translate("RockMorph", message)


class RasterReader:
    """
    Opens a QGIS raster layer via GDAL and exposes:
      - numpy array of band data
      - nodata mask
      - GeoTransform utilities
      - CRS info

    Usage
    -----
    reader = RasterReader(dem_layer)
    array  = reader.array          # full numpy array, nodata → nan
    gt     = reader.geo_transform  # GDAL GeoTransform tuple
    crs    = reader.crs            # QgsCoordinateReferenceSystem
    """

    def __init__(self, layer: QgsRasterLayer, band: int = 1):
        """
        Parameters
        ----------
        layer : QgsRasterLayer — input DEM layer
        band  : int            — band index (1-based, default 1)
        """
        if not isinstance(layer, QgsRasterLayer) or not layer.isValid():
            raise ValueError(tr("Invalid raster layer."))

        self._layer = layer
        self._band  = band
        self._ds    = None
        self._array = None

        self._open()

    # ------------------------------------------------------------------
    # Public properties
    # ------------------------------------------------------------------

    @property
    def array(self) -> np.ndarray:
        """Full raster as float32 numpy array. NoData values → np.nan."""
        return self._array

    @property
    def geo_transform(self) -> tuple:
        """
        GDAL GeoTransform tuple:
        (x_origin, pixel_width, x_rotation,
         y_origin, y_rotation, pixel_height)
        pixel_height is negative (top-left origin).
        """
        return self._gt

    @property
    def crs(self):
        """QgsCoordinateReferenceSystem of the layer."""
        return self._layer.crs()

    @property
    def nodata_value(self) -> float:
        """NoData value or None."""
        return self._nodata

    @property
    def pixel_size_x(self) -> float:
        """Pixel width in map units."""
        return abs(self._gt[1])

    @property
    def pixel_size_y(self) -> float:
        """Pixel height in map units (always positive)."""
        return abs(self._gt[5])

    @property
    def shape(self) -> tuple:
        """Array shape (rows, cols)."""
        return self._array.shape

    @property
    def is_geographic(self) -> bool:
        """True if CRS is geographic (degrees), False if projected (metres)."""
        return self._layer.crs().isGeographic()

    # ------------------------------------------------------------------
    # Coordinate utilities
    # ------------------------------------------------------------------

    def world_to_pixel(self, x: float, y: float) -> tuple:
        """
        Convert map coordinates to pixel indices (col, row).
        Returns (-1, -1) if outside raster extent.
        """
        # Guard against NaN coordinates
        if x is None or y is None:
            return -1, -1
        if np.isnan(x) or np.isnan(y):
            return -1, -1

        gt = self._gt
        col = int((x - gt[0]) / gt[1])
        row = int((y - gt[3]) / gt[5])

        rows, cols = self._array.shape
        if not (0 <= col < cols and 0 <= row < rows):
            return -1, -1

        return col, row

    def pixel_to_world(self, col: int, row: int) -> tuple:
        """
        Convert pixel indices to map coordinates (centre of pixel).
        """
        gt = self._gt
        x = gt[0] + (col + 0.5) * gt[1]
        y = gt[3] + (row + 0.5) * gt[5]
        return x, y

    def sample_at(self, x: float, y: float) -> float:
        """
        Sample raster value at map coordinates (x, y).
        Returns np.nan if outside extent or nodata.
        """
        col, row = self.world_to_pixel(x, y)
        if col == -1:
            return np.nan
        val = self._array[row, col]
        return float(val)

    def sample_points(self, points: list) -> np.ndarray:
        """
        Sample raster at a list of (x, y) map coordinate tuples.
        Returns numpy array of float values (nan for nodata/outside).

        Parameters
        ----------
        points : list of (x, y) tuples
        """
        return np.array([self.sample_at(x, y) for x, y in points],
                        dtype=np.float32)

    # ------------------------------------------------------------------
    # Private
    # ------------------------------------------------------------------

    def _open(self):
        """
        Read raster using GDAL dataset directly into numpy
        without gdal_array — avoids NumPy version conflict.
        """
        path = self._layer.source()
        
        self._ds = gdal.Open(path, gdal.GA_ReadOnly)
        if self._ds is None:
            raise IOError(tr(f"GDAL could not open raster: {path}"))

        self._gt = self._ds.GetGeoTransform()

        band = self._ds.GetRasterBand(self._band)
        self._nodata = band.GetNoDataValue()

        # Read using struct — avoids gdal_array entirely
        import struct
        xsize  = self._ds.RasterXSize
        ysize  = self._ds.RasterYSize

        # Read as bytes then convert via numpy frombuffer
        data_type = band.DataType  # GDT_Float32=6, GDT_Int16=3, etc.
        
        # Use gdal.GDT to numpy dtype mapping
        gdal_to_numpy = {
            1: np.uint8,
            2: np.uint16,
            3: np.int16,
            4: np.uint32,
            5: np.int32,
            6: np.float32,
            7: np.float64,
        }
        dtype = gdal_to_numpy.get(data_type, np.float32)
        
        # ReadRaster returns raw bytes — no gdal_array needed
        raw_bytes = band.ReadRaster(
            0, 0, xsize, ysize,
            xsize, ysize,
            data_type
        )
        raw = np.frombuffer(raw_bytes, dtype=dtype)\
                .reshape((ysize, xsize))\
                .astype(np.float32)

        if self._nodata is not None:
            raw[raw == self._nodata] = np.nan

        self._array = raw
        self._ds = None

    def __repr__(self):
        rows, cols = self.shape
        return (
            f"RasterReader("
            f"layer='{self._layer.name()}', "
            f"shape=({rows}x{cols}), "
            f"pixel_size={self.pixel_size_x:.4f}, "
            f"geographic={self.is_geographic})"
        )