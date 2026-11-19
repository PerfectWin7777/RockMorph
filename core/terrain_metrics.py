"""
core/terrain_metrics.py — Pure NumPy land-surface parameters

Geomorphometric derivatives computed on a plain elevation array, with no QGIS
dependency, so that they can be unit-tested and reused by any tool. Every
function takes the elevation grid ``z`` (2-D, rows from north to south, NaN for
NoData) and the cell sizes ``dx`` / ``dy`` in metres, and returns an array of
the same shape with NaN where the DEM has no data.

Contents
    gradients            first and second derivatives (Evans–Young 3 × 3)
    slope_degrees        slope gradient
    curvatures           profile, plan, mean, Gaussian, minimal, maximal
    illumination         cosine of the solar incidence angle (hillshade)
    relative_insolation  yearly relative insolation of the slopes
    swiss_hillshade      soft hillshade of a generalised DEM plus detail
    multiscale_dev       multi-scale topographic position (DEV)
    valley_bottoms       simplified multi-resolution valley bottom flatness
    iwahashi_landforms   8 terrain classes of Iwahashi & Pike (2007)
    geomorphons          10 landform elements of Jasiewicz & Stepinski (2013)

References
    Evans, I. S. (1979). Statistical characterization of altitude matrices by
        computer. Report 5, Univ. of Durham. (3 × 3 quadratic surface)
    Florinsky, I. V. (2016). Digital Terrain Analysis in Soil Science and
        Geology, 2nd ed. Academic Press. (curvature formulas, ch. 2)
    Kumar, L., Skidmore, A. K., Knowles, E. (1997). Modelling topographic
        variation in solar radiation in a GIS environment. IJGIS 11(5),
        475–497. doi:10.1080/136588197242266
    Jenny, B. (2001). An interactive approach to analytical relief shading.
        Cartographica 38(1–2), 67–75. doi:10.3138/F722-0825-3142-HW05
    Lindsay, J. B., Cockburn, J. M. H., Russell, H. A. J. (2015). An integral
        image approach to performing multi-scale topographic position
        analysis. Geomorphology 245, 51–61. doi:10.1016/j.geomorph.2015.05.025
    Gallant, J. C., Dowling, T. I. (2003). A multiresolution index of valley
        bottom flatness for mapping depositional areas. Water Resources
        Research 39(12), 1347. doi:10.1029/2002WR001426
    Iwahashi, J., Pike, R. J. (2007). Automated classifications of topography
        from DEMs by an unsupervised nested-means algorithm and a three-part
        geometric signature. Geomorphology 86(3–4), 409–440.
        doi:10.1016/j.geomorph.2006.09.012
    Jasiewicz, J., Stepinski, T. F. (2013). Geomorphons — a pattern
        recognition approach to classification and mapping of landforms.
        Geomorphology 182, 147–156. doi:10.1016/j.geomorph.2012.11.005

The insolation, Swiss shading, DEV, valley-bottom and landform algorithms
follow the GSV-Studio implementations (Core/Services/TerrainIndices.py) with
the same default constants.

Authors: RockMorph contributors / Tony
"""

from __future__ import annotations

import math
from typing import Callable, Dict, Iterable, Optional, Sequence, Tuple

import numpy as np  # type: ignore

ProgressFn = Optional[Callable[[float], None]]

# ── Default constants (GSV-Studio values) ────────────────────────────────────
INSOLATION_DECLINATIONS: Tuple[Tuple[float, float], ...] = ((23.44, 1.0), (0.0, 2.0), (-23.44, 1.0))
"""(solar declination in degrees, weight): June solstice, both equinoxes, December solstice."""
INSOLATION_HOUR_STEP = 1.5          # hours between two sun positions
INSOLATION_MIN_ELEVATION = 2.0      # degrees; a lower sun is ignored

SWISS_GENERALIZE_M = 300.0          # radius of the generalisation of the large forms
SWISS_LARGE_WEIGHT = 0.55           # share of the large forms in the final shade
SWISS_GAMMA = 0.8                   # < 1 brightens plains and gentle slopes

