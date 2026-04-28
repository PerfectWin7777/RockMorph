# core/watershed.py

"""
core/watershed.py

Low-level D8 watershed delineation and subdivision utilities for RockMorph.

ALGORITHM — CSR Upstream BFS
-----------------------------
The correct approach for sub-basin labeling is upstream BFS from outlet seeds.

Why not topological-sort propagation?
  Topological sort (ascending FAC) propagates labels DOWNSTREAM (push from
  headwaters toward outlet). But our outlets are interior confluence pixels,
  not headwaters. A headwater pixel starts with label=0 and has nothing to
  inherit from its downstream neighbour until that downstream pixel is seeded.
  Result: everything upstream of outlets stays unlabelled (the big blue blob).

Correct approach — CSR Upstream BFS:
  1. Build a Compressed Sparse Row (CSR) upstream index: for each pixel,
     store which pixels drain INTO it. Built with np.argsort in 0.2s.
  2. Seed outlet pixels with unique label IDs.
  3. BFS from each outlet going UPSTREAM: every upstream neighbour
     inherits the outlet's label (first-come-first-served for contested pixels).
  Result: all pixels upstream of outlet[i] get label i. Correct by construction.

Pipeline
--------
  1. detect_encoding       — infer D8 format from raster values
  2. build_downstream      — FDIR 2D → flat int32 array  downstream[i] = j
  3. build_upstream_csr    — invert downstream → CSR upstream structure
  4. find_outlets          — locate + filter confluence pixels
  5. propagate_labels_bfs  — BFS upstream from outlets → label map
  6. subbasins_from_labels — one mask + polygon per unique label ID
  7. mask_to_polygon       — raster mask → QgsGeometry  (Tony's code, untouched)

Complexity
----------
  build_downstream     : O(8N) vectorised
  build_upstream_csr   : O(N log N) — one np.argsort call
  find_outlets         : O(N) — np.bincount
  propagate_labels_bfs : O(N) — each pixel visited once
  Total wall time on 2000×2000 raster: ~0.5s

DESIGN RULES
------------
- Zero UI logic.
- All heavy work is vectorised NumPy — no pixel-level Python loops
  except the BFS queue iteration (unavoidable, but O(N) and fast).
- QgsGeometry only produced in mask_to_polygon.
- Thread-safe: no shared mutable state.

D8 ENCODING REFERENCE
---------------------
  ESRI / ArcGIS  — powers of 2, clockwise from E:
      32   64  128
      16    ·    1
       8    4    2

  GRASS r.watershed — 1-8, clockwise from NE:
       3    2    1
       4    ·    8
       5    6    7

  SAGA / TauDEM — 0-7, clockwise from N:
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
from osgeo import gdal, ogr                     # type: ignore


def tr(message: str) -> str:
    return QCoreApplication.translate("RockMorph", message)


# ---------------------------------------------------------------------------
# Constants
# ---------------------------------------------------------------------------

# Minimum number of pixels for a sub-basin to be kept.
MIN_BASIN_PIXELS: int = 50

# Minimum pixel spacing between two selected outlet confluences.
MIN_OUTLET_SPACING_PX: int = 20


# ---------------------------------------------------------------------------
# D8 offset look-up tables
# key   → raster cell value
# value → (dy, dx)  displacement TO the downstream neighbour
# ---------------------------------------------------------------------------

D8_OFFSETS: dict[str, dict[int, tuple[int, int]]] = {
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


# ===========================================================================
# 1. detect_encoding
# ===========================================================================

def detect_encoding(fdir_array: np.ndarray) -> str:
    """
    Infers the D8 encoding from the unique values present in the raster.

    Detection rules
    ---------------
    - Any value in {16, 32, 64, 128}  → ESRI  (powers-of-2 > 8)
    - 0 present AND max ≤ 7           → SAGA
    - Values in 1-8 only              → GRASS
    """
    valid = fdir_array[~np.isnan(fdir_array)]
    if valid.size == 0:
        raise ValueError(tr(
            "Flow direction raster is empty or contains only NoData. "
            "Cannot detect encoding."
        ))

    unique_vals = np.unique(valid).astype(int)
    unique_set  = set(unique_vals.tolist())

    if unique_set & {16, 32, 64, 128}:
        return "esri"

    if 0 in unique_set and int(unique_vals.max()) <= 7:
        return "saga"

    if unique_set <= {1, 2, 3, 4, 5, 6, 7, 8}:
        return "grass"

    raise ValueError(tr(
        f"Unrecognised D8 encoding. Values found: {sorted(unique_set)[:15]}. "
        "Make sure you selected the Flow Direction raster, not the DEM."
    ))


# ===========================================================================
# 2. build_downstream
# ===========================================================================

def build_downstream(fdir_array: np.ndarray, encoding: str) -> np.ndarray:
    """
    Convert the 2-D D8 FDIR raster into a flat int32 array where
    ``downstream[i]`` is the 1-D index of pixel i's downstream neighbour.

    Pixels with no valid direction (NoData, boundary) point to themselves,
    acting as sinks — they will keep their own label during propagation.

    Algorithm
    ---------
    Fully vectorised: for each of the (up to 8) direction values, we build
    a boolean mask of pixels carrying that value, shift their indices by the
    corresponding (dy, dx) offset, and write the result in one NumPy
    assignment. No Python loop over pixels.

    Complexity: O(8 × N) ≈ O(N).

    Parameters
    ----------
    fdir_array : np.ndarray  shape (rows, cols)
    encoding   : str — 'esri', 'grass', or 'saga'

    Returns
    -------
    downstream : np.ndarray  shape (rows × cols,)  dtype int32
        downstream[i] = flat index of downstream neighbour.
        downstream[i] = i  when pixel is a sink / NoData.
    """
    rows, cols = fdir_array.shape
    size = rows * cols

    # Every pixel starts pointing to itself (= sink / NoData)
    downstream = np.arange(size, dtype=np.int32)

    flat_fdir = fdir_array.ravel()
    offsets   = D8_OFFSETS[encoding]

    # Row and column index for every flat pixel
    all_rows = np.arange(size, dtype=np.int32) // cols
    all_cols = np.arange(size, dtype=np.int32) %  cols

    for val, (dy, dx) in offsets.items():
        # Pixels carrying this direction value
        mask = (flat_fdir == val)
        if not np.any(mask):
            continue

        dst_r = all_rows[mask] + dy
        dst_c = all_cols[mask] + dx

        # Keep only pixels whose downstream neighbour is inside the raster
        in_bounds = (dst_r >= 0) & (dst_r < rows) & (dst_c >= 0) & (dst_c < cols)

        src_idx = np.where(mask)[0][in_bounds]
        dst_idx = dst_r[in_bounds] * cols + dst_c[in_bounds]

        downstream[src_idx] = dst_idx.astype(np.int32)

    return downstream


# ===========================================================================
# 3. find_outlets (Rewritten with 'True Confluences' Geomorphology)
# ===========================================================================

def find_outlets(
    facc_array:  np.ndarray,
    downstream:  np.ndarray,
    shape:       tuple[int, int],
    px_area_m2:  float,
    mode:        str,            # "n" or "area"
    n_target:    int  = 10,
    min_area_m2: float = 10_000_000.0,
    segment_main_stem: bool = True,
) -> list[dict]:
    """
    Identify geomorphologically correct tributary mouths to serve as sub-basin outlets.

    Algorithm (True Confluences)
    ----------------------------
    1. Group all valid flow connections by their destination pixel.
    2. Sort the incoming source pixels for each destination by their FACC (descending).
    3. The incoming pixel with the largest FACC is the "Main Stem".
    4. Any OTHER incoming pixels (2nd largest, 3rd largest, etc.) are "Affluents".
    5. We collect all Affluent mouths across the entire DEM and sort them globally
       by their FACC to find the most significant tributaries.
    6. We yield these Affluent mouths as the sub-basin seeds.

    Why this works: By seeding the tributary JUST UPSTREAM of the confluence, the
    BFS will exclusively climb the tributary branch. The main stem remains separate 
    and becomes part of the downstream residual basin.

    Performance: 100% vectorized NumPy (O(N log N)), 0 Python loops for the search.
    """
    rows, cols = shape
    flat_size  = rows * cols
    flat_facc  = facc_array.ravel()

    # ── Step 1 : Identify valid flow connections
    src_all    = np.arange(flat_size, dtype=np.int32)
    not_sink   = (downstream != src_all)
    valid_src  = src_all[not_sink]
    valid_dst  = downstream[not_sink]
    
    valid_facc = flat_facc[valid_src]
    valid_facc = np.where(np.isnan(valid_facc), 0.0, valid_facc)

    if valid_src.size == 0:
        return[]

    # ── Step 2 : Sort sources grouped by destination
    order       = np.lexsort((-valid_facc, valid_dst))
    sorted_dst  = valid_dst[order]
    sorted_src  = valid_src[order]
    sorted_facc = valid_facc[order]

    # ── Step 3 : Find group boundaries
    changes = np.concatenate(([True], sorted_dst[1:] != sorted_dst[:-1]))
    starts  = np.where(changes)[0]
    lengths = np.diff(np.append(starts, len(sorted_dst)))

    # ── Step 4 : Isolate True Affluents (Tributaries)
    conf_mask = (lengths >= 2)
    in_confluence = np.repeat(conf_mask, lengths)
    
    is_main_stem = np.zeros(len(sorted_dst), dtype=bool)
    is_main_stem[starts] = True
    
    is_affluent = in_confluence & (~is_main_stem)
    aff_src = sorted_src[is_affluent]
    aff_dst = sorted_dst[is_affluent]
    aff_fac = sorted_facc[is_affluent]

    if aff_src.size == 0:
        return[]

    # Fast O(N) lookup : On sauvegarde qui est le Main Stem de chaque carrefour
    dst_to_main_src = np.full(flat_size, -1, dtype=np.int32)
    dst_to_main_fac = np.zeros(flat_size, dtype=np.float64)
    dst_to_main_src[sorted_dst[starts]] = sorted_src[starts]
    dst_to_main_fac[sorted_dst[starts]] = sorted_facc[starts]

    # ── Step 5 : Sort Affluents Globally
    global_order = np.argsort(aff_fac)[::-1]
    best_aff_src = aff_src[global_order]
    best_aff_dst = aff_dst[global_order]
    best_aff_fac = aff_fac[global_order]

    # ── Step 6 : Filter and Select at Major Junctions
    n_outlets = max(1, n_target - 1)
    selected: list[dict] =[]

    for src, dst, fac in zip(best_aff_src, best_aff_dst, best_aff_fac):
        
        if len(selected) >= n_outlets:
            break

        area = float(fac) * px_area_m2
        if mode == "area" and area < min_area_m2:
            break

        r, c = int(src) // cols, int(src) % cols

        # Spatial spacing (on évite de sélectionner des affluents de la même zone)
        too_close = any(
            abs(r - s["row"]) < MIN_OUTLET_SPACING_PX and
            abs(c - s["col"]) < MIN_OUTLET_SPACING_PX
            for s in selected
        )
        if too_close:
            continue

        # 1. On accepte l'affluent comme sous-bassin !
        selected.append({
            "idx": int(src),
            "row": r,
            "col": c,
            "area_m2": area,
        })

        # 2. LE COUP DE GÉNIE : Si on segmente le fleuve principal,
        #    on ajoute aussi la branche principale qui se jette dans CE MEME carrefour.
        if segment_main_stem:
            m_src = int(dst_to_main_src[dst])
            if m_src != -1:
                m_area = float(dst_to_main_fac[dst]) * px_area_m2
                
                selected.append({
                    "idx": m_src,
                    "row": m_src // cols,
                    "col": m_src % cols,
                    "area_m2": m_area,
                })

    return selected


# ===========================================================================
# 4. build_upstream_csr
# ===========================================================================

def build_upstream_csr(
    downstream: np.ndarray,
    size:       int,
) -> tuple[np.ndarray, np.ndarray]:
    """
    Build a Compressed Sparse Row (CSR) upstream index from the downstream
    array. This is the inverse of the downstream map: for each pixel i,
    the CSR structure gives the list of pixels that drain INTO i.

    Algorithm
    ---------
    We use np.argsort on the downstream array. After sorting, all pixels
    that share the same destination are grouped together in the sorted
    array. We then use np.searchsorted to find where each destination
    group starts.

    This replaces a Python loop over all pixels (which takes ~2.6s) with
    a single np.argsort call (~0.2s).

    Parameters
    ----------
    downstream : np.ndarray  shape (N,)  — downstream[i] = flat index of
                 pixel i's downstream neighbour (self-loop = sink)
    size       : int  — total number of pixels (rows × cols)

    Returns
    -------
    csr_indices  : np.ndarray  int32  — sorted source pixel indices
    csr_indptr   : np.ndarray  int32  — shape (size+1,)
                   upstream pixels of pixel i are:
                   csr_indices[ csr_indptr[i] : csr_indptr[i+1] ]

    Example
    -------
    upstream_of_pixel_42 = csr_indices[ csr_indptr[42] : csr_indptr[43] ]
    """
    # Sort source pixels by their destination — groups all upstreams together
    sort_order  = np.argsort(downstream, kind="stable").astype(np.int32)
    sorted_dst  = downstream[sort_order]

    # searchsorted gives the start index in sort_order for each destination
    indptr = np.searchsorted(sorted_dst, np.arange(size + 1, dtype=np.int32))
    indptr = indptr.astype(np.int32)

    return sort_order, indptr


# ===========================================================================
# 5. propagate_labels_bfs
# ===========================================================================

def propagate_labels_bfs(
    downstream:  np.ndarray,
    csr_indices: np.ndarray,
    csr_indptr:  np.ndarray,
    outlets:     list[dict],
    size:        int,
    shape:       tuple[int, int],
) -> np.ndarray:
    """
    Assign sub-basin labels by BFS going UPSTREAM from outlet seeds.

    Why BFS upstream and not topological-sort downstream?
    -------------------------------------------------------
    Our outlets are interior confluence pixels (not headwaters). A pixel
    upstream of an outlet has lower FAC and no label to start with. If we
    propagate downstream (push from headwaters), headwaters start with
    label=0 and have nothing to push — they stay unlabelled forever.

    Going UPSTREAM from the outlet is the natural direction:
    every pixel reachable by following flow toward the outlet
    belongs to that outlet's sub-basin.

    Algorithm
    ---------
    1. Seed each outlet with a unique label ID.
    2. Add all outlets to a BFS queue.
    3. For each pixel dequeued:
         For each upstream neighbour (from CSR structure):
           If unlabelled → assign same label, enqueue.
    4. After BFS, pixels still labelled 0 are either NoData or
       they are part of the area DOWNSTREAM of all outlets — they
       form the residual sub-basin and get the last label ID.

    First-come-first-served: if two BFS fronts meet, the pixel goes
    to whichever outlet reached it first (largest-area outlet wins
    because we seed in descending area order).

    Complexity: O(N) — each pixel is enqueued and dequeued at most once.
    Wall time on 2000×2000: ~0.02s for the BFS itself.

    Parameters
    ----------
    downstream  : np.ndarray  shape (N,)
    csr_indices : np.ndarray  from build_upstream_csr
    csr_indptr  : np.ndarray  from build_upstream_csr
    outlets     : list[dict]  from find_outlets(), sorted descending by area
    size        : int  rows × cols
    shape       : (rows, cols)

    Returns
    -------
    labels : np.ndarray  shape (rows, cols)  dtype int32
        0   = unlabelled residual (will be assigned residual label by caller)
        1…K = sub-basin IDs  (1 = outlet with largest upstream area)
    """
    
    rows, cols = shape
    labels   = np.zeros(size, dtype=np.int32)
    in_queue = np.zeros(size, dtype=bool)

    queue = deque()

    # 1. Identifier l'exutoire global (le point le plus en aval, donc plus grand FACC)
    # C'est lui qui représente le fleuve principal aval
    main_outlet_idx = max(outlets, key=lambda o: o["area_m2"])["idx"]

    # 2. Placer les graines
    for label_id, outlet in enumerate(outlets, start=1):
        start_idx = outlet["idx"]

        if start_idx == main_outlet_idx:
            # MAGIE GÉOMORPHOLOGIQUE : "Seed the Trunk"
            # On suit l'eau en aval jusqu'au vrai bord du MNT pour fermer le vide
            curr = start_idx
            path = [curr]
            while downstream[curr] != curr:
                next_node = downstream[curr]
                if next_node in path:  # Sécurité anti-boucle infinie (au cas où)
                    break
                curr = next_node
                path.append(curr)
                
            # Toute cette ligne (les 20 derniers pixels jusqu'à la mer) devient la source du BFS
            for node in path:
                labels[node] = label_id
                if not in_queue[node]:
                    in_queue[node] = True
                    queue.append(node)
        else:
            # Pour les autres sous-bassins, on utilise juste le point de l'affluent
            labels[start_idx] = label_id
            if not in_queue[start_idx]:
                in_queue[start_idx] = True
                queue.append(start_idx)

    # 3. BFS upstream (Le reste est standard)
    while queue:
        curr       = queue.popleft()
        curr_label = labels[curr]

        start = int(csr_indptr[curr])
        end   = int(csr_indptr[curr + 1])

        for k in range(start, end):
            up = int(csr_indices[k])

            if up == curr or in_queue[up]:
                continue
            if labels[up] != 0:
                continue

            labels[up]   = curr_label
            in_queue[up] = True
            queue.append(up)

    # AUCUNE RUSTINE "residual_label" A LA FIN !
    # Tout ce qui est 0 (le fond du rectangle) reste 0 et sera ignoré.
    return labels.reshape(rows, cols)


# ===========================================================================
# 5. subbasins_from_labels
# ===========================================================================

def subbasins_from_labels(
    label_map:    np.ndarray,
    facc_array:   np.ndarray,
    px_area_m2:   float,
    geo_transform: tuple,
    polygon_method: str = "auto",
) -> list[dict]:
    """
    Convert a label raster into a list of sub-basin dicts, one per label ID.

    For each unique non-zero label:
      - Extract the boolean mask.
      - Compute area and pixel count.
      - Find the outlet pixel (maximum FAC inside the mask).
      - Vectorise the mask to a QgsGeometry polygon.

    Sub-basins with fewer than MIN_BASIN_PIXELS pixels are silently discarded.

    Parameters
    ----------
    label_map     : np.ndarray (int32)   shape (rows, cols)
    facc_array    : np.ndarray (float)   shape (rows, cols)
    px_area_m2    : float
    geo_transform : tuple  — GDAL 6-tuple
    polygon_method: str  — passed to mask_to_polygon dispatcher

    Returns
    -------
    list of dict (sorted descending by area_m2):
        {
            'rank'      : int,
            'area_m2'   : float,
            'area_km2'  : float,
            'n_pixels'  : int,
            'outlet_row': int,
            'outlet_col': int,
            'outlet_xy' : (float, float),
            'geometry'  : QgsGeometry | None,
        }
    """
    unique_ids = np.unique(label_map)
    unique_ids = unique_ids[unique_ids > 0]   # skip label 0 (unlabelled)

    results = []

    for label_id in unique_ids.tolist():
        mask   = (label_map == label_id)
        n_px   = int(np.count_nonzero(mask))

        if n_px < MIN_BASIN_PIXELS:
            continue

        area_m2  = float(n_px) * px_area_m2

        # Outlet = pixel with maximum accumulation inside this sub-basin
        facc_masked = np.where(mask, facc_array, -np.inf)
        facc_masked = np.where(np.isnan(facc_masked), -np.inf, facc_masked)
        outlet_rc   = np.unravel_index(np.argmax(facc_masked), mask.shape)
        r_out, c_out = int(outlet_rc[0]), int(outlet_rc[1])

        # Map coordinates of outlet
        x0, px_w, _, y0, _, px_h = geo_transform
        out_x = x0 + c_out * px_w + px_w / 2.0
        out_y = y0 + r_out * px_h + px_h / 2.0

        # Vectorise mask → QgsGeometry
        geom = mask_to_polygon(mask, geo_transform, method=polygon_method)

        # ── NOUVEAU : TOPOLOGICAL HOLE FILLING (L'idée de génie) ──────
        # Supprime géométriquement tous les trous (sinks) dans le polygone
        if geom is not None and not geom.isEmpty():
            if geom.isMultipart():
                # Pour les multi-polygones, on garde le Ring[0] de chaque partie
                polys = geom.asMultiPolygon()
                filled_polys = [[poly[0]] for poly in polys if poly]
                geom = QgsGeometry.fromMultiPolygonXY(filled_polys)
            else:
                # Pour un polygone simple, on garde juste le Ring[0]
                poly = geom.asPolygon()
                if poly:
                    geom = QgsGeometry.fromPolygonXY([poly[0]])

        results.append({
            "label_id":   int(label_id),
            "area_m2":    round(area_m2, 1),
            "area_km2":   round(area_m2 / 1_000_000.0, 4),
            "n_pixels":   n_px,
            "outlet_row": r_out,
            "outlet_col": c_out,
            "outlet_xy":  (round(out_x, 4), round(out_y, 4)),
            "geometry":   geom,
        })

    # Sort descending by area and assign rank
    results.sort(key=lambda x: x["area_m2"], reverse=True)
    for rank, sb in enumerate(results, start=1):
        sb["rank"] = rank

    return results


# ===========================================================================
# 6. mask_to_polygon  (Tony's code — do not modify)
# ===========================================================================

def mask_to_polygon_numpy(
    mask: np.ndarray,
    geo_transform: tuple,
) -> Optional[QgsGeometry]:
    """
    Converts a binary raster mask to a QgsGeometry polygon using pixel-edge
    cancellation. Adjacent pixel edges cancel out, leaving only the perimeter.
    Pure NumPy — no GDAL dependency.
    """
    x0, px_w, _, y0, _, px_h = geo_transform

    def pixel_corners(r: int, c: int) -> tuple:
        xmin = x0 + c * px_w
        xmax = xmin + px_w
        ymax = y0 + r * px_h
        ymin = ymax + px_h
        if ymin > ymax:
            ymin, ymax = ymax, ymin
        return xmin, xmax, ymin, ymax

    edge_set: set = set()
    basin_rows, basin_cols = np.where(mask)

    for r, c in zip(basin_rows.tolist(), basin_cols.tolist()):
        xmin, xmax, ymin, ymax = pixel_corners(r, c)
        corners = [
            (xmin, ymax),
            (xmax, ymax),
            (xmax, ymin),
            (xmin, ymin),
        ]
        for i in range(4):
            p_a = corners[i]
            p_b = corners[(i + 1) % 4]
            edge = (min(p_a, p_b), max(p_a, p_b))
            if edge in edge_set:
                edge_set.remove(edge)
            else:
                edge_set.add(edge)

    if not edge_set:
        return None

    adjacency: dict = {}
    for edge in edge_set:
        p1, p2 = edge[0], edge[1]
        adjacency.setdefault(p1, []).append(p2)
        adjacency.setdefault(p2, []).append(p1)

    start   = next(iter(adjacency))
    ring    = [start]
    prev    = None
    current = start

    for _ in range(len(adjacency) + 2):
        neighbours = adjacency.get(current, [])
        next_pt = None
        for nb in neighbours:
            if nb != prev:
                next_pt = nb
                break
        if next_pt is None or next_pt == start:
            ring.append(start)
            break
        ring.append(next_pt)
        prev    = current
        current = next_pt

    if len(ring) < 4:
        return None

    qgs_points = [QgsPointXY(x, y) for x, y in ring]
    return QgsGeometry.fromPolygonXY([qgs_points])


def mask_to_polygon_gdal(
    mask: np.ndarray,
    geo_transform: tuple,
) -> Optional[QgsGeometry]:
    """
    Convert a binary mask to QgsGeometry using GDAL Polygonize.
    Handles multi-polygons, holes, and complex topology.
    """
    if mask is None or mask.size == 0 or not np.any(mask):
        return None

    rows, cols = mask.shape
    driver = gdal.GetDriverByName("MEM")
    ds     = driver.Create("", cols, rows, 1, gdal.GDT_Byte)
    ds.SetGeoTransform(geo_transform)

    band = ds.GetRasterBand(1)
    band.WriteArray(mask.astype(np.uint8))
    band.SetNoDataValue(0)

    drv = ogr.GetDriverByName("Memory")
    vds = drv.CreateDataSource("out")
    layer = vds.CreateLayer("polygonized", geom_type=ogr.wkbMultiPolygon)
    field_def = ogr.FieldDefn("value", ogr.OFTInteger)
    layer.CreateField(field_def)

    gdal.Polygonize(band, None, layer, 0, [], callback=None)

    geoms = []
    for feat in layer:
        if feat.GetField("value") != 1:
            continue
        geom = feat.GetGeometryRef()
        if geom is not None:
            geoms.append(geom.Clone())

    band  = None
    ds    = None
    vds   = None

    if not geoms:
        return None

    union_geom = geoms[0]
    for g in geoms[1:]:
        union_geom = union_geom.Union(g)

    return QgsGeometry.fromWkt(union_geom.ExportToWkt())


def mask_to_polygon(
    mask: np.ndarray,
    geo_transform: tuple,
    method: str = "auto",
    threshold_pixels: int = 10_000,
) -> Optional[QgsGeometry]:
    """
    Smart dispatcher: NumPy for small masks, GDAL for large or complex ones.

    Parameters
    ----------
    method           : "auto", "numpy", or "gdal"
    threshold_pixels : pixel count above which auto switches to GDAL
    """
    if mask is None or mask.size == 0 or not np.any(mask):
        return None

    n_pixels = int(np.count_nonzero(mask))

    if method == "numpy":
        return mask_to_polygon_numpy(mask, geo_transform)

    if method == "gdal":
        return mask_to_polygon_gdal(mask, geo_transform)

    # AUTO
    if n_pixels < threshold_pixels:
        try:
            result = mask_to_polygon_numpy(mask, geo_transform)
            if result is not None:
                return result
        except Exception:
            pass
        return mask_to_polygon_gdal(mask, geo_transform)
    else:
        try:
            return mask_to_polygon_gdal(mask, geo_transform)
        except Exception:
            return None


# ===========================================================================
# Utility helpers
# ===========================================================================

def pixel_area(geo_transform: tuple, crs_is_geographic: bool) -> float:
    """
    Area of one raster pixel in m².

    For projected CRS: |px_width × px_height|.
    For geographic CRS: approximate conversion at centre latitude.
    """
    _, px_w, _, y0, _, px_h = geo_transform
    pw = abs(px_w)
    ph = abs(px_h)

    if not crs_is_geographic:
        return pw * ph

    centre_lat_deg     = y0 + ph / 2.0
    metres_per_deg_lat = math.pi / 180.0 * 6_371_000.0
    metres_per_deg_lon = (
        math.pi / 180.0 * 6_371_000.0
        * math.cos(math.radians(centre_lat_deg))
    )
    return (pw * metres_per_deg_lon) * (ph * metres_per_deg_lat)


def world_to_pixel(
    x: float,
    y: float,
    geo_transform: tuple,
    shape: tuple,
) -> tuple[int, int]:
    """
    Convert map coordinates (x, y) to raster pixel indices (row, col).
    Returns (-1, -1) if the point falls outside the raster extent.
    """
    x0, px_w, _, y0, _, px_h = geo_transform
    rows, cols = shape

    col = int((x - x0) / px_w)
    row = int((y - y0) / px_h)

    if 0 <= row < rows and 0 <= col < cols:
        return row, col
    return -1, -1


def snap_outlet(
    row: int,
    col: int,
    facc_array: np.ndarray,
    radius_px: int = 25,
) -> tuple[int, int]:
    """
    Move a user outlet pixel to the highest-accumulation cell within a
    square search window of half-side radius_px.
    """
    rows, cols = facc_array.shape

    r0 = max(0, row - radius_px)
    r1 = min(rows, row + radius_px + 1)
    c0 = max(0, col - radius_px)
    c1 = min(cols, col + radius_px + 1)

    window = facc_array[r0:r1, c0:c1].copy().astype(np.float64)
    window[np.isnan(window)] = -np.inf

    if np.all(np.isinf(window)):
        return row, col

    local_idx = np.unravel_index(np.argmax(window), window.shape)
    return int(r0 + local_idx[0]), int(c0 + local_idx[1])