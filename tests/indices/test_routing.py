"""
tests/indices/test_routing.py

Unit tests of core/indices/routing.py (stream burning, MFD, D∞, HAND,
reach k_sn) and of the best-concavity search of core/indices/profiles.py.

Authors: RockMorph contributors / Tony
"""

import numpy as np

from core.indices.flow import d8_receivers, flow_accumulation, priority_flood_fill
from core.indices.profiles import best_concavity, linear_fit
from core.indices.routing import (
    burn_streams, dinf_accumulation, height_above_drainage, mfd_accumulation, reach_ksn, sink_mask,
)


def plane_south(width=40, height=12):
    """Plane dipping to the south, 5 m per row."""
    return np.array([[1000.0 - 5.0 * r for _c in range(width)] for r in range(height)])


def test_plane_drains_one_row_per_row():
    # The top border is a sink: an interior cell of row r drains rows 1..r.
    grid = plane_south()
    for accumulation in (dinf_accumulation(grid, 10, 10), mfd_accumulation(grid, 10, 10)):
        for r in range(1, grid.shape[0] - 1):
            for c in range(r + 1, grid.shape[1] - r - 1):
                assert abs(accumulation[r, c] - r * 100.0) < 1e-6 * r * 100.0


def test_all_water_leaves_through_sinks():
    rng = np.random.default_rng(11)
    grid = 500.0 + rng.random((25, 30)) * 3.0 + 0.3 * np.arange(30)[None, :]
    grid[7, 9] = np.nan
    filled = priority_flood_fill(grid, np.isfinite(grid))
    expected = np.isfinite(grid).sum() * 10.0 * 15.0
    for accumulation in (dinf_accumulation(filled, 10, 15), mfd_accumulation(filled, 10, 15)):
        assert abs(np.nansum(accumulation[sink_mask(filled)]) - expected) < 1e-6 * expected


def test_hand_is_height_above_valley_floor():
    rows, cols = np.indices((41, 41), dtype=float)
    dem = 100.0 + 0.5 * (41 - rows) + 2.0 * np.abs(cols - 20)
    mask = np.ones(dem.shape, bool)
    filled = priority_flood_fill(dem, mask)
    receiver = d8_receivers(filled, mask, 10.0)
    streams = flow_accumulation(filled, receiver, mask) >= 30
    hand = height_above_drainage(dem, receiver, streams)
    assert hand[20, 20] == 0.0
    assert abs(hand[20, 25] - 10.0) < 1e-9          # 5 columns × 2 m


def test_burning_lowers_only_the_stream_cells():
    dem = np.full((5, 5), 100.0)
    mask = np.zeros((5, 5), bool)
    mask[2, :] = True
    burned = burn_streams(dem, mask, 7.0)
    assert np.all(burned[2] == 93.0) and np.all(burned[mask == False] == 100.0)  # noqa: E712


def test_reach_ksn():
    assert abs(reach_ksn(110.0, 100.0, 1000.0, 1e6, 0.5) - 0.01 * 1000.0) < 1e-9
    assert reach_ksn(100.0, 100.0, 0.0, 1e6, 0.5) is None


def test_best_concavity_recovers_steady_state_theta():
    # Steady-state channel: S = ks · A^-θ with θ = 0.6 and A growing downstream.
    distance = np.linspace(0.0, 20000.0, 400)                 # from the source
    area = 1e5 + (distance / 20.0) ** 2 * 50.0
    chi = None
    from core.indices.profiles import chi_coordinate
    chi = chi_coordinate(distance, area, 0.6, 1.0)
    elevation = 200.0 + 80.0 * chi
    found = best_concavity(distance, area, elevation, 1.0)
    assert abs(found["theta"] - 0.6) < 0.011
    assert found["r2"] > 0.9999


def test_linear_fit():
    fit = linear_fit(np.array([0.0, 1.0, 2.0, np.nan]), np.array([1.0, 3.0, 5.0, 9.0]))
    assert abs(fit["slope"] - 2.0) < 1e-12 and abs(fit["r2"] - 1.0) < 1e-12 and fit["n"] == 3