DEV_RADII: Tuple[int, ...] = (3, 9, 27)      # cells
DEV_MIN_SPREAD = 0.01                        # metres; avoids division by zero on flats

MRVBF_RADII: Tuple[int, ...] = (0, 3, 9, 27)  # successive generalisations (cells)
MRVBF_SLOPE = 16.0                  # degrees: flat threshold at the finest scale
MRVBF_LOW_SHARPNESS = 3.0           # steepness of the low / high transition

LANDFORM_WINDOW_M = 300.0           # window of convexity and texture
LANDFORM_CURVATURE_EPS = 0.05       # metres: Laplacian below which a cell is planar
LANDFORM_TEXTURE_EPS = 0.5          # metres: departure from the 3 × 3 mean making a pit or a peak

GEOMORPHON_LOOKUP_M = 1500.0        # search distance
GEOMORPHON_FLAT_DEG = 1.0           # flatness threshold

CURVATURE_KINDS = ("profile", "plan", "mean", "gaussian", "minimal", "maximal")

LANDFORM_CLASSES = {
    1: "Steep, convex, fine texture",
    2: "Steep, convex, coarse texture",
    3: "Steep, concave, fine texture",
    4: "Steep, concave, coarse texture",
    5: "Gentle, convex, fine texture",
    6: "Gentle, convex, coarse texture",
    7: "Gentle, concave, fine texture",
    8: "Gentle, concave, coarse texture",
}

GEOMORPHON_CLASSES = {
    1: "Flat", 2: "Peak", 3: "Ridge", 4: "Shoulder", 5: "Spur",
    6: "Slope", 7: "Hollow", 8: "Footslope", 9: "Valley", 10: "Pit",
}

# Jasiewicz & Stepinski (2013, fig. 4): landform from the number of lower
# directions (row) and higher directions (column); 0 = impossible (> 8).
_FL, _PK, _RI, _SH, _SP, _SL, _HL, _FS, _VL, _PT = range(1, 11)
_GEOMORPHON_TABLE = np.array([
    [_FL, _FL, _FL, _FS, _FS, _VL, _VL, _VL, _PT],
    [_FL, _FL, _FS, _FS, _FS, _VL, _VL, _VL, 0],
    [_FL, _SH, _SL, _SL, _HL, _HL, _VL, 0, 0],
    [_SH, _SH, _SL, _SL, _SL, _HL, 0, 0, 0],
    [_SH, _SH, _SP, _SL, _SL, 0, 0, 0, 0],
    [_RI, _RI, _SP, _SP, 0, 0, 0, 0, 0],
    [_RI, _RI, _RI, 0, 0, 0, 0, 0, 0],
    [_RI, _RI, 0, 0, 0, 0, 0, 0, 0],
    [_PK, 0, 0, 0, 0, 0, 0, 0, 0],
], dtype=np.uint8)

# The eight D8 directions (row offset, column offset), clockwise from north.
D8_OFFSETS: Tuple[Tuple[int, int], ...] = (
    (-1, 0), (-1, 1), (0, 1), (1, 1), (1, 0), (1, -1), (0, -1), (-1, -1))


# ── Helpers ──────────────────────────────────────────────────────────────────

def nodata_mask(z: np.ndarray) -> np.ndarray:
    """True where the DEM has no data."""
    return ~np.isfinite(z)


def fill_gaps(z: np.ndarray) -> np.ndarray:
    """Copy of ``z`` (float64) with NoData replaced by the mean elevation.

    Moving-window filters do not know NaN; the gaps are put back afterwards
    by every public function.
    """
    values = np.asarray(z, dtype=np.float64)
    finite = np.isfinite(values)
    fill = float(values[finite].mean()) if finite.any() else 0.0
    return np.where(finite, values, fill)


