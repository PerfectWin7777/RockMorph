"""
core/indices/formulas.py

Pure implementation of every geomorphic index offered by RockMorph.

Each public function has the signature ``f(context, params) -> float | None``
and is referenced by exactly one ``IndexSpec`` in ``catalog.py``. A function
returns ``None`` (never raises) when its inputs are missing or degenerate,
and explains why by appending to ``context.warnings`` when the reason is not
obvious from the missing requirement.

Origin
------
Formulas were ported from GSV-Studio (``expert/workers/geo_index_worker.py``)
and checked against the original publications. Where the port deviates from
GSV-Studio, the function docstring says so in a "Choice" paragraph; the
decisions journal (doc/07) lists every deviation with its justification.

Units
-----
Inputs are SI (m, m²). Outputs use the unit declared in the catalogue, which
follows morphometric practice: lengths in km, areas in km², densities in
km/km² or km⁻². Conversions happen here, nowhere else.

Authors: RockMorph contributors / Tony
"""

from __future__ import annotations

import math
from typing import Iterable, List, Mapping, Optional, Sequence

import numpy as np  # type: ignore

# np.trapz was renamed np.trapezoid in NumPy 2.0 and later removed.
_trapezoid = getattr(np, "trapezoid", None) or getattr(np, "trapz")

from .context import BasinContext
from .profiles import (
    analyze_cross_profile, chi_coordinate, ksn_reaches, sl_reaches,
)

M_PER_KM = 1000.0
M2_PER_KM2 = 1.0e6

# Statistic names shared by every index that aggregates many local values
# (reaches, sections, cross-profiles, pixels) into one basin value.
STATISTICS = ("Mean", "Median", "Maximum", "Minimum", "P75", "P90")


def aggregate(values: Iterable[float], statistic: str) -> Optional[float]:
    """
    Reduce local values to one basin value with the named ``statistic``.

    Non-finite values are dropped first. Returns None when nothing is left.
    """
    data = np.asarray([v for v in values if v is not None and math.isfinite(v)], dtype=float)
    if data.size == 0:
        return None
    if statistic == "Median":
        return float(np.median(data))
    if statistic == "Maximum":
        return float(np.max(data))
    if statistic == "Minimum":
        return float(np.min(data))
    if statistic == "P75":
        return float(np.percentile(data, 75))
    if statistic == "P90":
        return float(np.percentile(data, 90))
    return float(np.mean(data))


def _positive(value: Optional[float]) -> bool:
    """True when ``value`` is a finite number strictly greater than zero."""
    return value is not None and math.isfinite(value) and value > 0


# ===========================================================================
# Basin geometry
# ===========================================================================

def basin_area(ctx: BasinContext, params: Mapping) -> Optional[float]:
    """Planimetric basin area A, in km²."""
    return ctx.area_m2 / M2_PER_KM2 if _positive(ctx.area_m2) else None


def basin_perimeter(ctx: BasinContext, params: Mapping) -> Optional[float]:
    """Basin perimeter P, in km."""
    return ctx.perimeter_m / M_PER_KM if _positive(ctx.perimeter_m) else None


def basin_length(ctx: BasinContext, params: Mapping) -> Optional[float]:
    """Basin length Lb, in km (see ``catalog`` for how Lb is measured)."""
    return ctx.length_m / M_PER_KM if _positive(ctx.length_m) else None


def form_factor(ctx: BasinContext, params: Mapping) -> Optional[float]:
    """Form factor Rf = A / Lb² (Horton, 1932)."""
    if not (_positive(ctx.area_m2) and _positive(ctx.length_m)):
        return None
    return ctx.area_m2 / ctx.length_m ** 2


def elongation_ratio(ctx: BasinContext, params: Mapping) -> Optional[float]:
    """Elongation ratio Re = 2·sqrt(A/π) / Lb (Schumm, 1956)."""
    if not (_positive(ctx.area_m2) and _positive(ctx.length_m)):
        return None
    return 2.0 * math.sqrt(ctx.area_m2 / math.pi) / ctx.length_m


