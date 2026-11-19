"""
core/indices/flow.py

Pure-NumPy flow routing used to build basin contexts.

Why RockMorph routes flow itself
--------------------------------
GSV-Studio relied on PySheds and RockMorph's fluvial tool on GRASS. Neither is
guaranteed in a stock QGIS install (PySheds never is; GRASS is optional on
several platforms). The morphometric indices only need flow routing *inside
one basin at a time*, which is small enough for a careful NumPy/heapq
implementation. Keeping it here also makes every hydrological step unit
testable.

Algorithms
----------
* Depression filling: Priority-Flood + epsilon (Barnes, Lehman & Mulla, 2014,
  Computers & Geosciences 62, 117-127). Cells are released from the basin
  border inwards in elevation order; any cell lower than the cell that
  reaches it is raised to that elevation plus a tiny epsilon, which both
  fills pits and gives flats a drainage gradient so D8 is always defined.
* Flow directions: D8 steepest descent (O'Callaghan & Mark, 1984) with
  diagonal distances weighted by sqrt(2).
* Accumulation: one pass over cells sorted from highest to lowest filled
  elevation (a topological order of the D8 tree).
* Trunk stream: from the outlet, repeatedly step to the donor cell with the
  largest accumulation until a cell without donors (the drainage divide) is
  reached. This is the "main stream" of Hack (1957): it follows the largest
  tributary at every junction and ends on the divide, so its length is the L
  of Hack's stream length-gradient index.
* Strahler ordering (Strahler, 1952) on the cells whose drainage area exceeds
  a channel-initiation threshold, and Horton stream statistics (streams, not
  links; see ``context.NetworkSummary``).

Grids are 2-D arrays addressed in row-major flat indices. Cells outside the
basin are excluded with a boolean ``mask``.

Authors: RockMorph contributors / Tony
"""

from __future__ import annotations

import heapq
import math
from collections import defaultdict
from typing import Dict, List, Optional, Sequence, Tuple

import numpy as np  # type: ignore

from .context import NetworkSummary

# Row/column offsets of the 8 neighbours, clockwise from north.
NEIGHBOUR_OFFSETS: Tuple[Tuple[int, int], ...] = (
    (-1, 0), (-1, 1), (0, 1), (1, 1), (1, 0), (1, -1), (0, -1), (-1, -1),
)

# Elevation increment imposed on flats by Priority-Flood + epsilon. 1e-5 m is
# far below DEM vertical precision (so it never changes a reported elevation
# in a meaningful way) but above float64 rounding at mountain elevations.
FLAT_EPSILON_M = 1e-5


# ---------------------------------------------------------------------------
# Depression filling
# ---------------------------------------------------------------------------

def priority_flood_fill(dem: np.ndarray, mask: np.ndarray,
                        epsilon: float = FLAT_EPSILON_M) -> np.ndarray:
    """
    Fill depressions inside ``mask`` with Priority-Flood + epsilon.

    Parameters
    ----------
    dem     : 2-D elevation array (NaN allowed outside the mask).
    mask    : 2-D boolean array, True for cells that belong to the basin.
    epsilon : increment given to cells raised by the fill (see module doc).

    Returns
    -------
    Filled float64 array; cells outside the mask are NaN.

    The basin border (mask cells touching a non-mask cell or the array edge)
    seeds the priority queue: water may leave the basin through any border
    cell, the lowest one becoming the outlet.
    """
    rows, cols = dem.shape
    valid = mask & np.isfinite(dem)
    filled = np.where(valid, dem.astype(np.float64), np.nan)
    closed = ~valid  # cells never to be processed

    # Border cells: valid cells with at least one invalid/out-of-grid neighbour.
    padded = np.pad(valid, 1, constant_values=False)
    interior = np.ones_like(valid)
    for dr, dc in NEIGHBOUR_OFFSETS:
        interior &= padded[1 + dr:1 + dr + rows, 1 + dc:1 + dc + cols]
    border = valid & ~interior

    heap: List[Tuple[float, int]] = []
    flat_filled = filled.ravel()
    flat_closed = closed.ravel()
    for index in np.flatnonzero(border.ravel()):
        heap.append((flat_filled[index], int(index)))
        flat_closed[index] = True
    heapq.heapify(heap)

    while heap:
        elevation, index = heapq.heappop(heap)
        row, col = divmod(index, cols)
        for dr, dc in NEIGHBOUR_OFFSETS:
            nr, nc = row + dr, col + dc
            if nr < 0 or nr >= rows or nc < 0 or nc >= cols:
                continue
            neighbour = nr * cols + nc
            if flat_closed[neighbour]:
                continue
            flat_closed[neighbour] = True
            if flat_filled[neighbour] <= elevation:
                # Raise to just above the spill elevation: fills the pit and
                # creates a gradient across what would otherwise be a flat.
                flat_filled[neighbour] = elevation + epsilon
            heapq.heappush(heap, (flat_filled[neighbour], neighbour))

    return flat_filled.reshape(rows, cols)


