"""
tests/indices/test_formulas.py

Unit tests of the pure index formulas on synthetic basins whose values are
known analytically (circle, rectangle, linear and steady-state profiles).

Authors: RockMorph contributors / Tony
"""

import math

import numpy as np
import pytest

from core.indices import compute_index, get, SPECS
from core.indices.context import (
    BasinContext, ChannelProfile, CrossProfile, NetworkSummary,
    TerrainWindow, TransverseSection,
)
from core.indices.multicriteria import (
    IAT_CLASSIFICATION, ahp_weights, class_scores, composite_index,
    reciprocal_matrix, relative_active_tectonics,
)
from core.indices.profiles import analyze_cross_profile, chi_coordinate, sl_reaches


def circle_context(radius=1000.0):
    """Circular basin of radius r: Rc = 1, Kc = 1."""
    return BasinContext(1, "circle", area_m2=math.pi * radius ** 2,
                        perimeter_m=2 * math.pi * radius,
                        length_m=2 * radius, width_m=2 * radius)


def test_circle_shape_indices():
    ctx = circle_context()
    assert compute_index("Rc", ctx) == pytest.approx(1.0)
    assert compute_index("Compactness", ctx) == pytest.approx(1.0)
    assert compute_index("Re", ctx) == pytest.approx(1.0)
    assert compute_index("Rf", ctx) == pytest.approx(math.pi / 4)
    assert compute_index("Bs", ctx) == pytest.approx(1.0)
    assert compute_index("BasinArea", ctx) == pytest.approx(math.pi)


def test_rectangle_shape_indices():
    # 4 km x 1 km rectangle.
    ctx = BasinContext(2, "rect", area_m2=4e6, perimeter_m=10e3, length_m=4e3, width_m=1e3)
    assert compute_index("Bs", ctx) == pytest.approx(4.0)
    assert compute_index("Rf", ctx) == pytest.approx(0.25)
    assert compute_index("Lemniscate", ctx) == pytest.approx(1.0)
    assert compute_index("BasinPerimeter", ctx) == pytest.approx(10.0)


def test_relief_and_hypsometry():
    # Uniform elevations 0..1000 m: HI = 0.5, TR = 1000, TD = 500, TRD = 500.
    elev = np.linspace(0.0, 1000.0, 10001)
    ctx = BasinContext(3, "ramp", area_m2=1e6, perimeter_m=4e3, length_m=1e3,
                       width_m=1e3, elevations=elev)
    assert compute_index("HI", ctx) == pytest.approx(0.5)
    assert compute_index("HI", ctx, {"formula": "Curve integral (Strahler 1952)"}) == \
        pytest.approx(0.5, abs=1e-3)
    assert compute_index("TR", ctx) == pytest.approx(1000.0)
    assert compute_index("TD", ctx) == pytest.approx(500.0)
    assert compute_index("TRD", ctx) == pytest.approx(500.0)
    assert compute_index("Rra", ctx) == pytest.approx(1.0)
    assert compute_index("MRN", ctx) == pytest.approx(1.0)
    assert compute_index("AltitudeSkewness", ctx) == pytest.approx(0.0, abs=1e-9)
    # Uniform law: Pearson kurtosis 1.8.
    assert compute_index("AltitudeKurtosis", ctx) == pytest.approx(1.8, abs=1e-3)


def test_network_ratios_follow_horton_laws():
    # Perfect Horton network: Rb = 4, Rl = 2, Ra = 5.
    net = NetworkSummary(
        stream_counts={1: 16, 2: 4, 3: 1},
        stream_lengths_m={1: [500.0] * 16, 2: [1000.0] * 4, 3: [2000.0]},
        basin_areas_m2={1: [1e6] * 16, 2: [5e6] * 4, 3: [25e6]},
        total_length_m=16 * 500 + 4 * 1000 + 2000, link_count=31, max_order=3,
    )
    ctx = BasinContext(4, "horton", area_m2=25e6, perimeter_m=20e3, network=net)
    for method in ("Mean of successive ratios", "Horton law regression"):
        assert compute_index("RB", ctx, {"method": method}) == pytest.approx(4.0)
        assert compute_index("RL", ctx, {"method": method}) == pytest.approx(2.0)
        assert compute_index("RA", ctx, {"method": method}) == pytest.approx(5.0)
    assert compute_index("Dd", ctx) == pytest.approx(14.0 / 25.0)
    assert compute_index("Fs", ctx) == pytest.approx(21 / 25.0)
    assert compute_index("C", ctx) == pytest.approx(25.0 / 14.0)
    assert compute_index("Di", ctx, {"formula": "Fs / Dd (Faniran 1968)"}) == \
        pytest.approx((21 / 25.0) / (14.0 / 25.0))


