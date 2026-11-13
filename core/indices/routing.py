"""
core/indices/routing.py

Whole-DEM hydrological fields in pure NumPy, complementing ``flow``:
stream burning, dispersive accumulation (MFD and D∞), Height Above the
Nearest Drainage (HAND) and reach steepness of a network.

Algorithms
----------
* Stream burning ("forçage du réseau"): the cells of a known river network
  are lowered by a fixed depth before filling, so that flow routing follows
  the mapped rivers instead of DEM artefacts (Hutchinson, 1989; Saunders,
  1999).
* MFD: the water of a cell is shared between all its lower neighbours in
  proportion to (slope)^p, p = 1.1 (Freeman, 1991, Computers & Geosciences
  17(3), 413–422, doi:10.1016/0098-3004(91)90048-I).
* D∞: steepest direction over the eight triangular facets around the cell,
  water shared between the two neighbours that frame it in proportion to the
  angles (Tarboton, 1997, Water Resources Research 33(2), 309–319,
  doi:10.1029/96WR03137).
* HAND: elevation of a cell above the stream cell where its D8 flow path
  first meets the drainage network (Rennó et al., 2008, Remote Sensing of
  Environment 112(9), 3469–3481, doi:10.1016/j.rse.2008.03.018), found by
  vectorised pointer jumping (log₂ of the longest path iterations).

MFD and D∞ follow GSV-Studio's ``engine/gsv-hydro/src/dispersive.rs``: they
work on the FILLED elevations; border cells and cells touching NoData are
sinks (they receive without passing water on). Accumulations are returned in
m² of drained area.

The accumulation loops visit every cell once in Python (cells sorted from
the highest to the lowest filled elevation), about 1–3 s per million cells;
the share computations themselves are vectorised.

Authors: RockMorph contributors / Tony
"""

from __future__ import annotations

import math
from typing import Callable, Optional, Tuple

import numpy as np  # type: ignore

from .flow import NEIGHBOUR_OFFSETS

FREEMAN_EXPONENT = 1.1

# Tarboton facets: (cardinal direction, diagonal direction), indices of
# NEIGHBOUR_OFFSETS (0 N, 1 NE, 2 E, 3 SE, 4 S, 5 SW, 6 W, 7 NW).
_FACETS = ((2, 1), (0, 1), (0, 7), (6, 7), (6, 5), (4, 5), (4, 3), (2, 3))

ProgressFn = Optional[Callable[[float], None]]


def burn_streams(dem: np.ndarray, stream_mask: np.ndarray, depth_m: float) -> np.ndarray:
    """Copy of ``dem`` lowered by ``depth_m`` on the cells of ``stream_mask``."""
    burned = np.array(dem, dtype=np.float64, copy=True)
    burned[stream_mask & np.isfinite(burned)] -= float(depth_m)
    return burned


def _neighbour_values(values: np.ndarray, dr: int, dc: int) -> np.ndarray:
    """``values`` of the neighbour at offset (dr, dc), NaN outside the grid."""
    rows, cols = values.shape
    padded = np.pad(values, 1, constant_values=np.nan)
    return padded[1 + dr:1 + dr + rows, 1 + dc:1 + dc + cols]


def sink_mask(filled: np.ndarray) -> np.ndarray:
    """Cells where water leaves the DEM: on the border or next to NoData."""
    valid = np.isfinite(filled)
    sinks = np.zeros(filled.shape, dtype=bool)
    for dr, dc in NEIGHBOUR_OFFSETS:
        sinks |= ~np.isfinite(_neighbour_values(filled, dr, dc))
    return sinks & valid


def _step(dr: int, dc: int, dx: float, dy: float) -> float:
    return math.hypot(dr * dy, dc * dx)


def mfd_shares(filled: np.ndarray, dx: float, dy: float,
               exponent: float = FREEMAN_EXPONENT) -> np.ndarray:
    """Fraction of each cell's water sent to each of its 8 neighbours (Freeman, 1991).

    Returns an array of shape (8, rows, cols) whose sum over the first axis
    is 1 for cells with a lower neighbour and 0 for sinks and flats.
    """
    weights = np.zeros((8,) + filled.shape, dtype=np.float64)
    for k, (dr, dc) in enumerate(NEIGHBOUR_OFFSETS):
        with np.errstate(invalid="ignore"):
            slope = (filled - _neighbour_values(filled, dr, dc)) / _step(dr, dc, dx, dy)
        weights[k] = np.where(np.isfinite(slope) & (slope > 0), np.abs(slope) ** exponent, 0.0)
    total = weights.sum(axis=0)
    with np.errstate(invalid="ignore", divide="ignore"):
        weights = np.where(total > 0, weights / total, 0.0)
    weights[:, sink_mask(filled)] = 0.0
    return weights