# ---------------------------------------------------------------------------
# D8 directions and accumulation
# ---------------------------------------------------------------------------

def d8_receivers(filled: np.ndarray, mask: np.ndarray, cell_size: float) -> np.ndarray:
    """
    D8 steepest-descent receiver of every cell.

    Returns a flat int64 array ``receiver`` where ``receiver[i]`` is the flat
    index of the cell receiving the flow of cell ``i``, or -1 when ``i`` is
    outside the mask or has no lower neighbour inside the mask (basin outlet).
    """
    rows, cols = filled.shape
    best_drop = np.zeros((rows, cols), dtype=np.float64)
    receiver = np.full((rows, cols), -1, dtype=np.int64)
    padded = np.pad(np.where(mask, filled, np.nan), 1, constant_values=np.nan)
    row_index, col_index = np.indices((rows, cols))

    for dr, dc in NEIGHBOUR_OFFSETS:
        neighbour = padded[1 + dr:1 + dr + rows, 1 + dc:1 + dc + cols]
        distance = cell_size * (math.sqrt(2.0) if dr and dc else 1.0)
        with np.errstate(invalid="ignore"):
            drop = (filled - neighbour) / distance
        better = mask & np.isfinite(drop) & (drop > best_drop)
        best_drop[better] = drop[better]
        receiver[better] = ((row_index + dr) * cols + (col_index + dc))[better]

    return receiver.ravel()


def flow_accumulation(filled: np.ndarray, receiver: np.ndarray, mask: np.ndarray) -> np.ndarray:
    """
    D8 flow accumulation in number of cells (each cell counts itself).

    Cells are visited from the highest to the lowest filled elevation, which
    guarantees every donor is processed before its receiver.
    """
    flat_mask = mask.ravel()
    accumulation = flat_mask.astype(np.float64)
    elevations = np.where(flat_mask, filled.ravel(), -np.inf)
    order = np.argsort(-elevations, kind="stable")
    order = order[flat_mask[order]]
    receiver_list = receiver.tolist()
    acc = accumulation.tolist()
    for index in order.tolist():
        target = receiver_list[index]
        if target >= 0:
            acc[target] += acc[index]
    return np.asarray(acc, dtype=np.float64).reshape(mask.shape)


def donors_csr(receiver: np.ndarray) -> Tuple[np.ndarray, np.ndarray]:
    """
    Invert the receiver array into a compressed donor list.

    Returns ``(donors, start)`` such that the donors of cell ``i`` are
    ``donors[start[i]:start[i + 1]]``.
    """
    size = receiver.size
    has_receiver = receiver >= 0
    sources = np.flatnonzero(has_receiver)
    targets = receiver[has_receiver]
    order = np.argsort(targets, kind="stable")
    donors = sources[order]
    start = np.searchsorted(targets[order], np.arange(size + 1))
    return donors, start


def step_lengths(receiver: np.ndarray, cols: int, cell_size: float) -> np.ndarray:
    """Distance from each cell to its receiver (0 for outlets)."""
    index = np.arange(receiver.size)
    has = receiver >= 0
    lengths = np.zeros(receiver.size, dtype=np.float64)
    dr = receiver[has] // cols - index[has] // cols
    dc = receiver[has] % cols - index[has] % cols
    lengths[has] = cell_size * np.where((dr != 0) & (dc != 0), math.sqrt(2.0), 1.0)
    return lengths


# ---------------------------------------------------------------------------
# Trunk stream
# ---------------------------------------------------------------------------

def trunk_stream(receiver: np.ndarray, accumulation: np.ndarray,
                 outlet: Optional[int] = None) -> np.ndarray:
    """
    Flat indices of the trunk stream, ordered SOURCE -> OUTLET.

    ``outlet`` defaults to the cell with the largest accumulation. At each
    junction the path continues into the donor with the largest accumulation
    (Hack's main stream), up to a cell without donors, which lies on the
    drainage divide. There is no length limit (GSV-Studio stopped
    after 1000 cells, truncating long rivers).
    """
    flat_acc = accumulation.ravel()
    if outlet is None:
        outlet = int(np.argmax(flat_acc))
    donors, start = donors_csr(receiver)
    path = [outlet]
    current = outlet
    while True:
        candidates = donors[start[current]:start[current + 1]]
        if candidates.size == 0:
            break
        current = int(candidates[np.argmax(flat_acc[candidates])])
        path.append(current)
    return np.asarray(path[::-1], dtype=np.int64)


