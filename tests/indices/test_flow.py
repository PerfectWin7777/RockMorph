"""
tests/indices/test_flow.py

Unit tests of the NumPy flow routing: Priority-Flood filling, D8 directions,
accumulation, trunk stream and Strahler/Horton statistics.

Authors: RockMorph contributors / Tony
"""

import numpy as np
import pytest

from core.indices.flow import (
    d8_receivers, flow_accumulation, network_from_segments, priority_flood_fill,
    strahler_network, trunk_stream,
)


def valley_dem(rows=41, cols=41, pit=False):
    """V-shaped valley draining south, with an optional closed pit."""
    r, c = np.indices((rows, cols), dtype=float)
    dem = 100.0 + 0.5 * (rows - r) + 2.0 * np.abs(c - cols // 2)
    if pit:
        dem[20, cols // 2] = 50.0
    return dem


def test_fill_removes_pits_and_flats():
    dem = valley_dem(pit=True)
    mask = np.ones(dem.shape, bool)
    filled = priority_flood_fill(dem, mask)
    assert np.all(filled >= dem - 1e-12)
    receivers = d8_receivers(filled, mask, 30.0)
    # Every interior cell drains somewhere after filling.
    interior = np.zeros(dem.shape, bool)
    interior[1:-1, 1:-1] = True
    assert np.all(receivers.reshape(dem.shape)[interior] >= 0)


def test_accumulation_and_trunk_stream():
    dem = valley_dem()
    mask = np.ones(dem.shape, bool)
    filled = priority_flood_fill(dem, mask)
    receivers = d8_receivers(filled, mask, 30.0)
    acc = flow_accumulation(filled, receivers, mask)
    # Border cells are outlets; the valley outlet collects the most cells.
    outlet = int(np.argmax(acc))
    assert outlet // dem.shape[1] == dem.shape[0] - 1
    path = trunk_stream(receivers, acc, outlet)
    assert path[-1] == outlet
    assert np.all(np.diff(acc.ravel()[path]) > 0)  # accumulation grows downstream


def test_strahler_on_a_y_network():
    # Two order-1 tributaries joining into an order-2 trunk.
    rows, cols = 7, 7
    receivers = -np.ones(rows * cols, dtype=np.int64)
    idx = lambda r, c: r * cols + c
    left = [idx(0, 1), idx(1, 2), idx(2, 3)]
    right = [idx(0, 5), idx(1, 4), idx(2, 3)]
    trunk = [idx(2, 3), idx(3, 3), idx(4, 3), idx(5, 3), idx(6, 3)]
    for path in (left, right, trunk):
        for a, b in zip(path[:-1], path[1:]):
            receivers[a] = b
    stream = np.zeros((rows, cols), bool)
    for cell in set(left + right + trunk):
        stream.flat[cell] = True
    acc = np.zeros(rows * cols)
    for i, cell in enumerate(left):
        acc[cell] = i + 1
    for i, cell in enumerate(right):
        acc[cell] = i + 1
    for i, cell in enumerate(trunk):
        acc[cell] = 10 + i
    summary, order = strahler_network(receivers, acc.reshape(rows, cols), stream, cols, 30.0)
    assert summary.stream_counts == {1: 2, 2: 1}
    assert summary.max_order == 2
    assert order[idx(6, 3)] == 2


def test_vector_network_counts_streams_not_segments():
    # Order-2 stream split into two segments: still one stream of order 2.
    segs = [
        {"start": (0, 10), "end": (5, 5), "length": 7.0},
        {"start": (10, 10), "end": (5, 5), "length": 7.0},
        {"start": (5, 5), "end": (5, 2), "length": 3.0},
        {"start": (5, 2), "end": (5, 0), "length": 2.0},
    ]
    summary = network_from_segments(segs, snap_m=0.5)
    assert summary.stream_counts == {1: 2, 2: 1}
    assert summary.stream_lengths_m[2] == [5.0]
    assert summary.link_count >= 3


def test_right_of_polyline_follows_flow_direction():
    pytest.importorskip("qgis.core")
    from core.basin_context_builder import right_of_polyline
    # Eastward line: south is on the right, north on the left.
    line = np.array([[0.0, 0.0], [5.0, 0.0], [10.0, 0.0]])
    pts = np.array([[3.0, -2.0], [3.0, 2.0], [12.0, -1.0]])
    assert right_of_polyline(pts, line).tolist() == [True, False, True]