def straight_channel(length=10000.0, drop=500.0, n=201):
    """Straight channel with a constant gradient and Hack-like area growth."""
    d = np.linspace(0.0, length, n)
    z = drop * (1 - d / length) + 100.0
    area = 1e4 + 50.0 * (d + 100.0) ** 1.6
    return ChannelProfile(d, z, area, d.copy(), np.zeros_like(d))


def test_sl_on_constant_gradient():
    ch = straight_channel()
    reaches = sl_reaches(ch.distance_m, ch.elevation_m, 1000.0)
    assert len(reaches) == 10
    gradient = 500.0 / 10000.0
    for reach in reaches:
        assert reach["gradient"] == pytest.approx(gradient)
        assert reach["sl"] == pytest.approx(gradient * reach["mid_m"])
    ctx = BasinContext(5, "chan", channel=ch)
    expected = gradient * np.mean([r["mid_m"] for r in reaches])
    assert compute_index("SL", ctx, {"reach_length_m": 1000.0}) == pytest.approx(expected)
    assert compute_index("HSI", ctx) == pytest.approx(1.0)
    assert compute_index("MASC", ctx) == pytest.approx(0.05)


def test_chi_is_aligned_and_recovers_ksn():
    # Steady-state river: z = z0 + ksn·chi. The Z-chi slope must return ksn
    # (the RockMorph fluvial tool returned a negative slope here, see audit C1).
    d = np.linspace(0.0, 20000.0, 400)
    area = 1e4 + 2.0 * (d + 50.0) ** 1.7
    chi = chi_coordinate(d, area, 0.45)
    assert chi[-1] == pytest.approx(0.0)
    assert chi[0] == pytest.approx(chi.max())
    z = 200.0 + 100.0 * chi
    ctx = BasinContext(6, "steady", channel=ChannelProfile(d, z, area, d, np.zeros_like(d)))
    assert compute_index("Chi", ctx, {"statistic": "Z-χ slope (k_χ)"}) == pytest.approx(100.0)
    assert compute_index("Ksn", ctx, {"reach_length_m": 500.0}) == pytest.approx(100.0, rel=0.02)


def test_asymmetry_and_symmetry_factors():
    ctx = BasinContext(7, "af", area_m2=10e6, right_bank_area_m2=7e6)
    assert compute_index("AF", ctx) == pytest.approx(70.0)
    assert compute_index("AF", ctx, {"side": "Left bank (looking downstream)"}) == pytest.approx(30.0)
    label, score = get("AF").classification().classify(70.0)
    assert score == 1  # |70 - 50| = 20 >= 15
    sections = [TransverseSection(200.0, 1000.0, 800.0, +1, 90.0),
                TransverseSection(400.0, 1000.0, 800.0, +1, 90.0)]
    ctx = BasinContext(8, "t", sections=sections)
    assert compute_index("TTSF", ctx) == pytest.approx(0.3)
    assert compute_index("TTSF", ctx, {"dd_method": "Maximum half-width (GSV-Studio)"}) == pytest.approx(0.3)
    assert compute_index("TTSF", ctx, {"dd_method": "Minimum half-width"}) == pytest.approx(0.375)
    assert compute_index("TTSF_Azimuth", ctx) == pytest.approx(90.0)


def test_vf_on_v_and_u_valleys():
    params = dict(smoothing_sigma=0.0, floor_method="flat_slope", max_flat_slope_deg=2.0,
                  basal_slope_threshold_deg=3.0, consistency_points=2, min_height_diff_m=0.5,
                  min_half_width_m=1.0, divide_method="percentile", percentile=100.0,
                  shoulder_min_rise_m=5.0, shoulder_slope_factor=0.3, shoulder_min_slope_deg=1.0)
    x = np.linspace(-250.0, 250.0, 501)
    v_valley = np.abs(x) * 0.5                       # V: no floor
    u_valley = np.where(np.abs(x) < 100, 0.0, (np.abs(x) - 100) * 0.5)  # 200 m floor
    v = analyze_cross_profile(x + 250.0, v_valley, 250.0, params)
    u = analyze_cross_profile(x + 250.0, u_valley, 250.0, params)
    assert v["vf"] < 0.5
    assert u["vfw_m"] == pytest.approx(200.0, abs=3.0)
    assert u["vf"] == pytest.approx(200.0 / 75.0, rel=0.05)


