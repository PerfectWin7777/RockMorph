"""
core/indices/context.py

Pure-data description of one drainage basin, as consumed by index formulas.

The QGIS side (``core/basin_context_builder.py``) extracts these quantities
from layers; the formulas (``core/indices/formulas.py``) only read them. This
split is deliberate:

* formulas stay free of QGIS/GDAL so they can be unit-tested with synthetic
  basins whose answer is known analytically;
* the expensive extraction (flow routing, cross-profiles) runs once per basin
  and is shared by every index that needs it.

Conventions
-----------
* All lengths are in metres, areas in square metres, elevations in metres.
  Formulas convert to the unit declared in their ``IndexSpec`` (km, km²...).
* The main channel profile is ordered SOURCE -> OUTLET: index 0 is the
  channel head on the drainage divide, the last index is the basin outlet.
  ``distance_m`` is therefore the distance from the source, which is the
  ``L`` of Hack's (1973) stream length-gradient index.
* Missing pieces are ``None``. A formula that needs a missing piece returns
  ``None`` and the builder records why in ``BasinContext.warnings``.

Authors: RockMorph contributors / Tony
"""

from __future__ import annotations

from dataclasses import dataclass, field
from typing import Dict, List, Optional, Tuple

import numpy as np  # type: ignore


@dataclass
class ChannelProfile:
    """
    Longitudinal profile of the trunk (main) stream.

    distance_m      : cumulative distance from the source along the channel.
    elevation_m     : channel elevation at each node (hydrologically
                      conditioned DEM, so it never rises downstream).
    drainage_area_m2: upstream drainage area at each node.
    x, y            : node coordinates in the work CRS (metres).
    """

    distance_m: np.ndarray
    elevation_m: np.ndarray
    drainage_area_m2: np.ndarray
    x: np.ndarray
    y: np.ndarray

    @property
    def length_m(self) -> float:
        """Channel length Lc (source to outlet)."""
        return float(self.distance_m[-1]) if self.distance_m.size else 0.0

    @property
    def straight_length_m(self) -> float:
        """Straight-line distance between source and outlet (valley length Lv)."""
        if self.x.size < 2:
            return 0.0
        return float(np.hypot(self.x[-1] - self.x[0], self.y[-1] - self.y[0]))


@dataclass
class NetworkSummary:
    """
    Horton-Strahler description of the stream network inside the basin.

    A *stream of order u* follows Strahler (1952): the maximal run of channel
    of the same order, from where that order starts (channel head for u = 1,
    junction of two streams of order u - 1 otherwise) down to where it joins a
    stream of higher order or leaves the basin. Counting *streams* rather than
    *links* (segments between any two junctions) is what Horton's laws
    require; GSV-Studio counted vector segments, which inflates Nu for higher
    orders.

    stream_counts       : {order: number of streams Nu}
    stream_lengths_m    : {order: [length of each stream of order u]}
    basin_areas_m2      : {order: [total drainage area at the outlet of each
                           stream of order u]} (Schumm 1956 area ratio)
    total_length_m      : sum of all channel lengths
    link_count          : number of links (segments between junctions)
    max_order           : highest Strahler order in the basin
    source              : "dem" (derived by flow routing) or "vector"
    """

    stream_counts: Dict[int, int]
    stream_lengths_m: Dict[int, List[float]]
    basin_areas_m2: Dict[int, List[float]]
    total_length_m: float
    link_count: int
    max_order: int
    source: str = "dem"

    @property
    def stream_count(self) -> int:
        """Total number of streams of all orders (ΣNu)."""
        return int(sum(self.stream_counts.values()))


@dataclass
class TerrainWindow:
    """
    Pixel-level terrain data covering the basin.

    slope_rad         : local slope (Horn 1981) in radians.
    accumulation_cells: D8 flow accumulation (number of cells, self included).
    mask              : True inside the basin.
    cell_size_m       : square cell size.
    """

    slope_rad: np.ndarray
    accumulation_cells: np.ndarray
    mask: np.ndarray
    cell_size_m: float


@dataclass
class CrossProfile:
    """
    Valley cross-profile drawn perpendicular to the main channel.

    distance_m  : position along the profile, 0 at the left end.
    elevation_m : sampled elevation (bilinear interpolation), NaN outside DEM.
    channel_at_m: position of the channel on the profile.
    """

    distance_m: np.ndarray
    elevation_m: np.ndarray
    channel_at_m: float


@dataclass
class TransverseSection:
    """
    One section of Cox's (1994) transverse topographic symmetry factor.

    da_m          : distance from the basin midline to the active channel.
    dd_left_m     : distance from the midline to the divide, left of the axis.
    dd_right_m    : distance from the midline to the divide, right of the axis.
    channel_side  : +1 when the channel lies left of the axis, -1 right, 0 on it.
    shift_azimuth : azimuth (degrees from north) of the vector midline -> channel.
    """

    da_m: float
    dd_left_m: float
    dd_right_m: float
    channel_side: int
    shift_azimuth: float


@dataclass
class BasinContext:
    """
    Everything the formulas may need about one basin.

    Only ``basin_id`` and ``label`` are always set; the builder fills the
    other attributes according to the requirements of the selected indices.
    """

    basin_id: object
    label: str

    # -- geometry ("geometry") ---------------------------------------------
    area_m2: Optional[float] = None
    perimeter_m: Optional[float] = None
    length_m: Optional[float] = None        # basin length Lb
    width_m: Optional[float] = None         # basin width Bw
    length_method: str = ""                 # how Lb/Bw were obtained

    # -- elevation ("elevation") -------------------------------------------
    elevations: Optional[np.ndarray] = None  # valid DEM values in the basin
    cell_size_m: Optional[float] = None

    # -- other requirements --------------------------------------------------
    terrain: Optional[TerrainWindow] = None
    channel: Optional[ChannelProfile] = None
    network: Optional[NetworkSummary] = None
    right_bank_area_m2: Optional[float] = None
    sections: Optional[List[TransverseSection]] = None
    cross_profiles: Optional[List[CrossProfile]] = None

    # -- diagnostics ---------------------------------------------------------
    warnings: List[str] = field(default_factory=list)

    # Cached elevation statistics (computed lazily, see ``elevation_stats``).
    _stats: Optional[Tuple[float, float, float, float]] = None

    def elevation_stats(self) -> Optional[Tuple[float, float, float, float]]:
        """
        Return ``(minimum, maximum, mean, median)`` of the basin elevations,
        or None when no elevation is available. The median is exact
        (``np.median``); GSV-Studio approximated it with a 100-bin histogram.
        """
        if self._stats is not None:
            return self._stats
        if self.elevations is None or self.elevations.size == 0:
            return None
        values = self.elevations
        self._stats = (
            float(np.min(values)),
            float(np.max(values)),
            float(np.mean(values)),
            float(np.median(values)),
        )
        return self._stats
