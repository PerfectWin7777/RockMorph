"""
core/indices/profiles.py

One-dimensional analyses of channel and valley profiles.

These functions turn arrays extracted by the context builder into the
quantities behind the profile-based indices:

* ``sl_reaches``          Hack's (1973) stream length-gradient index, reach by reach;
* ``ksn_reaches``         normalised channel steepness (Wobus et al., 2006);
* ``chi_coordinate``      chi transform (Perron & Royden, 2013);
* ``analyze_cross_profile`` valley floor width and divide heights for the
  Vf ratio (Bull & McFadden, 1977), ported from GSV-Studio;
* ``bilinear_sample``     raster sampling at arbitrary points.

All profiles follow the context convention: ordered SOURCE -> OUTLET, with
``distance`` measured from the source.

Authors: RockMorph contributors / Tony
"""

from __future__ import annotations

import math
from typing import Dict, List, Mapping, Optional, Tuple

import numpy as np  # type: ignore

try:
    from scipy.ndimage import gaussian_filter1d  # type: ignore
except ImportError:  # SciPy ships with QGIS; keep a pure-NumPy fallback anyway.
    gaussian_filter1d = None


# ---------------------------------------------------------------------------
# Raster sampling
# ---------------------------------------------------------------------------

def bilinear_sample(grid: np.ndarray, geotransform: Tuple[float, ...],
                    xs: np.ndarray, ys: np.ndarray) -> np.ndarray:
    """
    Bilinear interpolation of ``grid`` at map coordinates (xs, ys).

    The GDAL geotransform maps pixel *corners*; cell values belong to pixel
    *centres*, hence the 0.5 shift before interpolating. (GSV-Studio
    interpolated from corners, a half-pixel offset.) Points whose four
    surrounding centres are not all valid return NaN.
    """
    x0, dx, _, y0, _, dy = geotransform
    col = (np.asarray(xs, dtype=np.float64) - x0) / dx - 0.5
    row = (np.asarray(ys, dtype=np.float64) - y0) / dy - 0.5
    rows, cols = grid.shape
    c0 = np.floor(col).astype(np.int64)
    r0 = np.floor(row).astype(np.int64)
    inside = (c0 >= 0) & (r0 >= 0) & (c0 + 1 < cols) & (r0 + 1 < rows)
    result = np.full(col.shape, np.nan)
    if not np.any(inside):
        return result
    c0i, r0i = c0[inside], r0[inside]
    fx, fy = col[inside] - c0i, row[inside] - r0i
    z00 = grid[r0i, c0i]
    z01 = grid[r0i, c0i + 1]
    z10 = grid[r0i + 1, c0i]
    z11 = grid[r0i + 1, c0i + 1]
    value = (z00 * (1 - fx) * (1 - fy) + z01 * fx * (1 - fy)
             + z10 * (1 - fx) * fy + z11 * fx * fy)
    result[inside] = value  # NaN corners propagate to NaN automatically
    return result


def resample_profile(distance: np.ndarray, values: np.ndarray,
                     step: float) -> Tuple[np.ndarray, np.ndarray]:
    """Linear resampling of a profile at a regular ``step`` (metres)."""
    if distance.size < 2 or step <= 0:
        return distance.copy(), values.copy()
    regular = np.arange(0.0, float(distance[-1]) + 1e-9, step)
    if regular[-1] < distance[-1]:
        regular = np.append(regular, distance[-1])
    return regular, np.interp(regular, distance, values)


# ---------------------------------------------------------------------------
# Stream length-gradient index (Hack, 1973)
# ---------------------------------------------------------------------------