# ---------------------------------------------------------------------------
# Strahler network from the D8 tree
# ---------------------------------------------------------------------------

def strahler_network(receiver: np.ndarray, accumulation: np.ndarray,
                     stream_mask: np.ndarray, cols: int,
                     cell_size: float) -> Tuple[NetworkSummary, np.ndarray]:
    """
    Strahler orders and Horton stream statistics of a raster channel network.

    Parameters
    ----------
    receiver    : D8 receivers (flat), see ``d8_receivers``.
    accumulation: accumulation in cells (2-D).
    stream_mask : 2-D boolean, True for channel cells.
    cols        : number of grid columns.
    cell_size   : cell size in metres.

    Returns
    -------
    (summary, order_grid) where order_grid is a flat int array of Strahler
    orders (0 off-network).
    """
    flat_stream = stream_mask.ravel()
    flat_acc = accumulation.ravel()
    order = np.zeros(receiver.size, dtype=np.int64)
    lengths = step_lengths(receiver, cols, cell_size)

    stream_cells = np.flatnonzero(flat_stream)
    if stream_cells.size == 0:
        empty = NetworkSummary({}, {}, {}, 0.0, 0, 0, "dem")
        return empty, order

    # Donor lists restricted to channel cells, processed upstream -> downstream
    # (increasing accumulation is a topological order of the D8 tree).
    # A channel cell only passes flow to a receiver that is itself a channel
    # cell; anything else ends the network (basin outlet).
    stream_receiver = np.where(flat_stream & (receiver >= 0), receiver, -1)
    linked = stream_receiver >= 0
    stream_receiver[linked] = np.where(flat_stream[stream_receiver[linked]],
                                       stream_receiver[linked], -1)
    donors, start = donors_csr(stream_receiver)
    processing = stream_cells[np.argsort(flat_acc[stream_cells], kind="stable")]

    for cell in processing.tolist():
        upstream = donors[start[cell]:start[cell + 1]]
        if upstream.size == 0:
            order[cell] = 1
            continue
        upstream_orders = order[upstream]
        highest = int(upstream_orders.max())
        order[cell] = highest + 1 if int((upstream_orders == highest).sum()) >= 2 else highest

    # Horton streams: a stream of order u starts at a channel cell none of
    # whose channel donors has order u, then runs downstream while the order
    # stays u.
    counts: Dict[int, int] = defaultdict(int)
    stream_lengths: Dict[int, List[float]] = defaultdict(list)
    stream_areas: Dict[int, List[float]] = defaultdict(list)
    cell_area = cell_size * cell_size
    link_count = 0

    for cell in stream_cells.tolist():
        upstream = donors[start[cell]:start[cell + 1]]
        if upstream.size != 1:
            link_count += 1  # channel head or junction starts a new link
        u = int(order[cell])
        if upstream.size and np.any(order[upstream] == u):
            continue  # not the first cell of its stream
        counts[u] += 1
        length = 0.0
        current = cell
        while True:
            length += lengths[current]
            nxt = int(stream_receiver[current])
            if nxt < 0 or order[nxt] != u:
                break
            current = nxt
        stream_lengths[u].append(length)
        stream_areas[u].append(float(flat_acc[current]) * cell_area)

    summary = NetworkSummary(
        stream_counts=dict(counts),
        stream_lengths_m={k: list(v) for k, v in stream_lengths.items()},
        basin_areas_m2={k: list(v) for k, v in stream_areas.items()},
        total_length_m=float(lengths[stream_cells].sum()),
        link_count=int(link_count),
        max_order=int(order.max()),
        source="dem",
    )
    return summary, order


# ---------------------------------------------------------------------------
# Strahler network from vector segments
# ---------------------------------------------------------------------------