def circularity_ratio(ctx: BasinContext, params: Mapping) -> Optional[float]:
    """Circularity ratio Rc = 4πA / P² (Miller, 1953)."""
    if not (_positive(ctx.area_m2) and _positive(ctx.perimeter_m)):
        return None
    return 4.0 * math.pi * ctx.area_m2 / ctx.perimeter_m ** 2


def lemniscate_ratio(ctx: BasinContext, params: Mapping) -> Optional[float]:
    """Lemniscate ratio k = Lb² / 4A (Chorley et al., 1957)."""
    if not (_positive(ctx.area_m2) and _positive(ctx.length_m)):
        return None
    return ctx.length_m ** 2 / (4.0 * ctx.area_m2)


def compactness_coefficient(ctx: BasinContext, params: Mapping) -> Optional[float]:
    """Gravelius compactness Kc = P / (2·sqrt(πA)) (≈ 0.282·P/sqrt(A))."""
    if not (_positive(ctx.area_m2) and _positive(ctx.perimeter_m)):
        return None
    return ctx.perimeter_m / (2.0 * math.sqrt(math.pi * ctx.area_m2))


def fineness_ratio(ctx: BasinContext, params: Mapping) -> Optional[float]:
    """
    Fineness ratio Rfn.

    Choice: Melton (1957) divides the *channel* length by the perimeter;
    GSV-Studio used the basin length Lb. Both are offered through the
    ``length`` parameter, GSV-Studio's variant remaining the default.
    """
    if not _positive(ctx.perimeter_m):
        return None
    if params.get("length") == "Main channel length (Melton 1957)":
        if ctx.channel is None or not _positive(ctx.channel.length_m):
            return None
        return ctx.channel.length_m / ctx.perimeter_m
    if not _positive(ctx.length_m):
        return None
    return ctx.length_m / ctx.perimeter_m


def basin_shape_index(ctx: BasinContext, params: Mapping) -> Optional[float]:
    """Basin shape Bs = Bl / Bw (Ramírez-Herrera, 1998); >= 1 by construction."""
    if not (_positive(ctx.length_m) and _positive(ctx.width_m)):
        return None
    return max(1.0, ctx.length_m / ctx.width_m)


# ===========================================================================
# Relief and hypsometry
# ===========================================================================

def _stats(ctx: BasinContext):
    return ctx.elevation_stats()


def altitude_min(ctx, params):
    """Minimum basin elevation (m)."""
    s = _stats(ctx)
    return s[0] if s else None


def altitude_max(ctx, params):
    """Maximum basin elevation (m)."""
    s = _stats(ctx)
    return s[1] if s else None


def altitude_mean(ctx, params):
    """Mean basin elevation (m)."""
    s = _stats(ctx)
    return s[2] if s else None


def altitude_median(ctx, params):
    """Median basin elevation Hm (m), exact median of the DEM cells."""
    s = _stats(ctx)
    return s[3] if s else None


def total_relief(ctx, params):
    """Basin relief TR = Hmax - Hmin (m)."""
    s = _stats(ctx)
    return s[1] - s[0] if s else None


def topographic_dissection(ctx, params):
    """Absolute dissection TD = Hmean - Hmin (m) (Walcott & Summerfield, 2008)."""
    s = _stats(ctx)
    return s[2] - s[0] if s else None


def summit_relief(ctx, params):
    """
    Relief above the mean TRD = Hmax - Hmean (m).

    Choice: GSV-Studio's code returned TR × TD / 10⁶ while its own registry
    (and Walcott & Summerfield, 2008) define Hmax - Hmean; the definition is
    implemented. The legacy product remains available through ``formula``.
    """
    s = _stats(ctx)
    if not s:
        return None
    if params.get("formula") == "TR × TD / 10⁶ (GSV-Studio legacy)":
        return (s[1] - s[0]) * (s[2] - s[0]) / 1.0e6
    return s[1] - s[2]