def sl_reaches(distance: np.ndarray, elevation: np.ndarray,
               reach_length: float) -> List[Dict[str, float]]:
    """
    SL index of consecutive reaches of length ``reach_length``.

    For a reach between distances d1 and d2 from the source (d2 > d1),
    ``SL = (H(d1) - H(d2)) / (d2 - d1) * L`` where L is the distance from the
    source to the reach midpoint (Hack, 1973). Elevations at reach ends are
    linearly interpolated on the profile.

    Reaches that rise downstream (DEM artefacts) are reported with SL = 0
    rather than a negative value, following GSV-Studio: a negative stream
    power has no physical meaning and would bias basin averages downwards.

    Why reaches and not pixels: RockMorph's fluvial tool sampled the profile
    every 0.3 pixel, so most consecutive points shared a pixel and the local
    gradient alternated between 0 and spikes. Hack defined SL over finite
    reaches, which is what this function does.
    """
    reaches: List[Dict[str, float]] = []
    if distance.size < 2 or reach_length <= 0:
        return reaches
    total = float(distance[-1])
    start = 0.0
    while start < total - 1e-9:
        end = min(start + reach_length, total)
        length = end - start
        if length < reach_length * 0.25 and reaches:
            break  # a last fragment shorter than a quarter reach is noise
        z_start = float(np.interp(start, distance, elevation))
        z_end = float(np.interp(end, distance, elevation))
        drop = max(z_start - z_end, 0.0)
        midpoint = start + length / 2.0
        gradient = drop / length if length > 0 else 0.0
        reaches.append({
            "start_m": start, "end_m": end, "mid_m": midpoint,
            "gradient": gradient, "sl": gradient * midpoint,
        })
        start = end
    return reaches


# ---------------------------------------------------------------------------
# Channel steepness and chi
# ---------------------------------------------------------------------------

def ksn_reaches(distance: np.ndarray, elevation: np.ndarray, area: np.ndarray,
                theta: float, reach_length: float,
                reference_area: float = 1.0) -> List[Dict[str, float]]:
    """
    Normalised steepness ``ksn = S * (A / A0)^theta`` on consecutive reaches.

    S is the mean gradient of the reach, A the geometric mean of the drainage
    areas at the reach ends (the log-midpoint used in slope-area analysis).
    Flat or rising reaches are *excluded* (GSV-Studio forced them to
    S = 1e-5, which biases ksn statistics towards zero).
    """
    out: List[Dict[str, float]] = []
    if distance.size < 2 or reach_length <= 0:
        return out
    total = float(distance[-1])
    start = 0.0
    while start < total - 1e-9:
        end = min(start + reach_length, total)
        length = end - start
        if length < reach_length * 0.25 and out:
            break
        z1 = float(np.interp(start, distance, elevation))
        z2 = float(np.interp(end, distance, elevation))
        a1 = float(np.interp(start, distance, area))
        a2 = float(np.interp(end, distance, area))
        slope = (z1 - z2) / length if length > 0 else 0.0
        if slope > 0 and a1 > 0 and a2 > 0:
            mean_area = math.sqrt(a1 * a2)
            out.append({
                "mid_m": start + length / 2.0, "slope": slope,
                "area_m2": mean_area,
                "ksn": slope * (mean_area / reference_area) ** theta,
            })
        start = end
    return out


def chi_coordinate(distance: np.ndarray, area: np.ndarray, theta: float,
                   reference_area: float = 1.0) -> np.ndarray:
    """
    Chi transform ``chi(x) = ∫ (A0 / A)^theta dx`` integrated from the outlet.

    Returned in the SAME order as the inputs (source -> outlet), so chi[i]
    pairs with elevation[i]; chi is 0 at the outlet and maximal at the source.
    (RockMorph's fluvial engine returned chi reversed relative to elevation;
    this function is the corrected form.)
    """
    if distance.size < 2:
        return np.zeros_like(distance, dtype=np.float64)
    reversed_distance = distance[-1] - distance[::-1]       # 0 at the outlet
    integrand = (reference_area / np.maximum(area[::-1], 1e-12)) ** theta
    chi_from_outlet = np.concatenate((
        [0.0],
        np.cumsum(0.5 * (integrand[1:] + integrand[:-1]) * np.diff(reversed_distance)),
    ))
    return chi_from_outlet[::-1]


# ---------------------------------------------------------------------------
# Valley cross-profile analysis (Vf)
# ---------------------------------------------------------------------------

