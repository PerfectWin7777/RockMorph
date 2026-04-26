# core/watershed.py

"""
core/watershed.py

Low-level D8 watershed delineation and subdivision utilities for RockMorph.

OVERVIEW
--------
This module provides the computational backbone for the Watershed tool.
It operates exclusively on NumPy arrays produced by RasterReader and is
completely decoupled from any UI or QGIS rendering logic.

PIPELINE
--------
  1. detect_encoding     — infer D8 format (ESRI / GRASS / SAGA) from raster values
  2. build_upstream_map  — invert D8 grid → O(1) upstream-neighbor lookup
  3. snap_outlet         — find highest-accumulation pixel near a user point
  4. delineate_basin     — BFS flood-fill from outlet → binary basin mask
  5. find_confluences    — rank confluences inside basin by upstream area
  6. subdivide_by_n      — pick N-1 best confluences → N sub-basin outlets
  7. subdivide_by_area   — pick confluences above an area threshold
  8. delineate_subbasins — run delineate_basin for each outlet set
  9. mask_to_polygon     — raster mask → QgsGeometry (vectorisation)

DESIGN RULES
------------
- Zero UI logic — pure computation only.
- All functions accept / return plain NumPy arrays or primitive Python types.
- QgsGeometry is produced only in mask_to_polygon (the single QGIS dependency).
- Encoding-agnostic : all D8 values are normalised to (dy, dx) offsets on entry.
- Thread-safe : no shared mutable state; each call is self-contained.

D8 ENCODING REFERENCE
---------------------
Three conventions exist in the wild:

  ESRI / ArcGIS (powers of two, clockwise from E):
      32   64  128
      16    ·    1
       8    4    2

  GRASS r.watershed (1-8, clockwise from NE):
       3    2    1
       4    ·    8
       5    6    7

  SAGA / TauDEM (0-7, clockwise from N):
       7    0    1
       6    ·    2
       5    4    3

Authors: RockMorph contributors / Tony
"""

import math
import numpy as np # type: ignore
from collections import deque
from typing import Optional

from qgis.core import QgsGeometry, QgsPointXY  # type: ignore
from PyQt5.QtCore import QCoreApplication       # type: ignore
from osgeo import gdal, ogr  # type: ignore



def tr(message: str) -> str:
    return QCoreApplication.translate("RockMorph", message)


# ---------------------------------------------------------------------------
# Constants
# ---------------------------------------------------------------------------

# Minimum basin area in pixels below which a sub-basin is discarded.
MIN_BASIN_PIXELS: int = 10

# Snap search radius (pixels) when locating the best outlet near a point.
SNAP_RADIUS_PX: int = 10


# ---------------------------------------------------------------------------
# D8 offset look-up tables
# key   → raster cell value
# value → (dy, dx) displacement to the downstream neighbour
# ---------------------------------------------------------------------------

_D8_OFFSETS: dict[str, dict[int, tuple[int, int]]] = {
    "esri": {
        1:   ( 0,  1),   # E
        2:   ( 1,  1),   # SE
        4:   ( 1,  0),   # S
        8:   ( 1, -1),   # SW
        16:  ( 0, -1),   # W
        32:  (-1, -1),   # NW
        64:  (-1,  0),   # N
        128: (-1,  1),   # NE
    },
    "grass": {
        1:   (-1,  1),   # NE
        2:   (-1,  0),   # N
        3:   (-1, -1),   # NW
        4:   ( 0, -1),   # W
        5:   ( 1, -1),   # SW
        6:   ( 1,  0),   # S
        7:   ( 1,  1),   # SE
        8:   ( 0,  1),   # E
    },
    "saga": {
        0:   (-1,  0),   # N
        1:   (-1,  1),   # NE
        2:   ( 0,  1),   # E
        3:   ( 1,  1),   # SE
        4:   ( 1,  0),   # S
        5:   ( 1, -1),   # SW
        6:   ( 0, -1),   # W
        7:   (-1, -1),   # NW
    },
}

# Supported encoding names exposed to callers (including "auto")
SUPPORTED_ENCODINGS: tuple[str, ...] = ("auto", "esri", "grass", "saga")


# ===========================================================================
# 1. detect_encoding
# ===========================================================================