def box_mean(values: np.ndarray, radius: int) -> np.ndarray:
    """Mean over a (2·radius + 1)² square, edges repeated.

    Cumulative sums make the cost independent of the radius.
    """
    radius = max(1, int(radius))
    out = np.asarray(values, dtype=np.float64)
    width = 2 * radius + 1
    for axis in (0, 1):
        pad = [(0, 0), (0, 0)]
        pad[axis] = (radius + 1, radius)
        summed = np.cumsum(np.pad(out, pad, mode="edge"), axis=axis)
        size = out.shape[axis]
        if axis == 0:
            out = (summed[width:width + size] - summed[:size]) / width
        else:
            out = (summed[:, width:width + size] - summed[:, :size]) / width
    return out


def _restore_nodata(result: np.ndarray, z: np.ndarray) -> np.ndarray:
    out = np.asarray(result, dtype=np.float32)
    out[nodata_mask(z)] = np.nan
    return out


def shift(values: np.ndarray, dr: int, dc: int, fill: float = np.nan) -> np.ndarray:
    """``out[i, j] = values[i + dr, j + dc]`` (the neighbour at offset dr, dc), ``fill`` outside."""
    rows, cols = values.shape
    out = np.full(values.shape, fill, dtype=values.dtype)
    r0, r1 = max(0, -dr), min(rows, rows - dr)
    c0, c1 = max(0, -dc), min(cols, cols - dc)
    if r1 > r0 and c1 > c0:
        out[r0:r1, c0:c1] = values[r0 + dr:r1 + dr, c0 + dc:c1 + dc]
    return out


# ── Derivatives ──────────────────────────────────────────────────────────────

def gradients(z: np.ndarray, dx: float, dy: float, z_factor: float = 1.0):
    """Partial derivatives of the 3 × 3 quadratic surface (Evans, 1979).

    x points east and y points north (rows run southwards). Returns
    ``(p, q, r, s, t)`` = (∂z/∂x, ∂z/∂y, ∂²z/∂x², ∂²z/∂x∂y, ∂²z/∂y²) as float64
    arrays; edges use repeated values.
    """
    a = np.pad(fill_gaps(z) * z_factor, 1, mode="edge")
    z1, z2, z3 = a[:-2, :-2], a[:-2, 1:-1], a[:-2, 2:]     # north row (west → east)
    z4, z5, z6 = a[1:-1, :-2], a[1:-1, 1:-1], a[1:-1, 2:]
    z7, z8, z9 = a[2:, :-2], a[2:, 1:-1], a[2:, 2:]        # south row
    p = ((z3 + z6 + z9) - (z1 + z4 + z7)) / (6.0 * dx)
    q = ((z1 + z2 + z3) - (z7 + z8 + z9)) / (6.0 * dy)
    r = ((z1 + z3 + z4 + z6 + z7 + z9) - 2.0 * (z2 + z5 + z8)) / (3.0 * dx * dx)
    t = ((z1 + z2 + z3 + z7 + z8 + z9) - 2.0 * (z4 + z5 + z6)) / (3.0 * dy * dy)
    s = (z3 + z7 - z1 - z9) / (4.0 * dx * dy)
    return p, q, r, s, t


def slope_degrees(z: np.ndarray, dx: float, dy: float, z_factor: float = 1.0) -> np.ndarray:
    """Slope gradient in degrees."""
    p, q, _r, _s, _t = gradients(z, dx, dy, z_factor)
    return _restore_nodata(np.degrees(np.arctan(np.hypot(p, q))), z)