def dinf_shares(filled: np.ndarray, dx: float, dy: float) -> np.ndarray:
    """D∞ fractions (Tarboton, 1997) as an (8, rows, cols) array like ``mfd_shares``.

    For each facet (cardinal e1, diagonal e2): s1 = (e0 − e1)/d1,
    s2 = (e1 − e2)/d2, r = atan2(s2, s1) clamped to [0, atan(d2/d1)], slope
    √(s1² + s2²) (or the edge slope when clamped). The facet of steepest
    positive slope wins; the diagonal neighbour receives r / atan(d2/d1) of
    the water and the cardinal one the rest.
    """
    rows, cols = filled.shape
    best_slope = np.zeros(filled.shape)
    best_facet = np.full(filled.shape, -1, dtype=np.int64)
    best_fraction = np.zeros(filled.shape)
    for f, (cardinal, diagonal) in enumerate(_FACETS):
        cdr, cdc = NEIGHBOUR_OFFSETS[cardinal]
        ddr, ddc = NEIGHBOUR_OFFSETS[diagonal]
        e1 = _neighbour_values(filled, cdr, cdc)
        e2 = _neighbour_values(filled, ddr, ddc)
        d1 = _step(cdr, cdc, dx, dy)
        d2 = dx if cardinal in (0, 4) else dy
        r_max = math.atan2(d2, d1)
        with np.errstate(invalid="ignore"):
            s1 = (filled - e1) / d1
            s2 = (e1 - e2) / d2
            r = np.arctan2(s2, s1)
            slope = np.hypot(s1, s2)
            below = r < 0
            above = r > r_max
            slope = np.where(below, s1, slope)
            slope = np.where(above, (filled - e2) / math.hypot(d1, d2), slope)
            r = np.clip(r, 0.0, r_max)
            better = np.isfinite(e1) & np.isfinite(e2) & np.isfinite(slope) & (slope > best_slope)
        best_slope = np.where(better, slope, best_slope)
        best_facet = np.where(better, f, best_facet)
        best_fraction = np.where(better, r / r_max, best_fraction)

    weights = np.zeros((8, rows, cols), dtype=np.float64)
    for f, (cardinal, diagonal) in enumerate(_FACETS):
        chosen = best_facet == f
        weights[cardinal][chosen] += 1.0 - best_fraction[chosen]
        weights[diagonal][chosen] += best_fraction[chosen]
    weights[:, sink_mask(filled)] = 0.0
    return weights