def detect_encoding(fdir_array: np.ndarray) -> str:
    """
    Infers the D8 flow-direction encoding from the unique values present
    in the raster array.

    Detection strategy
    ------------------
    ESRI uses powers of 2 exclusively (1, 2, 4, 8, 16, 32, 64, 128).
    Any value strictly greater than 8 that is a power of 2 is a definitive
    ESRI fingerprint — GRASS and SAGA never exceed 8.

    Between GRASS (1–8) and SAGA (0–7):
      • SAGA always contains 0 as a valid direction.
      • GRASS never uses 0 (its values start at 1).

    Nodata values (NaN, -1, -9999, 0 in ESRI context) are stripped before
    comparison so they do not bias the detection.

    Parameters
    ----------
    fdir_array : np.ndarray
        Raw D8 raster band loaded by RasterReader (float32 or int).

    Returns
    -------
    str
        One of ``'esri'``, ``'grass'``, ``'saga'``.

    Raises
    ------
    ValueError
        If the unique values do not match any known encoding.

    Examples
    --------
    >>> detect_encoding(np.array([[64, 128, 1], [32, 4, 2]]))
    'esri'
    >>> detect_encoding(np.array([[2, 3, 4], [6, 7, 8]]))
    'grass'
    >>> detect_encoding(np.array([[0, 1, 2], [6, 5, 4]]))
    'saga'
    """
    # Flatten and remove NaN / common nodata sentinels
    flat = fdir_array.flatten().astype(np.float64)
    flat = flat[~np.isnan(flat)]
    flat = flat[flat != -1.0]
    flat = flat[flat != -9999.0]

    if flat.size == 0:
        raise ValueError(tr(
            "Flow direction raster contains only nodata values — "
            "cannot detect encoding."
        ))

    unique_vals = set(flat.astype(int))

    # ── ESRI fingerprint : any value that is a power of 2 and > 8 ────
    # Powers of 2 > 8 in the ESRI set: 16, 32, 64, 128
    esri_large = {16, 32, 64, 128}
    if unique_vals & esri_large:
        return "esri"

    # ── SAGA vs GRASS : both use values ≤ 8 ──────────────────────────
    # SAGA includes 0 as a valid direction; GRASS starts at 1
    if 0 in unique_vals:
        return "saga"

    # ── Remaining case : assume GRASS (values 1–8) ───────────────────
    grass_vals = {1, 2, 3, 4, 5, 6, 7, 8}
    if unique_vals.issubset(grass_vals):
        return "grass"

    raise ValueError(tr(
        f"Unrecognised D8 encoding. Unique values found: {sorted(unique_vals)}. "
        f"Expected ESRI (powers of 2), GRASS (1–8), or SAGA (0–7)."
    ))


# ===========================================================================
# 2. build_upstream_map
# ===========================================================================

def build_upstream_map(
    fdir_array: np.ndarray,
    encoding:   str,
) -> dict[tuple[int, int], list[tuple[int, int]]]:
    """
    Inverts the D8 flow-direction grid into an upstream-neighbour map.

    For every pixel ``(r, c)``, the map stores the list of pixels whose
    D8 arrow points **to** ``(r, c)``.  This inversion is computed once
    and reused for all subsequent BFS calls, making repeated delineations
    O(pixels) rather than O(pixels²).

    Algorithm
    ---------
    For each pixel ``(r, c)`` with a valid D8 value ``v``:
      1. Look up the downstream offset ``(dy, dx)`` from the encoding table.
      2. Compute the downstream pixel ``(r_dst, c_dst) = (r+dy, c+dx)``.
      3. Append ``(r, c)`` to ``upstream_map[(r_dst, c_dst)]``.

    Pixels with NaN / nodata are skipped silently.

    Parameters
    ----------
    fdir_array : np.ndarray
        D8 flow-direction raster, shape ``(rows, cols)``.
    encoding   : str
        One of ``'esri'``, ``'grass'``, ``'saga'``.

    Returns
    -------
    dict
        ``{ (row, col) : [(row_up1, col_up1), (row_up2, col_up2), ...] }``

        Every pixel in the raster extent has a key, even if its value list
        is empty (i.e., the pixel has no upstream contributors — it is a
        headwater source cell).

    Raises
    ------
    KeyError
        If ``encoding`` is not in ``_D8_OFFSETS``.
    """
    if encoding not in _D8_OFFSETS:
        raise KeyError(tr(
            f"Unknown encoding '{encoding}'. "
            f"Valid choices: {list(_D8_OFFSETS.keys())}."
        ))

    offsets  = _D8_OFFSETS[encoding]
    rows, cols = fdir_array.shape

    # Pre-populate every pixel with an empty list — avoids KeyError on lookup
    upstream_map: dict[tuple[int, int], list[tuple[int, int]]] = {
        (r, c): []
        for r in range(rows)
        for c in range(cols)
    }

    for r in range(rows):
        for c in range(cols):
            val = fdir_array[r, c]

            # Skip nodata / NaN
            if np.isnan(val):
                continue

            v = int(val)
            if v not in offsets:
                continue

            dy, dx   = offsets[v]
            r_dst    = r + dy
            c_dst    = c + dx

            # Stay within raster bounds
            if not (0 <= r_dst < rows and 0 <= c_dst < cols):
                continue

            upstream_map[(r_dst, c_dst)].append((r, c))

    return upstream_map


