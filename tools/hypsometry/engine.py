# tools/hypsometry/engine.py

"""
tools/hypsometry/engine.py

HypsometryEngine — computes hypsometric curves for watershed basins.

Handles:
  - Geographic and projected CRS
  - NaN masking from DEM nodata
  - Invalid / empty / duplicate basin geometries
  - GDAL rasterization without gdal_array
  - Graceful per-basin error reporting

Authors: RockMorph contributors / Tony
"""

import numpy as np # type: ignore
from osgeo import gdal, ogr  # type: ignore
from qgis.core import (  # type: ignore
    QgsVectorLayer, QgsRasterLayer, QgsProject,
    QgsCoordinateTransform, QgsWkbTypes,
)
from PyQt5.QtCore import QCoreApplication  # type: ignore

from ...base.base_engine import BaseEngine
from ...core.raster import RasterReader


def tr(message):
    return QCoreApplication.translate("RockMorph", message)


# Minimum number of valid pixels to consider a basin computable
MIN_VALID_PIXELS = 10


class HypsometryEngine(BaseEngine):
    """
    Computes hypsometric curves for all features in a basin layer.

    Returns a list of per-basin result dicts, one per valid feature.
    Invalid basins are skipped with a warning — never crash the whole run.
    """

    def validate(self, dem_layer=None, basin_layer=None, **kwargs) -> bool:
        if dem_layer is None or not dem_layer.isValid():
            return False
        if basin_layer is None or not basin_layer.isValid():
            return False
        if basin_layer.geometryType() != QgsWkbTypes.PolygonGeometry:
            return False
        return True

    def compute(
        self,
        dem_layer:    QgsRasterLayer,
        basin_layer:  QgsVectorLayer,
        label_field:  str  = None,
        n_points:     int  = 200,
    ) -> dict:
        """
        Compute hypsometric curves for all basins.

        Parameters
        ----------
        dem_layer    : QgsRasterLayer
        basin_layer  : QgsVectorLayer  — polygon layer, one feature per basin
        label_field  : str | None      — attribute field to use as curve label
        n_points     : int             — number of points on the output curve

        Returns
        -------
        dict with keys:
            results  : list of per-basin dicts (see _compute_basin)
            skipped  : list of (fid, reason) for invalid basins
            warnings : list of human-readable warning strings
        """
        reader   = RasterReader(dem_layer)
        results  = []
        skipped  = []
        warnings = []

        # Resolve label field once
        resolved_label = self._resolve_label_field(basin_layer, label_field)

        # Detect duplicate geometries upfront
        duplicate_fids = self._find_duplicate_geometries(basin_layer)
        if duplicate_fids:
            warnings.append(
                tr(f"Duplicate geometries detected (FIDs: "
                   f"{duplicate_fids}) — keeping first occurrence only.")
            )

        seen_geom_hashes = set()

        for feature in basin_layer.getFeatures():
            fid   = feature.id()
            label = self._get_label(feature, resolved_label, fid)

            # --- Geometry validation ---
            geom = feature.geometry()
            if geom is None or geom.isEmpty():
                skipped.append((fid, "empty geometry"))
                warnings.append(tr(f"Basin '{label}' (FID {fid}): empty geometry — skipped."))
                continue

            if not geom.isGeosValid():
                # Attempt auto-repair
                geom = geom.makeValid()
                if not geom.isGeosValid():
                    skipped.append((fid, "invalid geometry, repair failed"))
                    warnings.append(tr(f"Basin '{label}' (FID {fid}): invalid geometry, could not repair — skipped."))
                    continue
                warnings.append(tr(f"Basin '{label}' (FID {fid}): geometry repaired automatically."))

            # --- Duplicate detection ---
            geom_hash = self._geometry_hash(geom)
            if geom_hash in seen_geom_hashes:
                skipped.append((fid, "duplicate geometry"))
                warnings.append(tr(f"Basin '{label}' (FID {fid}): duplicate geometry — skipped."))
                continue
            seen_geom_hashes.add(geom_hash)

            # --- Compute ---
            try:
                result = self._compute_basin(
                    feature, geom, label, fid,
                    reader, dem_layer, basin_layer,
                    n_points
                )
                if result is None:
                    skipped.append((fid, "insufficient valid pixels"))
                    warnings.append(tr(f"Basin '{label}' (FID {fid}): too few valid pixels — skipped."))
                    continue
                results.append(result)

            except Exception as e:
                skipped.append((fid, str(e)))
                warnings.append(tr(f"Basin '{label}' (FID {fid}): error — {e}"))
                continue

        return {
            "results":  results,
            "skipped":  skipped,
            "warnings": warnings,
        }

    # ------------------------------------------------------------------
    # Per-basin computation
    # ------------------------------------------------------------------

    def _compute_basin(
        self, feature, geom, label, fid,
        reader, dem_layer, basin_layer, n_points
    ):
        """
        Compute hypsometric curve for one basin feature.
        Returns None if not enough valid pixels.
        """
        # 1. Reproject basin geometry to DEM CRS if needed
        geom_dem_crs = self._reproject_geom(geom, basin_layer, dem_layer)

        # 2. Rasterize basin polygon onto DEM grid → binary mask
        mask = self._rasterize_feature(geom_dem_crs, reader)

        # 3. Extract DEM values inside basin
        dem_array = reader.array   # float32, nodata → nan
        values    = dem_array[mask == 1]
        values    = values[~np.isnan(values)]   # remove nodata

        if len(values) < MIN_VALID_PIXELS:
            return None

        # 4. Compute hypsometric curve
        x, y = self._hypsometric_curve(values, n_points)

        # 5. Compute integral (HI)
        hi = float(np.trapz(y, x))

        # 6. Stats
        area_km2 = self._area_km2(geom, basin_layer)

        return {
            "label":    label,
            "fid":      fid,
            "hi":       round(hi, 4),
            "area_km2": round(area_km2, 2),
            "min_elev": round(float(np.min(values)), 1),
            "max_elev": round(float(np.max(values)), 1),
            "relief":   round(float(np.max(values) - np.min(values)), 1),
            "x":        x.tolist(),
            "y":        y.tolist(),
            "n_pixels": int(len(values)),
        }

    # ------------------------------------------------------------------
    # Hypsometric curve
    # ------------------------------------------------------------------

    @staticmethod
    def _hypsometric_curve(values: np.ndarray, n_points: int):
        """
        Build normalized hypsometric curve from elevation values.

        X = cumulative area fraction  (0 = 100% of area above, 1 = 0%)
        Y = normalized elevation      (0 = min, 1 = max)

        Parameters
        ----------
        values   : 1D float array of valid elevation pixels
        n_points : number of output points

        Returns
        -------
        x, y : numpy arrays of length n_points
        """
        v_min = values.min()
        v_max = values.max()

        if v_max - v_min < 1e-6:
            # Flat basin — degenerate curve
            x = np.linspace(0, 1, n_points)
            y = np.full(n_points, 0.5)
            return x, y

        # Sort descending — highest elevation first
        sorted_vals = np.sort(values)[::-1]

        # Sample n_points evenly along the sorted array
        indices = np.linspace(0, len(sorted_vals) - 1, n_points).astype(int)
        sampled = sorted_vals[indices]

        x = np.linspace(0, 1, n_points)
        y = (sampled - v_min) / (v_max - v_min)

        return x, y

    # ------------------------------------------------------------------
    # GDAL rasterization — no gdal_array
    # ------------------------------------------------------------------

    def _rasterize_feature(self, geom_dem_crs, reader: RasterReader) -> np.ndarray:
        """
        Rasterize a single polygon geometry onto the DEM grid.
        Returns a binary uint8 mask — 1 inside, 0 outside.
        Uses GDAL in-memory datasets — no gdal_array, no temp files.
        """
        rows, cols = reader.shape
        gt         = reader.geo_transform

        # Create in-memory OGR datasource with the single feature
        mem_drv    = ogr.GetDriverByName("Memory")
        mem_ds     = mem_drv.CreateDataSource("mem")
        srs        = None   # CRS already matched to DEM
        mem_layer  = mem_ds.CreateLayer("basin", srs=srs)

        ogr_feat   = ogr.Feature(mem_layer.GetLayerDefn())
        ogr_geom   = ogr.CreateGeometryFromWkt(geom_dem_crs.asWkt())
        ogr_feat.SetGeometry(ogr_geom)
        mem_layer.CreateFeature(ogr_feat)

        # Create in-memory raster mask
        mem_raster_drv = gdal.GetDriverByName("MEM")
        mask_ds = mem_raster_drv.Create("", cols, rows, 1, gdal.GDT_Byte)
        mask_ds.SetGeoTransform(gt)

        # Burn value 1 inside polygon
        mask_band = mask_ds.GetRasterBand(1)
        mask_band.Fill(0)
        gdal.RasterizeLayer(mask_ds, [1], mem_layer, burn_values=[1])
        mask_ds.FlushCache()

        # Read mask as numpy — same pattern as RasterReader (no gdal_array)
        raw = mask_band.ReadRaster(0, 0, cols, rows, cols, rows, gdal.GDT_Byte)
        mask = np.frombuffer(raw, dtype=np.uint8).reshape((rows, cols))

        # Cleanup
        mem_ds    = None
        mask_ds   = None

        return mask

    # ------------------------------------------------------------------
    # CRS reprojection
    # ------------------------------------------------------------------

    def _reproject_geom(self, geom, basin_layer, dem_layer):
        """
        Reproject basin geometry to DEM CRS if needed.
        Returns a new QgsGeometry in DEM CRS.
        """
        basin_crs = basin_layer.crs()
        dem_crs   = dem_layer.crs()

        if basin_crs == dem_crs:
            return geom

        transform = QgsCoordinateTransform(
            basin_crs, dem_crs, QgsProject.instance()
        )
        geom_copy = geom.__copy__() if hasattr(geom, '__copy__') else geom.clone() if hasattr(geom, 'clone') else geom
        geom_copy.transform(transform)
        return geom_copy

    # ------------------------------------------------------------------
    # Area computation — handles geographic CRS
    # ------------------------------------------------------------------

    @staticmethod
    def _area_km2(geom, basin_layer) -> float:
        """
        Compute basin area in km².
        Uses ellipsoidal area for geographic CRS, planar for projected.
        """
        from qgis.core import QgsDistanceArea  # type: ignore
        da = QgsDistanceArea()
        da.setSourceCrs(basin_layer.crs(), QgsProject.instance().transformContext())
        da.setEllipsoid('WGS84')
        area_m2 = da.measureArea(geom)
        return area_m2 / 1e6

    # ------------------------------------------------------------------
    # Duplicate detection
    # ------------------------------------------------------------------

    @staticmethod
    def _geometry_hash(geom) -> str:
        """
        Lightweight geometry hash for duplicate detection.
        Based on WKT — fast enough for typical basin counts (<1000).
        """
        wkt = geom.asWkt(precision=4)
        return str(hash(wkt))

    @staticmethod
    def _find_duplicate_geometries(layer) -> list:
        """Return list of FIDs that are duplicates of a previous feature."""
        seen   = set()
        dupes  = []
        for feature in layer.getFeatures():
            geom = feature.geometry()
            if geom is None or geom.isEmpty():
                continue
            h = str(hash(geom.asWkt(precision=4)))
            if h in seen:
                dupes.append(feature.id())
            seen.add(h)
        return dupes

    # ------------------------------------------------------------------
    # Label resolution
    # ------------------------------------------------------------------

    @staticmethod
    def _resolve_label_field(layer, user_field: str = None) -> str | None:
        """
        Find the best label field automatically.
        Priority: user choice → common name fields → None (use FID).
        """
        fields      = [f.name().lower() for f in layer.fields()]
        candidates  = ['nom', 'name', 'id', 'label', 'bv', 'basin', 'watershed']

        if user_field and user_field.lower() in fields:
            return user_field

        for candidate in candidates:
            if candidate in fields:
                # Return actual field name (original case)
                for f in layer.fields():
                    if f.name().lower() == candidate:
                        return f.name()

        return None   # fallback → use FID

    @staticmethod
    def _get_label(feature, label_field: str | None, fid: int) -> str:
        """Get label string for a feature."""
        if label_field is None:
            return f"Basin_{fid}"
        val = feature[label_field]
        if val is None or str(val).strip() == '':
            return f"Basin_{fid}"
        return str(val).strip()