def relief_ratio(ctx, params):
    """Relief ratio Rh = TR / Lb (Schumm, 1956), dimensionless."""
    s = _stats(ctx)
    if not s or not _positive(ctx.length_m):
        return None
    return (s[1] - s[0]) / ctx.length_m


def relative_relief(ctx, params):
    """Relative relief Rr = TR · 100 / P (Melton, 1957), in percent."""
    s = _stats(ctx)
    if not s or not _positive(ctx.perimeter_m):
        return None
    return (s[1] - s[0]) * 100.0 / ctx.perimeter_m


def melton_ruggedness(ctx, params):
    """Melton ruggedness number MRN = TR / sqrt(A) (Melton, 1965), both in m."""
    s = _stats(ctx)
    if not s or not _positive(ctx.area_m2):
        return None
    return (s[1] - s[0]) / math.sqrt(ctx.area_m2)


def hypsometric_integral(ctx, params):
    """
    Hypsometric integral HI.

    Variants (``formula`` parameter):
    * "Elevation-relief ratio (Pike & Wilson 1971)": (Hmean - Hmin)/(Hmax - Hmin),
      mathematically equal to the integral of the hypsometric curve; GSV-Studio
      default.
    * "Median variant": (Hmedian - Hmin)/(Hmax - Hmin). GSV-Studio offered it
      but silently ignored the choice; it is honoured here.
    * "Curve integral (Strahler 1952)": trapezoidal integral of the empirical
      hypsometric curve, as in RockMorph's hypsometry tool. Useful to verify
      the ratio on a given DEM.
    """
    s = _stats(ctx)
    if not s:
        return None
    hmin, hmax, hmean, hmedian = s
    relief = hmax - hmin
    if relief <= 1e-9:
        return None
    variant = params.get("formula", "")
    if variant.startswith("Median"):
        return (hmedian - hmin) / relief
    if variant.startswith("Curve"):
        values = np.sort(ctx.elevations)[::-1]
        relative_height = (values - hmin) / relief
        relative_area = np.linspace(0.0, 1.0, values.size)
        return float(_trapezoid(relative_height, relative_area))
    return (hmean - hmin) / relief


def uplift_index(ctx, params):
    """
    Relative tectonic uplift index U = Hm(km) + (1 - HI) (Sinha-Roy, 2002).

    HI is the elevation-relief ratio. Hm is the median elevation in km, as
    in GSV-Studio. The formula should be checked against Sinha-Roy (2002)
    before publication (see doc/07); the index is marked experimental.
    """
    s = _stats(ctx)
    if not s or s[1] - s[0] <= 1e-9:
        return None
    hi = (s[2] - s[0]) / (s[1] - s[0])
    return s[3] / M_PER_KM + (1.0 - hi)


def altitude_skewness(ctx, params):
    """Unbiased skewness of the basin elevation distribution."""
    if ctx.elevations is None or ctx.elevations.size < 3:
        return None
    x = ctx.elevations.astype(float)
    n = x.size
    sd = x.std(ddof=0)
    if sd <= 0:
        return None
    g1 = np.mean((x - x.mean()) ** 3) / sd ** 3
    return float(g1 * math.sqrt(n * (n - 1)) / (n - 2))


def altitude_kurtosis(ctx, params):
    """Unbiased Pearson kurtosis (3 for a normal law) of basin elevations."""
    if ctx.elevations is None or ctx.elevations.size < 4:
        return None
    x = ctx.elevations.astype(float)
    n = x.size
    sd2 = x.var(ddof=0)
    if sd2 <= 0:
        return None
    g2 = np.mean((x - x.mean()) ** 4) / sd2 ** 2 - 3.0
    excess = ((n + 1) * g2 + 6.0) * (n - 1) / ((n - 2) * (n - 3))
    return float(excess + 3.0)


