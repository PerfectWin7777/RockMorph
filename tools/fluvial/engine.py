# tools/fluvial/engine.py

"""
================================================================================
tools/fluvial/engine.py — Fluvial Geomorphology Analysis Engine

OVERVIEW
--------
This module computes quantitative geomorphologic metrics for river systems,
focusing on the relationships between channel morphology, topography, and
drainage basin characteristics. All metrics are derived from fluvial physics
and are used to infer bedrock erodibility, tectonic activity, and long-term
landscape evolution.

COMPUTATIONAL PIPELINE
----------------------
For each basin, the FluvialEngine executes this sequence:

  1. Extract Main River
     → MainRiverExtractor identifies the principal drainage path
        (core/hydro.py — reused)

  2. Sample River Hydraulic Profile
     → Extract elevation and drainage area at discrete points along river
        (core/hydro.py — sample_river_native_pixels)

  3. Prepare Flow Accumulation Raster (FAC)
     → Load user-provided FAC or compute via GRASS r.watershed (cached)
        Uses hydrological D8 algorithm for pixel-by-pixel runoff routing

  4. Compute SL Index and SLk (Hack 1973)
     → Local channel steepness weighted by distance from mouth
        SL[i] = (dz/dl) · L[i]  where L = distance along river
        SLk normalizes for total basin relief (dimensionless proxy)

  5. Chi Transformation (Perron & Royden 2013)
     → Integral transform that linearizes power-law river profiles
        χ = ∫[mouth→x] (A₀/A)^θ_ref dx
        Chi is insensitive to discharge variations; knickpoints appear as steep segments

  6. Steepness Index k_sn (Kirby & Whipple 2001, Wobus 2006)
     → Normalized channel steepness independent of drainage area
        k_sn = S / A^-θ_ref  (two calculation methods available)
        HIGH k_sn → actively uplifting terrain or hard bedrock
        LOW k_sn → old, stable landscape or soft bedrock

  7. Knickpoint Detection (Bai-Perron Segmented Regression)
     → Identifies abrupt slope changes in chi-space (topographic breaks)
        Each knickpoint marks a recent base-level drop or lithologic boundary

  8. Equilibrium Profile (Hack 1973 / Mvondo Owono 2010)
     → Reference concave-up profile if basin is in steady-state
        Deviations indicate non-equilibrium (ongoing adjustment to uplift/base-level)

KEY CONCEPTS
============

θ_ref (Theta Reference) — Concavity Index
-------------------------------------------
Universal slope-area relationship: S ∝ A^-θ_ref
  • Typical range: 0.4–0.6 (Whipple 2004)
  • Default: 0.45 (global average for bedrock rivers)
  • Physical meaning: steeper channels in smaller drainages

This concavity arises from DISCHARGE SCALING: Q ∝ A^0.5, and the Manning
equation for flow resistance. Setting θ_ref fixes the reference scaling,
allowing k_sn to measure basin-relative steepness.

k_sn (Steepness Index)
----------------------
The dimensionless steepness of a river, normalized to drainage area.

  EQUATION 1 — Direct (for continuous profiles):
    k_sn = S * A^θ_ref
    Robust against DEM pixel-scale noise; smooth visual profiles.

  EQUATION 2 — Regression-based (for segments):
    log(S) = log(k_sn) - θ_local * log(A)  ← OLS fit
    Solves for both k_sn AND local concavity θ_local; more analytical.

Typical values at equilibrium (stable mountains, no uplift):
  • k_sn ≈ 0–100  for low-relief, old mountains (Appalachian)
  • k_sn ≈ 100–300 for active orogens (Alps, Himalayas)
  • k_sn > 500 indicates young topography or hard rock

Chi-Space Analysis
-------------------
Transform elevation vs. distance → elevation vs. chi:

  • In (distance, elevation): river profile is concave, hard to compare basins
  • In (chi, elevation): river profile becomes ~linear (under equilibrium)
  • Slopes in chi-space ≈ k_sn (when θ = θ_ref)
  • Vertical jumps = KNICKPOINTS (transient response to uplift, base-level drop)

Knickpoints
-----------
Abrupt slope breaks in river profiles. Interpreted as:
  • Upstream migration waves from base-level lowering
  • Lithologic boundaries (soft rock → hard rock)
  • Tectonic steps (fault scarps)

Detection method: Segmented regression in chi-space minimizes residuals
across two linear segments separated by a breakpoint. Recursive bisection
finds the best position for each knickpoint.

DESIGN RULES
============
1. NO UI LOGIC — All code is pure computation; zero QGIS/PyQt5 beyond I/O.
2. FAC CACHING — Flow Accumulation is cached per DEM path; GRASS runs once
   per session.
3. THETA_REF IS RUNTIME — Change θ_ref without re-extracting rivers or
   re-running GRASS (χ and k_sn recompute instantly).
4. JSON-SERIALIZABLE — All outputs are pure Python dicts with numpy arrays
   converted to JSON-safe lists (NaN/Inf → null).

REFERENCES
==========
Hack, J. T. (1973). Stream-profile analysis and stream-gradient index.
  USGS Journal of Research, 1(4), 421–429.

Perron, J. T., & Royden, L. (2013). An integral approach to bedrock river
  profile analysis. Earth Surface Dynamics, 1(1), 21–46.

Kirby, E., & Whipple, K. X. (2001). Quantifying differential rock-uplift
  rates via stream profile analysis. Geology, 29(5), 415–418.

Wobus, C. W., et al. (2006). Tectonics from topography: Procedures,
  promise, and pitfalls. Geological Society of America Special Papers, 398, 55–74.

Mvondo Owono, R. (2010). Morphotectonic analysis of the Sanaga river
  drainage basin (Cameroon). PhD thesis, University of Yaoundé I.

AUTHORS
=======
RockMorph contributors / Tony winter
Geomorphology & fluvial process modeling
"""

