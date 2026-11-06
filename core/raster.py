# core/raster.py

"""


Low-level raster access layer for RockMorph.
Wraps GDAL to provide clean numpy arrays and coordinate utilities.
No UI logic — pure computation.
"""

import os
import threading
from collections import OrderedDict

import numpy as np # type: ignore
from osgeo import gdal  # type: ignore 
from qgis.core import QgsRasterLayer, QgsProject  # type: ignore
from qgis.PyQt.QtCore import QCoreApplication  # type: ignore


def tr(message):
    return QCoreApplication.translate("RockMorph", message)


# ---------------------------------------------------------------------------
# Shared cache of decoded bands (audit M7)
# ---------------------------------------------------------------------------
# Several tools create a RasterReader for the same DEM many times (the
# fluvial profile sampler once per basin, each terrain-derivative solver...),
# and every instance used to read and decode the WHOLE raster again. Decoded
# bands are now kept in a small LRU cache keyed by file, band, size and
# modification time, so an edited file is re-read. Cached arrays are
# READ-ONLY: callers that need to modify a DEM copy it first (all current
# callers do), and an accidental in-place write raises instead of silently
# corrupting the DEM seen by other tools.

_CACHE_SIZE = 3
_cache: "OrderedDict[tuple, tuple]" = OrderedDict()
_cache_lock = threading.Lock()   # readers may run in worker threads


def _cache_key(path: str, band: int):
    try:
        stat = os.stat(path)
        return (os.path.abspath(path), band, stat.st_size, stat.st_mtime_ns)
    except OSError:
        return None   # not a plain file (e.g. a GDAL connection string): no caching


def clear_raster_cache() -> None:
    """Drop every cached band (e.g. on plugin unload)."""
    with _cache_lock:
        _cache.clear()


def gdal_source_path(layer: QgsRasterLayer) -> str:
    """
    Path GDAL can open for ``layer``. QGIS sources may carry provider
    options after a '|' (e.g. 'file.gpkg|layername=dem'); GDAL needs the
    part before it, or a GPKG:file:table string for GeoPackage rasters.
    """
    source = layer.source()
    if "|" not in source:
        return source
    path, _, options = source.partition("|")
    for option in options.split("|"):
        key, _, value = option.partition("=")
        if key.strip().lower() in ("layername", "table") and path.lower().endswith(".gpkg"):
            return f"GPKG:{path}:{value.strip()}"
    return path


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
        return self.nodata

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
        Read the band as float32 with NoData -> NaN, from the shared cache
        when the same file and band were read before.

        GDAL converts every pixel type to Float32 during the read
        (buf_type), so Int8, Int64 and UInt64 rasters (GDAL types 12-14,
        missing from the former type table, which then reshaped bytes with
        the wrong item size) are read correctly, and no intermediate copy in
        the original type is made.
        """
        path = gdal_source_path(self._layer)
        key = _cache_key(path, self._band)
        if key is not None:
            with _cache_lock:
                cached = _cache.get(key)
                if cached is not None:
                    _cache.move_to_end(key)
            if cached is not None:
                self._array, self._gt, self.nodata = cached
                return

        dataset = gdal.Open(path, gdal.GA_ReadOnly)
        if dataset is None:
            raise IOError(tr(f"GDAL could not open raster: {path}"))

        self._gt = dataset.GetGeoTransform()
        band = dataset.GetRasterBand(self._band)
        self.nodata = band.GetNoDataValue()
        xsize, ysize = dataset.RasterXSize, dataset.RasterYSize

        # Raw bytes converted to Float32 by GDAL — no gdal_array import,
        # which avoids NumPy ABI conflicts in some QGIS builds.
        raw_bytes = band.ReadRaster(0, 0, xsize, ysize, xsize, ysize, gdal.GDT_Float32)
        array = np.frombuffer(raw_bytes, dtype=np.float32).reshape((ysize, xsize)).copy()
        if self.nodata is not None:
            array[array == np.float32(self.nodata)] = np.nan
        array.setflags(write=False)
        self._array = array
        band = None
        dataset = None

        if key is not None:
            with _cache_lock:
                _cache[key] = (self._array, self._gt, self.nodata)
                _cache.move_to_end(key)
                while len(_cache) > _CACHE_SIZE:
                    _cache.popitem(last=False)

    def __repr__(self):
        rows, cols = self.shape
        return (
            f"RasterReader("
            f"layer='{self._layer.name()}', "
            f"shape=({rows}x{cols}), "
            f"pixel_size={self.pixel_size_x:.4f}, "
            f"geographic={self.is_geographic})"
        )