def mean_slope_degree(ctx, params):
    """
    Mean basin slope in degrees (Horn 1981 slope of every basin cell).

    Choice: GSV-Studio returned atan(mean(tan β)); the mean of the slope
    angles is the usual definition and the default here. The legacy form is
    available through ``averaging``.
    """
    if ctx.terrain is None:
        return None
    slope = ctx.terrain.slope_rad[ctx.terrain.mask]
    slope = slope[np.isfinite(slope)]
    if slope.size == 0:
        return None
    if params.get("averaging") == "atan(mean tan β) (GSV-Studio legacy)":
        return float(np.degrees(np.arctan(np.mean(np.tan(slope)))))
    return float(np.degrees(np.mean(slope)))


def mean_slope_percent(ctx, params):
    """Mean basin gradient in percent (mean of tan β × 100)."""
    if ctx.terrain is None:
        return None
    slope = ctx.terrain.slope_rad[ctx.terrain.mask]
    slope = slope[np.isfinite(slope)]
    return float(np.mean(np.tan(slope)) * 100.0) if slope.size else None


# ===========================================================================
# Drainage network (Horton-Strahler)
# ===========================================================================

def _network(ctx):
    return ctx.network if ctx.network is not None and ctx.network.stream_count > 0 else None


def stream_order_max(ctx, params):
    """Highest Strahler order in the basin."""
    net = _network(ctx)
    return float(net.max_order) if net else None


def total_stream_length(ctx, params):
    """Total channel length ΣL, in km."""
    net = _network(ctx)
    return net.total_length_m / M_PER_KM if net else None


def stream_count(ctx, params):
    """Total number of Strahler streams ΣNu (all orders)."""
    net = _network(ctx)
    return float(net.stream_count) if net else None


def link_count(ctx, params):
    """Number of links (channel segments between junctions)."""
    net = _network(ctx)
    return float(net.link_count) if net else None


def _horton_ratio(per_order: dict, method: str, ascending: bool) -> Optional[float]:
    """
    Horton ratio from a {order: value} mapping.

    * "Mean of successive ratios": mean of v(u)/v(u+1) (descending law, Rb)
      or v(u+1)/v(u) (ascending laws, Rl and Ra) over consecutive orders
      present in the basin (GSV-Studio method).
    * "Horton law regression": 10^|slope| of log10 v against order, i.e. the
      geometric-series fit implied by Horton's laws (Strahler, 1964). Less
      sensitive to a single unusual order.
    """
    orders = sorted(k for k, v in per_order.items() if v and v > 0)
    if len(orders) < 2:
        return None
    if method == "Horton law regression":
        u = np.asarray(orders, dtype=float)
        v = np.log10([per_order[o] for o in orders])
        slope = np.polyfit(u, v, 1)[0]
        return float(10 ** abs(slope))
    ratios = []
    for low, high in zip(orders[:-1], orders[1:]):
        a, b = per_order[low], per_order[high]
        ratios.append(b / a if ascending else a / b)
    return float(np.mean(ratios)) if ratios else None


def bifurcation_ratio(ctx, params):
    """Bifurcation ratio Rb = Nu / Nu+1 (Horton, 1945; Strahler, 1964)."""
    net = _network(ctx)
    if not net:
        return None
    return _horton_ratio({k: float(v) for k, v in net.stream_counts.items()},
                         params.get("method", ""), ascending=False)


def length_ratio(ctx, params):
    """Length ratio Rl = L̄u / L̄u-1 (Horton, 1945), mean stream length per order."""
    net = _network(ctx)
    if not net:
        return None
    means = {k: float(np.mean(v)) for k, v in net.stream_lengths_m.items() if v}
    return _horton_ratio(means, params.get("method", ""), ascending=True)


def area_ratio(ctx, params):
    """
    Area ratio Ra = Āu / Āu-1 (Schumm, 1956).

    Choice: Āu is the mean *total* drainage area of the basins of order u, as
    defined by Schumm. GSV-Studio used local inter-basin areas (total minus
    tributaries), which is a different quantity.
    """
    net = _network(ctx)
    if not net:
        return None
    means = {k: float(np.mean(v)) for k, v in net.basin_areas_m2.items() if v}
    return _horton_ratio(means, params.get("method", ""), ascending=True)


