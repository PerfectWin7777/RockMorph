# tools/fluvial/engine.py

"""
tools/fluvial/engine.py

FluvialEngine — Fluvial geomorphology analysis.

For each basin:
  1. Extract main river via MainRiverExtractor      (core/hydro.py — reused)
  2. Sample hydraulic profile via sample_river_hydraulics (core/hydro.py — extended)
  3. Compute or load Flow Accumulation raster       (GRASS r.watershed — cached)
  4. Compute SL index and SLk normalized index      (Hack 1973)
  5. Compute chi transformation                     (Perron & Royden 2013)
  6. Compute k_sn via log S / log A regression      (Kirby & Whipple 2001)
  7. Detect knickpoints via segmented regression    (Bai & Perron simplified)
  8. Compute equilibrium profile                    (Hack 1973 / Mvondo Owono 2010)

Design rules
------------
- Zero UI logic.
- FAC raster cached per DEM path — GRASS runs once per session.
- m/n (theta_ref) is a runtime parameter — k_sn and chi recompute without
  re-extracting rivers or re-running GRASS.
- All results are pure Python dicts with numpy arrays serialized to lists.

Authors: RockMorph contributors / Tony
"""

import math
import numpy as np  # type: ignore
from qgis.core import (  # type: ignore
    QgsVectorLayer, QgsRasterLayer, QgsWkbTypes,
    QgsRasterLayer, QgsProject,
)
from PyQt5.QtCore import QCoreApplication  # type: ignore

from ...base.base_engine import BaseEngine
from ...core.hydro import (
    MainRiverExtractor,
    sample_river_hydraulics,
    sample_river_native_pixels
)
from ...core.utils import smooth_data


def tr(message):
    return QCoreApplication.translate("RockMorph", message)


# ---------------------------------------------------------------------------
# Constants
# ---------------------------------------------------------------------------

THETA_REF_DEFAULT = 0.45   # reference concavity index (Whipple 2004)
A0_DEFAULT        = 1.0    # reference drainage area m² (standard)
MIN_SEGMENT_PTS   = 8      # minimum points per regression segment
MAX_KNICKPOINTS   = 5      # maximum knickpoints to detect per river


# ---------------------------------------------------------------------------
# FluvialEngine
# ---------------------------------------------------------------------------