def curvatures(z: np.ndarray, dx: float, dy: float,
               kinds: Iterable[str] = CURVATURE_KINDS) -> Dict[str, np.ndarray]:
    """Surface curvatures in 1/m, sign convention of Florinsky (2016).

    Positive values are convex (crests, spurs, divergent or decelerating
    flow), negative values concave (valleys, hollows).

    profile   kv  curvature of the slope line (flow acceleration)
    plan      kh  horizontal curvature (flow convergence / divergence)
    mean      H   average of the two principal curvatures
    gaussian  K   product of the principal curvatures (> 0: dome or basin)
    minimal / maximal   principal curvatures (kmin ≤ kmax)

    On perfectly flat cells kv and kh are undefined and set to 0.
    """
    p, q, r, s, t = gradients(z, dx, dy)
    p2, q2 = p * p, q * q
    g = p2 + q2
    w = 1.0 + g
    out: Dict[str, np.ndarray] = {}
    wanted = set(kinds)
    with np.errstate(invalid="ignore", divide="ignore"):
        safe_g = np.where(g > 1e-12, g, np.nan)
        if "profile" in wanted:
            kv = -(p2 * r + 2.0 * p * q * s + q2 * t) / (safe_g * np.sqrt(w ** 3))
            out["profile"] = np.nan_to_num(kv, nan=0.0)
        if "plan" in wanted:
            kh = -(q2 * r - 2.0 * p * q * s + p2 * t) / (safe_g * np.sqrt(w))
            out["plan"] = np.nan_to_num(kh, nan=0.0)
        mean = -((1.0 + q2) * r - 2.0 * p * q * s + (1.0 + p2) * t) / (2.0 * np.sqrt(w ** 3))
        gauss = (r * t - s * s) / (w * w)
        root = np.sqrt(np.maximum(mean * mean - gauss, 0.0))
        if "mean" in wanted:
            out["mean"] = mean
        if "gaussian" in wanted:
            out["gaussian"] = gauss
        if "minimal" in wanted:
            out["minimal"] = mean - root
        if "maximal" in wanted:
            out["maximal"] = mean + root
    return {key: _restore_nodata(value, z) for key, value in out.items()}


def illumination(p: np.ndarray, q: np.ndarray, azimuth_deg: float, altitude_deg: float) -> np.ndarray:
    """Cosine of the incidence angle of a sun at (azimuth, altitude), ≥ 0.

    The unit normal of the surface z(x, y) is (−p, −q, 1)/√(1+p²+q²) in
    (east, north, up); the sun direction is (sin A cos h, cos A cos h, sin h)
    with A clockwise from north. Self-shadowed slopes return 0; cast shadows
    are not traced.
    """
    az, alt = math.radians(azimuth_deg), math.radians(altitude_deg)
    sx, sy, sz = math.sin(az) * math.cos(alt), math.cos(az) * math.cos(alt), math.sin(alt)
    cos_i = (-p * sx - q * sy + sz) / np.sqrt(1.0 + p * p + q * q)
    return np.clip(cos_i, 0.0, 1.0)


def sun_positions(latitude_deg: float,
                  declinations: Sequence[Tuple[float, float]] = INSOLATION_DECLINATIONS,
                  hour_step: float = INSOLATION_HOUR_STEP,
                  min_elevation_deg: float = INSOLATION_MIN_ELEVATION):
    """Sun (azimuth°, elevation°, weight) over the day at the chosen dates.

    Hour angle h = 15° per hour from solar noon; azimuth is clockwise from
    north. Positions lower than ``min_elevation_deg`` are skipped.
    """
    lat = math.radians(latitude_deg)
    positions = []
    for declination, weight in declinations:
        dec = math.radians(declination)
        for hour in np.arange(-12.0, 12.0, hour_step):
            h = math.radians(15.0 * hour)
            sin_e = math.sin(lat) * math.sin(dec) + math.cos(lat) * math.cos(dec) * math.cos(h)
            if sin_e <= math.sin(math.radians(min_elevation_deg)):
                continue
            azimuth = (math.degrees(math.atan2(
                math.sin(h), math.cos(h) * math.sin(lat) - math.tan(dec) * math.cos(lat))) + 180.0) % 360.0
            positions.append((azimuth, math.degrees(math.asin(sin_e)), weight))
    return positions


