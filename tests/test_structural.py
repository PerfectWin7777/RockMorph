"""
tests/test_structural.py

Unit tests of core/structural.py: three-point problem and outcrop trace.

Authors: RockMorph contributors / Tony
"""

import math

import numpy as np
import pytest

from core.structural import DegeneratePoints, fit_plane, height_above_plane, plane_reaches_surface


def plane_points(dip, dip_direction, n=3, noise=0.0, seed=0):
    """Points of a plane of known orientation (x east, y north, metres)."""
    rng = np.random.default_rng(seed)
    gradient = math.tan(math.radians(dip))
    # Down-dip unit vector (east, north); elevation decreases along it.
    ux, uy = math.sin(math.radians(dip_direction)), math.cos(math.radians(dip_direction))
    xy = rng.uniform(-500, 500, size=(n, 2))
    z = 1000.0 - gradient * (xy[:, 0] * ux + xy[:, 1] * uy) + rng.normal(0.0, noise, n)
    return [tuple(p) for p in np.column_stack([xy, z])]


@pytest.mark.parametrize("dip,direction", [(30.0, 120.0), (10.0, 0.0), (65.0, 275.0), (45.0, 181.0)])
def test_known_orientations_are_recovered(dip, direction):
    plane = fit_plane(plane_points(dip, direction))
    assert abs(plane.dip - dip) < 1e-6
    assert abs((plane.dip_direction - direction + 180) % 360 - 180) < 1e-6
    assert abs((plane.strike - (direction - 90.0) + 180.0) % 360.0 - 180.0) < 1e-6
    assert plane.rms_m < 1e-6


def test_noisy_points_give_residuals():
    plane = fit_plane(plane_points(25.0, 60.0, n=5, noise=2.0, seed=4))
    assert abs(plane.dip - 25.0) < 1.5 and plane.rms_m > 0.1
    assert plane.notation.count("/") == 1


def test_degenerate_points():
    with pytest.raises(DegeneratePoints):
        fit_plane([(0, 0, 1), (10, 10, 2)])
    with pytest.raises(DegeneratePoints):
        fit_plane([(0, 0, 1), (10, 10, 2), (20, 20, 3)])     # aligned in map view


def test_outcrop_of_horizontal_plane_on_a_slope():
    # Valley side rising to the east; horizontal bed at 1050 m crops out where z = 1050.
    x = np.arange(0.0, 1000.0, 10.0)
    y = np.arange(0.0, 500.0, 10.0)
    dem = 1000.0 + 0.1 * np.tile(x, (y.size, 1))
    plane = fit_plane([(0, 0, 1050.0), (100, 0, 1050.0), (0, 100, 1050.0)])
    difference = height_above_plane(dem, x, y, plane)
    assert plane_reaches_surface(difference)
    crossing = x[np.argmin(np.abs(difference[0]))]
    assert abs(crossing - 500.0) < 10.0
    above = fit_plane([(0, 0, 5000.0), (100, 0, 5000.0), (0, 100, 5000.0)])
    assert not plane_reaches_surface(height_above_plane(dem, x, y, above))
