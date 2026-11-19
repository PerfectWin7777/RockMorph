"""
core/structural.py — Plane orientation from points and predicted outcrop

Pure NumPy (no QGIS), unit-tested.

The three-point problem
-----------------------
Three (or more) points of a bed, a contact or a fault surface, with their
elevations, define its plane. With more than three points the plane is the
least-squares fit z = a·x + b·y + c (x east, y north, metres), and the
departure of each point from it measures how planar the surface really is.

From the fitted gradient (a, b):
    dip            = atan(√(a² + b²))                       (0–90°)
    dip direction  = azimuth of (−a, −b), clockwise from north (0–360°)
    strike         = dip direction − 90° (right-hand rule)
Notation "dip/dip direction", e.g. 30/120.

Predicted outcrop
-----------------
Where the plane meets the topography, z_DEM − z_plane changes sign: the zero
contour of that difference is the predicted outcrop trace (the "rule of Vs"
made explicit). A plane that never reaches the surface has no trace.

References
----------
Ragan, D. M. (2009). Structural Geology: An Introduction to Geometrical
    Techniques, 4th ed. Cambridge University Press, ch. 2–4 (three-point
    problem, outcrop patterns).
Same method as GSV-Studio's 3D "Pendage" tool (Core/Scene3D/Measure.py).

Authors: RockMorph contributors / Tony
"""

from __future__ import annotations

import math
from dataclasses import dataclass, field
from typing import List, Sequence, Tuple

import numpy as np  # type: ignore

# Ratio of the smallest to the largest singular value of the centred (x, y)
# coordinates below which the points are considered aligned (no plane).
COLLINEAR_RATIO = 1e-3


class DegeneratePoints(ValueError):
    """Fewer than three points, or points aligned in map view."""


@dataclass
class PlaneOrientation:
    """Least-squares plane through measured points."""
    a: float                 # ∂z/∂x (east)
    b: float                 # ∂z/∂y (north)
    c: float                 # z at x = y = 0 (in the local frame of the fit)
    origin: Tuple[float, float]   # (x0, y0) subtracted before fitting
    dip: float               # degrees
    dip_direction: float     # degrees clockwise from north
    strike: float            # degrees, right-hand rule
    rms_m: float             # RMS distance of the points to the plane (metres)
    max_residual_m: float    # largest distance of a point to the plane
    residuals_m: List[float] = field(default_factory=list)

    @property
    def notation(self) -> str:
        """Dip / dip direction, e.g. '30/120'."""
        return f"{self.dip:02.0f}/{self.dip_direction:03.0f}"

    def elevation(self, x, y):
        """Plane elevation at (x, y), scalars or arrays in the fit frame (metres)."""
        return self.a * (np.asarray(x) - self.origin[0]) + self.b * (np.asarray(y) - self.origin[1]) + self.c


def fit_plane(points: Sequence[Tuple[float, float, float]]) -> PlaneOrientation:
    """
    Plane through ≥ 3 points (x, y, z) in metres (x east, y north).

    Raises ``DegeneratePoints`` with fewer than three points or when they
    are aligned in map view (their plane is vertical or undefined).
    """
    data = np.asarray(points, dtype=np.float64)
    if data.ndim != 2 or data.shape[0] < 3 or data.shape[1] != 3:
        raise DegeneratePoints("At least three points with x, y and z are needed.")
    origin = data[:, :2].mean(axis=0)
    xy = data[:, :2] - origin
    singular = np.linalg.svd(xy, compute_uv=False)
    if singular[0] <= 0 or singular[-1] / singular[0] < COLLINEAR_RATIO:
        raise DegeneratePoints("The points are aligned in map view: they do not define a plane.")
    design = np.column_stack([xy, np.ones(len(data))])
    (a, b, c), *_ = np.linalg.lstsq(design, data[:, 2], rcond=None)

    gradient = math.hypot(a, b)
    dip = math.degrees(math.atan(gradient))
    dip_direction = (math.degrees(math.atan2(-a, -b)) + 360.0) % 360.0 if gradient > 1e-12 else 0.0
    strike = (dip_direction - 90.0) % 360.0
    vertical = data[:, 2] - design @ np.array([a, b, c])
    distance = vertical / math.sqrt(1.0 + a * a + b * b)        # perpendicular to the plane
    return PlaneOrientation(
        a=float(a), b=float(b), c=float(c), origin=(float(origin[0]), float(origin[1])),
        dip=dip, dip_direction=dip_direction, strike=strike,
        rms_m=float(np.sqrt(np.mean(distance ** 2))),
        max_residual_m=float(np.max(np.abs(distance))),
        residuals_m=[float(v) for v in distance],
    )


def height_above_plane(dem: np.ndarray, x: np.ndarray, y: np.ndarray, plane: PlaneOrientation) -> np.ndarray:
    """``dem − plane`` on a grid whose cell centres are ``x`` (columns) and ``y`` (rows), metres.

    The zero contour of the result is the predicted outcrop trace; NaN where
    the DEM has no data.
    """
    xx, yy = np.meshgrid(np.asarray(x, dtype=np.float64), np.asarray(y, dtype=np.float64))
    return np.asarray(dem, dtype=np.float64) - plane.elevation(xx, yy)


def plane_reaches_surface(difference: np.ndarray) -> bool:
    """True when the plane is above the ground somewhere and below it elsewhere."""
    finite = difference[np.isfinite(difference)]
    return finite.size > 0 and finite.min() < 0.0 < finite.max()
