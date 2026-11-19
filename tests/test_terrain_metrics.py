"""
tests/test_terrain_metrics.py

Unit tests of core/terrain_metrics.py on analytic surfaces.

Authors: RockMorph contributors / Tony
"""

import numpy as np

from core import terrain_metrics as tm

N, C = 81, 40
ROWS, COLS = np.mgrid[0:N, 0:N]
X = (COLS - C) * 10.0
Y = (C - ROWS) * 10.0           # north up


def test_curvature_signs_on_dome_and_bowl():
    dome = 500.0 - (X ** 2 + Y ** 2) / 400.0
    bowl = (X ** 2 + Y ** 2) / 400.0
    k_dome = tm.curvatures(dome, 10, 10)
    k_bowl = tm.curvatures(bowl, 10, 10, ["mean", "gaussian"])
    assert k_dome["mean"][C, C] > 0 and k_dome["profile"][C, C + 10] > 0 and k_dome["plan"][C, C + 10] > 0
    assert k_bowl["mean"][C, C] < 0 and k_bowl["gaussian"][C, C] > 0
    # Mean curvature at the top of a paraboloid z = -r²/400: H = 1/200.
    assert abs(k_dome["mean"][C, C] - 0.005) < 1e-6


def test_south_slope_is_sunnier_than_north_slope():
    south = tm.relative_insolation(0.3 * Y, 10, 10, 45.0)[C, C]
    north = tm.relative_insolation(-0.3 * Y, 10, 10, 45.0)[C, C]
    flat = tm.relative_insolation(np.zeros((N, N)), 10, 10, 45.0)[C, C]
    assert south > 1.0 > north and abs(flat - 1.0) < 1e-6


def test_geomorphons_of_simple_forms():
    cone = np.sqrt(X ** 2 + Y ** 2)
    assert tm.geomorphons(-cone, 10, 10, 10)[C, C] == 2          # peak
    assert tm.geomorphons(cone, 10, 10, 10)[C, C] == 10          # pit
    assert tm.geomorphons(np.zeros((N, N)), 10, 10, 10)[C, C] == 1   # flat
    assert tm.geomorphons(0.3 * Y, 10, 10, 10)[C, C] == 6        # slope
    assert tm.geomorphons(-np.abs(X) * 0.5, 10, 10, 10)[C, C] == 3   # ridge
    assert tm.geomorphons(np.abs(X) * 0.5 + 0.01 * Y, 10, 10, 10)[C, C] == 9   # valley


def test_dev_valley_bottoms_landforms_swiss():
    peak = -np.sqrt(X ** 2 + Y ** 2)
    assert tm.multiscale_dev(peak)[C, C] > 2.0
    floor = np.where(np.abs(X) < 100, 0.0, np.abs(X) * 0.5)
    assert tm.valley_bottoms(floor, 10, 10)[C, C] > 1.0
    assert tm.valley_bottoms(0.8 * Y, 10, 10)[C, C] == 0.0
    classes = tm.iwahashi_landforms(peak, 10, 10)
    assert set(np.unique(classes)) <= set(range(1, 9))
    shade = tm.swiss_hillshade(peak, 10, 10)
    assert 0.0 <= np.nanmin(shade) and np.nanmax(shade) <= 1.0


def test_nodata_is_preserved():
    dem = 0.3 * Y
    dem[10, 10] = np.nan
    for result in (tm.slope_degrees(dem, 10, 10), tm.multiscale_dev(dem),
                   tm.geomorphons(dem, 10, 10, 5), tm.relative_insolation(dem, 10, 10, 40.0)):
        assert np.isnan(result[10, 10]) and np.isfinite(result[C, C])