def drainage_density(ctx, params):
    """Drainage density Dd = ΣL / A, in km/km² (Horton, 1932)."""
    net = _network(ctx)
    if not net or not _positive(ctx.area_m2):
        return None
    return (net.total_length_m / M_PER_KM) / (ctx.area_m2 / M2_PER_KM2)


def stream_frequency(ctx, params):
    """Stream frequency Fs = ΣNu / A, in km⁻² (Horton, 1932)."""
    net = _network(ctx)
    if not net or not _positive(ctx.area_m2):
        return None
    return net.stream_count / (ctx.area_m2 / M2_PER_KM2)


def drainage_texture(ctx, params):
    """Drainage texture Dt = ΣNu / P, in km⁻¹ (Smith, 1950; Horton, 1945)."""
    net = _network(ctx)
    if not net or not _positive(ctx.perimeter_m):
        return None
    return net.stream_count / (ctx.perimeter_m / M_PER_KM)


def drainage_intensity(ctx, params):
    """
    Drainage intensity Di.

    Choice: the commonly cited definition of Faniran (1968) is Di = Fs / Dd;
    GSV-Studio implemented Fs × Dd. GSV-Studio's form is the default (as
    requested), the ratio is available through ``formula``. See doc/07.
    """
    fs = stream_frequency(ctx, params)
    dd = drainage_density(ctx, params)
    if fs is None or not _positive(dd):
        return None
    if params.get("formula") == "Fs / Dd (Faniran 1968)":
        return fs / dd
    return fs * dd


def overland_flow_length(ctx, params):
    """Length of overland flow Lg = 1 / (2·Dd), in km (Horton, 1945)."""
    dd = drainage_density(ctx, params)
    return 0.5 / dd if _positive(dd) else None


def channel_maintenance(ctx, params):
    """Constant of channel maintenance C = 1 / Dd, in km²/km (Schumm, 1956)."""
    dd = drainage_density(ctx, params)
    return 1.0 / dd if _positive(dd) else None


def length_area_ratio(ctx, params):
    """
    Ratio of the observed main-stream length to Hack's (1957) law.

    Hack's law L = 1.4·A^0.6 holds with L in *miles* and A in *square miles*.
    Choice: the default compares the trunk-stream length with Hack's law in
    its original units (converted), which is what the law describes.
    GSV-Studio compared the *total network length* with 1.4·A^0.6 in km/km²;
    that legacy ratio is kept as an option for comparison.
    """
    if not _positive(ctx.area_m2):
        return None
    area_km2 = ctx.area_m2 / M2_PER_KM2
    if params.get("formula") == "Total network length / 1.4·A^0.6 km (GSV-Studio legacy)":
        net = _network(ctx)
        if not net:
            return None
        return (net.total_length_m / M_PER_KM) / (1.4 * area_km2 ** 0.6)
    if ctx.channel is None or not _positive(ctx.channel.length_m):
        return None
    km_per_mile, km2_per_mile2 = 1.609344, 2.589988
    predicted_km = 1.4 * (area_km2 / km2_per_mile2) ** 0.6 * km_per_mile
    return (ctx.channel.length_m / M_PER_KM) / predicted_km


def length_area_texture(ctx, params):
    """La = Lg / A, in km⁻¹ (GSV-Studio "length-area", after Schumm 1956)."""
    lg = overland_flow_length(ctx, params)
    if lg is None or not _positive(ctx.area_m2):
        return None
    return lg / (ctx.area_m2 / M2_PER_KM2)


def ruggedness_number(ctx, params):
    """Ruggedness number Hd = TR(km) × Dd (Strahler, 1958)."""
    s = _stats(ctx)
    dd = drainage_density(ctx, params)
    if not s or dd is None:
        return None
    return (s[1] - s[0]) / M_PER_KM * dd


# ===========================================================================
# Main channel
# ===========================================================================

def _channel(ctx):
    ch = ctx.channel
    return ch if ch is not None and ch.distance_m.size >= 3 and ch.length_m > 0 else None