def _smooth(values: np.ndarray, sigma: float) -> np.ndarray:
    """Gaussian smoothing (SciPy when present, NumPy convolution otherwise)."""
    if sigma <= 0.1 or values.size < max(5, int(3 * sigma)):
        return values.astype(np.float64)
    if gaussian_filter1d is not None:
        return gaussian_filter1d(values.astype(np.float64), sigma=sigma, mode="mirror")
    radius = int(math.ceil(3 * sigma))
    kernel = np.exp(-0.5 * (np.arange(-radius, radius + 1) / sigma) ** 2)
    kernel /= kernel.sum()
    padded = np.pad(values.astype(np.float64), radius, mode="reflect")
    return np.convolve(padded, kernel, mode="valid")


def _floor_limit_flat_slope(slope: np.ndarray, elevation: np.ndarray, start: int,
                            direction: int, channel_z: float,
                            params: Mapping[str, float]) -> int:
    """
    Valley-floor limit on one side: walk away from the channel while the
    terrain is flatter than ``max_flat_slope_deg``. The floor ends once
    ``consistency_points`` consecutive steeper points are met AND the terrain
    stands at least ``min_height_diff_m`` above the channel (GSV-Studio
    "maximum slope on flat floor" method).
    """
    max_flat = math.tan(math.radians(params["max_flat_slope_deg"]))
    need = max(1, int(params["consistency_points"]))
    min_rise = params["min_height_diff_m"]
    index = start
    last_flat = start
    steep_run = 0
    while 0 < index < slope.size - 1:
        nxt = index + direction
        if abs(slope[nxt]) <= max_flat:
            last_flat = nxt
            steep_run = 0
        else:
            steep_run += 1
            if steep_run >= need and abs(elevation[nxt] - channel_z) >= min_rise:
                return last_flat
        index = nxt
    return last_flat