# ===========================================================================
# 3. snap_outlet
# ===========================================================================

def snap_outlet(
    row:       int,
    col:       int,
    facc_array: np.ndarray,
    radius_px: int = SNAP_RADIUS_PX,
) -> tuple[int, int]:
    """
    Moves a user-specified outlet pixel to the highest-accumulation cell
    within a search radius.

    Rationale
    ---------
    Digitised outlet points rarely fall exactly on the stream centreline
    in raster space.  Even a one-pixel offset can exclude the entire
    upstream network.  Snapping to the local accumulation maximum reliably
    places the outlet on the main channel.

    Parameters
    ----------
    row, col   : int   — initial pixel coordinates (from world_to_pixel)
    facc_array : np.ndarray — flow-accumulation raster (same grid as fdir)
    radius_px  : int   — half-side of the square search window (default 10)

    Returns
    -------
    tuple[int, int]
        ``(best_row, best_col)`` — snapped outlet coordinates.
        Returns the original ``(row, col)`` if the window is entirely nodata.
    """
    rows, cols = facc_array.shape

    r0 = max(0, row - radius_px)
    r1 = min(rows, row + radius_px + 1)
    c0 = max(0, col - radius_px)
    c1 = min(cols, col + radius_px + 1)

    window = facc_array[r0:r1, c0:c1].copy().astype(np.float64)
    window[np.isnan(window)] = -np.inf

    if np.all(np.isinf(window)):
        # Entirely nodata — return original position unchanged
        return row, col

    local_idx = np.unravel_index(np.argmax(window), window.shape)
    return int(r0 + local_idx[0]), int(c0 + local_idx[1])


# ===========================================================================
# 4. delineate_basin
# ===========================================================================

def delineate_basin(
    outlet_rc:    tuple[int, int],
    upstream_map: dict[tuple[int, int], list[tuple[int, int]]],
    shape:        tuple[int, int],
) -> np.ndarray:
    """
    BFS flood-fill from an outlet pixel to produce a binary basin mask.

    Starting from ``outlet_rc``, the algorithm visits every pixel whose
    D8 arrow eventually drains to the outlet, using the pre-inverted
    ``upstream_map`` for O(1) neighbour lookup at each step.

    Algorithm
    ---------
    1. Initialise a boolean mask of ``shape`` filled with ``False``.
    2. Push ``outlet_rc`` onto a :class:`collections.deque`.
    3. While the queue is non-empty:

       a. Pop a pixel ``(r, c)``.
       b. If already visited → skip.
       c. Mark ``mask[r, c] = True``.
       d. Extend queue with ``upstream_map[(r, c)]``.

    4. Return the completed mask.

    Complexity
    ----------
    O(B) where B = number of pixels inside the basin.
    Each pixel is visited at most once (visited-set guard).

    Parameters
    ----------
    outlet_rc    : tuple[int, int]
        ``(row, col)`` of the outlet in raster coordinates.
    upstream_map : dict
        Pre-computed upstream-neighbour map from :func:`build_upstream_map`.
    shape        : tuple[int, int]
        ``(rows, cols)`` of the output mask (must match the source raster).

    Returns
    -------
    np.ndarray
        Boolean array, shape ``(rows, cols)``.
        ``True``  → pixel belongs to the basin.
        ``False`` → pixel is outside.

    Notes
    -----
    The outlet pixel itself is always included in the mask (``True``).
    If ``outlet_rc`` is outside ``shape``, an empty mask is returned.
    """
    rows, cols = shape
    r0, c0 = outlet_rc

    mask = np.zeros((rows, cols), dtype=bool)

    # Guard: outlet outside raster extent
    if not (0 <= r0 < rows and 0 <= c0 < cols):
        return mask

    visited: set[tuple[int, int]] = set()
    queue: deque[tuple[int, int]] = deque()
    queue.append((r0, c0))

    while queue:
        r, c = queue.popleft()

        if (r, c) in visited:
            continue

        visited.add((r, c))
        mask[r, c] = True

        # Enqueue all pixels that drain directly into (r, c)
        for neighbour in upstream_map.get((r, c), []):
            if neighbour not in visited:
                queue.append(neighbour)

    return mask