def accumulate_shares(filled: np.ndarray, shares: np.ndarray, cell_area: float,
                      progress: ProgressFn = None) -> np.ndarray:
    """Drained area (m²) given the per-direction water fractions ``shares``.

    Cells are processed from the highest to the lowest filled elevation, so
    each cell has received all its water when its turn comes; it then passes
    ``acc × fraction`` to each receiving neighbour. NoData stays NaN.
    """
    rows, cols = filled.shape
    valid = np.isfinite(filled).ravel()
    n = rows * cols
    # Compressed list of (receiver, fraction) per cell.
    flat_index = np.arange(n).reshape(rows, cols)
    sources, targets, fractions = [], [], []
    for k, (dr, dc) in enumerate(NEIGHBOUR_OFFSETS):
        share = shares[k]
        has = share > 0
        if not has.any():
            continue
        src_r, src_c = np.nonzero(has)
        sources.append(flat_index[src_r, src_c])
        targets.append((src_r + dr) * cols + (src_c + dc))
        fractions.append(share[src_r, src_c])
    if sources:
        src = np.concatenate(sources)
        tgt = np.concatenate(targets)
        frc = np.concatenate(fractions)
        order = np.argsort(src, kind="stable")
        src, tgt, frc = src[order], tgt[order], frc[order]
        start = np.searchsorted(src, np.arange(n + 1))
    else:
        tgt = np.zeros(0, dtype=np.int64)
        frc = np.zeros(0)
        start = np.zeros(n + 1, dtype=np.int64)

    elevations = np.where(valid, filled.ravel(), -np.inf)
    cells = np.argsort(-elevations, kind="stable")
    cells = cells[valid[cells]]

    acc = np.where(valid, float(cell_area), 0.0).tolist()
    start_list, tgt_list, frc_list = start.tolist(), tgt.tolist(), frc.tolist()
    total = len(cells)
    report_every = max(1, total // 20)
    for position, i in enumerate(cells.tolist()):
        a = acc[i]
        for m in range(start_list[i], start_list[i + 1]):
            acc[tgt_list[m]] += a * frc_list[m]
        if progress and position % report_every == 0:
            progress(position / total)
    out = np.asarray(acc, dtype=np.float64).reshape(rows, cols)
    out[~valid.reshape(rows, cols)] = np.nan
    return out


def mfd_accumulation(filled: np.ndarray, dx: float, dy: float,
                     exponent: float = FREEMAN_EXPONENT, progress: ProgressFn = None) -> np.ndarray:
    """Drained area (m²) by Freeman's multiple flow direction."""
    return accumulate_shares(filled, mfd_shares(filled, dx, dy, exponent), dx * dy, progress)


def dinf_accumulation(filled: np.ndarray, dx: float, dy: float, progress: ProgressFn = None) -> np.ndarray:
    """Drained area (m²) by Tarboton's D∞."""
    return accumulate_shares(filled, dinf_shares(filled, dx, dy), dx * dy, progress)


def drainage_target(receiver: np.ndarray, stream_mask: np.ndarray) -> np.ndarray:
    """Flat index of the first stream cell met downstream of every cell.

    Stream cells point to themselves. Cells whose path leaves the DEM (or
    ends in an outlet) without meeting a stream get -1. Pointer jumping:
    ``target ← target[target]`` doubles the distance covered at each pass.
    """
    n = receiver.size
    stream = stream_mask.ravel()
    index = np.arange(n)
    target = np.where(stream, index, receiver).astype(np.int64)
    # Cells without receiver that are not streams: dead ends.
    dead = (target < 0)
    target[dead] = index[dead]
    for _ in range(64):
        jumped = target[target]
        if np.array_equal(jumped, target):
            break
        target = jumped
    target[~stream[target]] = -1
    return target


def height_above_drainage(dem: np.ndarray, receiver: np.ndarray, stream_mask: np.ndarray,
                          reference: Optional[np.ndarray] = None) -> np.ndarray:
    """HAND (m): elevation above the drainage cell reached by the D8 path.

    ``dem`` gives the elevation of every cell and, unless ``reference`` is
    given, of the stream cells too (pass the unburned, unfilled DEM so that
    burning or filling does not bias HAND). Negative values (a cell in a
    pit below its outlet stream) are set to 0; cells that never reach a
    stream are NaN.
    """
    reference = dem if reference is None else reference
    target = drainage_target(receiver, stream_mask)
    flat = np.asarray(dem, dtype=np.float64).ravel()
    ref = np.asarray(reference, dtype=np.float64).ravel()
    hand = np.full(flat.shape, np.nan)
    ok = target >= 0
    hand[ok] = np.maximum(flat[ok] - ref[target[ok]], 0.0)
    hand[~np.isfinite(flat)] = np.nan
    return hand.reshape(dem.shape)


def reach_ksn(z_up: float, z_down: float, length_m: float, area_m2: float,
              theta: float, min_slope: float = 1e-5) -> Optional[float]:
    """Steepness of one reach: k_sn = S · A^θ, S = Δz / length (≥ ``min_slope``).

    Returns None when the length or the area is not positive. This is the
    reach-by-reach estimate used to colour a whole network (GSV-Studio's
    "Ksn de tout le réseau"); per-channel χ analysis stays in the Fluvial
    Toolbox.
    """
    if not (length_m > 0 and area_m2 > 0) or not (np.isfinite(z_up) and np.isfinite(z_down)):
        return None
    slope = max((z_up - z_down) / length_m, min_slope)
    return float(slope * area_m2 ** theta)