def channel_sinuosity(ctx, params):
    """
    Channel sinuosity Lc / Lv (GSV-Studio "HSI").

    Note: Mueller's (1968) hydraulic sinuosity index needs a separate valley
    length; this is the plain sinuosity (Schumm, 1963) of the trunk stream.
    """
    ch = _channel(ctx)
    if not ch or not _positive(ch.straight_length_m):
        return None
    return ch.length_m / ch.straight_length_m


def valley_basin_ratio(ctx, params):
    """Lv / Lb: straight trunk-valley length over basin length (GSV-Studio "TSI")."""
    ch = _channel(ctx)
    if not ch or not _positive(ctx.length_m):
        return None
    return ch.straight_length_m / ctx.length_m


def mean_axial_slope(ctx, params):
    """Mean axial slope of the channel MASC = (Hmax - Hmin)_channel / Lc (m/m)."""
    ch = _channel(ctx)
    if not ch:
        return None
    return float(np.max(ch.elevation_m) - np.min(ch.elevation_m)) / ch.length_m


def hydraulic_gradient(ctx, params):
    """Hydraulic gradient HG = MASC × 100 (%), as in GSV-Studio."""
    masc = mean_axial_slope(ctx, params)
    return masc * 100.0 if masc is not None else None


def stream_length_gradient(ctx, params):
    """
    Stream length-gradient index SL (Hack, 1973), aggregated over reaches.

    Reaches of ``reach_length_m`` along the trunk stream, L measured from the
    source (drainage divide) to the reach midpoint. See
    ``profiles.sl_reaches`` for the reach definition.
    """
    ch = _channel(ctx)
    if not ch:
        return None
    reaches = sl_reaches(ch.distance_m, ch.elevation_m, float(params.get("reach_length_m", 200.0)))
    return aggregate((r["sl"] for r in reaches), params.get("statistic", "Mean"))


def normalized_sl(ctx, params):
    """
    SL/K (Seeber & Gornitz, 1983): SL normalised by the graded-river
    gradient K = (Hsource - Houtlet) / ln(Lc). Values > 2 flag anomalies.
    """
    ch = _channel(ctx)
    if not ch or ch.length_m <= math.e:
        return None
    drop = float(ch.elevation_m[0] - ch.elevation_m[-1])
    if drop <= 0:
        return None
    k = drop / math.log(ch.length_m)
    reaches = sl_reaches(ch.distance_m, ch.elevation_m, float(params.get("reach_length_m", 200.0)))
    return aggregate((r["sl"] / k for r in reaches), params.get("statistic", "Mean"))


def normalized_steepness(ctx, params):
    """Normalised channel steepness ksn = S·A^θref (Wobus et al., 2006)."""
    ch = _channel(ctx)
    if not ch:
        return None
    reaches = ksn_reaches(ch.distance_m, ch.elevation_m, ch.drainage_area_m2,
                          float(params.get("theta_ref", 0.45)),
                          float(params.get("reach_length_m", 250.0)))
    return aggregate((r["ksn"] for r in reaches), params.get("statistic", "Mean"))


def chi_index(ctx, params):
    """
    Chi (χ) of the trunk stream (Perron & Royden, 2013).

    Statistics: "Chi at source" (χ max), "Mean", "Median", or
    "Z-χ slope (k_χ)": least-squares slope of elevation against χ, equal to
    ksn when A0 = 1 m².

    Choice: GSV-Studio's default, "value at the outlet", is always 0 because
    χ is integrated from the outlet; the default here is "Chi at source".
    """
    ch = _channel(ctx)
    if not ch:
        return None
    chi = chi_coordinate(ch.distance_m, ch.drainage_area_m2,
                         float(params.get("theta_ref", 0.45)),
                         float(params.get("reference_area_m2", 1.0)))
    statistic = params.get("statistic", "Chi at source")
    if statistic == "Mean":
        return float(np.mean(chi))
    if statistic == "Median":
        return float(np.median(chi))
    if statistic.startswith("Z-χ slope"):
        if np.ptp(chi) <= 0:
            return None
        return float(np.polyfit(chi, ch.elevation_m, 1)[0])
    return float(chi[0])


