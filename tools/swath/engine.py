# tools/swath/engine.py

"""


SwathEngine — orchestrates RasterReader + SwathSampler
to produce a clean data dict for the Swath panel.
Inherits BaseEngine.
"""

from qgis.core import QgsRasterLayer, QgsVectorLayer # type: ignore
from PyQt5.QtCore import QCoreApplication # type: ignore

from ...base.base_engine import BaseEngine
from ...core.raster import RasterReader
from ...core.sampling import SwathSampler
from ...core.utils import smooth_data, reorient_profile_high_to_low
import math
import numpy as np # type: ignore

def tr(message):
    return QCoreApplication.translate("RockMorph", message)


class SwathEngine(BaseEngine):
    """
    Computes a swath profile from a DEM and a line vector layer.

    compute() parameters
    --------------------
    dem_layer      : QgsRasterLayer  — input DEM
    line_layer     : QgsVectorLayer  — profile line
    n_stations     : int             — sample points along line (50-500)
    width_m        : float           — swath half-width in metres
    n_transversal  : int             — sample points per transversal (20-100)
    compute_q      : bool            — compute Q1/Q3 envelopes
    compute_relief : bool            — compute local relief profile
    compute_hyps   : bool            — compute transversal hypsometry

    Returns
    -------
    dict with keys:
        distances       : list[float]
        mean            : list[float]
        min             : list[float]
        max             : list[float]
        q1              : list[float] | None
        q3              : list[float] | None
        relief          : list[float] | None
        hyps            : list[float] | None
        total_length_m  : float
        width_m         : float
        n_stations      : int
        dem_name        : str
        line_name       : str
        crs_is_geographic : bool
    """

    def validate(self, **kwargs) -> bool:
        dem  = kwargs.get("dem_layer")
        line = kwargs.get("line_layer")

        if dem is None or not isinstance(dem, QgsRasterLayer):
            return False
        if not dem.isValid():
            return False
        if line is None or not isinstance(line, QgsVectorLayer):
            return False
        if not line.isValid():
            return False
        if line.featureCount() == 0:
            return False

        return True

    def compute(self, **kwargs) -> dict:
        """
        Full swath pipeline:
        1. Open DEM via RasterReader
        2. Run SwathSampler
        3. Return enriched data dict
        """
        dem_layer         = kwargs["dem_layer"]
        line_layer        = kwargs["line_layer"]
        n_stations        = kwargs.get("n_stations",     200)
        width_m           = kwargs.get("width_m",        1000.0)
        n_transversal     = kwargs.get("n_transversal",  50)
        force_high_to_low = kwargs.get("force_high_to_low", False)
        smooth_win        = kwargs.get("smooth_window", 0)
        compute_q         = kwargs.get("compute_q",      False)
        compute_relief    = kwargs.get("compute_relief", True)
        compute_hyps      = kwargs.get("compute_hyps",   True)

        # Step 1 — Open DEM
        try:
            reader = RasterReader(dem_layer)
        except Exception as e:
            raise RuntimeError(tr(f"Failed to open DEM: {e}"))

        # Step 2 — Run sampler
        sampler = SwathSampler(
            reader        = reader,
            line_layer    = line_layer,
            n_stations    = n_stations,
            width_m       = width_m,
            n_transversal = n_transversal,
            compute_q     = compute_q,
            compute_relief= compute_relief,
            compute_hyps  = compute_hyps,
        )

        try:
            data = sampler.sample()

            # --- NEW: Reorientation logic ---
            if force_high_to_low:
                # We package profiles for the utility
                profiles_to_flip = {
                    "mean": data["mean"], "min": data["min"], "max": data["max"],
                    "q1": data.get("q1"), "q3": data.get("q3"),
                    "relief": data.get("relief"), "hyps": data.get("hyps")
                }
                new_dist, new_profs = reorient_profile_high_to_low(data["distances"], profiles_to_flip)
                
                # Update data dict
                data["distances"] = new_dist
                data.update(new_profs)
            # --------------------------------

            
            if smooth_win >= 3:
                # apply smoothing to all profiles that exist in the data dict
                keys_to_smooth = ["mean", "min", "max", "q1", "q3", "relief", "hyps"]
                
                for key in keys_to_smooth:
                    if data.get(key) is not None:
                        # Convert to numpy array for smoothing, then back to list
                        # plotly waits for lists/json, but numpy is faster for convolution operations
                        arr = np.array(data[key], dtype=np.float32)
                        smoothed_arr = smooth_data(arr, smooth_win)
                        data[key] = smoothed_arr.tolist() 

            # Auto tick intervals
            total_m   = data["total_length_m"]
            x_dtick   = self._nice_interval(total_m / 8)

            valid_mean = [v for v in data["mean"] if v is not None]
            if valid_mean:
                elev_range = max(valid_mean) - min(valid_mean)
                y_dtick    = self._nice_interval(elev_range / 6)
            else:
                y_dtick = 100

            data["x_dtick"] = x_dtick
            data["y_dtick"] = y_dtick

        except Exception as e:
            raise RuntimeError(tr(f"Swath sampling failed: {e}"))

        # Step 3 — Enrich with metadata
        data["dem_name"]           = dem_layer.name()
        data["line_name"]          = line_layer.name()
        data["crs_is_geographic"]  = reader.is_geographic
        data["crs_name"]           = reader.crs.authid()

        return data
    
    @staticmethod
    def _nice_interval(value: float) -> float:
        """
        Round value to a 'nice' number for axis ticks.
        e.g. 23456 → 5000, 187 → 50, 4500 → 1000
        """
       
        if value <= 0:
            return 1.0
        magnitude = 10 ** math.floor(math.log10(value))
        normalized = value / magnitude
        if normalized < 1.5:
            nice = 1
        elif normalized < 3:
            nice = 2
        elif normalized < 7:
            nice = 5
        else:
            nice = 10
        return nice * magnitude