def _floor_limit_footslope(slope: np.ndarray, elevation: np.ndarray,
                           distance: np.ndarray, start: int, direction: int,
                           params: Mapping[str, float]) -> int:
    """
    Valley-floor limit on one side by scoring candidate foot-of-slope points
    (GSV-Studio "foot of slope search"): +1 for a run of steep points above
    an adaptive threshold, +1 for a change of curvature sign, +0.5 for a
    height contrast with the channel. The first point scoring 2 wins.
    """
    basal = math.tan(math.radians(params["basal_slope_threshold_deg"]))
    need = max(1, int(params["consistency_points"]))
    min_rise = params["min_height_diff_m"]
    spacing = float(np.mean(np.diff(distance))) if distance.size > 1 else 1.0
    curvature = np.gradient(slope, spacing)
    magnitude = np.abs(slope)
    threshold = max(basal, float(magnitude.mean() + 0.5 * magnitude.std()))
    channel_z = elevation[start]
    best, best_score, steep_run = start, 0.0, 0
    for step in range(1, max(2, slope.size // 3)):
        index = start + step * direction
        if not 0 < index < slope.size - 1:
            break
        steep_run = steep_run + 1 if magnitude[index] > threshold else 0
        score = 0.0
        if steep_run >= need:
            score += 1.0
        if curvature[index - 1] * curvature[index + 1] < 0:
            score += 1.0
        if abs(elevation[index] - channel_z) > min_rise:
            score += 0.5
        if score > best_score:
            best, best_score = index, score
            if score >= 2.0:
                break
    # Enforce a minimum half-width so a single-pixel floor is not reported.
    min_half = params["min_half_width_m"]
    if abs(distance[best] - distance[start]) < min_half:
        for step in range(1, slope.size):
            index = start + step * direction
            if not 0 <= index < slope.size:
                break
            if abs(distance[index] - distance[start]) >= min_half:
                return index
    return best


def _divide_height(elevation: np.ndarray, side: np.ndarray, channel_z: float,
                   params: Mapping[str, float], toward_outside: bool) -> float:
    """
    Elevation of the valley divide (Eld or Erd) on one side.

    "percentile": a high percentile of the side elevations (robust to single
    high pixels). "shoulder": first break to gentle slope after a minimum rise
    above the channel, else the side maximum (GSV-Studio adaptive shoulder).
    """
    if side.size == 0:
        return float("nan")
    values = elevation[side]
    if params["divide_method"] == "percentile":
        return float(np.percentile(values, params["percentile"]))
    gradient = np.gradient(elevation)
    magnitude = np.abs(gradient[side])
    threshold = max(math.tan(math.radians(params["shoulder_min_slope_deg"])),
                    float(magnitude.std()) * params["shoulder_slope_factor"])
    ordered = side if toward_outside else side[::-1]
    candidates = [i for i in ordered
                  if elevation[i] - channel_z >= params["shoulder_min_rise_m"]
                  and abs(gradient[i]) < threshold]
    if candidates:
        return float(elevation[candidates[-1]])
    return float(values.max())


def analyze_cross_profile(distance: np.ndarray, elevation: np.ndarray,
                          channel_at: float,
                          params: Mapping[str, float]) -> Optional[Dict[str, float]]:
    """
    Measure valley-floor width (Vfw), channel elevation (Esc) and the two
    divide elevations (Eld, Erd) on one cross-profile.

    ``params`` keys: smoothing_sigma, floor_method ("flat_slope" |
    "footslope"), max_flat_slope_deg, basal_slope_threshold_deg,
    consistency_points, min_height_diff_m, min_half_width_m, divide_method
    ("percentile" | "shoulder"), percentile, shoulder_min_rise_m,
    shoulder_slope_factor, shoulder_min_slope_deg; optional channel_search_m
    (re-centring radius) and min_floor_width_m (resolution floor of Vfw).

    Returns a dict with vfw_m, esc_m, eld_m, erd_m, eh_mean_m and vf, or None
    when the profile is unusable (too short, flat, or both sides lower than
    the channel). Vf = Vfw / mean(Eld - Esc, Erd - Esc), which equals Bull &
    McFadden's 2·Vfw / [(Eld - Esc) + (Erd - Esc)].
    """
    valid = np.isfinite(elevation)
    if valid.sum() < 5:
        return None
    distance = distance[valid]
    smooth = _smooth(elevation[valid], params["smoothing_sigma"])
    spacing = float(np.mean(np.diff(distance))) if distance.size > 1 else 1.0
    slope = np.gradient(smooth, spacing)
    # The D8 trunk stream runs through cell centres, so the true valley
    # bottom may lie up to one cell away from ``channel_at``: re-centre on the
    # lowest smoothed point within ``channel_search_m``.
    search = float(params.get("channel_search_m", 0.0))
    near = np.nonzero(np.abs(distance - channel_at) <= search)[0]
    if near.size:
        centre = int(near[np.argmin(smooth[near])])
    else:
        centre = int(np.argmin(np.abs(distance - channel_at)))
    esc = float(smooth[centre])

    if params["floor_method"] == "footslope":
        left = _floor_limit_footslope(slope, smooth, distance, centre, -1, params)
        right = _floor_limit_footslope(slope, smooth, distance, centre, +1, params)
    else:
        left = _floor_limit_flat_slope(slope, smooth, centre, -1, esc, params)
        right = _floor_limit_flat_slope(slope, smooth, centre, +1, esc, params)
    left, right = min(left, right), max(left, right)
    # A floor narrower than the DEM resolution cannot be measured: report the
    # resolution itself instead of 0 (a spurious "perfect V").
    vfw = max(float(distance[right] - distance[left]),
              float(params.get("min_floor_width_m", 0.0)))

    left_side = np.arange(0, left)
    right_side = np.arange(right + 1, smooth.size)
    eld = _divide_height(smooth, left_side, esc, params, toward_outside=False)
    erd = _divide_height(smooth, right_side, esc, params, toward_outside=True)

    heights = [h - esc for h in (eld, erd) if math.isfinite(h) and h - esc > 0.01]
    if not heights:
        return None
    eh_mean = float(np.mean(heights))
    return {
        "vfw_m": vfw, "esc_m": esc, "eld_m": eld, "erd_m": erd,
        "eh_mean_m": eh_mean, "vf": vfw / eh_mean,
    }