# ===========================================================================
# 5. find_confluences
# ===========================================================================

def find_confluences(
    basin_mask:  np.ndarray,
    facc_array:  np.ndarray,
    fdir_array:  np.ndarray,
    encoding:    str,
    pixel_area_m2: float,
) -> list[dict]:
    """
    Identifies and ranks confluence pixels inside a basin by upstream area.

    A confluence is defined as any pixel inside the basin that receives
    flow from **two or more** upstream pixels (i.e., ``len(upstream) >= 2``
    in the upstream_map sense), making it a branching point in the network.

    Each confluence is characterised by its total upstream drainage area,
    derived from the flow-accumulation raster.

    Parameters
    ----------
    basin_mask   : np.ndarray (bool)
        Binary mask from :func:`delineate_basin`.
    facc_array   : np.ndarray
        Flow-accumulation raster (cell count, same grid as fdir).
    fdir_array   : np.ndarray
        Flow-direction raster (same grid).
    encoding     : str
        D8 encoding — ``'esri'``, ``'grass'``, or ``'saga'``.
    pixel_area_m2 : float
        Area of one raster pixel in m² (from :func:`core.watershed.pixel_area`).

    Returns
    -------
    list of dict, sorted descending by ``area_m2``:
        {
            'row'     : int   — pixel row,
            'col'     : int   — pixel col,
            'area_m2' : float — upstream drainage area (m²),
            'n_up'    : int   — number of direct upstream tributaries,
        }

    Notes
    -----
    The outlet pixel itself is excluded — it is always a confluence by
    definition but is not a useful subdivision point.
    """
    offsets = _D8_OFFSETS[encoding]
    rows, cols = fdir_array.shape

    # Build a lightweight upstream count array restricted to the basin
    # We do NOT rebuild the full upstream_map here to save memory.
    # Instead we count, for each pixel, how many of its 8 neighbours
    # flow into it AND are inside the basin.

    confluences = []

    basin_rows, basin_cols = np.where(basin_mask)

    for r, c in zip(basin_rows.tolist(), basin_cols.tolist()):
        n_up = 0

        # Check all 8 neighbours
        for dr in (-1, 0, 1):
            for dc in (-1, 0, 1):
                if dr == 0 and dc == 0:
                    continue
                nr, nc = r + dr, c + dc
                if not (0 <= nr < rows and 0 <= nc < cols):
                    continue
                if not basin_mask[nr, nc]:
                    continue

                # Does neighbour (nr,nc) flow into (r,c)?
                v = fdir_array[nr, nc]
                if np.isnan(v):
                    continue
                vi = int(v)
                if vi not in offsets:
                    continue
                dy, dx = offsets[vi]
                if nr + dy == r and nc + dx == c:
                    n_up += 1

        # A confluence receives flow from 2+ tributaries
        if n_up >= 2:
            fac_val = facc_array[r, c]
            if np.isnan(fac_val):
                continue
            area_m2 = float(fac_val) * pixel_area_m2
            confluences.append({
                "row":     r,
                "col":     c,
                "area_m2": area_m2,
                "n_up":    n_up,
            })

    # Sort descending by drainage area — largest confluences first
    confluences.sort(key=lambda x: x["area_m2"], reverse=True)
    return confluences


