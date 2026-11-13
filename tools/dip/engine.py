"""
tools/dip/engine.py — Three-Point Dip engine

Fits the plane of a bed, contact or fault through three or more points
(elevations read on the DEM or given), reports dip, dip direction, strike
and residuals, and traces the predicted outcrop line where the plane meets
the topography (zero contour of DEM − plane, GDAL ContourGenerate).

Geographic DEMs are handled in a local metric frame (metres per degree at
the centre latitude) so that dips are true angles.

The geometry lives in core/structural.py (pure, unit-tested).

Authors: RockMorph contributors / Tony
"""

from __future__ import annotations

import math
from typing import List, Optional, Sequence, Tuple

import numpy as np  # type: ignore
from osgeo import gdal, ogr, osr  # type: ignore
from qgis.core import (  # type: ignore
    QgsFeature, QgsField, QgsGeometry, QgsRasterLayer, QgsVectorLayer,
)
from qgis.PyQt.QtCore import QCoreApplication, QVariant  # type: ignore

from ...base.base_engine import BaseEngine
from ...core.raster import RasterReader
from ...core.structural import DegeneratePoints, fit_plane, height_above_plane, plane_reaches_surface


def tr(message: str) -> str:
    return QCoreApplication.translate("RockMorph", message)


class DipEngine(BaseEngine):
    """Plane orientation and predicted outcrop from points on a DEM."""

    def validate(self, **kwargs) -> bool:
        dem = kwargs.get("dem_layer")
        return dem is not None and dem.isValid() and len(kwargs.get("points") or []) >= 3

    def compute(self, **kwargs) -> dict:
        """
        Parameters
        ----------
        dem_layer      : QgsRasterLayer (its CRS is the CRS of ``points``).
        points         : [(x, y, z or None)] in the DEM CRS; a missing z is
                         read on the DEM.
        predict_outcrop: trace the zero contour of DEM − plane.
        radius_m       : outcrop search radius around the points (0 = DEM).
        progress_callback

        Returns
        -------
        {"dip", "dip_direction", "strike", "notation", "rms_m",
         "max_residual_m", "points": [(x, y, z, residual)], "centroid": (x, y),
         "outcrop_layer": QgsVectorLayer | None, "warnings": [...]}
        """
        dem_layer = kwargs["dem_layer"]
        progress = kwargs.get("progress_callback")
        reader = RasterReader(dem_layer)
        geographic = dem_layer.crs().isGeographic()
        warnings: List[str] = []

        points = []
        for x, y, z in kwargs["points"]:
            if z is None or (isinstance(z, float) and math.isnan(z)):
                z = reader.sample_at(x, y)
                if reader.nodata is not None and z == reader.nodata:
                    z = float("nan")
            if not np.isfinite(z):
                raise ValueError(tr("A point lies outside the DEM or on NoData: {:.1f}, {:.1f}.").format(x, y))
            points.append((float(x), float(y), float(z)))

        x0, y0 = np.mean([p[0] for p in points]), np.mean([p[1] for p in points])
        ux, uy = self._metres_per_unit(geographic, y0)

        def to_metres(x, y):
            return (np.asarray(x) - x0) * ux, (np.asarray(y) - y0) * uy

        local = [(*to_metres(x, y), z) for x, y, z in points]
        try:
            plane = fit_plane(local)
        except DegeneratePoints as error:
            raise ValueError(tr(str(error)))

        if progress:
            progress(40, tr("Plane fitted: {}.").format(plane.notation))

        outcrop_layer = None
        if kwargs.get("predict_outcrop", True):
            outcrop_layer = self._outcrop(reader, dem_layer, plane, to_metres, (x0, y0),
                                          float(kwargs.get("radius_m", 0.0)), ux, uy, warnings, plane.notation)
        if progress:
            progress(100, tr("Done."))

        return {
            "dip": plane.dip, "dip_direction": plane.dip_direction, "strike": plane.strike,
            "notation": plane.notation, "rms_m": plane.rms_m, "max_residual_m": plane.max_residual_m,
            "points": [(x, y, z, r) for (x, y, z), r in zip(points, plane.residuals_m)],
            "centroid": (float(x0), float(y0)),
            "outcrop_layer": outcrop_layer,
            "warnings": warnings,
        }

    # ------------------------------------------------------------------

    @staticmethod
    def _metres_per_unit(geographic: bool, latitude: float) -> Tuple[float, float]:
        if not geographic:
            return 1.0, 1.0
        return 111320.0 * math.cos(math.radians(latitude)), 110574.0

    def _outcrop(self, reader, dem_layer, plane, to_metres, centre, radius_m, ux, uy,
                 warnings, notation) -> Optional[QgsVectorLayer]:
        """Zero contour of DEM − plane, as a memory line layer in the DEM CRS."""
        gt = reader.geo_transform
        dem = reader.array.astype(np.float64, copy=True)
        if reader.nodata is not None:
            dem[dem == reader.nodata] = np.nan
        rows, cols = dem.shape
        r0, r1, c0, c1 = 0, rows, 0, cols
        if radius_m > 0:
            # Window of ± radius around the centroid of the points.
            col_c = (centre[0] - gt[0]) / gt[1]
            row_c = (centre[1] - gt[3]) / gt[5]
            half_c = radius_m / (abs(gt[1]) * ux)
            half_r = radius_m / (abs(gt[5]) * uy)
            c0, c1 = max(0, int(col_c - half_c)), min(cols, int(col_c + half_c) + 1)
            r0, r1 = max(0, int(row_c - half_r)), min(rows, int(row_c + half_r) + 1)
            if c1 - c0 < 2 or r1 - r0 < 2:
                warnings.append(tr("The search window is outside the DEM."))
                return None
        window = dem[r0:r1, c0:c1]
        x_centres = gt[0] + (np.arange(c0, c1) + 0.5) * gt[1]
        y_centres = gt[3] + (np.arange(r0, r1) + 0.5) * gt[5]
        xm, _ = to_metres(x_centres, np.zeros_like(x_centres))
        _, ym = to_metres(np.zeros_like(y_centres), y_centres)
        difference = height_above_plane(window, xm, ym, plane)
        if not plane_reaches_surface(difference):
            warnings.append(tr("The plane does not cut the topography in the search area: no outcrop trace."))
            return None

        # Difference raster in memory, then its zero contour.
        mem = gdal.GetDriverByName("MEM").Create("", c1 - c0, r1 - r0, 1, gdal.GDT_Float32)
        mem.SetGeoTransform((gt[0] + c0 * gt[1], gt[1], gt[2], gt[3] + r0 * gt[5], gt[4], gt[5]))
        wkt = dem_layer.crs().toWkt()
        mem.SetProjection(wkt)
        band = mem.GetRasterBand(1)
        band.WriteArray(np.where(np.isfinite(difference), difference, -99999.0).astype(np.float32))
        band.SetNoDataValue(-99999.0)
        srs = osr.SpatialReference()
        srs.ImportFromWkt(wkt)
        source = ogr.GetDriverByName("Memory").CreateDataSource("outcrop")
        contour_layer = source.CreateLayer("outcrop", srs=srs, geom_type=ogr.wkbLineString)
        contour_layer.CreateField(ogr.FieldDefn("id", ogr.OFTInteger))
        contour_layer.CreateField(ogr.FieldDefn("level", ogr.OFTReal))
        gdal.ContourGenerate(band, 0.0, 0.0, [0.0], 1, -99999.0, contour_layer, 0, 1)

        layer = QgsVectorLayer(f"LineString?crs={dem_layer.crs().authid() or 'EPSG:4326'}",
                               tr("Outcrop {}").format(notation), "memory")
        if not dem_layer.crs().authid():
            layer.setCrs(dem_layer.crs())
        provider = layer.dataProvider()
        provider.addAttributes([QgsField("plane", QVariant.String), QgsField("length_m", QVariant.Double)])
        layer.updateFields()
        features = []
        for item in contour_layer:
            geometry = QgsGeometry()
            geometry.fromWkb(bytes(item.GetGeometryRef().ExportToWkb()))
            if geometry.isEmpty():
                continue
            feature = QgsFeature(layer.fields())
            feature.setGeometry(geometry)
            length = geometry.length() * (ux if ux == uy else math.sqrt(ux * uy))
            feature.setAttributes([notation, round(length, 1)])
            features.append(feature)
        provider.addFeatures(features)
        layer.updateExtents()
        # Created in the worker thread: hand it to the GUI thread, where the
        # panel adds it to the project.
        application = QCoreApplication.instance()
        if application is not None:
            layer.moveToThread(application.thread())
        return layer if features else None
