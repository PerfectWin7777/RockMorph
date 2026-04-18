# tools/ncp/engine.py

"""
tools/ncp/engine.py

NCPEngine — Normalized Channel Profile analysis.

For each basin:
  1. Extract main river via MainRiverExtractor (core/hydro.py)
  2. Sample longitudinal profile via sample_river_profile (core/hydro.py)
  3. Normalize distance and elevation to [0, 1]
  4. Compute MaxC, dL, Concavity %

Inherits BaseEngine. Zero UI logic.

Authors: RockMorph contributors / Tony
"""

import numpy as np  # type: ignore
from qgis.core import QgsVectorLayer, QgsRasterLayer, QgsWkbTypes  # type: ignore
from PyQt5.QtCore import QCoreApplication  # type: ignore

from ...base.base_engine import BaseEngine
from ...core.hydro import MainRiverExtractor, sample_river_profile
from ...core.utils import smooth_data

def tr(message):
    return QCoreApplication.translate("RockMorph", message)






class NCPEngine(BaseEngine):
    """
    Computes Normalized Channel Profile metrics for all basins.

    compute() parameters
    --------------------
    dem_layer     : QgsRasterLayer
    basin_layer   : QgsVectorLayer  — polygon
    stream_layer  : QgsVectorLayer  — polyline network
    label_field   : str | None
    n_points      : int             — profile sample points (default 200)
    snap_dist_m   : float           — stream snapping tolerance (default 2.0)

    Returns
    -------
    dict with keys:
        results  : list of per-basin dicts
        skipped  : list of (fid, reason)
        warnings : list of str
    """

    def validate(self, **kwargs) -> bool:
        dem    = kwargs.get("dem_layer")
        basin  = kwargs.get("basin_layer")
        stream = kwargs.get("stream_layer")
        if dem    is None or not dem.isValid():    return False
        if basin  is None or not basin.isValid():  return False
        if stream is None or not stream.isValid(): return False
        if basin.geometryType()  != QgsWkbTypes.PolygonGeometry: return False
        if stream.geometryType() != QgsWkbTypes.LineGeometry:    return False
        return True

    def compute(self, **kwargs) -> dict:
        dem_layer    = kwargs["dem_layer"]
        basin_layer  = kwargs["basin_layer"]
        stream_layer = kwargs["stream_layer"]
        label_field  = kwargs.get("label_field",  None)
        n_points     = kwargs.get("n_points",      200)
        snap_dist_m  = kwargs.get("snap_dist_m",   2.0)
        smooth_window = kwargs.get("smooth", 0)
        progress_callback = kwargs.get("progress_callback")

        results  = []
        skipped  = []
        warnings = []

        # ── Step 1 : extract main rivers ─────────────────────────
        if progress_callback:
            progress_callback(5, tr("Extracting main river network..."))
        extractor = MainRiverExtractor(
            basin_layer  = basin_layer,
            stream_layer = stream_layer,
            dem_layer    = dem_layer,
            snap_dist_m  = snap_dist_m,
            label_field  = label_field,
        )
        extraction = extractor.extract_all()

        skipped.extend(extraction["skipped"])
        warnings.extend(extraction["warnings"])

        rivers = extraction["results"]
        total = len(rivers)

        # ── Step 2 : profile + metrics per basin ─────────────────
        for i, river in enumerate(rivers):
            fid   = river["fid"]
            label = river["label"]

            if progress_callback:
                    percent = 40 + int((i / total) * 60) # On commence à 40% après l'extraction
                    progress_callback(percent, tr(f"Analyzing river: {river['label']}"))

            try:
                profile = sample_river_profile(
                    river_geom  = river["geom"],
                    dem_layer   = dem_layer,
                    stream_crs  = stream_layer.crs(),
                    n_points    = n_points,
                )

                if not profile["valid"]:
                    skipped.append((fid, "insufficient valid profile points"))
                    warnings.append(tr(
                        f"Basin '{label}' (FID {fid}): "
                        f"profile has too few valid points — skipped."
                    ))
                    continue

                metrics = self._compute_metrics(profile, river, label, fid, smooth_window)
                results.append(metrics)

                


            except Exception as e:
                skipped.append((fid, str(e)))
                warnings.append(tr(
                    f"Basin '{label}' (FID {fid}): error — {e}"
                ))
                continue

        return {
            "results":  results,
            "skipped":  skipped,
            "warnings": warnings,
        }

    # ------------------------------------------------------------------
    # Metrics computation
    # ------------------------------------------------------------------

    def _compute_metrics(
        self,
        profile: dict,
        river:   dict,
        label:   str,
        fid:     int,
        smooth_window: int
    ) -> dict:
        """
        Normalize profile and compute MaxC, dL, Concavity %.

        Logic:
        - Source is at (0, 1). Mouth is at (1, 0).
        - Diagonal line: y = 1 - x.
        - Concavity (MaxC) is only measured BELOW the diagonal.

        Normalization
        -------------
        x_norm = (distance - d_min) / (d_max - d_min)   → [0, 1]
        y_norm = (elev     - e_min) / (e_max - e_min)   → [0, 1]

        Equilibrium diagonal : y = 1 - x  (straight line from origin to (0,1))

        Metrics
        -------
        deviation[i] = y_norm[i] - x_norm[i]
        MaxC  = max(deviation)
        dL    = x_norm at argmax(deviation)
        Concavity % = trapz(deviation, x_norm) / 0.5 * 100
                      (area between curve and diagonal / triangle area)
        """
        dist = profile["distances_m"]
        elev = profile["elevations"]
        
        # 1. Before normalization, apply smoothing if requested
        if smooth_window > 2:
            elev = smooth_data(elev, smooth_window)

        # ─── CRITICAL STEP: FORCE UPSTREAM TO DOWNSTREAM ───
        # In geomorphology, index 0 MUST be the Source (Highest point)
        if elev[0] < elev[-1]:
            print("flipping profile to ensure source-to-mouth order")
            # The profile was extracted in reverse (Mouth to Source)
            # We flip both arrays to restore scientific order
            elev = np.flip(elev)
            # For distances, we recalculate from the new start
            dist = dist[-1] - np.flip(dist) 

        
        d_min, d_max = dist[0],  dist[-1]
        e_min, e_max = elev.min(), elev.max()

        # Guard flat river
        if d_max - d_min < 1e-6:
            raise ValueError(tr("River has zero length."))
        if e_max - e_min < 1e-6:
            raise ValueError(tr("River has zero elevation range — flat profile."))

        # 2. Normalization
        # x: 0 (source) to 1 (mouth)
        # y: 1 (source) to 0 (mouth)
        x_norm = (dist - d_min) / (d_max - d_min)
        y_norm = (elev - e_min) / (e_max - e_min) 

        # 3. Compute deviation from diagonal (y_diag = 1 - x)
        # Positive values mean the curve is BELOW the diagonal (Concave)
        deviation = (1.0 - x_norm) - y_norm

        # 4. Extract MaxC and dL ONLY from concave sections
        concave_mask = deviation > 0
        
        if np.any(concave_mask):
            # Find the index of the maximum positive deviation
            indices = np.where(concave_mask)[0]
            sub_idx = np.argmax(deviation[concave_mask])
            idx_max = indices[sub_idx]
            
            maxC = float(deviation[idx_max])
            dL   = float(x_norm[idx_max])
            y_at_dL      = float(y_norm[idx_max])
            y_diag_at_dL = 1.0 - dL
        else:
            # Strictly convex profile: No MaxC/dL measurement
            maxC = dL = y_at_dL = y_diag_at_dL = None

        # 5. Global Concavity Percentage (Integral)
        # Area: positive if curve below diagonal, negative if above
        area      = float(np.trapz(deviation, x_norm))
        concavity = round((area / 0.5) * 100, 2)        # % signed

        # Convention: positive = curve BELOW diagonal (concave, mature)
        #             negative = curve ABOVE diagonal (convex, juvenile/uplifted)
    

        return {
            "label":        label,
            "fid":          fid,
            "maxC":         round(maxC, 4) if maxC is not None else None,
            "dL":           round(dL, 4)   if dL is not None else None,
            "y_at_dL":      round(y_at_dL, 4) if y_at_dL is not None else None,
            "y_diag_at_dL": round(y_diag_at_dL, 4) if y_diag_at_dL is not None else None,
            "concavity":    concavity,
            "x":            x_norm.tolist(),
            "y":            y_norm.tolist(),
            "length_m":     river["length_m"],
            "n_points":     profile["n_points"],
        }