def relative_insolation(z: np.ndarray, dx: float, dy: float, latitude_deg: float,
                        z_factor: float = 1.0, progress: ProgressFn = None) -> np.ndarray:
    """Relative yearly insolation (1 = horizontal ground).

    Sum over the sun positions of the solstices and equinoxes of the cosine
    of the incidence angle, divided by the same sum on flat ground (Kumar et
    al., 1997). > 1: sunny slopes, < 1: shaded slopes. Cast shadows by the
    surrounding relief are not counted.
    """
    p, q, _r, _s, _t = gradients(z, dx, dy, z_factor)
    positions = sun_positions(latitude_deg)
    total = np.zeros(z.shape)
    flat = 0.0
    for index, (azimuth, elevation, weight) in enumerate(positions):
        total += weight * illumination(p, q, azimuth, elevation)
        flat += weight * math.sin(math.radians(elevation))
        if progress:
            progress((index + 1) / len(positions))
    return _restore_nodata(total / flat if flat > 0 else total, z)


def multidirectional_hillshade(p: np.ndarray, q: np.ndarray, altitude_deg: float = 45.0) -> np.ndarray:
    """Mean of four hillshades lit from 225°, 270°, 315° and 360° (0–1)."""
    return sum(illumination(p, q, az, altitude_deg) for az in (225.0, 270.0, 315.0, 360.0)) / 4.0


def swiss_hillshade(z: np.ndarray, dx: float, dy: float, generalize_m: float = SWISS_GENERALIZE_M,
                    large_weight: float = SWISS_LARGE_WEIGHT, gamma: float = SWISS_GAMMA,
                    z_factor: float = 1.0) -> np.ndarray:
    """Soft "Swiss" relief shading in [0, 1] (Jenny, 2001).

    The large forms come from a north-west light on a DEM generalised over
    ``generalize_m`` (two box passes = tent filter), the detail from a
    multidirectional shading of the full DEM; the mix is brightened by
    ``gamma`` < 1 so that plains stay light, as on Imhof's maps.
    """
    cell = max(abs(dx), abs(dy))
    radius = max(1, int(round(generalize_m / cell)))
    filled = fill_gaps(z)
    general = box_mean(box_mean(filled, radius), radius)
    pg, qg, *_ = gradients(general, dx, dy, z_factor)
    large = illumination(pg, qg, 315.0, 45.0)
    p, q, *_ = gradients(filled, dx, dy, z_factor)
    detail = multidirectional_hillshade(p, q, 45.0)
    shade = np.clip(large_weight * large + (1.0 - large_weight) * detail, 0.0, 1.0) ** gamma
    return _restore_nodata(shade, z)


def deviation(filled: np.ndarray, radius: int) -> np.ndarray:
    """Deviation from mean elevation (DEV) on a (2·radius + 1)² window."""
    mean = box_mean(filled, radius)
    spread = np.sqrt(np.maximum(box_mean(filled * filled, radius) - mean * mean, 0.0))
    return (filled - mean) / np.maximum(spread, DEV_MIN_SPREAD)


def multiscale_dev(z: np.ndarray, radii: Sequence[int] = DEV_RADII) -> np.ndarray:
    """Largest |DEV| over several scales (Lindsay et al., 2015).

    > 0: crests and highs, < 0: valleys and hollows; the value is in
    standard deviations of the local elevation.
    """
    filled = fill_gaps(z)
    best = np.zeros(filled.shape)
    for radius in radii:
        dev = deviation(filled, int(radius))
        best = np.where(np.abs(dev) > np.abs(best), dev, best)
    return _restore_nodata(best, z)


def valley_bottoms(z: np.ndarray, dx: float, dy: float, radii: Sequence[int] = MRVBF_RADII,
                   slope_threshold: float = MRVBF_SLOPE,
                   sharpness: float = MRVBF_LOW_SHARPNESS) -> np.ndarray:
    """Simplified multi-resolution valley bottom flatness (Gallant & Dowling, 2003).

    At each scale k a cell is flat (slope of the generalised DEM below
    ``slope_threshold / 2^k``) and low (below the mean of its surroundings).
    The value keeps the coarsest scale where it still is: 0 on slopes and
    summits, larger values for wider flat valley floors.
    """
    filled = fill_gaps(z)
    out = np.zeros(filled.shape)
    for k, radius in enumerate(radii):
        smooth = box_mean(filled, radius) if radius > 0 else filled
        p, q, *_ = gradients(smooth, dx, dy)
        slope = np.degrees(np.arctan(np.hypot(p, q)))
        threshold = slope_threshold / 2 ** k
        flatness = 1.0 / (1.0 + (slope / threshold) ** 4)
        with np.errstate(over="ignore"):
            lowness = 1.0 / (1.0 + np.exp(sharpness * deviation(filled, max(radius, 1) * 3)))
        bottom = flatness * lowness
        out = np.where(bottom > 0.5, k + bottom, out)
    return _restore_nodata(out, z)


