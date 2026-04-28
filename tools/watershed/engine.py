# tools/watershed/engine.py

"""
tools/watershed/engine.py

WatershedEngine — orchestrates watershed delineation and sub-basin subdivision.

PIPELINE
--------
For a single run, the engine executes this sequence:

  1. Load fdir + facc rasters via RasterReader
  2. Detect or accept user-specified D8 encoding
  3. Build upstream-neighbour map (computed once, reused for all BFS calls)
  4. Locate outlet pixel
       • User-provided point  → reproject → world_to_pixel → snap_outlet
       • No point provided    → pixel of maximum accumulation in the raster
  5. Delineate parent basin via BFS flood-fill
  6. Find and rank confluences inside the parent basin
  7. Select sub-basin outlets
       • Mode "n"    → subdivide_by_n    (N-1 best confluences)
       • Mode "area" → subdivide_by_area (confluences above area threshold)
  8. Delineate each sub-basin mask + residual mask
  9. Vectorise each mask → QgsGeometry polygon
 10. Return structured result dict

DESIGN RULES
------------
- Inherits BaseEngine — validate() + compute() contract enforced.
- Zero UI logic — all QGIS layer I/O happens here; no Qt widgets.
- progress_callback(int, str) injected by ComputeWorker — optional.
- All exceptions caught per sub-basin; engine never crashes the UI thread.

Authors: RockMorph contributors / Tony
"""

import math
import traceback
import numpy as np                          # type: ignore
from typing import Optional

from qgis.core import (                     # type: ignore
    QgsRasterLayer,
    QgsVectorLayer,
    QgsWkbTypes,
    QgsPointXY,
    QgsCoordinateTransform,
    QgsProject,
)
from PyQt5.QtCore import QCoreApplication   # type: ignore

from ...base.base_engine import BaseEngine
from ...core.raster import RasterReader
from ...core.watershed import (
    detect_encoding,
    build_downstream,
    build_upstream_csr,
    find_outlets,
    propagate_labels_bfs,
    subbasins_from_labels,
    pixel_area,
    world_to_pixel,
    snap_outlet,
)


def tr(message: str) -> str:
    return QCoreApplication.translate("RockMorph", message)


# ---------------------------------------------------------------------------
# Constants
# ---------------------------------------------------------------------------

# Default subdivision mode
DEFAULT_MODE: str = "n"

# Default number of sub-basins when mode == "n"
DEFAULT_N_SUBBASINS: int = 25

# Default minimum area (m²) when mode == "area" — 10 km²
DEFAULT_MIN_AREA_M2: float = 10_000_000.0

DEFAULT_MIN_AREA_KM2: float = 10.0

# Snap radius used when projecting the user outlet onto the FAC raster
OUTLET_SNAP_RADIUS_PX: int = 25

# Polygon method passed to mask_to_polygon dispatcher
DEFAULT_POLYGON_METHOD: str = "auto"


# ===========================================================================
# WatershedEngine
# ===========================================================================

