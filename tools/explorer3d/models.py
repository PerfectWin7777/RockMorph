# tools/explorer3d/models.py

"""
Data models for the 3D Explorer tool in RockMorph.
Provides serializable structures for rasters, vectors, and scene controls.

Authors: RockMorph contributors
"""

from dataclasses import dataclass, asdict
from typing import List, Optional, Dict, Any


@dataclass
class ThreeDElement:
    """Base class for all elements rendered in the 3D scene."""
    element_id: str
    element_type: str

    def to_dict(self) -> Dict[str, Any]:
        """Convert the dataclass into a dictionary for JSON serialization."""
        return asdict(self)


@dataclass
class ThreeDRaster(ThreeDElement):
    """
    Represents a subsampled digital elevation model (DEM) grid
    or a draped geological map layer for WebGL rendering.
    """
    width: int
    height: int
    x_coords: List[float]  # Real-world projected X coordinates
    y_coords: List[float]  # Real-world projected Y coordinates
    z_values: List[List[float]]  # 2D grid matrix of elevations
    z_min: float
    z_max: float
    nodata_value: Optional[float] = None
    color_ramp: str = "terrain"


@dataclass
class ThreeDVector(ThreeDElement):
    """
    Represents a vector feature (stream, fault, or contour boundary)
    reprojected and projected onto the 3D terrain surface.
    """
    geom_type: str  # "line", "point", "polygon"
    vertices: List[List[float]]  # 3D points [[X1, Y1, Z1], [X2, Y2, Z2], ...]
    color: str = "#ffffff"
    label: str = ""
    extrude_depth: float = 0.0  # Used to build vertical planes (faults)
    attribute_values: Optional[List[float]] = None  # Quantities along vertices (e.g., k_sn)


@dataclass
class ThreeDLight(ThreeDElement):
    """
    Represents a dynamic light source in the scene (point, spot, or directional).
    """
    light_type: str  # "directional", "point", "spot"
    color: str = "#ffffff"
    intensity: float = 1.0
    position: Optional[List[float]] = None  # [X, Y, Z]
    focal_point: Optional[List[float]] = None  # [X, Y, Z] for spot or directional