def iwahashi_landforms(z: np.ndarray, dx: float, dy: float, window_m: float = LANDFORM_WINDOW_M,
                       curvature_eps: float = LANDFORM_CURVATURE_EPS,
                       texture_eps: float = LANDFORM_TEXTURE_EPS) -> np.ndarray:
    """Terrain class 1–8 (``LANDFORM_CLASSES``) after Iwahashi & Pike (2007).

    Slope gradient, local convexity and surface texture are each compared
    with their mean over the DEM (first level of the nested means): steep or
    gentle, convex or concave, fine or coarse.
    """
    cell = max(abs(dx), abs(dy))
    filled = fill_gaps(z)
    window = max(1, int(round(window_m / cell)))
    p, q, *_ = gradients(filled, dx, dy)
    slope = np.degrees(np.arctan(np.hypot(p, q)))
    local_mean = box_mean(filled, 1)
    laplacian = local_mean - filled
    convexity = box_mean((laplacian < -curvature_eps).astype(float), window)
    texture = box_mean((np.abs(filled - local_mean) > texture_eps).astype(float), window)
    valid = ~nodata_mask(z)
    steep = slope > float(slope[valid].mean()) if valid.any() else slope > 0
    convex = convexity > float(convexity[valid].mean()) if valid.any() else convexity > 0
    fine = texture > float(texture[valid].mean()) if valid.any() else texture > 0
    index = (~steep).astype(int) * 4 + (~convex).astype(int) * 2 + (~fine).astype(int) + 1
    return _restore_nodata(index.astype(float), z)


def geomorphons(z: np.ndarray, dx: float, dy: float, lookup_cells: int,
                flat_deg: float = GEOMORPHON_FLAT_DEG, progress: ProgressFn = None) -> np.ndarray:
    """Geomorphon class 1–10 (``GEOMORPHON_CLASSES``) of every cell.

    In each D8 direction, up to ``lookup_cells`` cells away, the zenith-side
    (highest) and nadir-side (lowest) elevation angles are found. The
    direction is "higher" when their sum exceeds ``flat_deg``, "lower" when
    it is below −``flat_deg``, flat otherwise; the counts of higher and
    lower directions give the landform (Jasiewicz & Stepinski, 2013, fig. 4).
    Elevation angles are compared through their tangents, which keeps the
    order and avoids an arctangent per step.
    """
    values = np.asarray(z, dtype=np.float64)
    lookup_cells = max(1, int(lookup_cells))
    flat = math.radians(flat_deg)
    higher = np.zeros(values.shape, dtype=np.uint8)
    lower = np.zeros(values.shape, dtype=np.uint8)
    for index, (dr, dc) in enumerate(D8_OFFSETS):
        step = math.hypot(dr * dy, dc * dx)
        high = np.full(values.shape, -np.inf)
        low = np.full(values.shape, np.inf)
        for distance in range(1, lookup_cells + 1):
            ahead = shift(values, dr * distance, dc * distance)
            tangent = (ahead - values) / (distance * step)
            high = np.fmax(high, tangent)
            low = np.fmin(low, tangent)
        seen = np.isfinite(high)
        balance = np.where(seen, np.arctan(np.where(seen, high, 0.0)) + np.arctan(np.where(seen, low, 0.0)), 0.0)
        higher += (balance > flat).astype(np.uint8)
        lower += (balance < -flat).astype(np.uint8)
        if progress:
            progress((index + 1) / len(D8_OFFSETS))
    classes = _GEOMORPHON_TABLE[lower, higher].astype(np.float32)
    return _restore_nodata(classes, z)