def test_terrain_indices_use_slope_not_elevation():
    slope = np.full((3, 3), math.radians(10.0))
    acc = np.full((3, 3), 4.0)
    terrain = TerrainWindow(slope, acc, np.ones((3, 3), bool), 10.0)
    ctx = BasinContext(9, "terrain", terrain=terrain)
    a = 4 * 100.0 / 10.0
    assert compute_index("TWI", ctx) == pytest.approx(math.log(a / math.tan(math.radians(10))))
    assert compute_index("SPI", ctx) == pytest.approx(a * math.tan(math.radians(10)))
    assert compute_index("SlopeMeanDegree", ctx) == pytest.approx(10.0)


def test_classification_conventions():
    sl = get("SL").classification()
    assert sl.classify(600)[1] == 1
    assert sl.classify(500)[1] == 1   # lower bound inclusive
    assert sl.classify(299.9)[1] == 3
    hi = get("HI").classification()
    assert hi.classify(0.5)[1] == 1   # last rule: >= 0.5
    assert hi.classify(0.45)[1] == 2
    assert IAT_CLASSIFICATION.classify(1.4)[1] == 1
    assert IAT_CLASSIFICATION.classify(2.6)[1] == 4


def test_iat_and_ahp():
    sl = class_scores([600, 400, 100], get("SL").classification())
    vf = class_scores([0.3, 0.7, None], get("VF").classification())
    iat = relative_active_tectonics({"SL": sl, "VF": vf})
    assert iat.score.tolist()[:2] == [1.0, 2.0]
    assert iat.n_used.tolist() == [2, 2, 1]
    # Iat = 2.0 belongs to class 3 (2.0 <= Iat < 2.5, El Hamdouni et al. 2008).
    assert iat.class_number.tolist()[:2] == [1.0, 3.0]
    # A perfectly consistent matrix: weights 4:2:1, CR = 0.
    matrix = reciprocal_matrix({(0, 1): 2, (0, 2): 4, (1, 2): 2}, 3)
    result = ahp_weights(matrix)
    assert result.weights == pytest.approx([4 / 7, 2 / 7, 1 / 7])
    assert result.consistency_ratio == pytest.approx(0.0, abs=1e-9)


def test_composite_reverses_direction():
    values, _ = composite_index({"a": np.array([0.0, 1.0]), "b": np.array([1.0, 0.0])},
                                "Mean", directions={"b": -1})
    assert values.tolist() == [0.0, 1.0]


def test_every_index_runs_on_a_complete_context():
    d = np.linspace(0, 5000, 101)
    channel = ChannelProfile(d, 500 - d * 0.05, 1e4 + 30 * (d + 10) ** 1.5, d, np.zeros_like(d))
    x = np.linspace(0, 500, 251)
    profile = CrossProfile(x, np.abs(x - 250) * 0.4 + 400, 250.0)
    net = NetworkSummary({1: 8, 2: 2, 3: 1}, {1: [300.0] * 8, 2: [900.0] * 2, 3: [2000.0]},
                         {1: [2e5] * 8, 2: [1e6] * 2, 3: [5e6]}, 6200.0, 15, 3)
    ctx = BasinContext(10, "full", area_m2=5e6, perimeter_m=11e3, length_m=4e3, width_m=1.5e3,
                       elevations=np.linspace(400, 900, 500), channel=channel, network=net,
                       terrain=TerrainWindow(np.full((4, 4), 0.2), np.full((4, 4), 3.0),
                                             np.ones((4, 4), bool), 30.0),
                       right_bank_area_m2=2e6,
                       sections=[TransverseSection(100, 800, 900, -1, 200.0)],
                       cross_profiles=[profile])
    for spec in SPECS:
        value = compute_index(spec.key, ctx)
        assert value is not None, f"{spec.key} returned None on a complete context ({ctx.warnings})"


def test_rank_classes_put_highest_values_in_class_one():
    from core.indices.multicriteria import rank_classes
    values = np.array([0.1, 0.2, 0.3, 0.4, 0.5, 0.6, 0.7, 0.8, np.nan])
    classes, breaks = rank_classes(values, "Quantiles", 4)
    assert classes[-2] == 1 and classes[0] == 4 and np.isnan(classes[-1])
    assert len(breaks) == 3
    classes, _ = rank_classes(values, "Equal intervals", 2)
    assert classes[:4].tolist() == [2, 2, 2, 2] and classes[4:8].tolist() == [1, 1, 1, 1]