# ===========================================================================
# Active tectonics
# ===========================================================================

def asymmetry_factor(ctx, params):
    """
    Drainage-basin asymmetry factor AF = 100 · Ar / At (Hare & Gardner, 1985).

    Ar is the basin area to the right of the trunk stream looking downstream
    (or to the left when ``side`` asks so). AF = 50 for a symmetric basin.
    """
    if not (_positive(ctx.area_m2) and ctx.right_bank_area_m2 is not None):
        return None
    right = ctx.right_bank_area_m2
    if params.get("side") == "Left bank (looking downstream)":
        right = ctx.area_m2 - right
    return float(np.clip(100.0 * right / ctx.area_m2, 0.0, 100.0))


def _t_values(ctx, params) -> List[float]:
    """Local T = Da/Dd values of every valid section (Cox, 1994)."""
    return [t for _, t in _t_sections(ctx, params)]


def _t_sections(ctx, params) -> list:
    """
    Pairs ``(section, local T)`` for every section with a usable Dd.

    ``dd_method`` selects the half-width used as Dd (see
    ``transverse_symmetry``). Local T is capped at 1: a channel cannot lie
    beyond the divide; values above 1 come from an irregular divide.
    """
    method = params.get("dd_method", "Channel side (Cox 1994)")
    pairs = []
    for s in ctx.sections or []:
        left, right = s.dd_left_m, s.dd_right_m
        if method.startswith("Channel side"):
            dd = left if s.channel_side > 0 else right if s.channel_side < 0 else max(left, right)
        elif method.startswith("Maximum"):
            dd = max(left, right)
        elif method.startswith("Minimum"):
            dd = min(left, right)
        else:
            dd = 0.5 * (left + right)
        if math.isfinite(dd) and dd > 1e-6 and math.isfinite(s.da_m):
            pairs.append((s, min(s.da_m / dd, 1.0)))
    return pairs


def transverse_symmetry(ctx, params):
    """
    Transverse topographic symmetry factor T = Da / Dd (Cox, 1994).

    Choice: Cox measures Dd from the midline to the divide on the side
    towards which the channel has shifted ("Channel side", default here).
    GSV-Studio defaulted to the larger half-width; that and the other
    GSV-Studio variants are available through ``dd_method``.
    """
    return aggregate(_t_values(ctx, params), "Mean")


def transverse_symmetry_azimuth(ctx, params):
    """
    Mean azimuth of the channel shift (degrees from north), i.e. the
    direction of the T vector of Cox (1994), weighted by the local T.
    """
    pairs = _t_sections(ctx, params)
    if not pairs:
        return None
    east = north = 0.0
    for section, t in pairs:
        angle = math.radians(section.shift_azimuth)
        east += t * math.sin(angle)
        north += t * math.cos(angle)
    if abs(east) + abs(north) < 1e-12:
        return None
    return math.degrees(math.atan2(east, north)) % 360.0


def valley_floor_ratio(ctx, params):
    """
    Valley floor width to height ratio Vf (Bull & McFadden, 1977),
    aggregated over cross-profiles of the trunk valley.
    """
    if not ctx.cross_profiles:
        return None
    analysis = {
        "smoothing_sigma": float(params.get("smoothing_sigma", 1.5)),
        "floor_method": "footslope" if str(params.get("floor_method", "")).startswith("Foot")
                        else "flat_slope",
        "max_flat_slope_deg": float(params.get("max_flat_slope_deg", 2.0)),
        "basal_slope_threshold_deg": float(params.get("basal_slope_threshold_deg", 3.0)),
        "consistency_points": int(params.get("consistency_points", 2)),
        "min_height_diff_m": float(params.get("min_height_diff_m", 0.5)),
        "min_half_width_m": float(params.get("min_half_width_m", 1.0)),
        "divide_method": "percentile" if str(params.get("divide_method", "")).startswith("Percentile")
                         else "shoulder",
        "percentile": float(params.get("percentile", 90.0)),
        "shoulder_min_rise_m": float(params.get("shoulder_min_rise_m", 5.0)),
        "shoulder_slope_factor": float(params.get("shoulder_slope_factor", 0.3)),
        "shoulder_min_slope_deg": float(params.get("shoulder_min_slope_deg", 1.0)),
        # Resolution limits: re-centre on the valley bottom within one cell
        # and never report a floor narrower than one cell.
        "channel_search_m": float(ctx.cell_size_m or 0.0),
        "min_floor_width_m": float(ctx.cell_size_m or 0.0),
    }
    values = []
    for profile in ctx.cross_profiles:
        result = analyze_cross_profile(profile.distance_m, profile.elevation_m,
                                       profile.channel_at_m, analysis)
        if result is not None:
            values.append(result["vf"])
    return aggregate(values, params.get("statistic", "Median"))