class FluvialEngine(BaseEngine):
    """
    Computes fluvial geomorphology metrics for all basins.

    compute() parameters
    --------------------
    dem_layer      : QgsRasterLayer
    basin_layer    : QgsVectorLayer  — polygon
    stream_layer   : QgsVectorLayer  — polyline network
    fac_layer      : QgsRasterLayer | None  — flow accumulation (auto if None)
    label_field    : str | None
    n_points       : int    — profile sample points (default 200)
    snap_dist_m    : float  — stream snapping tolerance (default 2.0)
    theta_ref      : float  — reference concavity m/n (default 0.45)
    a0             : float  — reference drainage area m² (default 1.0)
    sl_window_m    : float  — SL moving window in metres (default 500.0)
    n_knickpoints  : int    — max knickpoints to detect (default 3)
    smooth         : int    — elevation smoothing window (default 0)

    Returns
    -------
    dict:
        results  : list of per-basin dicts
        skipped  : list of (fid, reason)
        warnings : list of str
        fac_auto : bool — True if FAC was computed automatically
    """

    # Class-level FAC cache — persists across compute() calls in same session
    _fac_cache: dict = {}

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
        fac_layer    = kwargs.get("fac_layer",     None)
        label_field  = kwargs.get("label_field",   None)
        n_points     = kwargs.get("n_points",       200)
        snap_dist_m  = kwargs.get("snap_dist_m",    2.0)
        theta_ref    = kwargs.get("theta_ref",      THETA_REF_DEFAULT)
        a0           = kwargs.get("a0",             A0_DEFAULT)
        sl_window_m  = kwargs.get("sl_window_m",    500.0)
        n_knick      = kwargs.get("n_knickpoints",  3)
        smooth_win   = kwargs.get("smooth",         0)
        progress_cb  = kwargs.get("progress_callback")

        results  = []
        skipped  = []
        warnings = []
        fac_auto = False

        # ── Step 1 : FAC raster — load or compute ────────────────────
        if progress_cb:
            progress_cb(5, tr("Preparing flow accumulation raster..."))

        fac_layer, fac_auto, fac_warn = self._get_or_compute_fac(
            dem_layer, fac_layer, progress_cb
        )
        if fac_warn:
            warnings.append(fac_warn)

        if fac_layer is None:
            return {
                "results":  [],
                "skipped":  [],
                "warnings": [tr("Flow accumulation unavailable — cannot proceed.")],
                "fac_auto": fac_auto,
            }

        # ── Step 2 : extract main rivers ─────────────────────────────
        if progress_cb:
            progress_cb(20, tr("Extracting main river network..."))

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
        total  = max(len(rivers), 1)

        # ── Step 3 : metrics per basin ────────────────────────────────
        for i, river in enumerate(rivers):
            fid   = river["fid"]
            label = river["label"]

            if progress_cb:
                pct = 40 + int((i / total) * 55)
                progress_cb(pct, tr(f"Analyzing basin: {label}"))

            try:
                # Sample hydraulic profile (DEM + FAC)
                profile = sample_river_native_pixels(
                    river_geom = river["geom"],
                    dem_layer  = dem_layer,
                    fac_layer  = fac_layer,
                    stream_crs = stream_layer.crs(),
                )

                if not profile["valid"]:
                    skipped.append((fid, "insufficient valid profile points"))
                    warnings.append(tr(
                        f"Basin '{label}' (FID {fid}): "
                        f"profile has too few valid points — skipped."
                    ))
                    continue

                if not profile["fac_valid"]:
                    warnings.append(tr(
                        f"Basin '{label}' (FID {fid}): "
                        f"FAC sampling partially failed — results may be inaccurate."
                    ))

                metrics = self._compute_metrics(
                    profile      = profile,
                    river        = river,
                    label        = label,
                    fid          = fid,
                    theta_ref    = theta_ref,
                    a0           = a0,
                    sl_window_m  = sl_window_m,
                    n_knick      = n_knick,
                    smooth_win   = smooth_win,
                )
                results.append(metrics)

            except Exception as e:
                import traceback
                traceback.print_exc()
                skipped.append((fid, str(e)))
                warnings.append(tr(f"Basin '{label}' (FID {fid}): error — {e}"))

        if progress_cb:
            progress_cb(100, tr("Done."))

        return {
            "results":  results,
            "skipped":  skipped,
            "warnings": warnings,
            "fac_auto": fac_auto,
        }

    # ------------------------------------------------------------------
    # Metrics — orchestrates all sub-computations
    # ------------------------------------------------------------------

    def _compute_metrics(
        self,
        profile:     dict,
        river:       dict,
        label:       str,
        fid:         int,
        theta_ref:   float,
        a0:          float,
        sl_window_m: float,
        n_knick:     int,
        smooth_win:  int,
    ) -> dict:

        dist  = profile["distances_m"].copy()
        elev  = profile["elevations"].copy()
        area  = profile["area_m2"].copy()
        slope = profile["slope_local"].copy()


        # --- CRITICAL STEP: FORCE SOURCE-TO-MOUTH DIRECTION ---
        # In geomorphology, index 0 MUST be the Source (Highest point)
        if elev[0] < elev[-1]:
            print("flipping profile to ensure source-to-mouth order")
            # The profile is currently Mouth -> Source, we must flip it
            elev  = np.flip(elev)
            area  = np.flip(area)
            # Distances must be recalculated to start at 0 from the new start (source)
            total_len = dist[-1]
            dist = total_len - np.flip(dist)
            # Re-estimate slope on flipped profile later or flip it now
            slope = np.flip(slope) 
        # ------------------------------------------------------


        # Optional smoothing on elevation only
        if smooth_win > 2:
            elev = smooth_data(elev, smooth_win)
            # Recompute slope after smoothing — centred differences
            slope = self._recompute_slope(elev, dist)

        total_length_m = profile["total_length_m"]

        # ── SL and SLk ───────────────────────────────────────────────
        sl, slk = self._compute_sl_slk(dist, elev, total_length_m, sl_window_m)

        # ── Chi transformation ───────────────────────────────────────
        chi = self._compute_chi(dist, area, theta_ref, a0)

        # ── k_sn via log S / log A (Kirby & Whipple 2001) ────────────
        ksn_profile, theta_local = self._compute_ksn_loglog(
            slope, area, theta_ref
        )

        # ── Knickpoint detection — segmented regression on chi-plot ──
        knickpoints = self._detect_knickpoints(chi, elev, n_knick)

        # ── k_sn per segment (between knickpoints) ───────────────────
        ksn_segments = self._compute_ksn_segments(
            chi, elev, slope, area, theta_ref, knickpoints
        )

        # ── Equilibrium profile (Hack 1973 / Mvondo Owono 2010) ──────
        equil = self._compute_equilibrium_hack(dist, elev)


        chi_plot    = chi[::-1]          # 0 → chi_max
        elev_plot   = elev[::-1]         # Z_mouth → Z_source
        ksn_plot    = ksn_profile[::-1]
        equil_plot  = equil[::-1]

        # Knickpoints — leurs indices doivent être recalculés
        n = len(chi)
        for kp in knickpoints:
            kp["idx_plot"] = n - 1 - kp["idx"]
            kp["chi"]      = float(chi_plot[kp["idx_plot"]])

        return {
            # Identity
            "label":         label,
            "fid":           fid,
            "length_m":      river["length_m"],
            "length_km":     round(river["length_m"] / 1000, 3),
            "n_points":      profile["n_points"],

            # Raw arrays — serialized to lists for JSON/JS transfer
            "distances_m":   self._sanitize_list(dist),
            "elevations":    self._sanitize_list(elev),
            "area_m2":       self._sanitize_list(area),
            "slope_local":   self._sanitize_list(slope),

            # SL / SLk
            "sl":            self._sanitize_list(sl),
            "slk":           self._sanitize_list(slk),
            "sl_max":        round(float(np.nanmax(sl)), 4) if np.any(~np.isnan(sl)) else 0,
            "slk_max":       round(float(np.nanmax(slk)), 4) if np.any(~np.isnan(slk)) else 0,

            # Chi
            "chi":           self._sanitize_list(chi_plot),
            "elev_chi":      self._sanitize_list(elev_plot),
            "chi_max":       round(float(np.nanmax(chi_plot)), 4) if np.any(~np.isnan(chi_plot)) else 0,

            # k_sn
            "ksn_profile":   self._sanitize_list(ksn_plot),
            "ksn_mean":      round(float(np.nanmean(ksn_plot)), 2) if np.any(~np.isnan(ksn_plot)) else 0,
            "ksn_max":       round(float(np.nanmax(ksn_plot)), 2) if np.any(~np.isnan(ksn_plot)) else 0,
            "theta_local":   round(float(np.nanmean(theta_local)), 4) if np.any(~np.isnan(theta_local)) else 0,
            "ksn_segments":  ksn_segments,   # list of segment dicts

            # Knickpoints
            "knickpoints":   knickpoints,    # list of {chi, dist_m, elev, ksn}

            # Equilibrium profile
            "equil_elev":    self._sanitize_list(equil),

            # Parameters used — stored for reproducibility
            "theta_ref":     theta_ref,
            "a0":            a0,
        }

    # ------------------------------------------------------------------
    # SL and SLk  (Hack 1973)
    # ------------------------------------------------------------------

    def _compute_sl_slk(
        self,
        dist:            np.ndarray,
        elev:            np.ndarray,
        total_length_m:  float,
        window_m:        float,
    ) -> tuple:
        """
        SL = (dz/dl) · L    (Hack 1973)

        Implementation:
        2. Compute local slope via centred finite differences at each point.
        3. SL[i] = slope[i] * dist[i]
        4. Keep all values — NO spike filter, NO zeroing.
            Negative values (counter-slope) set to 0.

        SLk = SL / k,  k = H_total / ln(L_total)
        """
        n = len(dist)

       
        # ── Step 2 : local slope — centred finite differences ────────────
        sl = np.zeros(n, dtype=np.float64)

        for i in range(1, n - 1):
            dz = elev[i - 1] - elev[i + 1]   # drop = positive for normal river
            dl = dist[i + 1]   - dist[i - 1]

            if dl < 1e-6:
                continue

            slope = dz / dl
            val   = slope * dist[i]
            sl[i] = val
            # sl[i] = max(val, 0.0)   # negative = counter-slope → 0, not filtered

        # Edges — one-sided
        if dist[1] - dist[0] > 1e-6:
            dz     = elev[0] - elev[1]
            dl     = dist[1] - dist[0]
            sl[0]  = (dz / dl) * dist[0]  

        if dist[-1] - dist[-2] > 1e-6:
            dz      = elev[-2] - elev[-1]
            dl      = dist[-1] - dist[-2]
            sl[-1]  = (dz / dl) * dist[-1]

        # ── Step 3 : SLk normalisation ────────────────────────────────────
        H_total = abs(float(elev[0] - elev[-1]))
        if total_length_m > 1.0 and H_total > 0:
            k   = H_total / math.log(total_length_m)
            slk = sl / max(k, 1e-10)
        else:
            slk = sl.copy()

        return sl, slk

    
    # ------------------------------------------------------------------
    # Chi transformation  (Perron & Royden 2013)
    # ------------------------------------------------------------------

    def _compute_chi(
        self,
        dist:      np.ndarray,
        area_m2:   np.ndarray,
        theta_ref: float,
        a0:        float,
    ) -> np.ndarray:
        """
        χ(x) = ∫[mouth→x] (A0 / A(x'))^theta_ref  dx'

        Integration from mouth toward source using trapezoidal rule.
        Profile is ordered source→mouth, so we integrate on the
        reversed arrays and flip back.

        Returns chi[] ordered source→mouth.
        chi[0] = chi_max (source), chi[-1] ≈ 0 (mouth).
        """
        # Reverse: work mouth → source
        dist_rev = dist[-1] - dist[::-1]   # starts at 0 at mouth
        area_rev = area_m2[::-1]

        integrand = (a0 / area_rev) ** theta_ref

        chi_rev = np.zeros(len(dist_rev))
        for i in range(1, len(dist_rev)):
            chi_rev[i] = chi_rev[i - 1] + np.trapz(
                integrand[i - 1 : i + 1],
                dist_rev[i - 1 : i + 1],
            )

        # Flip back to source→mouth order
        return chi_rev # mouth→source, 0 at mouth, max at source

    # ------------------------------------------------------------------
    # k_sn via log S / log A  (Kirby & Whipple 2001)
    # ------------------------------------------------------------------

    def _compute_ksn_loglog(
        self,
        slope:     np.ndarray,
        area_m2:   np.ndarray,
        theta_ref: float,
    ) -> tuple:
        """
        Point-wise k_sn estimate using a sliding window in log S / log A space.

        For each window:
            log S = log Ks  -  θ · log A   (linear regression)
            Ks    = 10^intercept
            A_cent = 10^((log Amax + log Amin) / 2)
            k_sn  = Ks · A_cent^(theta_ref - |θ_local|)

        Returns
        -------
        ksn_profile  : np.ndarray — k_sn at each point
        theta_local  : np.ndarray — local concavity at each point
        """
        n           = len(slope)
        ksn_profile = np.full(n, np.nan)
        theta_local = np.full(n, np.nan)

        # Window: ~10% of profile length, minimum MIN_SEGMENT_PTS
        half_win = max(MIN_SEGMENT_PTS, n // 20)

        log_A = np.log10(np.where(area_m2 > 0, area_m2, 1e-6))
        log_S = np.log10(np.where(slope   > 0, slope,   1e-10))

        for i in range(half_win, n - half_win):
            i0 = i - half_win
            i1 = i + half_win

            lA = log_A[i0:i1]
            lS = log_S[i0:i1]

            # Need variation in log A to fit a line
            if np.ptp(lA) < 0.1:
                continue

            try:
                # polyfit returns [slope, intercept]
                # slope here = -theta_local (negative in theory)
                coeffs   = np.polyfit(lA, lS, 1)
                th_local = abs(coeffs[0])     # take absolute value
                log_Ks   = coeffs[1]
                # log_Ks = np.clip(coeffs[1], -20, 20) # avoid overflow in 10^intercept 20
                Ks       = 10 ** log_Ks

                A_max  = area_m2[i0:i1].max()
                A_min  = area_m2[i0:i1].min()
                if A_min <= 0:
                    continue

                A_cent = 10 ** (
                    (math.log10(A_max) + math.log10(A_min)) / 2.0
                )

                ksn_profile[i] = Ks * (A_cent ** (theta_ref - th_local))
                ksn_profile = np.where(
                    (ksn_profile > 0) & (ksn_profile < 5000),
                    ksn_profile,
                    np.nan
                )

                theta_local[i] = th_local

            except (np.linalg.LinAlgError, ValueError):
                continue

        return ksn_profile, theta_local

    # ------------------------------------------------------------------
    # Knickpoint detection — segmented regression on chi-plot
    # ------------------------------------------------------------------

    def _detect_knickpoints(
        self,
        chi:    np.ndarray,
        elev:   np.ndarray,
        n_knick: int,
    ) -> list:
        """
        Detects up to n_knick knickpoints by iteratively finding the
        breakpoint that minimises total residual sum of squares (RSS)
        in the chi vs elevation space.

        For each candidate breakpoint b:
            Fit line on chi[:b] vs elev[:b]
            Fit line on chi[b:] vs elev[b:]
            RSS(b) = RSS_left + RSS_right

        The best b minimises RSS(b).
        Then recurse on the two sub-segments.

        Returns list of dicts sorted by chi:
            {idx, chi_val, dist_m, elev_m}
        """
        valid = ~np.isnan(chi) & ~np.isnan(elev)
        chi_v = chi[valid]
        elev_v = elev[valid]
        dist_v = np.linspace(0, 1, len(chi_v))  # placeholder index

        breakpoints = []
        self._recursive_breakpoints(
            chi_v, elev_v, 0, len(chi_v),
            n_knick, breakpoints
        )

        # Convert indices back to original arrays
        valid_idx = np.where(valid)[0]
        results   = []
        for b in sorted(set(breakpoints)):
            orig_idx = valid_idx[b]
            results.append({
                "idx":    int(orig_idx),
                "chi":    round(float(chi[orig_idx]),  4),
                "dist_m": 0.0,   # filled by panel from distances_m[idx]
                "elev_m": round(float(elev[orig_idx]), 2),
            })

        return results[:n_knick]

    def _recursive_breakpoints(
        self,
        chi:    np.ndarray,
        elev:   np.ndarray,
        i_start: int,
        i_end:   int,
        remaining: int,
        out:    list,
    ):
        """Recursive bisection — finds best breakpoint in [i_start, i_end]."""
        if remaining <= 0:
            return
        seg_len = i_end - i_start
        if seg_len < 2 * MIN_SEGMENT_PTS:
            return

        best_rss = np.inf
        best_b   = -1

        for b in range(i_start + MIN_SEGMENT_PTS, i_end - MIN_SEGMENT_PTS):
            rss = self._segment_rss(chi, elev, i_start, b) + \
                  self._segment_rss(chi, elev, b, i_end)
            if rss < best_rss:
                best_rss = rss
                best_b   = b

        if best_b == -1:
            return

        out.append(best_b)
        self._recursive_breakpoints(chi, elev, i_start, best_b,
                                    remaining - 1, out)
        self._recursive_breakpoints(chi, elev, best_b,  i_end,
                                    remaining - 1, out)

    @staticmethod
    def _segment_rss(
        chi:     np.ndarray,
        elev:    np.ndarray,
        i_start: int,
        i_end:   int,
    ) -> float:
        """RSS of a linear fit on chi[i_start:i_end] vs elev[i_start:i_end]."""
        x = chi[i_start:i_end]
        y = elev[i_start:i_end]
        if len(x) < 2 or np.ptp(x) < 1e-10:
            return np.inf
        coeffs  = np.polyfit(x, y, 1)
        y_pred  = np.polyval(coeffs, x)
        return float(np.sum((y - y_pred) ** 2))

    # ------------------------------------------------------------------
    # k_sn per segment (between knickpoints)
    # ------------------------------------------------------------------

    def _compute_ksn_segments(
        self,
        chi:         np.ndarray,
        elev:        np.ndarray,
        slope:       np.ndarray,
        area_m2:     np.ndarray,
        theta_ref:   float,
        knickpoints: list,
    ) -> list:
        """
        Computes one k_sn value per segment defined by knickpoints.
        Uses log S / log A regression on each segment.

        Returns list of dicts:
            {chi_start, chi_end, ksn, theta_local, label}
        """
        n = len(chi)

        # Build segment boundaries from knickpoint indices
        boundaries = [0] + [kp["idx"] for kp in knickpoints] + [n - 1]
        boundaries = sorted(set(boundaries))

        segments = []
        for j in range(len(boundaries) - 1):
            i0 = boundaries[j]
            i1 = boundaries[j + 1]
            if i1 - i0 < MIN_SEGMENT_PTS:
                continue

            sl  = slope[i0:i1]
            ar  = area_m2[i0:i1]

            log_A = np.log10(np.where(ar  > 0,    ar,   1e-6))
            log_S = np.log10(np.where(sl  > 1e-8, sl,  1e-10))

            if np.ptp(log_A) < 0.1:
                continue

            try:
                coeffs   = np.polyfit(log_A, log_S, 1)
                th_local = abs(coeffs[0])
                Ks       = 10 ** coeffs[1]
                A_max    = ar.max()
                A_min    = max(ar.min(), 1e-6)
                A_cent   = 10 ** (
                    (math.log10(A_max) + math.log10(A_min)) / 2.0
                )
                ksn_seg = Ks * (A_cent ** (theta_ref - th_local))
            except (np.linalg.LinAlgError, ValueError):
                continue

            segments.append({
                "chi_start":   round(float(chi[i0]),  4),
                "chi_end":     round(float(chi[i1]),  4),
                "elev_start":  round(float(elev[i0]), 2),
                "elev_end":    round(float(elev[i1]), 2),
                "ksn":         round(float(ksn_seg),  2),
                "theta_local": round(float(th_local), 4),
            })

        return segments

    # ------------------------------------------------------------------
    # Equilibrium profile  (Hack 1973 / Mvondo Owono 2010)
    # ------------------------------------------------------------------

    def _compute_equilibrium_hack(
        self,
        dist: np.ndarray,
        elev: np.ndarray,
    ) -> np.ndarray:
        """
        AN = AH - [((AH - AL) / (log Lm - log Lm)) * (log Li - log Lm)]

        Where:
            AH, AL = max and min elevation
            Lm     = max distance (mouth)
            Lm_min = min distance > 0 (source, avoid log(0))
            Li     = distance at point i

        Returns array of equilibrium elevations, same length as dist.
        """
        AH = float(elev.max())
        AL = float(elev.min())

        # Avoid log(0) — use first non-zero distance as source anchor
        dist_safe = np.where(dist > 0, dist, dist[dist > 0].min()
                             if np.any(dist > 0) else 1.0)

        log_Lm  = math.log10(float(dist_safe[-1]))   # log of max distance
        log_Lmin = math.log10(float(dist_safe[dist_safe > 0].min()))

        denom = log_Lm - log_Lmin
        if abs(denom) < 1e-10:
            # Degenerate case — return flat line at mean elevation
            return np.full(len(dist), (AH + AL) / 2.0)

        log_Li = np.log10(dist_safe)

        equil = AH - ((AH - AL) / denom) * (log_Li - log_Lmin)

        return equil

    # ------------------------------------------------------------------
    # Slope recomputation after smoothing
    # ------------------------------------------------------------------

    @staticmethod
    def _recompute_slope(
        elev: np.ndarray,
        dist: np.ndarray,
    ) -> np.ndarray:
        """Centred finite differences on smoothed elevation profile."""
        n     = len(elev)
        slope = np.empty(n, dtype=np.float64)

        dz = elev[2:] - elev[:-2]
        dl = dist[2:] - dist[:-2]
        slope[1:-1] = np.where(dl > 1e-6, np.abs(dz / dl), 1e-8)

        # Edges
        dl0 = dist[1] - dist[0]
        slope[0] = abs(elev[1] - elev[0]) / dl0 if dl0 > 1e-6 else 1e-8

        dl1 = dist[-1] - dist[-2]
        slope[-1] = abs(elev[-1] - elev[-2]) / dl1 if dl1 > 1e-6 else 1e-8

        return np.where(slope > 1e-8, slope, 1e-8)

    # ------------------------------------------------------------------
    # FAC — load from cache or compute via GRASS
    # ------------------------------------------------------------------

    def _get_or_compute_fac(
        self,
        dem_layer:   QgsRasterLayer,
        fac_layer:   QgsRasterLayer | None,
        progress_cb,
    ) -> tuple:
        """
        Returns (fac_layer, was_auto_computed, warning_str).
        Caches result per DEM source path.
        """
        # User provided a valid FAC — use it directly
        if fac_layer is not None and fac_layer.isValid():
            return fac_layer, False, None

        dem_path = dem_layer.source()

        # Cache hit
        if dem_path in self._fac_cache:
            return self._fac_cache[dem_path], True, None

        # Compute via GRASS r.watershed
        if progress_cb:
            progress_cb(10, tr("Computing flow accumulation (GRASS r.watershed)..."))

        try:
            import processing  # type: ignore
            result = processing.run(
                "grass7:r.watershed",
                {
                    "elevation":    dem_layer,
                    "accumulation": "TEMPORARY_OUTPUT",
                    "-a":           True,   # absolute accumulation in m²
                    "GRASS_REGION_PARAMETER": None,
                    "GRASS_REGION_CELLSIZE_PARAMETER": 0,
                },
            )
            fac = result.get("accumulation")
            if fac is None or (
                isinstance(fac, QgsRasterLayer) and not fac.isValid()
            ):
                raise RuntimeError("r.watershed returned invalid layer")

            # Wrap path in QgsRasterLayer if processing returned a string
            if isinstance(fac, str):
                fac = QgsRasterLayer(fac, "fac_auto", "gdal")

            self._fac_cache[dem_path] = fac
            return fac, True, tr(
                "Flow accumulation computed automatically via GRASS r.watershed. "
                "For best results, provide a pre-conditioned FAC raster."
            )

        except Exception as e:
            return None, True, tr(f"GRASS r.watershed failed: {e}")

    def _sanitize_list(self, data):
        """
        Converts a numpy array or list to a JSON-safe list.
        Replaces NaN and Infinity with None (which becomes 'null' in JSON).
        """
        if data is None: 
            return None
        arr = np.array(data, dtype=np.float64)
        # Replace Inf and NaN with a safe value (None/null)
        mask = np.isnan(arr) | np.isinf(arr)
        clean_list = [val if not m else None for val, m in zip(arr.tolist(), mask)]
        return clean_list