# ===========================================================================
# 6. subdivide_by_n
# ===========================================================================

def subdivide_by_n(
    confluences: list[dict],
    n_subbasins: int,
) -> list[dict]:
    """
    Selects the N-1 most important confluences to produce N sub-basins.

    Strategy
    --------
    The confluences list is already sorted descending by drainage area
    (from :func:`find_confluences`).  We simply take the first ``N-1``
    entries — these represent the largest tributary junctions, which
    create the most hydrologically meaningful subdivision.

    Each selected confluence becomes the **outlet** of one sub-basin:
    the sub-basin above it drains exclusively to that confluence.

    Parameters
    ----------
    confluences  : list[dict]
        Ranked confluences from :func:`find_confluences`.
    n_subbasins  : int
        Desired number of sub-basins (must be ≥ 2).

    Returns
    -------
    list[dict]
        Subset of ``confluences`` — the selected outlet pixels.
        Length = ``min(n_subbasins - 1, len(confluences))``.

    Notes
    -----
    If fewer confluences exist than requested, all available confluences
    are returned without error — the caller receives fewer sub-basins
    than requested and should inform the user via a warning.
    """
    if n_subbasins < 2:
        return []

    n_outlets = n_subbasins - 1
    return confluences[:n_outlets]


# ===========================================================================
# 7. subdivide_by_area
# ===========================================================================

def subdivide_by_area(
    confluences:    list[dict],
    min_area_m2:    float,
) -> list[dict]:
    """
    Selects confluences whose upstream drainage area exceeds a threshold.

    Each selected confluence becomes the outlet of one sub-basin.
    Sub-basins smaller than ``min_area_m2`` are discarded — this prevents
    the creation of tiny, hydrologically insignificant units.

    Parameters
    ----------
    confluences  : list[dict]
        Ranked confluences from :func:`find_confluences`.
    min_area_m2  : float
        Minimum upstream drainage area (m²) for a confluence to be selected.
        Typical values: 1e6 (1 km²) to 1e8 (100 km²).

    Returns
    -------
    list[dict]
        Confluences with ``area_m2 >= min_area_m2``, preserving rank order.
        Empty list if no confluence exceeds the threshold.
    """
    return [c for c in confluences if c["area_m2"] >= min_area_m2]


# ===========================================================================
# 8. delineate_subbasins
# ===========================================================================

