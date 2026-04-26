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
    build_upstream_map,
    snap_outlet,
    delineate_basin,
    find_confluences,
    subdivide_by_n,
    subdivide_by_area,
    delineate_subbasins,
    mask_to_polygon,
    pixel_area,
    world_to_pixel,
    SUPPORTED_ENCODINGS,
)


def tr(message: str) -> str:
    return QCoreApplication.translate("RockMorph", message)


# ---------------------------------------------------------------------------
# Constants
# ---------------------------------------------------------------------------

# Default subdivision mode
DEFAULT_MODE: str = "n"

# Default number of sub-basins when mode == "n"
DEFAULT_N_SUBBASINS: int = 5

# Default minimum area (m²) when mode == "area" — 10 km²
DEFAULT_MIN_AREA_M2: float = 10_000_000.0

# Snap radius used when projecting the user outlet onto the FAC raster
OUTLET_SNAP_RADIUS_PX: int = 15

# Polygon method passed to mask_to_polygon dispatcher
DEFAULT_POLYGON_METHOD: str = "auto"


# ===========================================================================
# WatershedEngine
# ===========================================================================

class WatershedEngine(BaseEngine):
    """
    Orchestrates D8 watershed delineation and sub-basin subdivision.

    Inherits :class:`base.base_engine.BaseEngine`.
    Called by :class:`tools.watershed.panel.WatershedPanel` via
    :class:`base.base_panel.ComputeWorker` (background thread).

    compute() keyword arguments
    ---------------------------
    Mandatory
    ~~~~~~~~~
    fdir_layer : QgsRasterLayer
        Flow-direction raster.  Encoding is auto-detected unless
        ``encoding`` is provided explicitly.

    facc_layer : QgsRasterLayer
        Flow-accumulation raster (cell-count or m²).
        Must be co-registered with ``fdir_layer`` (same grid, same CRS).

    Optional
    ~~~~~~~~
    outlet_layer : QgsVectorLayer | None  (default None)
        Point layer containing one outlet feature.
        If None, the pixel of maximum accumulation is used as outlet.

    encoding : str  (default "auto")
        D8 encoding of ``fdir_layer``.
        One of ``"auto"``, ``"esri"``, ``"grass"``, ``"saga"``.

    mode : str  (default "n")
        Subdivision strategy:
        • ``"n"``    — produce exactly N sub-basins (subdivide_by_n)
        • ``"area"`` — produce sub-basins above a minimum area threshold

    n_subbasins : int  (default 5)
        Target number of sub-basins.  Used only when ``mode == "n"``.
        Must be ≥ 2.

    min_area_km2 : float  (default 10.0)
        Minimum sub-basin area in km².  Used only when ``mode == "area"``.

    polygon_method : str  (default "auto")
        Vectorisation strategy passed to :func:`core.watershed.mask_to_polygon`.
        One of ``"auto"``, ``"numpy"``, ``"gdal"``.

    progress_callback : callable | None
        Injected automatically by ComputeWorker.
        Signature: ``progress_callback(percent: int, message: str)``.

    Return value — compute() → dict
    --------------------------------
    ::

        {
            "subbasins": [
                {
                    "rank":        int,
                    "area_m2":     float,
                    "area_km2":    float,
                    "n_pixels":    int,
                    "outlet_row":  int,
                    "outlet_col":  int,
                    "outlet_xy":   (float, float),   # map coords (x, y)
                    "geometry":    QgsGeometry | None,
                },
                ...
            ],
            "parent_basin": {
                "n_pixels":  int,
                "area_m2":   float,
                "area_km2":  float,
                "outlet_rc": (int, int),
            },
            "encoding":      str,     # detected or user-specified
            "n_confluences": int,     # total confluences found
            "mode":          str,
            "warnings":      list[str],
            "skipped":       list[str],
        }
    """

    # ------------------------------------------------------------------
    # validate
    # ------------------------------------------------------------------

    def validate(self, **kwargs) -> bool:
        """
        Checks that mandatory raster inputs are present and valid.

        Returns False (without raising) so the panel can display a
        friendly error message instead of crashing.
        """
        fdir = kwargs.get("fdir_layer")
        facc = kwargs.get("facc_layer")

        if fdir is None or not isinstance(fdir, QgsRasterLayer):
            return False
        if not fdir.isValid():
            return False

        if facc is None or not isinstance(facc, QgsRasterLayer):
            return False
        if not facc.isValid():
            return False

        return True

    # ------------------------------------------------------------------
    # compute
    # ------------------------------------------------------------------

    def compute(self, **kwargs) -> dict:
        """
        Main entry point — runs the full delineation + subdivision pipeline.

        All sub-basin failures are caught individually; the engine returns
        whatever sub-basins it managed to produce, plus a ``skipped`` list
        explaining any failures.
        """
        # ── Unpack parameters ─────────────────────────────────────────
        fdir_layer     = kwargs["fdir_layer"]
        facc_layer     = kwargs["facc_layer"]
        outlet_layer   = kwargs.get("outlet_layer",    None)
        encoding_hint  = kwargs.get("encoding",        "auto")
        mode           = kwargs.get("mode",            DEFAULT_MODE)
        n_subbasins    = kwargs.get("n_subbasins",     DEFAULT_N_SUBBASINS)
        min_area_km2   = kwargs.get("min_area_km2",    DEFAULT_MIN_AREA_M2 / 1e6)
        polygon_method = kwargs.get("polygon_method",  DEFAULT_POLYGON_METHOD)
        progress_cb    = kwargs.get("progress_callback")

        warnings: list[str] = []
        skipped:  list[str] = []

        min_area_m2 = min_area_km2 * 1_000_000.0

        # ── Step 1 : read rasters ─────────────────────────────────────
        if progress_cb:
            progress_cb(5, tr("Reading rasters…"))

        try:
            fdir_reader = RasterReader(fdir_layer)
            facc_reader = RasterReader(facc_layer)
        except Exception as e:
            return self._empty_result(
                tr(f"Failed to read rasters: {e}"), mode
            )

        fdir_array = fdir_reader.array          # float32, nodata → nan
        facc_array = facc_reader.array
        gt         = fdir_reader.geo_transform  # GDAL 6-tuple
        shape      = fdir_reader.shape          # (rows, cols)

        # ── Step 2 : detect / validate encoding ───────────────────────
        if progress_cb:
            progress_cb(10, tr("Detecting D8 encoding…"))

        if encoding_hint == "auto":
            try:
                encoding = detect_encoding(fdir_array)
            except ValueError as e:
                return self._empty_result(str(e), mode)
        else:
            if encoding_hint not in SUPPORTED_ENCODINGS:
                return self._empty_result(
                    tr(f"Unknown encoding '{encoding_hint}'."), mode
                )
            encoding = encoding_hint

        # ── Step 3 : build upstream map (expensive — done once) ───────
        if progress_cb:
            progress_cb(20, tr("Building upstream map…"))

        upstream_map = build_upstream_map(fdir_array, encoding)

        # ── Step 4 : locate outlet pixel ──────────────────────────────
        if progress_cb:
            progress_cb(35, tr("Locating outlet…"))

        outlet_rc, outlet_warn = self._resolve_outlet(
            outlet_layer = outlet_layer,
            facc_reader  = facc_reader,
            facc_array   = facc_array,
            fdir_layer   = fdir_layer,
            shape        = shape,
            gt           = gt,
        )

        if outlet_warn:
            warnings.append(outlet_warn)

        if outlet_rc is None:
            return self._empty_result(
                tr("Could not determine a valid outlet pixel."), mode
            )

        # ── Step 5 : delineate parent basin ───────────────────────────
        if progress_cb:
            progress_cb(45, tr("Delineating parent basin…"))

        parent_mask = delineate_basin(outlet_rc, upstream_map, shape)
        n_parent    = int(np.sum(parent_mask))

        if n_parent == 0:
            return self._empty_result(
                tr("Parent basin delineation produced an empty mask. "
                   "Check that the outlet point falls within the raster extent "
                   "and that the flow-direction raster is correctly conditioned."),
                mode,
            )

        px_area    = pixel_area(gt, fdir_reader.is_geographic)
        parent_info = {
            "n_pixels":  n_parent,
            "area_m2":   round(n_parent * px_area, 1),
            "area_km2":  round(n_parent * px_area / 1e6, 4),
            "outlet_rc": outlet_rc,
        }

        if progress_cb:
            progress_cb(55, tr(
                f"Parent basin: {parent_info['area_km2']:.2f} km² "
                f"({n_parent:,} pixels)"
            ))

        # ── Step 6 : find confluences ──────────────────────────────────
        if progress_cb:
            progress_cb(62, tr("Locating confluences…"))

        confluences = find_confluences(
            basin_mask    = parent_mask,
            facc_array    = facc_array,
            fdir_array    = fdir_array,
            encoding      = encoding,
            pixel_area_m2 = px_area,
        )

        if not confluences:
            warnings.append(tr(
                "No confluences found inside the basin. "
                "The basin may be too small or the stream network too simple. "
                "A single sub-basin (the parent basin itself) will be returned."
            ))

        # ── Step 7 : select sub-basin outlets ─────────────────────────
        if progress_cb:
            progress_cb(70, tr("Selecting sub-basin outlets…"))

        if mode == "n":
            selected = subdivide_by_n(confluences, n_subbasins)
        else:
            selected = subdivide_by_area(confluences, min_area_m2)

        if not selected:
            warnings.append(tr(
                f"No confluences met the subdivision criterion "
                f"({'N=' + str(n_subbasins) if mode == 'n' else str(min_area_km2) + ' km²'}). "
                f"Returning the parent basin as a single unit."
            ))

        # ── Step 8 : delineate sub-basins ─────────────────────────────
        if progress_cb:
            progress_cb(78, tr("Delineating sub-basins…"))

        raw_subbasins = delineate_subbasins(
            selected_outlets = selected,
            basin_mask       = parent_mask,
            upstream_map     = upstream_map,
            facc_array       = facc_array,
            pixel_area_m2    = px_area,
        )

        # ── Step 9 : vectorise masks → QgsGeometry ────────────────────
        if progress_cb:
            progress_cb(88, tr("Vectorising sub-basin polygons…"))

        subbasins: list[dict] = []
        total = max(len(raw_subbasins), 1)

        for i, sb in enumerate(raw_subbasins):
            if progress_cb:
                pct = 88 + int((i / total) * 10)
                progress_cb(pct, tr(f"Vectorising sub-basin {i + 1}/{total}…"))

            try:
                geom = mask_to_polygon(
                    mask          = sb["mask"],
                    geo_transform = gt,
                    method        = polygon_method,
                )
            except Exception as e:
                traceback.print_exc()
                geom = None
                skipped.append(tr(
                    f"Sub-basin rank {sb['rank']}: vectorisation failed — {e}"
                ))

            # Outlet map coordinates
            r_out, c_out = sb["outlet_row"], sb["outlet_col"]
            out_x, out_y = fdir_reader.pixel_to_world(c_out, r_out)

            subbasins.append({
                "rank":       sb["rank"],
                "area_m2":   sb["area_m2"],
                "area_km2":  round(sb["area_m2"] / 1e6, 4),
                "n_pixels":  sb["n_pixels"],
                "outlet_row": r_out,
                "outlet_col": c_out,
                "outlet_xy":  (round(out_x, 4), round(out_y, 4)),
                "geometry":   geom,
            })

        if progress_cb:
            progress_cb(100, tr("Done."))

        return {
            "subbasins":     subbasins,
            "parent_basin":  parent_info,
            "encoding":      encoding,
            "n_confluences": len(confluences),
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
        Resolves the outlet pixel from a user point or the FAC maximum.

        Returns
        -------
        tuple
            ``(outlet_rc, warning_message)``
            ``outlet_rc`` is ``None`` on failure.
            ``warning_message`` is ``None`` on clean success.
        """
        # ── Case A : user provided an outlet point layer ──────────────
        if outlet_layer is not None and outlet_layer.isValid():
            features = list(outlet_layer.getFeatures())

            if not features:
                return None, tr(
                    "Outlet layer is empty — falling back to maximum "
                    "accumulation pixel."
                )

            # Use the first feature only; warn if more exist
            feat  = features[0]
            geom  = feat.geometry()
            pt    = geom.asPoint()

            warn = None
            if len(features) > 1:
                warn = tr(
                    f"Outlet layer contains {len(features)} features; "
                    f"only the first will be used."
                )

            # Reproject outlet point to fdir CRS if needed
            outlet_crs = outlet_layer.crs()
            fdir_crs   = fdir_layer.crs()

            if outlet_crs != fdir_crs:
                transform = QgsCoordinateTransform(
                    outlet_crs, fdir_crs, QgsProject.instance()
                )
                pt = transform.transform(QgsPointXY(pt))

            # Convert map coords → pixel indices
            row, col = world_to_pixel(pt.x(), pt.y(), gt, shape)

            if row == -1:
                return None, tr(
                    "Outlet point falls outside the flow-direction raster extent."
                )

            # Snap to highest-accumulation pixel nearby
            row, col = snap_outlet(row, col, facc_array, OUTLET_SNAP_RADIUS_PX)
            return (row, col), warn

        # ── Case B : no outlet — use global FAC maximum ───────────────
        facc_clean = facc_array.copy()
        facc_clean[np.isnan(facc_clean)] = -np.inf

        if np.all(np.isinf(facc_clean)):
            return None, tr(
                "Flow-accumulation raster contains only nodata values."
            )

        # --- LE "SENIOR MOVE" : AJOUT D'UNE MARGE DE SÉCURITÉ ---
        # On définit une bordure de 10 pixels où on ne veut PAS chercher l'exutoire.
        # Pourquoi ? Car au bord, il n'y a plus de place pour délimiter un bassin.
        margin = 10 
        rows, cols = facc_clean.shape
        
        if rows > 2*margin and cols > 2*margin:
            # On met les bords à -inf pour que argmax les ignore
            facc_clean[0:margin, :] = -np.inf  # Haut
            facc_clean[rows-margin:rows, :] = -np.inf # Bas
            facc_clean[:, 0:margin] = -np.inf  # Gauche
            facc_clean[:, cols-margin:cols] = -np.inf # Droite

        if np.all(np.isinf(facc_clean)):
            return None, tr("Flow-accumulation raster is empty or too small.")

        # On trouve maintenant le point le plus important qui est BIEN au milieu de la carte
        idx = np.unravel_index(np.argmax(facc_clean), facc_clean.shape)
        
        return (int(idx[0]), int(idx[1])), tr(
            "Auto-detected main outlet (buffered from edges)."
        )

    @staticmethod
    def _empty_result(reason: str, mode: str) -> dict:
        """Returns a structurally valid but empty result dict with one warning."""
        return {
            "subbasins":     [],
            "parent_basin":  {},
            "encoding":      "unknown",
            "n_confluences": 0,
            "mode":          mode,
            "warnings":      [reason],
            "skipped":       [],
        }