# ===========================================================================
# Hydrological terrain indices (pixel statistics)
# ===========================================================================

def _pixel_index(ctx, params, kind: str) -> Optional[float]:
    """
    Basin statistic of TWI, SPI or STI computed on every basin cell.

    Specific catchment area a = A / w, with A the upslope area (cells × cell
    area) and w the cell width (the contour length crossed by D8 flow).
    tan β is floored at 0.001 (≈ 0.06°) so flat cells do not produce infinite
    TWI. GSV-Studio clipped the DEM instead of the slope raster and therefore
    computed these indices on elevations; this implementation uses slope.
    """
    t = ctx.terrain
    if t is None:
        return None
    mask = t.mask & np.isfinite(t.slope_rad) & (t.accumulation_cells > 0)
    if not np.any(mask):
        return None
    beta = t.slope_rad[mask]
    a = t.accumulation_cells[mask] * t.cell_size_m ** 2 / t.cell_size_m
    tan_b = np.maximum(np.tan(beta), 1e-3)
    if kind == "twi":
        values = np.log(a / tan_b)
    elif kind == "spi":
        values = a * tan_b
    else:  # sti, Moore & Burch (1986)
        values = (a / 22.13) ** 0.6 * (np.sin(beta) / 0.0896) ** 1.3
    return aggregate(values, params.get("statistic", "Mean"))


def topographic_wetness(ctx, params):
    """Topographic wetness index TWI = ln(a / tan β) (Beven & Kirkby, 1979)."""
    return _pixel_index(ctx, params, "twi")


def stream_power(ctx, params):
    """Stream power index SPI = a · tan β (Moore et al., 1991)."""
    return _pixel_index(ctx, params, "spi")


def sediment_transport(ctx, params):
    """Sediment transport index STI (Moore & Burch, 1986)."""
    return _pixel_index(ctx, params, "sti")


# ===========================================================================
# Empirical erosion relations (experimental)
# ===========================================================================

def denudation_rate(ctx, params):
    """
    Denudation rate D = 28 · e^(0.00774·S) (GSV-Studio, after Reinhardt et
    al., 2007), S = mean basin slope in degrees as in GSV-Studio.
    Experimental: the unit of S in the source must be confirmed (doc/07).
    """
    slope = mean_slope_degree(ctx, {"averaging": "atan(mean tan β) (GSV-Studio legacy)"})
    return 28.0 * math.exp(0.00774 * slope) if slope is not None else None


def bulk_erosion(ctx, params):
    """
    "Ebulk" = TR × D, as in GSV-Studio. Experimental and dimensionally
    questionable: Brocklehurst & Whipple (2002) define a volume between a
    ridge envelope and the present surface. Kept only for comparison.
    """
    s = _stats(ctx)
    d = denudation_rate(ctx, params)
    if not s or d is None:
        return None
    return (s[1] - s[0]) * d


def erosion_rate(ctx, params):
    """Erosion rate E = 1.4·10⁻⁶ · TR^1.8 (GSV-Studio; experimental)."""
    s = _stats(ctx)
    return 1.4e-6 * (s[1] - s[0]) ** 1.8 if s else None