def delineate_subbasins(
    selected_outlets: list[dict],
    basin_mask:       np.ndarray,
    upstream_map:     dict,
    facc_array:       np.ndarray,
    pixel_area_m2:    float,
) -> list[dict]:
    """
    Delineates one sub-basin mask per selected outlet.

    Each sub-basin is obtained by running :func:`delineate_basin` from the
    confluence outlet pixel, then restricting the result to the parent basin
    mask.  Pixels already claimed by a larger sub-basin are excluded from
    smaller ones, preventing overlaps.

    Assignment strategy
    -------------------
    Outlets are processed in **descending drainage-area order** (already
    guaranteed by :func:`find_confluences`).  When two sub-basins overlap,
    the pixel is assigned to the sub-basin with the **larger** outlet area —
    this mimics the natural downstream-dominance principle.

    The **residual** pixels (inside the parent basin but not captured by any
    confluence sub-basin) form an implicit sub-basin that contains the
    outlet of the whole basin.  It is always appended last.

    Parameters
    ----------
    selected_outlets : list[dict]
        Output of :func:`subdivide_by_n` or :func:`subdivide_by_area`.
    basin_mask       : np.ndarray (bool)
        Parent basin mask — sub-basins are clipped to this.
    upstream_map     : dict
        From :func:`build_upstream_map` — shared across all BFS calls.
    facc_array       : np.ndarray
        Flow-accumulation raster — used to compute sub-basin area stats.
    pixel_area_m2    : float
        Area of one pixel in m².

    Returns
    -------
    list of dict, one per sub-basin:
        {
            'outlet_row'  : int,
            'outlet_col'  : int,
            'mask'        : np.ndarray (bool) — clipped to parent basin,
            'area_m2'     : float,
            'n_pixels'    : int,
            'rank'        : int   — 1 = largest, N = smallest / residual,
        }

    Notes
    -----
    Sub-basins with fewer than ``MIN_BASIN_PIXELS`` pixels are silently
    discarded to avoid degenerate geometries during vectorisation.
    """
    shape  = basin_mask.shape
    claimed = np.zeros(shape, dtype=bool)   # tracks already-assigned pixels
    subbasins: list[dict] = []
    rank = 1

    for outlet in selected_outlets:
        r_out = outlet["row"]
        c_out = outlet["col"]

        # Full BFS from this confluence
        raw_mask = delineate_basin(
            outlet_rc    = (r_out, c_out),
            upstream_map = upstream_map,
            shape        = shape,
        )

        # Clip to parent basin and remove already-claimed pixels
        sub_mask = raw_mask & basin_mask & ~claimed

        n_px = int(np.sum(sub_mask))
        if n_px < MIN_BASIN_PIXELS:
            continue  # Too small — skip silently

        claimed |= sub_mask

        subbasins.append({
            "outlet_row": r_out,
            "outlet_col": c_out,
            "mask":       sub_mask,
            "area_m2":    round(n_px * pixel_area_m2, 1),
            "n_pixels":   n_px,
            "rank":       rank,
        })
        rank += 1

    # Residual sub-basin — pixels inside basin but unclaimed
    residual_mask = basin_mask & ~claimed
    n_res = int(np.sum(residual_mask))
    if n_res >= MIN_BASIN_PIXELS:
        # Find the pixel with maximum accumulation inside the residual
        # → that is the outlet of the whole parent basin
        facc_res = np.where(residual_mask, facc_array, -np.inf)
        facc_res = np.where(np.isnan(facc_res), -np.inf, facc_res)
        res_outlet = np.unravel_index(np.argmax(facc_res), shape)

        subbasins.append({
            "outlet_row": int(res_outlet[0]),
            "outlet_col": int(res_outlet[1]),
            "mask":       residual_mask,
            "area_m2":    round(n_res * pixel_area_m2, 1),
            "n_pixels":   n_res,
            "rank":       rank,
        })

    return subbasins


# ===========================================================================
# 9. mask_to_polygon
# ===========================================================================