def network_from_segments(segments: Sequence[dict], snap_m: float) -> NetworkSummary:
    """
    Horton statistics of a vector stream network.

    Parameters
    ----------
    segments : list of dicts with keys
               ``start`` (x, y), ``end`` (x, y): segment ends, already
               oriented upstream -> downstream by the caller (using the DEM);
               ``length`` (m);
               ``order`` (int or None): user Strahler order, if any;
               ``outlet_area`` (m², optional): drainage area at the
               downstream end, used for Schumm's area ratio.
    snap_m   : tolerance used to connect segment ends (vertices closer than
               this are the same node).

    When at least one segment lacks an order, Strahler orders are recomputed
    for the whole network from its topology so that the result never mixes
    two ordering conventions.
    """
    if not segments:
        return NetworkSummary({}, {}, {}, 0.0, 0, 0, "vector")

    def node_key(point: Tuple[float, float]) -> Tuple[int, int]:
        size = max(snap_m, 1e-6)
        return (int(round(point[0] / size)), int(round(point[1] / size)))

    n = len(segments)
    up_node = [node_key(s["start"]) for s in segments]
    down_node = [node_key(s["end"]) for s in segments]
    ending_at: Dict[Tuple[int, int], List[int]] = defaultdict(list)
    for i, key in enumerate(down_node):
        ending_at[key].append(i)
    starting_at: Dict[Tuple[int, int], List[int]] = defaultdict(list)
    for i, key in enumerate(up_node):
        starting_at[key].append(i)

    upstream = [ending_at.get(up_node[i], []) for i in range(n)]
    upstream = [[j for j in ups if j != i] for i, ups in enumerate(upstream)]
    downstream = [next((j for j in starting_at.get(down_node[i], []) if j != i), None)
                  for i in range(n)]

    user_orders = [s.get("order") for s in segments]
    if all(isinstance(o, (int, np.integer)) and o > 0 for o in user_orders):
        orders = [int(o) for o in user_orders]
    else:
        orders = [0] * n
        state = [0] * n  # 0 = new, 1 = visiting, 2 = done (cycle-safe DFS)

        def solve(i: int) -> int:
            if state[i] == 2:
                return orders[i]
            if state[i] == 1:
                return 1  # cycle in a malformed network: treat as headwater
            state[i] = 1
            ups = [solve(j) for j in upstream[i]]
            if not ups:
                value = 1
            else:
                top = max(ups)
                value = top + 1 if ups.count(top) >= 2 else top
            orders[i] = value
            state[i] = 2
            return value

        for i in range(n):
            solve(i)

    counts: Dict[int, int] = defaultdict(int)
    lengths: Dict[int, List[float]] = defaultdict(list)
    areas: Dict[int, List[float]] = defaultdict(list)
    for i in range(n):
        u = orders[i]
        if any(orders[j] == u for j in upstream[i]):
            continue  # continuation of a stream that started upstream
        counts[u] += 1
        total = 0.0
        current: Optional[int] = i
        last = i
        guard = 0
        while current is not None and orders[current] == u and guard <= n:
            total += float(segments[current]["length"])
            last = current
            current = downstream[current]
            guard += 1
        lengths[u].append(total)
        area = segments[last].get("outlet_area")
        if area is not None and math.isfinite(area):
            areas[u].append(float(area))

    junction_like = sum(1 for i in range(n) if len(upstream[i]) != 1)
    return NetworkSummary(
        stream_counts=dict(counts),
        stream_lengths_m={k: list(v) for k, v in lengths.items()},
        basin_areas_m2={k: list(v) for k, v in areas.items()},
        total_length_m=float(sum(s["length"] for s in segments)),
        link_count=int(max(junction_like, len(segments))),
        max_order=int(max(orders)),
        source="vector",
    )


# ---------------------------------------------------------------------------
# Terrain derivatives on a window
# ---------------------------------------------------------------------------

def horn_slope(dem: np.ndarray, cell_size: float) -> np.ndarray:
    """
    Slope in radians with Horn's (1981) third-order finite differences, the
    same estimator as GDAL/QGIS "Slope". Edge cells reuse their own value for
    missing neighbours, so the basin border keeps a defined slope.
    """
    rows, cols = dem.shape
    centre = dem.astype(np.float64)
    padded = np.pad(centre, 1, mode="edge")

    def neighbour(dr: int, dc: int) -> np.ndarray:
        # Missing (NaN) neighbours take the centre value: zero gradient on
        # that side instead of propagating NaN into the basin.
        values = padded[1 + dr:1 + dr + rows, 1 + dc:1 + dc + cols]
        return np.where(np.isfinite(values), values, centre)

    a, b, c = neighbour(-1, -1), neighbour(-1, 0), neighbour(-1, 1)
    d, f = neighbour(0, -1), neighbour(0, 1)
    g, h, i = neighbour(1, -1), neighbour(1, 0), neighbour(1, 1)
    dzdx = ((c + 2 * f + i) - (a + 2 * d + g)) / (8.0 * cell_size)
    dzdy = ((g + 2 * h + i) - (a + 2 * b + c)) / (8.0 * cell_size)
    slope = np.arctan(np.hypot(dzdx, dzdy))
    return np.where(np.isfinite(dem), slope, np.nan)