import math
import numpy as np  # type: ignore
from numpy.lib.stride_tricks import sliding_window_view  # type: ignore
from qgis.core import (  # type: ignore
     QgsRasterLayer, QgsWkbTypes,
    QgsRasterLayer, 
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
    Orchestrates fluvial geomorphologic analysis for multiple basins.

    This is the main computational engine. It:
      • Validates input layers (DEM, basins, streams)
      • Extracts main rivers via flow routing
      • Samples hydraulic profiles (elevation + drainage area)
      • Computes all geomorphic metrics (SL, χ, k_sn, knickpoints, etc.)
      • Returns pure Python dicts suitable for JSON export

    CLASS-LEVEL STATE
    =================
    _fac_cache : dict
        Persistent cache of Flow Accumulation rasters keyed by DEM path.
        GRASS r.watershed is expensive; caching avoids redundant computation
        across multiple compute() calls in a single session.

    MAIN METHOD: compute(**kwargs) → dict
    =====================================

    Mandatory Arguments
    -------------------
    dem_layer      : QgsRasterLayer
        Digital Elevation Model (1–30 m resolution typical).
        Must be valid and have a defined CRS.

    basin_layer    : QgsVectorLayer (polygon geometry)
        Vector polygon layer defining drainage basin boundaries.
        Must have a numeric FID field for basin identification.

    stream_layer   : QgsVectorLayer (line geometry)
        Vector line network representing mapped streams/rivers.
        Used as the skeleton for extracting main rivers.

    Optional Arguments with Defaults
    ---------------------------------
    fac_layer      : QgsRasterLayer | None (default: None)
        Pre-computed Flow Accumulation raster (from r.watershed, r.cost, etc.).
        If None, FluvialEngine computes FAC automatically via GRASS.
        Providing a valid FAC saves significant computation time.

    label_field    : str | None (default: None)
        Column name in basin_layer containing user-friendly labels.
        If None, basins are labeled by FID only.

    snap_dist_m    : float (default: 2.0)
        Maximum distance in meters to snap extracted rivers to DEM pixels.
        Prevents small gaps between digitized streams and actual topography.

    theta_ref      : float (default: 0.45)
        Reference concavity index (m/n exponent in S ∝ A^-θ_ref).
        Controls normalization of k_sn and χ calculations.
        Standard range: 0.40–0.60 (Whipple 2004).
        • 0.40 → channels steepen more steeply with area (lower k_sn typical)
        • 0.50 → middle ground; recommended for global comparisons
        • 0.60 → channels steepen gentler with area (higher k_sn typical)

    a0             : float (default: 1.0)
        Reference drainage area in m² for chi transformation scaling.
        Usually left at 1.0 (standard normalization).

    n_knickpoints  : int (default: 3)
        Maximum number of knickpoints to detect per river.
        Higher values → more segmentation but risk of false positives in noisy DEMs.
        Typical: 2–5.

    smooth         : int (default: 0)
        Elevation smoothing window size (moving average).
        0 → no smoothing (raw DEM).
        5–10 → gentle smoothing; removes single-pixel spikes.
        > 15 → aggressive smoothing; may erase small-scale landforms.

    method         : str (default: "chi_slope")
        Segmented k_sn calculation strategy:
        • "chi_slope" — Geometric: slope of Z vs. χ line for each segment
          (Robust, insensitive to DEM noise, ideal for visualization)
        • "regression" — Analytical: OLS regression on log(S) vs. log(A)
          (Solves for local θ_local; more complex but scientifically detailed)

    progress_callback : callable | None
        Optional progress function: progress_cb(percent, message).
        Allows UIs to display real-time computation status.

    RETURN VALUE: dict
    ==================
    {
        "results": [
            # Per-basin results (see _compute_metrics for structure)
            {
                "label", "fid", "length_m", "length_km", "n_points",
                "distances_m", "elevations", "area_m2", "slope_local",
                "sl", "slk", "sl_max", "slk_max",
                "chi", "elev_chi", "chi_max",
                "ksn_profile", "ksn_mean", "ksn_max", "theta_local",
                "ksn_segments", "knickpoints", "equil_elev",
                "theta_ref", "a0"
            },
            ...
        ],

        "skipped": [
            # (fid, reason) tuples for basins that could not be processed
            (1, "insufficient valid profile points"),
            (5, "GRASS failed: invalid DEM"),
            ...
        ],

        "warnings": [
            # Informational strings (not fatal)
            "Basin 'Upper Amazon' (FID 3): FAC sampling partially failed...",
            ...
        ],

        "fac_auto": bool
            # True if FAC was computed automatically; False if user-provided
    }

    ERROR HANDLING
    ==============
    • Missing/invalid layers → empty results; warning message
    • Basins with sparse data → skipped with reason
    • Exceptions during metric computation → logged and skipped
    • All exceptions are caught and reported; compute() never crashes
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
        snap_dist_m  = kwargs.get("snap_dist_m",    2.0)
        theta_ref    = kwargs.get("theta_ref",      THETA_REF_DEFAULT)
        a0           = kwargs.get("a0",             A0_DEFAULT)
        n_knick      = kwargs.get("n_knickpoints",  3)
        smooth_win   = kwargs.get("smooth",         0)
        progress_cb  = kwargs.get("progress_callback")
        ksn_method       = kwargs.get("ksn_method", "chi_slope")


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
                    n_knick      = n_knick,
                    smooth_win   = smooth_win,
                    ksn_method       = ksn_method,

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
        n_knick:     int,
        smooth_win:  int,
        ksn_method:  str = "chi_slope",
    ) -> dict:
        """
        Computes all geomorphologic metrics for a single river basin.

        This is the core computation kernel. It orchestrates eight sub-calculations
        to produce a comprehensive river analysis profile.

        INPUTS
        ======
        profile : dict
            River hydraulic profile from sample_river_native_pixels():
              • distances_m : array — cumulative distance along river from mouth
              • elevations : array — elevation at each sample point (m, source→mouth)
              • area_m2 : array — drainage area at each point (m²)
              • slope_local : array — local channel slope (dimensionless)
              • n_points : int — number of samples
              • total_length_m : float — river length (m)

        river : dict
            River metadata:
              • fid : int — unique identifier
              • label : str — user-friendly name
              • length_m : float
              • geom : geometry object

        label, fid : Identifier strings for output.

        theta_ref : float
            Reference concavity index (0.40–0.60, typically 0.45).
            Determines k_sn and χ normalization.

        a0 : float
            Reference drainage area (m²), usually 1.0.

        n_knick : int
            Maximum knickpoints to detect.

        smooth_win : int
            Elevation smoothing window (0 = none).

        ksn_method : str
            Segmented k_sn calculation method ("chi_slope" or "regression").

        COMPUTATION SEQUENCE
        ====================

        STEP 1 — Ensure Source→Mouth Ordering
        ─────────────────────────────────────
        Critical geomorphic requirement: index 0 = SOURCE (highest point),
        index -1 = MOUTH (lowest point). profile data may come reversed.

        This is essential because:
          • Chi integration proceeds from mouth → source
          • All references to "upstream" assume increasing indices
          • Knickpoint detection assumes elevation increases toward source

        STEP 2 — Adaptive Window Sizing
        ──────────────────────────────
        Dynamic window size for moving-window statistics (k_sn, theta_local):

          1. Compute average point spacing: avg_spacing = total_length / n_points
          2. Convert distance scale to point scale: n_points = 125m / avg_spacing
          3. Ensure odd number for symmetric centering: n_points += 1 if even
          4. Enforce minimum: window_size = max(MIN_SEGMENT_PTS=8, n_points)

        Why 125m? Field experience shows this is optimal for DEM spatial
        resolution (10–30m). Too small → noisy estimates; too large → loss
        of local detail.

        STEP 3 — SL Index (Hack 1973)
        ─────────────────────────────
        SL = (dz/dl) · L[i]  where:
          • dz/dl = local channel gradient
          • L[i] = distance from mouth to point i

        Physical meaning: measures steepness weighted by river length.
        Sensitive to local bathymetry; used to visualize slope anomalies.

        SLk normalizes SL by total basin relief: SLk = SL / k
        where k = H_total / ln(L_total), making it dimensionless and
        comparable across basins.

        STEP 4 — Optional Elevation Smoothing
        ──────────────────────────────────────
        If smooth_win > 2:
          • Apply moving-average filter (eliminates single-pixel noise)
          • Recompute slope from smoothed elevations (centred differences)
          • Does NOT affect underlying DEM; only local analysis

        STEP 5 — Chi Transformation (Perron & Royden 2013)
        ───────────────────────────────────────────────────
        χ = ∫[mouth→x] (A₀/A)^θ_ref dx

        Chi is a coordinate system that "unfolds" a river profile into a
        linear form (under equilibrium). Key properties:

          • χ = 0 at mouth; χ = χ_max at source
          • Linear relationship: Z ≈ χ · k_sn (equilibrium profile)
          • Knickpoints appear as vertical jumps (deviations from linearity)
          • θ_ref is a FREE PARAMETER; changing it rescales χ but doesn't
            affect physical reality (use for testing sensitivity)

        Output: chi[] ordered source→mouth, max at source, ~0 at mouth.

        STEP 6 — k_sn Continuous Profile (Analytical Direct)
        ──────────────────────────────────────────────────────
        Method: _compute_ksn_loglog_V3()
        Formula: k_sn = S · A^θ_ref

        This is the "direct" formula: slope × area scaling. It is:
          ✓ Computationally fast (no regression needed)
          ✓ Visually smooth (averaged via implicit local window)
          ✓ Insensitive to DEM pixel-scale noise
          ✗ Does NOT solve for local θ (fixed to θ_ref)

        Returns both k_sn profile AND theta_local (via windowed regression
        for reference; theta_local is metadata only, not used to compute k_sn).

        STEP 7 — Knickpoint Detection (Segmented Regression)
        ──────────────────────────────────────────────────────
        Method: _detect_knickpoints()
        Algorithm: Recursive bisection in chi-space.

        Interpretation:
          • Knickpoint = break in chi-elevation regression line
          • Indicates non-equilibrium: margin of active incision or uplift
          • Migration speed ∝ (k_sn − k_sn_eq) · basin_area
          • Can reveal lithologic boundaries (mode of failure changes at k_sn)

        Output: list of {idx, chi, dist_m, elev_m} sorted by position.

        STEP 8 — k_sn Segmented (Per-Knickpoint Section)
        ───────────────────────────────────────────────
        Method: _compute_ksn_segments()
        Separates river into sections between knickpoints.

        Two strategies:
          • "chi_slope" (default) — Robust geometric slope: Δz/Δχ
          • "regression" — Solves log(S) vs log(A) for k_sn AND θ_local

        Returns per-segment metrics for the chi-plot area-shading visualization.

        STEP 9 — Equilibrium Profile (Hack Scaling)
        ──────────────────────────────────────────
        Reference curve for a basin in steady-state (no uplift).
        Formula: logistic scaling from valley floor to divide.

        Deviations from equilibrium profile indicate:
          • Positive (above): transient incision (base-level lowering response)
          • Negative (below): aggradation or long-term sluggish response

        STEP 10 — Output Assembly and Serialization
        ──────────────────────────────────────────
        • Flip all arrays to mouth→source order for visualization
        • Recalculate knickpoint indices in flipped space
        • Sanitize NaN/Inf to None (JSON-safe)
        • Return complete dict with metadata for reproducibility

        OUTPUT STRUCTURE
        ================
        Returns dict with keys:
          Identity:    label, fid, length_m, length_km, n_points
          Raw arrays:  distances_m, elevations, area_m2, slope_local
          SL metrics:  sl, slk, sl_max, slk_max
          Chi metrics: chi, elev_chi, chi_max
          k_sn metrics: ksn_profile, ksn_mean, ksn_max, theta_local, ksn_segments
          Knickpoints: knickpoints (list of dicts)
          Equilibrium: equil_elev
          Parameters:  theta_ref, a0 (reproducibility)

        All arrays are serialized to JSON-safe lists (NaN → null).
        """

        dist  = profile["distances_m"].copy()
        elev  = profile["elevations"].copy()
        area  = profile["area_m2"].copy()
        slope = profile["slope_local"].copy()

    
        # --- CRITICAL STEP: FORCE SOURCE-TO-MOUTH DIRECTION ---
        # In geomorphology, index 0 MUST be the Source (Highest point)
        if elev[0] < elev[-1]:
            # print("flipping profile to ensure source-to-mouth order")
            # The profile is currently Mouth -> Source, we must flip it
            elev  = np.flip(elev)
            area  = np.flip(area)
            # Distances must be recalculated to start at 0 from the new start (source)
            total_len = dist[-1]
            dist = total_len - np.flip(dist)
            # Re-estimate slope on flipped profile later or flip it now
            slope = np.flip(slope) 
        # ------------------------------------------------------

        total_length_m = profile["total_length_m"]
        

        # ROBUST COMPUTE OF window_size 
        # We want a window that is ~125m in real space, but we only have the number of points and total length.
        # compute the average spacing between points and derive the window size from that.
        avg_spacing = total_length_m / len(dist) if len(dist) > 0 else 1

        #  Conversion from metres to number of points in the profile, 
        # with a minimum threshold to ensure statistical validity of the regression.
        n_points = int(125 / avg_spacing)

        # force odd number for symmetry around the central point
        if n_points % 2 == 0:
            n_points += 1

        # Ensure window size does not exceed profile length and has a minimum number of points
        window_size = max(MIN_SEGMENT_PTS, n_points)

        # ── SL and SLk ───────────────────────────────────────────────
        sl, slk = self._compute_sl_slk(dist, elev, total_length_m)

        # Optional smoothing on elevation only but after 
        if smooth_win > 2:
            elev = smooth_data(elev, smooth_win)
            # Recompute slope after smoothing — centred differences
            slope = self._recompute_slope(elev, dist)

        # ── Chi transformation ───────────────────────────────────────
        chi = self._compute_chi(dist, area, theta_ref, a0)

        # ── k_sn via log S / log A (Kirby & Whipple 2001) ────────────
        ksn_profile, theta_local = self._compute_ksn_loglog_V3(
            slope, area, theta_ref, window_size
        )

        # ── Knickpoint detection — segmented regression on chi-plot ──
        knickpoints = self._detect_knickpoints(chi, elev, n_knick)

        # ── k_sn per segment (between knickpoints) ───────────────────
        ksn_segments = self._compute_ksn_segments(
            chi, elev, slope, area, theta_ref, knickpoints, ksn_method
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
    ) -> tuple:
        r"""
        Computes SL and SLk indices from river profile (Hack 1973).

        CONCEPT: Slope-Length Index
        ============================
        The SL index combines local relief (slope) with position along the river.
        It reveals where the river is STEEPER than expected at its current distance
        from the mouth, indicating:
          • Lithologic boundaries (soft → hard rock)
          • Tectonic features (fault scarps, folding)
          • Knickpoint migration (transient features)
          • Local base-level changes

        FORMULA
        =======
        SL[i] = (dz/dl) · L[i]

        where:
          dz/dl = local channel gradient at point i (m/m)
          L[i]  = cumulative distance from mouth to point i (m)

        Units: meters (dimensionally similar to relief).
        Range: 0–100+ (higher values = steeper anomalies).

        PHYSICAL INTERPRETATION
        =======================
        • SL_low (0–10) → Gentle gradient, low-relief terrain
          Typical in: coastal plains, stable shields, mature landscapes
        • SL_moderate (10–50) → Normal mountain river steepness
          Typical in: old orogens (Appalachians), stable basins
        • SL_high (50+) → Steep anomalies, knickpoints, active uplift
          Typical in: young mountains (Himalayas), fault steps, lithologic changes

        NORMALIZATION: SLk
        ==================
        Raw SL varies strongly with river length L (longer rivers → higher SL
        just from accumulating distance). To compare basins of different sizes,
        normalize by basin relief "gradient":

        SLk = SL / k    where k = H_total / ln(L_total)

        This makes SLk DIMENSIONLESS and COMPARABLE across basins.

        Interpretation of SLk:
          • SLk ~ 1     → Integrated steepness matches basin relief
          • SLk >> 1    → Local anomalies; likely tectonic or lithologic
          • SLk << 1    → Broader, distributed gradients; stable region

        IMPLEMENTATION
        ===============

        Args:
            dist (np.ndarray)
                Cumulative distance along river from mouth (m).
                Length n. Ordered mouth→source (increasing values).

            elev (np.ndarray)
                Elevation at each point (m). Length n.
                elev[0] = mouth (lower), elev[-1] = source (higher).

            total_length_m (float)
                Total river length from mouth to source (m).

        Returns:
            tuple: (sl, slk)
                sl  : np.ndarray of shape (n,)
                    SL value at each point (m)
                slk : np.ndarray of shape (n,)
                    Normalized SL (dimensionless)

        ALGORITHM
        =========

        Step 1: Local Gradient (Centred Finite Differences)
        ────────────────────────────────────────────────────
        At interior points, use centred differences for accuracy:
          dz/dl = (elev[i] - elev[i+1]) / (dist[i+1] - dist[i])

        Interior gradients are more stable than forward/backward differences.
        Edge points (0, n-1) use one-sided differences.

        Clipping: If dz < 0 (counter-slope, data artifact), set SL = 0.
        Do NOT filter/remove these; they represent data quality issues
        to visualize (e.g., DEM errors, backwater effects).

        Step 2: Weight by Distance
        ──────────────────────────
        SL[i] = gradient[i] · dist[i]
        Distance weighting emphasizes steeper sections that occur far
        from the mouth (physically important for incision).

        Step 3: Baseline Normalization
        ──────────────────────────────
        k = H_total / ln(L_total)
          H_total = abs(elev[0] - elev[-1]) (total relief)
          L_total = total_length_m

        Dividing by k removes the baseline relief trend, isolating LOCAL anomalies.

        Edge Case: If L_total < 1m or H_total ≈ 0, return SL unchanged
        (very short rivers or flat basins; can't normalize meaningfully).

        NUMERICAL STABILITY
        ====================
        • Check distance steps: if dl < 1e-6 m, skip (avoids division by tiny dx)
        • Clamp k to avoid division by very small numbers
        • Use np.log (natural log) as per Hack's original formula
        """
        n = len(dist)

        # ── Step 1 : local slope — centred finite differences ────────────
        sl = np.zeros(n, dtype=np.float64)

        for i in range(n - 1):
            dz = elev[i] - elev[i + 1]   # drop = positive for normal river
            dl = dist[i + 1]   - dist[i]

            if dl < 1e-6:
                continue

            slope = dz / dl
            val   = slope * dist[i]
            sl[i] = max(val, 0.0)   # negative = counter-slope → 0, not filtered

        # Edges — one-sided
        if dist[1] - dist[0] > 1e-6:
            dz     = elev[0] - elev[1]
            dl     = dist[1] - dist[0]
            sl[0]  = (dz / dl) * dist[0]  

        if dist[-1] - dist[-2] > 1e-6:
            dz      = elev[-2] - elev[-1]
            dl      = dist[-1] - dist[-2]
            sl[-1]  = (dz / dl) * dist[-1]

        # ── Step 2 : SLk normalisation ────────────────────────────────────
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
        r"""
        Computes the chi (χ) integral transform of a river profile
        (Perron & Royden 2013).

        MOTIVATION: Why Transform to Chi-Space?
        =======================================
        River profiles have inherent concavity (steep near source, gentle near mouth)
        due to discharge scaling: Q ∝ A^0.5. This makes it hard to compare rivers
        or detect anomalies visually.

        Chi transformation "unfolds" this concavity. In chi-space:
          • Equilibrium profiles become LINEAR (Z ≈ k_sn · χ)
          • Knickpoints appear as VERTICAL JUMPS (deviations from the line)
          • Different basins become COMPARABLE (same units, same slope interpretation)

        FORMULA
        =======
        χ(x) = ∫[mouth→x] (A₀/A(ξ))^θ_ref dξ

        where:
          x          = position along river (m, measured from mouth)
          A(ξ)       = drainage area at upstream position ξ (m²)
          A₀         = reference area (usually 1 m²)
          θ_ref      = reference concavity (0.40–0.60, typically 0.45)
          dξ         = infinitesimal distance

        PHYSICAL INTERPRETATION
        =======================
        χ is a "distance" measured in area units (m², technically).
        The integrand (A₀/A)^θ_ref accounts for discharge reduction:
          • Where A is large (mouth), term ≈ (1/A)^θ → small contribution
          • Where A is small (source), term ≈ (1/A)^θ → large contribution
          • Integration accumulates these weights from mouth to any point

        Result:
          • χ ≈ 0 at mouth (integration starts at 0)
          • χ increases nonlinearly toward source
          • χ_max at source (highest accumulated weight)

        Why θ_ref?
        ──────────
        Universal river scaling: S ∝ A^(-θ_ref)
        (Flint 1974; supported by global empirics)

        Setting θ_ref = 0.45 means we NORMALIZE upstream/downstream slope
        differences caused by discharge scaling. With this normalization:
          • k_sn becomes INDEPENDENT of drainage area (truly normalized)
          • All rivers of same "strength" have same slope in chi-space
          • Deviations from linearity → TRANSIENT or SPECIAL features

        NUMERICAL METHOD: Trapezoidal Integration
        ==========================================
        We use numerical integration (trapezoidal rule) to approximate the
        integral over discrete sample points.

        Algorithm:
          1. Reverse arrays (work mouth → source, because integration
             proceeds downstream→upstream in the formula)
          2. Compute integrand at each point: w[i] = (A₀/A[i])^θ_ref
          3. Integrate using np.trapz on consecutive pairs
          4. Flip result back to source→mouth order

        Trapezoidal rule:
          ∫ w dx ≈ sum[(w[i] + w[i+1])/2 · (x[i+1] - x[i])]
        Accuracy: O(h²) where h = average spacing; excellent for n > 50.

        IMPORTANT: Distance Reference
        ============================
        Our profile is ordered MOUTH→SOURCE (increasing distance).
        But chi integration is done MOUTH→SOURCE mathematically.
        So we integrate on dist_reversed = Total_len - dist[::-1],
        which starts at 0 at the mouth and increases toward the source.

        OUTPUT
        ======
        chi : np.ndarray of shape (n,)
            Chi values at each point, SORTED to match input order (mouth→source).
            χ[0]  = χ_max (at source, highest accumulated weight)
            χ[-1] ≈ 0 (at mouth)

        Wait — that seems backwards. Let me verify:
        After reversal and integration, we have chi_rev ordered MOUTH→SOURCE.
        When we flip chi_rev back, we get:
            chi = chi_rev[::-1]
        which puts the LARGEST values first (source) and smallest last (mouth).

        VISUALIZATION
        ==============
        Typical chi-plot: (χ x-axis) vs. (Z y-axis)
          • Shape: approximately linear under equilibrium
          • Slope ≈ k_sn (for this basin with given θ_ref)
          • Knickpoints: sudden vertical jumps (slope breaks)

        Interpretation:
          • ABOVE the fitted line → actively incising (knickpoint ↓)
          • BELOW the fitted line → aggrading or in slowdown (knickpoint ↑, less common)
          • LINEAR → equilibrium (steady-state, no recent base-level change)

        SENSITIVITY TO THETA_REF
        =======================
        Changing θ_ref rescales chi:
          • Higher θ (e.g., 0.60) → smaller chi values, less curvature
          • Lower θ (e.g., 0.40) → larger chi values, more curvature

        Choose θ_ref = 0.45 for global studies (default).
        Vary it (0.40–0.60) for sensitivity analysis (does conclusion hold?).

        Args:
            dist (np.ndarray)
                Cumulative distance along river (m), length n, mouth→source.
            area_m2 (np.ndarray)
                Drainage area at each point (m²), length n, same order.
            theta_ref (float)
                Reference concavity exponent (usually 0.45).
            a0 (float)
                Reference area for normalization (usually 1.0 m²).

        Returns:
            np.ndarray
                Chi at each point, same length as inputs, mouth→source order.
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

    def _compute_ksn_loglog_V1(
        self,
        slope:     np.ndarray,
        area_m2:   np.ndarray,
        theta_ref: float,
        total_length_m : int
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
    
    def _compute_ksn_loglog_V2(
        self,
        slope: np.ndarray,
        area_m2: np.ndarray,
        theta_ref: float,
        total_length_m: int
    ) -> tuple:
        """
        Vectorized point-wise k_sn estimation using OLS regression on log S / log A.
        
        This implementation replaces Python loops with NumPy sliding windows, 
        making it ~50x to 100x faster for large river profiles.

        Formula:
            log S = log Ks  -  θ · log A   (linear regression)
            Ks    = 10^intercept
            A_cent = 10^((log Amax + log Amin) / 2)
            k_sn  = Ks · A_cent^(theta_ref - |θ_local|)

        Args:
            slope: Local channel slope (m/m).
            area_m2: Drainage area (m2).
            theta_ref: Reference concavity (m/n).

        Returns:
            tuple: (ksn_profile, theta_local_profile) aligned with input arrays.
        """
        n_total = len(slope)
        # Define window size: ~10% of profile, forced to be odd for symmetry
        # use actual profile length to determine window size, with a minimum threshold
        avg_dist = total_length_m / n_total
        half_win = max(MIN_SEGMENT_PTS, int(125 / avg_dist))
        # half_win = max(MIN_SEGMENT_PTS, n // 20)
        window_size = 2 * half_win + 1

        # --- SECURITY : RIVER TOO SHORT ---
        if n_total < window_size:
            # return NaN arrays of the same length to maintain consistency
            return np.full(n_total, np.nan), np.full(n_total, np.nan)
        

        # 1. Prepare log-transformed data
        # Avoid log10(0) by using a tiny floor
        log_A = np.log10(np.where(area_m2 > 1e-6, area_m2, 1e-6))
        log_S = np.log10(np.where(slope > 1e-10, slope, 1e-10))

        # 2. Create sliding window views (Memory efficient: no data copying)
        # Shape: (n_windows, window_size)
        
        win_x = sliding_window_view(log_A, window_size)
        win_y = sliding_window_view(log_S, window_size)

        # 3. Vectorized OLS (Ordinary Least Squares) Regression
        # Slope (m) = [n*sum(xy) - sum(x)*sum(y)] / [n*sum(xx) - sum(x)^2]
        n = window_size
        sum_x  = np.sum(win_x, axis=1)
        sum_y  = np.sum(win_y, axis=1)
        sum_xy = np.sum(win_x * win_y, axis=1)
        sum_xx = np.sum(win_x**2, axis=1)

        denom = (n * sum_xx - sum_x**2)
        
        # Avoid division by zero (if log_A is constant in a window)
        with np.errstate(divide='ignore', invalid='ignore'):
            m = (n * sum_xy - sum_x * sum_y) / denom
            b = (sum_y - m * sum_x) / n

        # θ_local is the positive concavity (negative of the slope m)
        theta_local_win = np.abs(m)
        ks_win = 10**b

        # 4. Geomorphic Ksn Calculation
        # Compute A_cent logic: 10^((logA_max + logA_min) / 2)
        log_A_max = np.max(win_x, axis=1)
        log_A_min = np.min(win_x, axis=1)
        log_A_cent = (log_A_max + log_A_min) / 2.0
        A_cent_win = 10**log_A_cent

        ksn_win = ks_win * (A_cent_win**(theta_ref - theta_local_win))

        # 5. Clean results and handle outliers
        # Apply user filters: 0 < ksn < 5000
        ksn_win = np.where((ksn_win > 0) & (ksn_win < 5000), ksn_win, np.nan)
        
        # Filter by log_A variation (ptp = peak to peak) 
        # If a window has no area change, the regression is invalid.
        ptp_x = np.ptp(win_x, axis=1)
        ksn_win = np.where(ptp_x > 0.1, ksn_win, np.nan)
        theta_local_win = np.where(ptp_x > 0.1, theta_local_win, np.nan)

        # 6. Re-align with original profile length (padding)
        # sliding_window_view reduces size by (window_size - 1)
        # We pad with NaN at both ends to maintain array alignment
        ksn_profile = np.full(n_total, np.nan)
        theta_profile = np.full(n_total, np.nan)
        
        ksn_profile[half_win : n_total - half_win] = ksn_win
        theta_profile[half_win : n_total - half_win] = theta_local_win

        return ksn_profile, theta_profile

    def _compute_ksn_loglog_V3(
        self,
        slope: np.ndarray,
        area_m2: np.ndarray,
        theta_ref: float,
        window_size: int = None
    ) -> tuple:
        r"""
        Computes a continuous, point-wise Normalized Steepness Index (k_sn)
        profile with dynamic local concavity estimation.

        CONTEXT: What is k_sn?
        ======================
        k_sn (sometimes written k_{sn} or K_s) is the geomorphologist's
        measure of HOW STEEP a river is, normalized for drainage area.

        Universal slope-area relation (Flint 1974; Whipple & Tucker 1999):
            S = k_sn · A^(-θ)
            log(S) = log(k_sn) - θ · log(A)

        where:
          S       = channel slope (m/m)
          A       = drainage area (m²)
          θ       = concavity index (m/n exponent)
          k_sn    = steepness coefficient (intercept when [log(A), θ] are held fixed)

        PHYSICAL MEANING OF k_sn
        ========================
        k_sn measures ERODIBILITY × UPLIFT RATE:
            k_sn ∝ √(U/K)

        where U = uplift rate, K = bedrock erodibility.

        High k_sn → active uplift or soft bedrock (readily eroded)
        Low k_sn  → stable shield or hard bedrock (resistant)

        Typical values (Kirby & Whipple 2001; Wobus et al. 2006):
          • k_sn ≈ 50–100   Passive margins, shields (U ~ 0, K high)
          • k_sn ≈ 200–400  Mountain ranges (U ~ 10 mm/yr, moderate K)
          • k_sn ≈ 500+     Young orogens (U >> 50 mm/yr, or K low)

        TWO CALCULATION METHODOLOGIES
        ==============================

        METHOD 1 — "DIRECT" (This function)
        ────────────────────────────────────
        Formula: k_sn = S · A^θ_ref

        Advantages
        ──────────
        ✓ Computational speed: O(n), just multiplication
        ✓ Stability: inherently smooth; no regression sensitivity
        ✓ Visual quality: produces the chi-plot profiles geomorphologists expect
        ✓ Noise resistance: DEM pixel-scale fluctuations average implicitly

        Disadvantages
        ─────────────
        ✗ Conservation: fixes θ = θ_ref (cannot estimate local concavity from data)
        ✗ Analytical: does NOT solve for k_sn; uses given formula directly

        When to use: Continuous profiles, visualization, QA/QC.

        METHOD 2 — "REGRESSION" (in _compute_ksn_segments)
        ────────────────────────────────────────────────
        Formula: Fit log(S) = log(k_sn) - θ_local · log(A), solve for intercept.

        Advantages
        ──────────
        ✓ Analytical: solves the fundamental equation; may be physically different
        ✓ Flexible: can estimate θ_local from data (local scaling)
        ✓ Scientific: more aligned with statistical mechanics

        Disadvantages
        ─────────────
        ✗ Computational cost: O(n window_size) sliding regression
        ✗ Stability: intercept (Ks) is hypersensitive to outliers; DEM steps
          create "staircase" artifacts
        ✗ Visual: produces noisier profiles; hard to interpret small changes

        When to use: Per-segment analysis (between knickpoints), scientific publications.

        THIS FUNCTION (DIRECT METHOD)
        =============================
        We use the DIRECT formula because:
          1. River profiles in elevation vs. distance ARE inherently noisy
             (DEM pixel resolution, stream routing stochasticity)
          2. Regression intercepts amplify this noise → uninterpretable wiggles
          3. We ALREADY FIX θ_ref as a parameter → makes θ_local estimation moot
          4. Visualization quality matters: users need to see true knickpoints,
             not random k_sn fluctuations

        However, we ALSO compute θ_local via windowed OLS regression for metadata.
        This θ_local is INFORMATIONAL only—it shows where the actual slope-area
        scaling deviates from our assumed θ_ref. It does NOT affect k_sn values.

        ALGORITHM
        =========

        PART 1: Direct k_sn Calculation
        ───────────────────────────────
        For each point i:
            k_sn[i] = slope[i] · area[i]^θ_ref

        Vectorized in NumPy: O(n) operation, extremely fast.

        Input validation:
          • slope < 1e-10 → clipped to 1e-10 (avoid log(0))
          • area < 1e-6 → clipped to 1e-6 (avoid negative powers of tiny areas)

        Output filtering:
          • Remove k_sn outside [0, 1500]: likely noise or DEM artifacts
          • Set to NaN (will be null in JSON)

        PART 2: Theta-Local Approximation (Windowed OLS)
        ────────────────────────────────────────────────
        To understand WHERE the slope-area scaling differs from θ_ref,
        we estimate θ_local using a sliding log-log regression.

        Algorithm:
          1. Log-transform both slope and area
          2. Create sliding windows of size window_size
          3. Fit linear regression in log-space on each window
          4. Slope of fit = -θ_local (negative relationship)
          5. Take absolute value → θ_local (positive concavity)

        Vectorized OLS:
            Slope = Cov(log A, log S) / Var(log A)
            Using vectorized NumPy operations (sliding_window_view)

        Why local θ?
        ────────────
        Different reaches of a river may have different DRAINAGE PATTERNS:
          • Steep side tributaries → effective θ is higher
          • Meandering lowlands → effective θ is lower
          • Lithologic boundary → θ may jump sharply

        Reporting θ_local helps interpret k_sn context. E.g.:
          "k_sn is high, but θ_local is also high → maybe discharge-driven,
           not rock-hardness driven."

        OUTPUT
        ======
        tuple: (ksn_profile, theta_local)

        ksn_profile : np.ndarray
            k_sn value at each point (m/m raised to θ_ref power).
            Same shape as inputs.
            Units: m^(1 - θ_ref) [dimensionally: m when θ=1, dimensionless when θ=0]
            Typical range: 0–500 (anything > 1500 set to NaN)

        theta_local : np.ndarray
            Local concavity estimate at each point.
            Same shape as inputs.
            Range: typically 0.3–0.7 (deviations from θ_ref indicate drainage variability)
            NaN at edges (insufficient window coverage)

        EDGE HANDLING
        =============
        Window size is window_size. Therefore:
          • First half_win points (near source): NaN (incomplete window)
          • Last half_win points (near mouth): NaN (incomplete window)
          • Interior points: valid estimates

        Args:
            slope (np.ndarray)
                Local channel slope (m/m), length n.
            area_m2 (np.ndarray)
                Drainage area (m²), length n.
            theta_ref (float)
                Reference concavity (0.40–0.60).
            window_size (int)
                Moving window size for θ_local estimation.
                Typically 9–31 (odd number).

        Returns:
            tuple
                (ksn_profile, theta_local_profile) both shape (n,)
        """
        n = len(slope)

        # --- 1. CORE KSN CALCULATION ---
        # Formula: ksn = S / A^-theta_ref  =>  S * A^theta_ref
        # This is the analytical solution for a normalized profile.
        ksn_profile = slope * (area_m2 ** theta_ref)

        # --- 2. THETA LOCAL APPROXIMATION ---
        # To provide scientific context, we approximate the "real" local concavity 
        # using a sliding log-log regression window. We don't use this for ksn, 
        # only for the 'theta_local' metadata.
        theta_local = np.full(n, theta_ref)
        
        if n > window_size or not window_size :
            
            # Log-transform for regression
            log_A = np.log10(np.where(area_m2 > 1e-6, area_m2, 1e-6))
            log_S = np.log10(np.where(slope > 1e-10, slope, 1e-10))
            
            # Create sliding windows
            win_A = sliding_window_view(log_A, window_size)
            win_S = sliding_window_view(log_S, window_size)
            
            # Vectorized OLS Slope (theta = -regression_slope)
            # Using simple Slope = Cov(x,y) / Var(x)
            x_mean = np.mean(win_A, axis=1)[:, None]
            y_mean = np.mean(win_S, axis=1)[:, None]
            
            num = np.sum((win_A - x_mean) * (win_S - y_mean), axis=1)
            den = np.sum((win_A - x_mean)**2, axis=1)
            
            # Valid windows only (avoid division by zero)
            valid = den > 1e-6
            m = np.zeros(len(win_A))
            m[valid] = num[valid] / den[valid]
            
            # Local concavity is the absolute value of the slope
            th_win = np.abs(m)
            
            # Re-align with original array (padding edges)
            half = window_size // 2
            theta_local[half : n - (window_size - 1 - half)] = th_win

        # --- 3. SIGNAL CLEANING ---
        # Remove mathematical artifacts from extreme DEM noise (negative slopes or outliers)
        ksn_profile = np.where((ksn_profile > 0) & (ksn_profile < 1500), ksn_profile, np.nan)

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
        Detects up to n_knick knickpoints using recursive segmented regression
        in chi-space (Bai-Perron algorithm simplified).

        CONCEPT: What is a Knickpoint?
        ==============================
        A knickpoint is an ABRUPT BREAK in slope of a river profile.
        Visually: looks like a step or waterfall or rapid on the ground.
        In chi-space: appears as a deviation from the linearity of the
        chi vs. elevation profile.

        Causes of Knickpoints:
        ─────────────────────
        1. BASE-LEVEL LOWERING
           When downstream base-level drops (e.g., gorge cut through resistant rock,
           sea-level fall, or river capture), the river cannot immediately adjust.
           An upstream-migrating knickpoint forms, marking the "wave" of adjustment.
           Migration speed: v ≈ (k_sn - k_sn_eq)^(1/m) where m ≈ 2–3.
           Time scale: thousands to millions of years depending on basin area.

        2. LITHOLOGIC BOUNDARIES
           Hard rock (granite) → soft rock (shale) interface.
           Erosion rate changes abruptly.
           The knickpoint is STABLE and marks the boundary permanently.

        3. TECTONIC STEPS
           Fault scarps or fold hinges create sudden drops.
           Combined with high uplift rate, these remain visible.

        4. GLACIAL LEGACY
           Glacial valley steps (hanging valleys, truncated spurs).
           Less common in actively eroding non-glacial regions.

        DETECTION ALGORITHM: Recursive Bisection (Bai-Perron Simplified)
        ==============================================================
        We seek n_knick breakpoints that MINIMIZE the total residual sum
        of squares (RSS) when fitting LINE SEGMENTS in chi-space.

        Core idea:
          • Fit a line: Z = a + b·χ on entire profile
          • RSS = sum[(Z_observed - Z_fit)^2]
          • For each candidate breakpoint b:
              - Fit line on [0:b] and [b:end] separately
              - RSS_split(b) = RSS_left + RSS_right
          • BEST breakpoint b* minimizes RSS_split(b)
          • Recurse on two sub-segments to find next breakpoint

        Why chi-space?
        ───────────────
        • Equilibrium profiles are LINEAR in chi-space
        • Deviations from linearity = disequilibrium (knickpoints)
        • Chi is MORE STABLE than distance for this purpose
        • Insensitive to discharge variations upstream

        Why recursive?
        ────────────
        • First knickpoint is the STRONGEST (biggest residual drop)
        • Recursing on left/right segments finds PROGRESSIVELY WEAKER features
        • Limits spurious detections (need minimum number of points per segment)

        ALGORITHM DETAILS
        =================

        Input Validation:
        ─────────────────
        Only use VALID (non-NaN) points from chi and elev arrays.
        Filter out any NaN or Inf values before analysis.

        Recursion Criteria:
        ──────────────────
        • remaining = 0 → stop (detected enough knickpoints)
        • segment length < 2 · MIN_SEGMENT_PTS → too short, skip
        • no valid breakpoint found → return empty

        Segment Size Constraint:
        ──────────────────────
        MIN_SEGMENT_PTS = 8 (global constant).
        Each candidate breakpoint b must satisfy:
          i_start + MIN_SEGMENT_PTS ≤ b ≤ i_end - MIN_SEGMENT_PTS
        Ensures each segment has enough points for a meaningful fit
        (minimum 2 points for a line; 8 for statistical robustness).

        Linear Regression in Chi-Space:
        ──────────────────────────────
        For segment [i_start:i_end]:
            Z_fit[i] = a + b·χ[i]
            RSS = sum((Z_obs - Z_fit)^2)

        Computed via np.polyfit(χ, Z, 1) → [slope, intercept].

        OUTPUT
        ======
        list of dicts, each dict:
          {
              "idx":    int — original array index in the chi/elev arrays
              "chi":    float — chi value at knickpoint (m, rounded to 4 decimals)
              "dist_m": float — horizontal distance (placeholder, filled by panel)
              "elev_m": float — elevation at knickpoint (m, rounded to 2 decimals)
          }

        Sorted by chi value (from mouth to source).
        Maximum length: n_knick items.

        INTERPRETATION FOR END-USERS
        =============================
        Knickpoint at (χ, Z):
          • Vertical position Z → knickpoint elevation (absolute reference)
          • Horizontal position χ → accumulated drainage area scaling
            (χ_large → near source; χ_small → near mouth)
          • Later in workflow, indices are converted back to distances_m
            for spatial visualization on maps or 3D profiles

        False Positives:
        ────────────────
        Noisy DEMs may produce spurious knickpoints.
        Mitigation:
          • Smooth elevation before analysis (optional smooth parameter)
          • Use MIN_SEGMENT_PTS = 8 (sufficient data before accepting break)
          • Inspect chi-plots visually; knickpoints should be visually obvious

        Args:
            chi (np.ndarray)
                Chi coordinate at each point (m, dimensionally).
                Usually shape (n,); may contain NaN.

            elev (np.ndarray)
                Elevation at each point (m).
                Same shape as chi; may contain NaN.

            n_knick (int)
                Maximum knickpoints to detect (usually 2–5).

        Returns:
            list of dict
                Knickpoint locations sorted by chi.
                Empty list if no knickpoints found or profile too short.
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
        """
        Recursive helper for knickpoint detection (Bai-Perron algorithm).

        ALGORITHM
        =========
        This function searches for the SINGLE BEST breakpoint (knickpoint)
        in the range [i_start, i_end] of the chi-elev profile that minimizes
        the combined RSS (residual sum of squares) when fitting TWO line
        segments (left and right of the breakpoint).

        Recursion:
        ──────────
        • If this breakpoint is found, add it to list
        • Recursively search [i_start, breakpoint] for next strongest knickpoint
        • Recursively search [breakpoint, i_end] for next strongest knickpoint
        • Stop when remaining = 0 (found enough knickpoints)

        INPUTS
        ======
        chi, elev (np.ndarray)
            Profile data (chi, elev) ordered source→mouth.
            Must be same length; may be subset of original (from slicing).

        i_start, i_end (int)
            Index range [i_start, i_end) to search.
            Note: Python slicing convention, but we use inclusive range here.

        remaining (int)
            Number of knickpoints still to find.
            Decrement on each successful discovery.
            When remaining = 0, recursion stops.

        out (list)
            Output list where breakpoint indices are APPENDED.
            Passed by reference; modified in-place.

        TERMINATION CONDITIONS
        ======================
        1. remaining ≤ 0
           → Found enough knickpoints; return without searching

        2. segment length < 2 · MIN_SEGMENT_PTS
           → Too short to subdivide further; return

        3. No valid breakpoint found in search
           → RSS is monotonic or no improvement; return

        SEARCH PROCESS
        ==============
        For each candidate breakpoint b in [i_start + MIN_SEGMENT_PTS, i_end - MIN_SEGMENT_PTS]:

            1. Fit line on chi[i_start:b] vs elev[i_start:b]
            2. Fit line on chi[b:i_end] vs elev[b:i_end]
            3. Compute RSS = RSS_left + RSS_right
            4. Track best_b and best_rss

        CONSTRAINT
        ==========
        MIN_SEGMENT_PTS = 8 (global):
            Each side of the breakpoint must have ≥ 8 points.
            Prevents overfitting and ensures statistical significance.

        LOOP RANGE
        ==========
        b ranges from (i_start + 8) to (i_end - 8), inclusive.
            • Ensures left segment [i_start:b] has ≥ 8 points
            • Ensures right segment [b:i_end] has ≥ 8 points

        Args:
            chi, elev (np.ndarray): Profile data
            i_start, i_end (int): Search range
            remaining (int): Budget of knickpoints to find
            out (list): Accumulator for results (modified in-place)

        Returns:
            None (modifies out list in-place)
        """
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
        r"""
        Computes the Residual Sum of Squares (RSS) for a linear fit
        in chi-elevation space.

        PURPOSE
        =======
        Quantifies goodness-of-fit when fitting a straight line through
        a segment of the chi-elevation profile. Used by knickpoint detection
        to find the "best" breakpoint location.

        DEFINITION
        ==========
        RSS = Σ(observed - predicted)²
            = Σ(elev[i] - Z_fit[i])²

        where Z_fit[i] = a + b·chi[i] (linear fit).

        INTERPRETATION
        ==============
        • RSS = 0       → Perfect fit (all points on the line)
        • RSS >> 0      → Poor fit (scatter above/below line)
        • RSS is ALWAYS ≥ 0 (squared residuals)

        Lower RSS = better fit. So minimizing RSS finds the line
        that "best explains" the data.

        ALGORITHM
        =========

        Step 1: Extract segment
        ──────────────────────
        x = chi[i_start:i_end]    (chi values)
        y = elev[i_start:i_end]   (elevation values)

        Step 2: Fit linear model
        ───────────────────────
        polyfit(x, y, 1) returns [slope, intercept]
        Linear model: y_fit = intercept + slope · x

        Step 3: Predict and compute residuals
        ─────────────────────────────────────
        y_pred = polyval([slope, intercept], x)
        residuals = y - y_pred
        RSS = sum(residuals²)

        Step 4: Degenerate cases
        ───────────────────────
        If all x values are constant (no variation in chi):
            → Cannot fit a line (singular system)
            → Return RSS = inf (penalty: not a valid breakpoint)

        np.ptp(x) = peak-to-peak range = max(x) - min(x)
        If np.ptp(x) < 1e-10 → all points at same chi → return inf

        NUMERICAL STABILITY
        ====================
        • Uses np.polyfit and np.polyval (robust numerical routines)
        • Float conversion: float(RSS) ensures scalar output
        • Degenerate case handled explicitly

        Args:
            chi (np.ndarray)
                Chi values for the segment.
            elev (np.ndarray)
                Elevation values for the segment.
            i_start, i_end (int)
                Array indices for the segment [i_start:i_end].

        Returns:
            float
                RSS value (m² units, dimensionally).
                Returns inf if segment is degenerate (no x variation).
        """
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
        ksn_method:      str = "chi_slope"
    ) -> list:
        r"""
        Computes k_sn value for each river SEGMENT (section between knickpoints).

        PURPOSE OF SEGMENTATION
        =======================
        A river with knickpoints is in NON-EQUILIBRIUM — it's adjusting to
        a recent change (base-level, uplift, lithology). By analyzing
        k_sn SEPARATELY in each segment:

          1. Distinguish old equilibrium (below knickpoint) from new adjustment (above)
          2. Infer WHEN the change occurred (migration distance → time via velocity)
          3. Identify WHAT changed (compare k_sn above vs. below knickpoint)

        Example:
        ────────
        River with ONE knickpoint at χ=5.0 m:

        Segment 1 (Mouth → χ=5.0): k_sn = 50
        Segment 2 (χ=5.0 → Source): k_sn = 150

        Interpretation: Downstream is at equilibrium with STABLE conditions
        (k_sn = 50). Upstream is STEEPER, suggesting:
        • Recent base-level lowering (main hypothesis)
        • Or rock hardness increase (secondary hypothesis)
        • Knickpoint migrating upstream; hasn't reached equilibrium yet

        Migration distance ≈ 30 km; if velocity ≈ 1 km/Myr, then age ≈ 30 Myr.

        TWO METHODOLOGIES
        =================

        METHOD 1: "chi_slope" (DEFAULT, RECOMMENDED)
        ────────────────────────────────────────────
        Formula: k_sn = Δ Z / Δ χ

        Direct geometric slope of the chi-elevation line between segment endpoints.

        Advantages
        ──────────
        ✓ ROBUST: Insensitive to internal pixel-scale noise (only endpoints matter)
        ✓ FAST: O(1) per segment (just subtraction)
        ✓ VISUAL: Matches the slope OF THE FITTED LINE in chi-plots
        ✓ STABLE: No regression needed; cannot have outlier sensitivity

        Disadvantages
        ─────────────
        ✗ SIMPLE: Doesn't estimate local θ_local (assumes θ_ref throughout)
        ✗ LIMITED: Only produces k_sn; no concavity analysis

        When to use: Default choice for analysis and visualization.

        METHOD 2: "regression" (ANALYTICAL, DETAILED)
        ──────────────────────────────────────────
        Formula: Fit log(S) = log(k_sn) - θ_local · log(A) via OLS regression.
        Solves for BOTH k_sn (intercept) AND θ_local (slope).

        Advantages
        ──────────
        ✓ ANALYTICAL: Solves the fundamental slope-area equation
        ✓ FLEXIBLE: Estimates local concavity (θ_local) for the segment
        ✓ SCIENTIFIC: More detailed information; publishable

        Disadvantages
        ─────────────
        ✗ SENSITIVE: Intercept (Ks = 10^intercept) is hyper-sensitive to outliers
        ✗ NOISY: DEM pixel steps create "staircase" artifacts in log-log plots
        ✗ COMPLEX: Requires robust outlier filtering to work well

        When to use: Scientific papers; sensitivity analysis; testing methodologies.

        ALGORITHM FOR METHOD 1 (chi_slope)
        ==================================

        For each segment [i0, i1] defined by knickpoints:

        Step 1: Check validity
        ─────────────────────
        • Segment length: i1 - i0 ≥ MIN_SEGMENT_PTS
        • Both endpoints have valid chi, elev
        • Skip if too short (cannot define meaningful slope)

        Step 2: Compute geometric slope
        ──────────────────────────────
        dz = |elev[i0] - elev[i1]|   (elevation difference)
        dchi = |chi[i0] - chi[i1]|   (chi difference)

        k_sn = dz / dchi   (if dchi > 1e-6)

        This is EXACTLY the slope of the best-fit line through those two points.
        For a segment with n points, the line from endpoint to endpoint
        minimizes the DIRECTIONAL error (chi direction); interior noise
        averages out due to summation in the integration formula.

        Step 3: Optional local θ estimation (metadata)
        ──────────────────────────────────────────────
        Even though we don't USE θ_local to correct k_sn, we CAN estimate it
        from the segment's slope-area data, purely for information.

        Mask out points where slope < 1e-6 (DEM flat spots, non-flowing pixels).
        Fit: log(S) = log(Ks) - θ_local · log(A)
        θ_local = |slope of fit|

        This gives scientists a sense of "what's the actual concavity here?"
        Deviations from θ_ref might indicate:
        • Braiding (multiple channels → higher effective θ)
        • Meandering (gentle curves → lower effective θ)
        • Tributary-heavy reach (discharge increases rapidly → higher θ)

        ALGORITHM FOR METHOD 2 (regression)
        ==================================

        Step 1: Extract segment data
        ────────────────────────────
        slope_seg = slope[i0:i1]
        area_seg = area_m2[i0:i1]
        chi_data = chi[i0:i1]
        elev_data = elev[i0:i1]

        Step 2: Mask zero slopes
        ───────────────────────
        mask = slope_seg > 1e-6   (avoid log(0))
        if sum(mask) < MIN_SEGMENT_PTS:
            skip segment (too few valid points)

        Step 3: Log-transform
        ────────────────────
        log_A_seg = log10(area_seg[mask])
        log_S_seg = log10(slope_seg[mask])

        Step 4: OLS Regression
        ──────────────────────
        coeffs = polyfit(log_A_seg, log_S_seg, 1)
        slope_coeff = coeffs[0]  ← this is -θ_local (negative by convention)
        intercept = coeffs[1]    ← this is log(Ks)

        θ_local = |slope_coeff|    (take absolute value)
        Ks = 10^intercept

        Step 5: Compute k_sn
        ──────────────────
        A_cent = 10^(mean(log_A_seg))   (central drainage area of segment)

        k_sn = Ks · A_cent^(θ_ref - θ_local)

        This formula CORRECTS Ks (which was calibrated to the segment's
        specific θ_local) back to the GLOBAL θ_ref standard.

        Why? Because if θ_local ≠ θ_ref, the intercept Ks would be
        systematically different. We want k_sn normalized to θ_ref
        for cross-basin comparisons.

        VALIDATION & FILTERING
        ======================

        Segment validity checks:
          • Length ≥ MIN_SEGMENT_PTS (at least 8 points)
          • Chi range Δχ > 1e-6 (some horizontal spread)
          • Slope range varies (at least 2 valid measurements)
          • For regression: area variation > 0.01 in log₁₀ (enough dynamic range)

        Outlier handling:
          • k_sn values outside [0, 1500]: likely artifacts, skip
          • Degenerate regression (singular matrix): catch exception, skip

        OUTPUT
        ======
        list of dict, each:
          {
              "chi_start": float — chi at begin of segment
              "chi_end": float — chi at end of segment
              "elev_start": float — elevation at begin (m)
              "elev_end": float — elevation at end (m)
              "ksn": float — steepness index for this segment
              "theta_local": float — local concavity (estimate; informational)
          }

        Sorted by chi_start (mouth to source).

        VISUALIZATION USE-CASE
        ======================
        In the chi-plot web interface, these segments are used to:
        1. SHADE areas under the profile curve (color by k_sn value)
        2. LABEL each segment with its k_sn (text annotation)
        3. MARK knickpoints with symbols (vertices between segments)

        Users can immediately see:
        • Which segment is steepest (highest k_sn) → active incision zone?
        • How many segments → how many adjustment events?
        • Monotonic increase or decrease → unidirectional change?

        Args:
            chi (np.ndarray)
                Chi coordinate array.

            elev (np.ndarray)
                Elevation array.

            slope (np.ndarray)
                Local slope array (m/m).

            area_m2 (np.ndarray)
                Drainage area array (m²).

            theta_ref (float)
                Reference concavity (0.40–0.60).

            knickpoints (list)
                List of dicts with 'idx' key (array indices).

            ksn_method (str)
                "chi_slope" (default) or "regression".

        Returns:
            list
                Segment metrics, each a dict with keys:
                chi_start, chi_end, elev_start, elev_end, ksn, theta_local.
        """
        n = len(chi)
    
        # 1. Define segment boundaries: Source (0) -> Knickpoints -> Mouth (n-1)
        boundaries = [0] + [kp["idx"] for kp in knickpoints] + [n - 1]
        boundaries = sorted(list(set(boundaries))) # Ensure order and uniqueness

        segments = []
        
        for j in range(len(boundaries) - 1):
            i0, i1 = boundaries[j], boundaries[j + 1]
            
            # Check if segment has enough data points for statistical validity
            if i1 - i0 < MIN_SEGMENT_PTS:
                continue
            
            # Coordinates for the segment edges
            z_start, z_end = elev[i0], elev[i1]
            chi_start, chi_end = chi[i0], chi[i1]

            if ksn_method == "chi_slope":
                # --- METHOD A: GEOMETRIC INTEGRAL (Chi-Slope) ---
                # Standard: ksn is the slope of the line in Chi-Elevation space.
                # Insensitive to internal pixel-level fluctuations.
                dz = abs(z_start - z_end)
                dchi = abs(chi_start - chi_end)
                
                ksn_seg = dz / dchi if dchi > 1e-6 else 0.0
                # approximation of local concavity by the slope/regression of the segment in chi-elev space (just for info/statistics, not used to correct ksn)
                s_seg = slope[i0:i1]
                a_seg = area_m2[i0:i1]
                mask = s_seg > 1e-6
                
                if np.sum(mask) > MIN_SEGMENT_PTS:
                   # Local concavity (theta) can be approximated by the slope of log S vs log A in the segment, but only if there are enough valid points
                    th_local = abs(np.polyfit(np.log10(a_seg[mask]), np.log10(s_seg[mask]), 1)[0])
                else:
                    th_local = theta_ref

            else:
                # --- METHOD B: ANALYTICAL REGRESSION (Log S / Log A) ---
                # Standard: Solve log(S) = log(Ks) - theta * log(A).
                # Requires cleaning points where slope is zero (DEM steps).
                s_seg = slope[i0:i1]
                a_seg = area_m2[i0:i1]
                
                mask = s_seg > 1e-6 # Mask out horizontal pixels (steps)
                if np.sum(mask) < MIN_SEGMENT_PTS:
                    continue
                    
                log_A = np.log10(a_seg[mask])
                log_S = np.log10(s_seg[mask])

                # Regression is only valid if there is sufficient variation in Drainage Area
                if np.ptp(log_A) < 0.01:
                    continue

                try:
                    # OLS Regression: returns [slope, intercept]
                    # polyfit returns slope = -theta_local (negative in theory), intercept = log(Ks)
                    coeffs   = np.polyfit(log_A, log_S, 1)
                    slope = coeffs[0]
                    intercept = coeffs[1]
                    th_local = abs(slope)     # Local concavity (positive value)
                    Ks       = 10 ** intercept # Geomorphic steepness index for the segment (intercept = log(Ks) then ks = 10^intercept)
                    log_A_max = np.max(log_A)
                    log_A_min = np.min(log_A)
                    A_cent = 10**((log_A_max + log_A_min) / 2.0)

                    ksn_seg = Ks * (A_cent ** (theta_ref - th_local))
                except (np.linalg.LinAlgError, ValueError):
                    continue

            segments.append({
                "chi_start":   round(float(chi_start), 4),
                "chi_end":     round(float(chi_end),   4),
                "elev_start":  round(float(z_start),   2),
                "elev_end":    round(float(z_end),     2),
                "ksn":         round(float(ksn_seg),   2),
                "theta_local": round(float(th_local),  4),
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
        r"""
        Computes the theoretical equilibrium river profile using Hack's (1973)
        scaling relation and logarithmic interpolation.

        CONCEPT: Equilibrium Profile
        ============================
        In STEADY-STATE (no change in base-level, uniform uplift, constant
        climate), a river profile reaches DYNAMIC EQUILIBRIUM:

        Z(x) = Z_mouth + k_sn · χ(x)

        This is a linear relationship in chi-space, which translates to a
        CONCAVE-UP (non-linear) curve in distance-space.

        The equilibrium profile represents the EXPECTED shape if the basin
        has NO RECENT TRANSIENT FEATURES (knickpoints, base-level changes).

        Deviations from Equilibrium
        ───────────────────────────
        Actual profile vs. equilibrium tells us:

        • Profile ABOVE equilibrium line
          ↓ River is ACTIVELY INCISING
          ↓ Recent base-level drop OR increased uplift
          ↓ Energy available to cut rock faster than usual

        • Profile BELOW equilibrium line  
          ↓ River is AGGRADING (unusual)
          ↓ Sediment supply exceeds transport capacity
          ↓ Less common in tectonically active regions; more common in deltas

        • Profile ON equilibrium line (mean deviation ≈ 0 over 100+ km)
          ↓ Long-term steady-state
          ↓ Uplift ≈ erosion averaged over basin
          ↓ No recent tectonics or base-level change

        HACK'S SCALING LAW (Hack 1973)
        =============================
        Hack observed that river profiles follow:

            L = a · (Area)^b

        where L = distance along channel, Area = drainage area.
        Empirically: b ≈ 0.6 for most terrains.

        This translates to elevation scaling:

            Z = AH - [(AH - AL) / log(L_max - L_min)] · log(L / L_min)

        A simplified logarithmic form:

            Z(x) = AH - [(AH - AL) / log(L_total)] · log(L(x) / L_start)

        where:
          AH = maximum elevation (at source) [meters]
          AL = minimum elevation (at mouth) [meters]
          L_total = total river length [meters]
          L_start = distance at the first valid point (avoid log(0))
          L(x) = distance at position x

        FORMULA (This Function)
        =======================
        We use a variant that normalizes distance to avoid log(0):

        Z_eq[i] = AH - [(AH - AL) / (log(L_max) - log(L_start))] · (log(L[i]) - log(L_start))

        where L_max is the total length from source to mouth.

        Normalization:
          • If L[i] = L_start → log ratio = 0 → Z_eq = AH (source)
          • If L[i] = L_max → log ratio = maximum → Z_eq = AL (mouth)
          • Intermediate: logarithmic decay from source to mouth

        EQUILIBRIUM INTERPRETATION
        ==========================
        This formula assumes:
          1. Channel aspect ratio is self-similar (Hack scaling)
          2. No recent base-level changes
          3. No lithologic boundaries
          4. Uniform climate and tectonics

        If actual river profile wiggles around this curve with RMS deviation < 50m,
        the basin is likely IN EQUILIBRIUM over the timescale that shaped it.

        NUMERICAL IMPLEMENTATION
        ========================

        Step 1: Identify Extrema
        ───────────────────────
        AH = max(elev)   → source elevation
        AL = min(elev)   → mouth elevation
        dH = AH - AL     → total relief

        Step 2: Distance Safeguarding
        ─────────────────────────────
        dist_safe = distance array, with zero values clipped.
        Prevents log(0) error.

        log_L_max = log10(dist_safe[-1])      ← log of total length
        log_L_min = log10(min of dist_safe > 0)  ← log of minimum distance

        Step 3: Formula Application
        ──────────────────────────
        For each point i:
            log_L[i] = log10(dist_safe[i])

            Z_eq[i] = AH - (dH / (log_L_max - log_L_min)) · (log_L[i] - log_L_min)

        This is a linear function in log-distance space.

        Step 4: Edge Cases
        ───────────────────
        • If dH ≈ 0 (flat basin) → return uniform elevation (impossible to normalize)
        • If log-distance range is too small (< 1e-10) → return mean elevation
        • If dist array has all zeros → return all AH (degenerate case)

        OUTPUT
        ======
        equilibrium_profile : np.ndarray
            Theoretical equilibrium elevations at each point (m).
            Same length as input arrays.
            Smoothly decreasing from source to mouth.

        USAGE
        =====
        In the analysis output, we return:
          • "equil_elev": the equilibrium profile (array)
          • Visualization tools can plot (dist, equil_elev) alongside (dist, actual_elev)
          • Residuals: resid[i] = elev[i] - equil_elev[i]
            (Positive → above equilibrium → active incision)

        REFERENCES
        ==========
        Hack, J. T. (1973). Stream-profile analysis and stream-gradient index.
        U.S. Geological Survey Journal of Research, 1(4), 421–429.

        Mvondo Owono, R. (2010). Morphotectonic analysis of the Sanaga river
        drainage basin. PhD thesis, University of Yaoundé I.

        Args:
            dist (np.ndarray)
                Cumulative distance along river (m), length n.
            elev (np.ndarray)
                Elevation at each point (m), length n.

        Returns:
            np.ndarray
                Equilibrium elevation at each point (m), same shape as inputs.
                Monotonically decreasing from source to mouth.
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
        """
        Recomputes local channel slope after elevation smoothing.

        PURPOSE
        =======
        When elevation data is smoothed (moving average filter), slope
        values must also be recalculated to remain consistent with the
        SMOOTHED elevations. Otherwise slope and elevation become uncoupled.

        METHOD
        ======
        Uses CENTRED FINITE DIFFERENCES for interior points:

            slope[i] = |Δelev / Δdist| = |elev[i+1] - elev[i-1]| / (dist[i+1] - dist[i-1])

        This averages over TWO intervals and is more stable than forward
        or backward differences.

        Edge Points:
        ────────────
        Index 0 (source):  slope[0] = |elev[1] - elev[0]| / (dist[1] - dist[0])
        Index n-1 (mouth): slope[-1] = |elev[-1] - elev[-2]| / (dist[-1] - dist[-2])

        ONE-SIDED differences at edges (unavoidable).

        NUMERICAL SAFEGUARDS
        ====================
        • If Δdist < 1e-6 m → set slope = 1e-8 (avoid division by zero)
        • All slopes clipped to minimum 1e-8 (prevent negative or zero values)
        • Absolute value ensures slope is always positive

        Args:
            elev (np.ndarray): Smoothed elevation array (m)
            dist (np.ndarray): Distance array (m)

        Returns:
            np.ndarray: Local slope array (m/m), same shape as inputs
        """
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
        Retrieves or computes the Flow Accumulation raster (FAC).

        PURPOSE
        =======
        Flow Accumulation (FAC) is a DERIVED RASTER that counts the number
        of upstream pixels that drain to EACH pixel, weighted by pixel area.

        Formula (for pixel-based D8 algorithm):
            FAC[i,j] = 1 + sum(FAC of 8 upstream neighbors that flow to [i,j])

        Units: m² (when multiplied by pixel area) or "cells" (dimensionless count).

        PHYSICAL MEANING
        ================
        FAC ~ Drainage Area:
            • High FAC = large upstream area = major river channel
            • Low FAC = small upstream area = headwater stream
            • FAC = 1 = source pixel (only itself drains here)

        Importance in morphology:
            • k_sn scaling depends on accurate area (S = k_sn · A^-θ)
            • Chi transformation integrates over area: χ = ∫(A₀/A)^θ dx
            • Any error in FAC propagates into all downstream metrics

        COMPUTATION METHOD: GRASS r.watershed
        =====================================
        If user does not provide a pre-computed FAC, we use GRASS r.watershed:

        Command:
        ────────
        processing.run("grass7:r.watershed", {
            "elevation": DEM raster,
            "accumulation": "TEMPORARY_OUTPUT",  ← output raster
            "-a": True,  ← absolute accumulation (m²), not cell count
            ...
        })

        Why GRASS?
        ──────────
        • Industry standard hydrological tool (accurate D8 flow routing)
        • Handles large rasters efficiently (C implementation)
        • Produces absolute accumulation (A in m²), not cell counts
        • Properly accounts for flow divergence and confluence

        Why Cache?
        ──────────
        Computing FAC via GRASS is EXPENSIVE (typically 10–60 seconds for
        large DEMs). We cache by DEM path (class-level _fac_cache dict).

        Same DEM → same FAC (deterministic).
        Cache persists across compute() calls in single session.
        Avoids redundant computation for the same DEM.

        ALTERNATIVE: User-Provided FAC
        ===============================
        Users can provide a pre-computed FAC raster:

        • From GRASS (already conditioned)
        • From TauDEM (topographic analysis)
        • From other GIS software (ArcGIS, QGIS native tools)

        Advantage: Use custom FAC (e.g., flow-conditioned, pit-filled)
        that may be better than automatic computation.
        Disadvantage: Responsibility on user to provide VALID FAC.

        ALGORITHM
        =========

        Step 1: Check if user provided valid FAC
        ────────────────────────────────────────
        if fac_layer is not None and fac_layer.isValid():
            return fac_layer (user-provided; use directly)

        Step 2: Check cache
        ──────────────────
        dem_path = dem_layer.source()   (file path or URI)
        if dem_path in _fac_cache:
            return self._fac_cache[dem_path]   (reuse previous result)

        Step 3: Compute via GRASS
        ────────────────────────
        Call processing.run("grass7:r.watershed", ...)
        If successful: fac_layer is a valid raster
        If failed: raise exception (caught by caller)

        Step 4: Cache and return
        ───────────────────────
        self._fac_cache[dem_path] = fac_layer
        return (fac_layer, True, warning_msg)

        RETURN VALUE
        ============
        tuple: (fac_layer, was_auto_computed, warning_msg)

        fac_layer : QgsRasterLayer | None
            Valid FAC raster if success.
            None if computation failed (fatal).

        was_auto_computed : bool
            True if FAC was computed automatically or loaded from cache.
            False if user provided it directly.
            (Used to report to user: "FAC was auto-generated" vs. "FAC was pre-computed")

        warning_msg : str | None
            Informational message if non-fatal issue:
            • "FAC computed via GRASS. For best results, provide pre-conditioned FAC."
            • "FAC loaded from cache; previously computed for this DEM."
            • None if clean success OR fatal failure (error message in exception)

        ERROR HANDLING
        ==============
        • Invalid DEM layer → early return (None, ..., reason)
        • GRASS command fails → catch exception, return (None, ..., error msg)
        • FAC output is invalid → raise RuntimeError (treated as fatal)
        • User provides invalid FAC → rejected and auto-computed instead

        Args:
            dem_layer (QgsRasterLayer)
                Digital Elevation Model (input for GRASS).
            fac_layer (QgsRasterLayer | None)
                User-provided FAC (optional).
            progress_cb (callable | None)
                Progress callback: progress_cb(percentage, message).

        Returns:
            tuple (fac_layer, was_auto, warning)
                fac_layer : QgsRasterLayer | None
                was_auto : bool
                warning : str | None
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
        Converts a numpy array or list to a JSON-safe Python list.

        PURPOSE
        =======
        Numpy arrays and numpy scalars are NOT JSON-serializable by default.
        Matplotlib has NaN and Inf values that JSON doesn't recognize.

        This function converts:
        • np.ndarray → list (JSON-compatible)
        • np.NaN → None (becomes null in JSON)
        • np.Inf, -np.Inf → None (becomes null in JSON)
        • Regular floats → floats (unchanged)

        JSON COMPATIBILITY
        ==================
        JSON standard (RFC 7159) defines:
        • null (None in Python) — valid
        • NaN, Infinity — NOT VALID (will cause parsing errors in strict parsers)

        Many web clients (JavaScript JSON.parse) will accept NaN and Infinity,
        but best practice is to use null for missing/invalid data.

        VISUALIZATION IMPACT
        ====================
        Web plotting libraries (Plotly, Chart.js):
        • null data points → skipped or line gaps
        • NaN → often treated as null automatically
        • Inf → may break axis scaling

        For fluvial analysis:
        • NaN/Inf usually appear at profile edges (insufficient window)
          or in low-signal regions (flat terrain)
        • Replacing with null allows JavaScript to skip them gracefully

        ALGORITHM
        =========

        Step 1: Validate input
        ─────────────────────
        if data is None:
            return None (pass through)

        Step 2: Convert to numpy array
        ──────────────────────────────
        arr = np.array(data, dtype=np.float64)

        Ensures all scalars/lists are uniform, even if input was mixed.

        Step 3: Create boolean mask
        ───────────────────────────
        mask = np.isnan(arr) | np.isinf(arr)

        True where value is NaN or Inf (any sign).

        Step 4: Build JSON-safe list
        ────────────────────────────
        clean_list = [val if not m else None for val, m in zip(arr.tolist(), mask)]

        • val (if m is False) → normal float value
        • None (if m is True) → becomes JSON null

        Step 5: Return
        ──────────────
        Return clean_list (pure Python, JSON-serializable).

        EXAMPLE
        =======
        Input:  np.array([1.0, np.nan, 3.5, np.inf, 2.1])
        Output: [1.0, None, 3.5, None, 2.1]

        JSON:   [1.0, null, 3.5, null, 2.1]

        PERFORMANCE
        ===========
        • O(n) where n = array length (single pass)
        • Negligible for typical arrays (< 1000 points)
        • Vectorized operations (isnan, isinf are fast)

        Args:
            data (np.ndarray | list | None)
                Numeric array with possible NaN/Inf values.

        Returns:
            list | None
                JSON-safe Python list with None replacing NaN/Inf.
                Returns None if input is None.
        """
        if data is None: 
            return None
        arr = np.array(data, dtype=np.float64)
        # Replace Inf and NaN with a safe value (None/null)
        mask = np.isnan(arr) | np.isinf(arr)
        clean_list = [val if not m else None for val, m in zip(arr.tolist(), mask)]
        return clean_list