def mask_to_polygon_numpy(
    mask:        np.ndarray,
    geo_transform: tuple,
) -> Optional[QgsGeometry]:
    """
    Converts a binary raster mask to a QgsGeometry polygon.

    Each ``True`` pixel is represented as a unit square in map coordinates.
    Adjacent squares are merged by building an outer boundary from the
    pixel-edge set — edges shared by two ``True`` pixels cancel out, leaving
    only the perimeter.

    This approach avoids GDAL/OGR dependencies and works purely in Python /
    NumPy, making it portable across all QGIS installations.

    Algorithm (pixel-edge cancellation)
    ------------------------------------
    For every ``True`` pixel at ``(r, c)``:
      1. Compute the four corner coordinates in map space.
      2. For each of the four edges, toggle it in an edge-set.
         (Adding an already-present edge removes it — shared edges cancel.)
    After processing all pixels, the remaining edges form the polygon
    boundary.  These edges are then chained into a closed ring and
    converted to a ``QgsGeometry``.

    Parameters
    ----------
    mask          : np.ndarray (bool)
        Binary basin mask, shape ``(rows, cols)``.
    geo_transform : tuple
        GDAL GeoTransform ``(x_origin, px_width, 0, y_origin, 0, px_height)``.
        ``px_height`` is negative (top-left origin convention).

    Returns
    -------
    QgsGeometry or None
        Polygon geometry in the raster CRS.
        ``None`` if the mask is empty or the boundary cannot be resolved.

    Notes
    -----
    For very large or complex basins, consider using GDAL's
    ``gdal.Polygonize`` instead — it handles multi-part and holed polygons
    more robustly.  This implementation targets typical sub-basin masks
    (< 10 000 pixels) where simplicity and zero-dependency matter more.
    """
    x0, px_w, _, y0, _, px_h = geo_transform

    def pixel_corners(r: int, c: int) -> tuple:
        """Returns (xmin, xmax, ymin, ymax) for pixel (r, c)."""
        xmin = x0 + c * px_w
        xmax = xmin + px_w
        ymax = y0 + r * px_h        # px_h is negative → ymax > ymin
        ymin = ymax + px_h
        # Normalise so ymin < ymax regardless of px_h sign
        if ymin > ymax:
            ymin, ymax = ymax, ymin
        return xmin, xmax, ymin, ymax

    # ── Build edge set via toggle (shared edges cancel) ───────────────
    # Each edge is stored as a frozenset of its two endpoint tuples
    # to be order-independent.
    edge_set: set[frozenset] = set()

    basin_rows, basin_cols = np.where(mask)

    for r, c in zip(basin_rows.tolist(), basin_cols.tolist()):
        xmin, xmax, ymin, ymax = pixel_corners(r, c)

        corners = [
            (xmin, ymax),   # top-left
            (xmax, ymax),   # top-right
            (xmax, ymin),   # bottom-right
            (xmin, ymin),   # bottom-left
        ]

        for i in range(4):
            p_a = corners[i]
            p_b = corners[(i + 1) % 4]
            edge = (min(p_a, p_b), max(p_a, p_b)) 
            if edge in edge_set:
                edge_set.remove(edge)   # shared edge — cancel it
            else:
                edge_set.add(edge)

    if not edge_set:
        return None

    # ── Chain edges into a closed ring ────────────────────────────────
    # Build adjacency: each vertex maps to its two connected vertices
    adjacency: dict[tuple, list[tuple]] = {}
    for edge in edge_set:
        p1, p2 = edge[0], edge[1]
        adjacency.setdefault(p1, []).append(p2)
        adjacency.setdefault(p2, []).append(p1)

    # Walk the ring starting from an arbitrary vertex
    start   = next(iter(adjacency))
    ring    = [start]
    prev    = None
    current = start

    max_steps = len(adjacency) + 2   # safety limit
    for _ in range(max_steps):
        neighbours = adjacency.get(current, [])
        next_pt = None
        for nb in neighbours:
            if nb != prev:
                next_pt = nb
                break
        if next_pt is None or next_pt == start:
            ring.append(start)   # close the ring
            break
        ring.append(next_pt)
        prev    = current
        current = next_pt

    if len(ring) < 4:
        return None

    # ── Convert to QgsGeometry ─────────────────────────────────────────
    qgs_points = [QgsPointXY(x, y) for x, y in ring]
    return QgsGeometry.fromPolygonXY([qgs_points])


def mask_to_polygon_gdal(
    mask: np.ndarray,
    geo_transform: tuple,
) -> Optional[QgsGeometry]:
    """
    Convert a binary mask to QgsGeometry using GDAL Polygonize.

    Handles:
    - multi-polygons
    - holes
    - complex topology

    Returns:
        QgsGeometry (MultiPolygon) or None
    """
    if mask is None or mask.size == 0 or not np.any(mask):
        return None

    rows, cols = mask.shape

    # --- Create in-memory raster ---
    driver = gdal.GetDriverByName("MEM")
    ds = driver.Create("", cols, rows, 1, gdal.GDT_Byte)
    ds.SetGeoTransform(geo_transform)

    band = ds.GetRasterBand(1)
    band.WriteArray(mask.astype(np.uint8))
    band.SetNoDataValue(0)

    # --- Create in-memory vector layer ---
    drv = ogr.GetDriverByName("Memory")
    vds = drv.CreateDataSource("out")

    layer = vds.CreateLayer("polygonized", geom_type=ogr.wkbMultiPolygon)
    field_def = ogr.FieldDefn("value", ogr.OFTInteger)
    layer.CreateField(field_def)

    # --- Polygonize ---
    gdal.Polygonize(
        band,
        None,
        layer,
        0,  # field index
        [],
        callback=None
    )

    # --- Collect geometries where value == 1 ---
    geoms = []

    for feat in layer:
        val = feat.GetField("value")
        if val != 1:
            continue

        geom = feat.GetGeometryRef()
        if geom is None:
            continue

        geoms.append(geom.Clone())

    if not geoms:
        return None

    # --- Merge geometries ---
    union_geom = geoms[0]
    for g in geoms[1:]:
        union_geom = union_geom.Union(g)

    # --- Convert to QgsGeometry ---
    qgs_geom = QgsGeometry.fromWkt(union_geom.ExportToWkt())

    # Cleanup (important in QGIS)
    band = None
    ds = None
    vds = None

    return qgs_geom