class WatershedEngine(BaseEngine):
    """
    Orchestrates D8 watershed delineation and sub-basin subdivision.
 
    compute() keyword arguments
    ---------------------------
    fdir_layer     : QgsRasterLayer  — flow direction (mandatory)
    facc_layer     : QgsRasterLayer  — flow accumulation (mandatory)
    outlet_layer   : QgsVectorLayer | None  — optional pour point
    encoding       : str  — 'auto' | 'esri' | 'grass' | 'saga'
    mode           : str  — 'n' | 'area'
    n_subbasins    : int  — target number of sub-basins (mode='n')
    min_area_km2   : float — minimum sub-basin area km² (mode='area')
    polygon_method : str  — 'auto' | 'numpy' | 'gdal'
 
    Return value
    ------------
    {
        "subbasins":     list[dict],   # one dict per sub-basin
        "encoding":      str,
        "n_confluences": int,
        "mode":          str,
        "warnings":      list[str],
        "skipped":       list[str],
    }
 
    Each sub-basin dict:
    {
        "rank":       int,
        "area_km2":   float,
        "area_m2":    float,
        "n_pixels":   int,
        "outlet_xy":  (float, float),
        "geometry":   QgsGeometry | None,
    }
    """
 
    # ------------------------------------------------------------------
    # validate
    # ------------------------------------------------------------------
 
    def validate(self, **kwargs) -> bool:
        fdir = kwargs.get("fdir_layer")
        facc = kwargs.get("facc_layer")
 
        if not isinstance(fdir, QgsRasterLayer) or not fdir.isValid():
            return False
        if not isinstance(facc, QgsRasterLayer) or not facc.isValid():
            return False
        return True
 
    # ------------------------------------------------------------------
    # compute
    # ------------------------------------------------------------------
 
    def compute(self, **kwargs) -> dict:
        """
        Full delineation + subdivision pipeline.
        """
        # ── Unpack parameters ─────────────────────────────────────────
        fdir_layer      = kwargs["fdir_layer"]
        facc_layer      = kwargs["facc_layer"]
        outlet_layer    = kwargs.get("outlet_layer",    None)
        encoding_hint   = kwargs.get("encoding",        "auto")
        mode            = kwargs.get("mode",            DEFAULT_MODE)
        n_subbasins     = kwargs.get("n_subbasins",     DEFAULT_N_SUBBASINS)
        min_area_km2    = kwargs.get("min_area_km2",    DEFAULT_MIN_AREA_KM2)
        polygon_method  = kwargs.get("polygon_method",  DEFAULT_POLYGON_METHOD)
        progress_cb     = kwargs.get("progress_callback")
        segment_main_stem = kwargs.get("segment_main_stem", True)
 
        warnings: list[str] = []
        skipped:  list[str] = []
        min_area_m2 = min_area_km2 * 1_000_000.0
 
        def _progress(pct: int, msg: str) -> None:
            if progress_cb:
                progress_cb(pct, msg)
 
        # ── Step 1 : Read rasters ─────────────────────────────────────
        _progress(5, tr("Reading rasters…"))
        try:
            fdir_reader = RasterReader(fdir_layer)
            facc_reader = RasterReader(facc_layer)
        except Exception as e:
            return self._empty_result(tr(f"Failed to read rasters: {e}"), mode)
 
        fdir_array = fdir_reader.array.astype(np.float32)
        facc_array = facc_reader.array.astype(np.float64)
        gt         = fdir_reader.geo_transform
        shape      = fdir_reader.shape          # (rows, cols)
 
        # ── Step 2 : Detect encoding ──────────────────────────────────
        _progress(10, tr("Detecting D8 encoding…"))
        if encoding_hint == "auto":
            try:
                encoding = detect_encoding(fdir_array)
            except ValueError as e:
                return self._empty_result(str(e), mode)
        else:
            encoding = encoding_hint
 
        # ── Step 3 : Build downstream + upstream CSR ──────────────────
        _progress(20, tr("Building flow network…"))
        downstream   = build_downstream(fdir_array, encoding)
        size         = shape[0] * shape[1]
        csr_indices, csr_indptr = build_upstream_csr(downstream, size)
 
        # ── Step 4 : Resolve outlet ───────────────────────────────────
        _progress(30, tr("Locating outlet…"))
        px_area_m2 = pixel_area(gt, fdir_reader.is_geographic)
 
        outlet_rc, outlet_warn = self._resolve_outlet(
            outlet_layer, facc_reader, facc_array, fdir_layer, shape, gt
        )
        if outlet_warn:
            warnings.append(outlet_warn)
 
        # ── Step 5 : Find confluence outlets ──────────────────────────
        _progress(45, tr("Finding confluences…"))
        outlets = find_outlets(
            facc_array  = facc_array,
            downstream  = downstream,
            shape       = shape,
            px_area_m2  = px_area_m2,
            mode        = mode,
            n_target    = n_subbasins,
            min_area_m2 = min_area_m2,
            segment_main_stem = segment_main_stem,
        )
 
        if not outlets:
            warnings.append(tr(
                "No confluences found. The network may be too simple, "
                "or the accumulation raster may be incorrect."
            ))
 
        # If user provided an outlet point, add it as an extra barrier
        # so the engine delineates only the area upstream of that point.
        if outlet_rc is not None and outlet_rc != (-1, -1):
            r_out, c_out = outlet_rc
            outlet_flat  = r_out * shape[1] + c_out
 
            # Check it's not already in the list
            existing_ids = {o["idx"] for o in outlets}
            if outlet_flat not in existing_ids:
                outlets.append({
                    "idx":     outlet_flat,
                    "row":     r_out,
                    "col":     c_out,
                    "area_m2": float(facc_array[r_out, c_out]) * px_area_m2,
                })
 
        # ── Step 6 : BFS upstream label propagation ───────────────────
        _progress(60, tr("Propagating sub-basin labels…"))
        label_map = propagate_labels_bfs(
            downstream  = downstream,
            csr_indices = csr_indices,
            csr_indptr  = csr_indptr,
            outlets     = outlets,
            size        = size,
            shape       = shape,
        )
 
        # ── Step 7 : Build sub-basin dicts + polygons ─────────────────
        _progress(80, tr("Vectorising sub-basins…"))
        subbasins = subbasins_from_labels(
            label_map      = label_map,
            facc_array     = facc_array,
            px_area_m2     = px_area_m2,
            geo_transform  = gt,
            polygon_method = polygon_method,
        )
 
        if not subbasins:
            return self._empty_result(
                tr("Label propagation produced no valid sub-basins. "
                   "Check your FDIR and FACC rasters."),
                mode,
            )
 
        _progress(100, tr("Done."))
 
        return {
            "subbasins":     subbasins,
            "encoding":      encoding,
            "n_confluences": len(outlets),
            "mode":          mode,
            "warnings":      warnings,
            "skipped":       skipped,
        }
 
    # ------------------------------------------------------------------
    # Private helpers
    # ------------------------------------------------------------------
 
    def _resolve_outlet(
        self,
        outlet_layer: Optional[QgsVectorLayer],
        facc_reader:  RasterReader,
        facc_array:   np.ndarray,
        fdir_layer:   QgsRasterLayer,
        shape:        tuple,
        gt:           tuple,
    ) -> tuple[Optional[tuple[int, int]], Optional[str]]:
        """
        Resolve outlet pixel from a user point layer, or fall back to the
        pixel of maximum accumulation (with a safety margin from the edges).
 
        Returns (outlet_rc, warning_message).
        outlet_rc is None when no outlet could be determined.
        """
        # ── Case A : user-provided outlet point ───────────────────────
        if outlet_layer is not None and outlet_layer.isValid():
            features = list(outlet_layer.getFeatures())
 
            if not features:
                return None, tr(
                    "Outlet layer is empty — using maximum accumulation pixel."
                )
 
            feat = features[0]
            pt   = feat.geometry().asPoint()
            warn = None
 
            if len(features) > 1:
                warn = tr(
                    f"Outlet layer has {len(features)} features; "
                    "only the first will be used."
                )
 
            # Reproject if CRS differ
            outlet_crs = outlet_layer.crs()
            fdir_crs   = fdir_layer.crs()
            if outlet_crs != fdir_crs:
                xform = QgsCoordinateTransform(
                    outlet_crs, fdir_crs, QgsProject.instance()
                )
                pt = xform.transform(QgsPointXY(pt))
 
            row, col = world_to_pixel(pt.x(), pt.y(), gt, shape)
            if row == -1:
                return None, tr(
                    "Outlet point is outside the flow-direction raster extent."
                )
 
            row, col = snap_outlet(row, col, facc_array, OUTLET_SNAP_RADIUS_PX)
            return (row, col), warn
 
        # ── Case B : automatic — FAC maximum with edge margin ─────────
        nodata_val  = facc_reader.nodata
        facc_clean  = facc_array.copy().astype(np.float64)
 
        if nodata_val is not None:
            facc_clean[facc_array == nodata_val] = -np.inf
        facc_clean[np.isnan(facc_array)] = -np.inf
 
        # Ignore a border margin to avoid edge artifacts
        margin = 20
        rows, cols = facc_clean.shape
        if rows > 2 * margin and cols > 2 * margin:
            facc_clean[:margin,  :]  = -np.inf
            facc_clean[-margin:, :]  = -np.inf
            facc_clean[:,  :margin]  = -np.inf
            facc_clean[:, -margin:]  = -np.inf
 
        if np.all(facc_clean == -np.inf):
            return None, tr("FAC raster contains only NoData values.")
 
        idx     = np.unravel_index(np.argmax(facc_clean), facc_clean.shape)
        max_val = facc_clean[idx]
 
        return (
            (int(idx[0]), int(idx[1])),
            tr(f"Auto outlet detected at row={idx[0]}, col={idx[1]} "
               f"(FAC={max_val:.0f})."),
        )
 
    # ------------------------------------------------------------------
 
    @staticmethod
    def _empty_result(reason: str, mode: str) -> dict:
        return {
            "subbasins":     [],
            "encoding":      "unknown",
            "n_confluences": 0,
            "mode":          mode,
            "warnings":      [reason],
            "skipped":       [],
        }



    