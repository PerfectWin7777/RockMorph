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

        results  = []
        skipped  = []
        warnings = []

        # ── Step 1 : extract main rivers ─────────────────────────
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

        # ── Step 2 : profile + metrics per basin ─────────────────
        for river in extraction["results"]:
            fid   = river["fid"]
            label = river["label"]

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

                metrics = self._compute_metrics(profile, river, label, fid)
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
    ) -> dict:
        """
        Normalize profile and compute MaxC, dL, Concavity %.

        Normalization
        -------------
        x_norm = (distance - d_min) / (d_max - d_min)   → [0, 1]
        y_norm = (elev     - e_min) / (e_max - e_min)   → [0, 1]

        Equilibrium diagonal : y = x  (straight line from origin to (1,1))

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

        d_min, d_max = dist[0],  dist[-1]
        e_min, e_max = elev.min(), elev.max()

        # Guard flat river
        if d_max - d_min < 1e-6:
            raise ValueError(tr("River has zero length."))
        if e_max - e_min < 1e-6:
            raise ValueError(tr("River has zero elevation range — flat profile."))

        x_norm = (dist - d_min) / (d_max - d_min)
        # Invert y-axis so concave profiles have positive deviation from diagonal
        y_norm = 1.0 - (elev - e_min) / (e_max - e_min)

        # Diagonal goes from (0,1) to (1,0) → equation: y_diag = 1 - x
        deviation = (1.0 - x_norm) - y_norm

        # MaxC and dL
        idx_max   = int(np.argmax(np.abs(deviation)))   # max absolute deviation
        maxC      = float(deviation[idx_max])            # signed — negative if above
        dL        = float(x_norm[idx_max])

        y_at_dL      = float(y_norm[idx_max])
        y_diag_at_dL = 1.0 - dL


        # Area: positive if curve below diagonal, negative if above
        area      = float(np.trapz(deviation, x_norm))
        concavity = round((area / 0.5) * 100, 2)        # % signed

        # Convention: positive = curve BELOW diagonal (concave, mature)
        #             negative = curve ABOVE diagonal (convex, juvenile/uplifted)

        # Concavity % — area between curve and diagonal / 0.5
        area        = float(np.trapz(deviation, x_norm))
        concavity   = round((area / 0.5) * 100, 2)

        return {
            "label":       label,
            "fid":         fid,
            "maxC":        round(maxC,  4),
            "dL":          round(dL,    4),
            "y_at_dL":     round(y_at_dL, 4),      # Point on the curve
            "y_diag_at_dL": round(y_diag_at_dL, 4), # Point on the diagonal
            "concavity":   concavity,
            "x":           x_norm.tolist(),
            "y":           y_norm.tolist(),
            "length_m":    river["length_m"],
            "n_points":    profile["n_points"],
        }