def mask_to_polygon(
    mask: np.ndarray,
    geo_transform: tuple,
    method: str = "auto",
    threshold_pixels: int = 10000,
) -> Optional[QgsGeometry]:
    """
    Smart dispatcher between NumPy and GDAL polygonization.

    Parameters
    ----------
    method : str
        "auto", "numpy", "gdal"
    threshold_pixels : int
        switch threshold for auto mode

    Returns
    -------
    QgsGeometry or None
    """

    if mask is None or mask.size == 0 or not np.any(mask):
        return None

    n_pixels = int(np.count_nonzero(mask))

    # --- Explicit override ---
    if method == "numpy":
        return mask_to_polygon_numpy(mask, geo_transform)

    if method == "gdal":
        return mask_to_polygon_gdal(mask, geo_transform)

    # --- AUTO MODE ---
    # Heuristic: small mask → numpy, large → GDAL
    # AUTO — small mask tries numpy first, large goes straight to GDAL
    if n_pixels < threshold_pixels:
        try:
            result = mask_to_polygon_numpy(mask, geo_transform)
            if result is not None:
                return result
            # NumPy returned None (e.g. ring too short) → fall through
        except Exception as e:
            # Log the reason instead of swallowing silently
            import traceback
            traceback.print_exc()
            # fallback safety
            return mask_to_polygon_gdal(mask, geo_transform)
    else:
        # GDAL path — handles holes, multi-polygons, large masks
        try:
            return mask_to_polygon_gdal(mask, geo_transform)
        except Exception as e:
            import traceback
            traceback.print_exc()
            return None   # Both methods failed — caller will skip this sub-basin



# ===========================================================================
# Utility helpers
# ===========================================================================

def pixel_area(geo_transform: tuple, crs_is_geographic: bool) -> float:
    """
    Computes the area of one raster pixel in m².

    For projected CRS (metres), area = |px_width × px_height|.
    For geographic CRS (degrees), the pixel width is converted to metres
    at the centre latitude using the WGS84 ellipsoid approximation.

    Parameters
    ----------
    geo_transform      : tuple  — GDAL GeoTransform 6-tuple
    crs_is_geographic  : bool   — True if CRS unit is degrees

    Returns
    -------
    float : pixel area in m²
    """
    _, px_w, _, y0, _, px_h = geo_transform
    pw = abs(px_w)
    ph = abs(px_h)

    if not crs_is_geographic:
        return pw * ph

    # Geographic CRS — approximate centre-latitude correction
    centre_lat_deg = y0 + (ph / 2.0)
    metres_per_deg_lat = math.pi / 180.0 * 6_371_000.0
    metres_per_deg_lon = (
        math.pi / 180.0
        * 6_371_000.0
        * math.cos(math.radians(centre_lat_deg))
    )
    return (pw * metres_per_deg_lon) * (ph * metres_per_deg_lat)


def world_to_pixel(
    x: float,
    y: float,
    geo_transform: tuple,
    shape:         tuple,
) -> tuple[int, int]:
    """
    Converts map coordinates (x, y) to raster pixel indices (row, col).

    Returns ``(-1, -1)`` if the point falls outside the raster extent.

    Parameters
    ----------
    x, y          : float  — map coordinates in the raster CRS
    geo_transform : tuple  — GDAL GeoTransform 6-tuple
    shape         : tuple  — (rows, cols) of the raster

    Returns
    -------
    tuple[int, int] : (row, col) or (-1, -1) if out of bounds
    """
    x0, px_w, _, y0, _, px_h = geo_transform
    rows, cols = shape

    col = int((x - x0) / px_w)
    row = int((y - y0) / px_h)

    if 0 <= row < rows and 0 <= col < cols:
        return row, col
    return -1, -1