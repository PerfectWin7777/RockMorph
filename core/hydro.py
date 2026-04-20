# core/hydro.py

"""
core/hydro.py

Hydrological utilities for RockMorph.

Provides:
  - MainRiverExtractor : extracts the main river (Hack stream) per basin
                         from a stream network layer + DEM, using a
                         topology-aware graph with fuzzy snapping.
  - sample_river_profile : discretizes an oriented river geometry and
                           samples elevation from a DEM.
                           Shared by NCP tool and future SL-index tool.

Design rules
------------
- Zero UI logic — pure computation only.
- Mirrors RasterReader / SwathSampler conventions exactly.
- Handles geographic and projected CRS transparently.
- Skips invalid inputs gracefully — never crashes the full run.

Authors: RockMorph contributors / Tony
"""

import math
import numpy as np  # type: ignore

from qgis.core import (  # type: ignore
    QgsVectorLayer, QgsRasterLayer, QgsProject,
    QgsCoordinateTransform, QgsGeometry, QgsWkbTypes,
    QgsSpatialIndex, QgsDistanceArea, QgsPointXY,
)
from PyQt5.QtCore import QCoreApplication  # type: ignore


def tr(message):
    return QCoreApplication.translate("RockMorph", message)


# Minimum number of points to consider a river profile computable
MIN_RIVER_POINTS = 5


# ---------------------------------------------------------------------------
# MainRiverExtractor
# ---------------------------------------------------------------------------

class MainRiverExtractor:
    """
    Extracts the main river (longest upstream path) for every basin polygon
    in a basin layer, using a stream network layer and a DEM for orientation.

    Algorithm (Tony's robust bridge algorithm)
    ------------------------------------------
    For each basin:
      1. Pre-filter stream candidates via QgsSpatialIndex (bounding box)
      2. Clip each candidate to the basin geometry
      3. Orient every segment upstream → downstream via DEM elevation
      4. Build a connectivity graph with fuzzy snapping (snap_dist_m)
      5. Compute cumulative upstream weight (recursive, cycle-safe)
      6. Trace the main river from the best outlet back to the source

    Parameters
    ----------
    basin_layer   : QgsVectorLayer  — polygon layer, one feature per basin
    stream_layer  : QgsVectorLayer  — polyline network (all streams)
    dem_layer     : QgsRasterLayer  — DEM for elevation sampling
    snap_dist_m   : float           — snapping tolerance in metres (default 2.0)
    label_field   : str | None      — attribute field for basin labels
    """

    def __init__(
        self,
        basin_layer:  QgsVectorLayer,
        stream_layer: QgsVectorLayer,
        dem_layer:    QgsRasterLayer,
        snap_dist_m:  float = 2.0,
        label_field:  str   = None,
    ):
        self.basin_layer  = basin_layer
        self.stream_layer = stream_layer
        self.dem_layer    = dem_layer
        self.snap_dist_m  = snap_dist_m
        self.label_field  = label_field

        # Build spatial index and feature cache once — reused for all basins
        self._stream_index    = QgsSpatialIndex(stream_layer.getFeatures())
        self._stream_features = {
            f.id(): f for f in stream_layer.getFeatures()
        }

        # CRS transforms — computed once
        self._t_basin_to_stream = self._make_transform(
            basin_layer.crs(), stream_layer.crs()
        )
        self._t_stream_to_dem = self._make_transform(
            stream_layer.crs(), dem_layer.crs()
        )

        # Distance area — metric length even for geographic CRS
        self._da = QgsDistanceArea()
        self._da.setSourceCrs(
            stream_layer.crs(),
            QgsProject.instance().transformContext()
        )
        self._da.setEllipsoid('WGS84')

        # Resolve label field
        self._resolved_label = self._resolve_label_field(
            basin_layer, label_field
        )

    # ------------------------------------------------------------------
    # Public API
    # ------------------------------------------------------------------

    def extract_all(self) -> dict:
        """
        Run extraction for every basin feature.

        Returns
        -------
        dict with keys:
            results  : list of per-basin dicts (see _extract_basin)
            skipped  : list of (fid, reason) tuples
            warnings : list of human-readable warning strings
        """
        results  = []
        skipped  = []
        warnings = []

        seen_geom_hashes = set()

        for basin_feat in self.basin_layer.getFeatures():
            fid   = basin_feat.id()
            label = self._get_label(basin_feat, fid)

            # ── Geometry validation ───────────────────────────────
            geom = basin_feat.geometry()
            if geom is None or geom.isEmpty():
                skipped.append((fid, "empty geometry"))
                warnings.append(tr(
                    f"Basin '{label}' (FID {fid}): empty geometry — skipped."
                ))
                continue

            if not geom.isGeosValid():
                geom = geom.makeValid()
                if not geom.isGeosValid():
                    skipped.append((fid, "invalid geometry, repair failed"))
                    warnings.append(tr(
                        f"Basin '{label}' (FID {fid}): "
                        f"invalid geometry, could not repair — skipped."
                    ))
                    continue
                warnings.append(tr(
                    f"Basin '{label}' (FID {fid}): "
                    f"geometry repaired automatically."
                ))

            # ── Duplicate detection ───────────────────────────────
            geom_hash = str(hash(geom.asWkt(precision=4)))
            if geom_hash in seen_geom_hashes:
                skipped.append((fid, "duplicate geometry"))
                warnings.append(tr(
                    f"Basin '{label}' (FID {fid}): "
                    f"duplicate geometry — skipped."
                ))
                continue
            seen_geom_hashes.add(geom_hash)

            # ── Reproject basin to stream CRS if needed ───────────
            geom_stream_crs = self._reproject_geom(
                geom, self._t_basin_to_stream
            )

            # ── Extract ───────────────────────────────────────────
            try:
                result = self._extract_basin(
                    fid, label, geom_stream_crs
                )
                if result is None:
                    skipped.append((fid, "no stream found in basin"))
                    warnings.append(tr(
                        f"Basin '{label}' (FID {fid}): "
                        f"no stream found — skipped."
                    ))
                    continue
                results.append(result)

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
    # Per-basin extraction — Tony's algorithm
    # ------------------------------------------------------------------

    def _extract_basin(
        self,
        fid:   int,
        label: str,
        geom:  QgsGeometry,   # already in stream CRS
    ) -> dict | None:
        """
        Extract the main river for one basin.
        Returns None if no valid stream found.
        """

        # ── Step 1 : pre-filter via spatial index ────────────────
        segments = []
        candidate_ids = self._stream_index.intersects(geom.boundingBox())

        for s_id in candidate_ids:
            s_feat = self._stream_features.get(s_id)
            if s_feat is None:
                continue
            if not s_feat.geometry().intersects(geom):
                continue

            clipped = s_feat.geometry().intersection(geom)
            if clipped.isEmpty():
                continue
            if clipped.type() != QgsWkbTypes.LineGeometry:
                continue

            parts = (
                clipped.asMultiPolyline()
                if clipped.isMultipart()
                else [clipped.asPolyline()]
            )

            for part in parts:
                if len(part) < 2:
                    continue

                # ── Step 2 : orient upstream → downstream via DEM ─
                part = self._orient_segment(part)
                length_m = self._da.measureLine(
                    [QgsPointXY(p) for p in part]
                )
                segments.append({
                    'pts':   part,
                    'len':   length_m,
                    'start': part[0],
                    'end':   part[-1],
                })

        if not segments:
            return None

        # ── Step 3 : connectivity graph with fuzzy snap ───────────
        n = len(segments)
        upstream_map = {i: [] for i in range(n)}
        for i in range(n):
            s_node = segments[i]['start']
            for j in range(n):
                if i == j:
                    continue
                if s_node.distance(segments[j]['end']) <= self.snap_dist_m:
                    upstream_map[i].append(j)

        # ── Step 4 : cumulative upstream weights (cycle-safe) ─────
        weights = {}

        def get_w(idx, visiting=None):
            if idx in weights:
                return weights[idx]
            if visiting is None:
                visiting = set()
            if idx in visiting:
                # Cycle detected — cut here
                return segments[idx]['len']
            visiting.add(idx)
            up_idxs = upstream_map[idx]
            if not up_idxs:
                weights[idx] = segments[idx]['len']
            else:
                weights[idx] = segments[idx]['len'] + max(
                    get_w(u, visiting) for u in up_idxs
                )
            return weights[idx]

        for i in range(n):
            get_w(i)

        # ── Step 5 : best outlet = max cumulative weight ──────────
        best_outlet_idx = max(range(n), key=lambda i: weights[i])
        total_length_m  = weights[best_outlet_idx]

        # ── Step 6 : trace main river source → mouth ──────────────
        final_points = []
        curr_idx     = best_outlet_idx
        visited_path = set()

        while True:
            if curr_idx in visited_path:
                break
            visited_path.add(curr_idx)

            seg = segments[curr_idx]
            pts = list(seg['pts'])
            if not final_points:
                final_points.extend(pts[::-1])
            else:
                final_points.extend(pts[::-1][1:])

            up_idxs = upstream_map[curr_idx]
            if not up_idxs:
                break
            curr_idx = max(up_idxs, key=lambda i: weights[i])

        if len(final_points) < 2:
            return None

        # Build final geometry in stream CRS
        river_geom = QgsGeometry.fromPolylineXY(final_points)

        return {
            "label":         label,
            "fid":           fid,
            "geom":          river_geom,       # QgsGeometry, stream CRS
            "length_m":      round(total_length_m, 2),
            "n_segments":    len(segments),
        }

    # ------------------------------------------------------------------
    # DEM orientation helper
    # ------------------------------------------------------------------

    def _orient_segment(self, part: list) -> list:
        """
        Orient a polyline segment so that part[0] is upstream (high elev)
        and part[-1] is downstream (low elev).
        Reprojects points to DEM CRS before sampling.
        """
        pt_first = QgsPointXY(part[0])
        pt_last  = QgsPointXY(part[-1])

        # Reproject to DEM CRS
        if self._t_stream_to_dem is not None:
            pt_first = self._t_stream_to_dem.transform(pt_first)
            pt_last  = self._t_stream_to_dem.transform(pt_last)

        z_first = self._get_z(pt_first)
        z_last  = self._get_z(pt_last)

        # If first point is lower than last → reverse
        if z_first < z_last:
            return part[::-1]
        return part

    def _get_z(self, pt: QgsPointXY) -> float:
        """Sample DEM elevation at a QgsPointXY (in DEM CRS)."""
        val, ok = self.dem_layer.dataProvider().sample(pt, 1)
        if ok and not math.isnan(val):
            return val
        return -9999.0

    # ------------------------------------------------------------------
    # CRS helpers
    # ------------------------------------------------------------------

    @staticmethod
    def _make_transform(src_crs, dst_crs) -> QgsCoordinateTransform | None:
        """Return a QgsCoordinateTransform or None if CRS are identical."""
        if src_crs == dst_crs:
            return None
        return QgsCoordinateTransform(
            src_crs, dst_crs, QgsProject.instance()
        )

    @staticmethod
    def _reproject_geom(
        geom: QgsGeometry,
        transform: QgsCoordinateTransform | None,
    ) -> QgsGeometry:
        """Return a reprojected copy of geom, or the original if no transform."""
        if transform is None:
            return geom
        geom_copy = QgsGeometry(geom)
        geom_copy.transform(transform)
        return geom_copy

    # ------------------------------------------------------------------
    # Label helpers — same pattern as HypsometryEngine
    # ------------------------------------------------------------------

    @staticmethod
    def _resolve_label_field(layer, user_field: str = None) -> str | None:
        fields     = [f.name().lower() for f in layer.fields()]
        candidates = ['nom', 'name', 'id', 'label', 'bv', 'basin', 'watershed']
        if user_field and user_field.lower() in fields:
            return user_field
        for candidate in candidates:
            if candidate in fields:
                for f in layer.fields():
                    if f.name().lower() == candidate:
                        return f.name()
        return None

    def _get_label(self, feature, fid: int) -> str:
        if self._resolved_label is None:
            return f"Basin_{fid}"
        val = feature[self._resolved_label]
        if val is None or str(val).strip() == '':
            return f"Basin_{fid}"
        return str(val).strip()


# ---------------------------------------------------------------------------
# sample_river_profile — shared by NCP and future SL-index tool
# ---------------------------------------------------------------------------

def sample_river_profile(
    river_geom:  QgsGeometry,
    dem_layer:   QgsRasterLayer,
    stream_crs,
    n_points:    int = 200,
) -> dict:
    """
    Discretize an oriented river geometry and sample elevation from DEM.

    The river geometry must already be oriented source → mouth
    (as returned by MainRiverExtractor).

    Parameters
    ----------
    river_geom  : QgsGeometry     — oriented polyline in stream CRS
    dem_layer   : QgsRasterLayer  — DEM
    stream_crs  : QgsCoordinateReferenceSystem
    n_points    : int             — number of sample points along river

    Returns
    -------
    dict with keys:
        distances_m  : np.ndarray  — cumulative distance from source (m)
        elevations   : np.ndarray  — elevation at each point (m)
        valid        : bool        — False if fewer than MIN_RIVER_POINTS valid
        total_length_m : float
    """
    # Transform stream → DEM CRS if needed
    dem_crs   = dem_layer.crs()
    transform = None
    if stream_crs != dem_crs:
        transform = QgsCoordinateTransform(
            stream_crs, dem_crs, QgsProject.instance()
        )

    # Distance area for metric lengths
    da = QgsDistanceArea()
    da.setSourceCrs(stream_crs, QgsProject.instance().transformContext())
    da.setEllipsoid('WGS84')

    # Extract polyline vertices in order
    vertices = [QgsPointXY(v) for v in river_geom.vertices()]
    if len(vertices) < 2:
        return {"valid": False}

    # Build cumulative distance along vertices
    cum_dist = [0.0]
    for i in range(1, len(vertices)):
        d = da.measureLine(vertices[i - 1], vertices[i])
        cum_dist.append(cum_dist[-1] + max(d, 0.0))

    total_length_m = cum_dist[-1]
    if total_length_m < 1e-6:
        return {"valid": False}

    # Interpolate N equally-spaced points along the polyline
    target_dists = np.linspace(0, total_length_m, n_points)
    sample_pts   = []

    for td in target_dists:
        for i in range(1, len(cum_dist)):
            if cum_dist[i] >= td or i == len(cum_dist) - 1:
                seg_len = cum_dist[i] - cum_dist[i - 1]
                if seg_len < 1e-10:
                    pt = vertices[i]
                else:
                    t  = (td - cum_dist[i - 1]) / seg_len
                    pt = QgsPointXY(
                        vertices[i-1].x() + t * (vertices[i].x() - vertices[i-1].x()),
                        vertices[i-1].y() + t * (vertices[i].y() - vertices[i-1].y()),
                    )
                sample_pts.append(pt)
                break

    # Sample DEM at each point
    elevations = []
    for pt in sample_pts:
        pt_dem = transform.transform(pt) if transform else pt
        val, ok = dem_layer.dataProvider().sample(pt_dem, 1)
        elevations.append(
            float(val) if (ok and not math.isnan(val)) else np.nan
        )

    distances_m = np.array(target_dists, dtype=np.float64)
    elevations  = np.array(elevations,   dtype=np.float64)

    # Remove NaN — keep paired arrays aligned
    valid_mask  = ~np.isnan(elevations)
    distances_m = distances_m[valid_mask]
    elevations  = elevations[valid_mask]

    if len(elevations) < MIN_RIVER_POINTS:
        return {"valid": False}

    return {
        "valid":           True,
        "distances_m":     distances_m,
        "elevations":      elevations,
        "total_length_m":  total_length_m,
        "n_points":        len(elevations),
    }




def sample_river_hydraulics(
    river_geom:  QgsGeometry,
    dem_layer:   QgsRasterLayer,
    fac_layer:   QgsRasterLayer,
    stream_crs,
    n_points:    int = 200,
) -> dict:
    """
    Extends sample_river_profile() with hydraulic attributes
    required for SL, SLk, k_sn, and chi computation.

    Calls sample_river_profile() internally — never duplicates logic.

    Parameters
    ----------
    river_geom  : oriented polyline, source → mouth (from MainRiverExtractor)
    dem_layer   : QgsRasterLayer — DEM
    fac_layer   : QgsRasterLayer — flow accumulation (cells count)
    stream_crs  : QgsCoordinateReferenceSystem
    n_points    : number of sample points

    Returns
    -------
    dict — everything from sample_river_profile(), plus:
        area_m2      : np.ndarray  — drainage area at each point (m²)
        slope_local  : np.ndarray  — local slope dz/dl (m/m), centred diff
        fac_valid    : bool        — False if FAC sampling failed for >50% pts
    """
    # ── Step 1 : delegate to existing function ────────────────────────
    base = sample_river_profile(
        river_geom  = river_geom,
        dem_layer   = dem_layer,
        stream_crs  = stream_crs,
        n_points    = n_points,
    )

    if not base["valid"]:
        return {**base, "fac_valid": False}

    distances_m = base["distances_m"]
    elevations  = base["elevations"]
    n           = len(distances_m)

    # ── Step 2 : sample FAC along the same points ─────────────────────
    # We rebuild the interpolated points from distances — same logic as
    # sample_river_profile, but we only need the XY coordinates here.
    sample_pts = _interpolate_river_points(river_geom, distances_m, stream_crs)

    # FAC CRS transform
    fac_crs   = fac_layer.crs()
    t_to_fac  = None
    if stream_crs != fac_crs:
        t_to_fac = QgsCoordinateTransform(
            stream_crs, fac_crs, QgsProject.instance()
        )

    # Pixel area in m² — needed to convert cell count → drainage area
    pixel_size_m = _pixel_size_metres(fac_layer)
    cell_area_m2 = pixel_size_m ** 2

    raw_fac = []
    for pt in sample_pts:
        pt_fac = t_to_fac.transform(pt) if t_to_fac else pt
        val, ok = fac_layer.dataProvider().sample(pt_fac, 1)
        raw_fac.append(float(val) if (ok and not math.isnan(val)) else np.nan)

    area_m2 = np.array(raw_fac, dtype=np.float64) * cell_area_m2

    # Guard: if >50% NaN, FAC sampling is unusable
    fac_valid = np.sum(~np.isnan(area_m2)) >= (n * 0.5)

    # Replace NaN with forward-fill then backward-fill — avoids gaps
    # in the middle of the profile breaking chi integral
    area_m2 = _fill_nan(area_m2)

    # Clamp to minimum 1 m² — log10(0) would crash chi computation
    area_m2 = np.where(area_m2 > 0, area_m2, cell_area_m2)

    # ── Step 3 : local slope — centred finite differences ─────────────
    # S[i] = |dz/dl| using neighbours i-1 and i+1
    # Edges use one-sided differences
    slope_local = np.empty(n, dtype=np.float64)

    # Interior points — centred
    dz = elevations[2:] - elevations[:-2]
    dl = distances_m[2:] - distances_m[:-2]
    slope_local[1:-1] = np.where(dl > 1e-6, np.abs(dz / dl), np.nan)

    # Edges — one-sided
    if distances_m[1] - distances_m[0] > 1e-6:
        slope_local[0] = abs(elevations[1] - elevations[0]) / (
            distances_m[1] - distances_m[0]
        )
    else:
        slope_local[0] = np.nan

    if distances_m[-1] - distances_m[-2] > 1e-6:
        slope_local[-1] = abs(elevations[-1] - elevations[-2]) / (
            distances_m[-1] - distances_m[-2]
        )
    else:
        slope_local[-1] = np.nan

    # Clamp zero slope to tiny value — log10(0) guard for k_sn
    slope_local = np.where(slope_local > 1e-8, slope_local, 1e-8)

    return {
        **base,                        # distances_m, elevations, total_length_m …
        "area_m2":      area_m2,
        "slope_local":  slope_local,
        "fac_valid":    fac_valid,
    }


# ---------------------------------------------------------------------------
# Private helpers — used only by sample_river_hydraulics
# ---------------------------------------------------------------------------

def _interpolate_river_points(
    river_geom: QgsGeometry,
    distances_m: np.ndarray,
    stream_crs,
) -> list:
    """
    Re-interpolates QgsPointXY positions at given cumulative distances
    along river_geom. Mirrors the logic inside sample_river_profile
    so the returned XY coordinates match the elevation samples exactly.

    Returns list of QgsPointXY (in stream_crs).
    """
    da = QgsDistanceArea()
    da.setSourceCrs(stream_crs, QgsProject.instance().transformContext())
    da.setEllipsoid('WGS84')

    vertices  = [QgsPointXY(v) for v in river_geom.vertices()]
    cum_dist  = [0.0]
    for i in range(1, len(vertices)):
        d = da.measureLine(vertices[i - 1], vertices[i])
        cum_dist.append(cum_dist[-1] + max(d, 0.0))

    sample_pts = []
    for td in distances_m:
        for i in range(1, len(cum_dist)):
            if cum_dist[i] >= td or i == len(cum_dist) - 1:
                seg_len = cum_dist[i] - cum_dist[i - 1]
                if seg_len < 1e-10:
                    pt = vertices[i]
                else:
                    t  = (td - cum_dist[i - 1]) / seg_len
                    pt = QgsPointXY(
                        vertices[i-1].x() + t * (vertices[i].x() - vertices[i-1].x()),
                        vertices[i-1].y() + t * (vertices[i].y() - vertices[i-1].y()),
                    )
                sample_pts.append(pt)
                break
    return sample_pts


def _pixel_size_metres(raster_layer: QgsRasterLayer) -> float:
    """
    Returns the pixel size in metres.
    Handles both projected (metres) and geographic (degrees) CRS.
    For geographic CRS, approximates using the centre latitude.
    """
    crs = raster_layer.crs()
    ext = raster_layer.extent()
    px  = raster_layer.rasterUnitsPerPixelX()

    if not crs.isGeographic():
        return abs(px)

    # Geographic CRS — convert degrees to metres at centre latitude
    centre_lat = (ext.yMinimum() + ext.yMaximum()) / 2.0
    metres_per_degree = (
        math.pi / 180.0
        * 6_371_000
        * math.cos(math.radians(centre_lat))
    )
    return abs(px) * metres_per_degree


def _fill_nan(arr: np.ndarray) -> np.ndarray:
    """
    Forward-fill then backward-fill NaN values in a 1-D array.
    Avoids gaps that would break cumulative integrals.
    """
    out = arr.copy()
    # Forward fill
    last = np.nan
    for i in range(len(out)):
        if not np.isnan(out[i]):
            last = out[i]
        elif not np.isnan(last):
            out[i] = last
    # Backward fill (handles leading NaNs)
    last = np.nan
    for i in range(len(out) - 1, -1, -1):
        if not np.isnan(out[i]):
            last = out[i]
        elif not np.isnan(last):
            out[i] = last
    return out



def sample_river_native_pixels(
    river_geom:  QgsGeometry,
    dem_layer:   QgsRasterLayer,
    fac_layer:   QgsRasterLayer,
    stream_crs,
) -> dict:
    """
    Replaces sample_river_hydraulics() for SL/SLk computation.

    Instead of resampling N equidistant points (which smooths the signal),
    this function reads the NATIVE DEM pixels whose centres fall within
    half a pixel-width of the river centreline — exactly as researchers
    do manually in ArcGIS/Excel.

    No interpolation. No smoothing. Raw pixel values in source→mouth order.

    Returns the same dict structure as sample_river_hydraulics() so the
    engine can use it as a drop-in replacement.
    """
    from .raster import RasterReader

    # ── Step 1 : read DEM as numpy array ─────────────────────────────
    reader     = RasterReader(dem_layer)
    dem_crs    = dem_layer.crs()
    pixel_size = reader.pixel_size_x   # metres (or degrees — handled below)

    # CRS transforms
    t_stream_to_dem = None
    if stream_crs != dem_crs:
        t_stream_to_dem = QgsCoordinateTransform(
            stream_crs, dem_crs, QgsProject.instance()
        )

    fac_crs     = fac_layer.crs()
    t_stream_to_fac = None
    if stream_crs != fac_crs:
        t_stream_to_fac = QgsCoordinateTransform(
            stream_crs, fac_crs, QgsProject.instance()
        )

    # ── Step 2 : walk river vertices, collect pixel centres ──────────
    da = QgsDistanceArea()
    da.setSourceCrs(stream_crs, QgsProject.instance().transformContext())
    da.setEllipsoid('WGS84')

    vertices = [QgsPointXY(v) for v in river_geom.vertices()]
    if len(vertices) < 2:
        return {"valid": False, "fac_valid": False}

    # Convert pixel_size to metres for the snap threshold
    snap_m = _pixel_size_metres(dem_layer) * 0.5

    # Walk along the polyline segment by segment
    # At each step advance by ~pixel_size along the segment
    # and snap to the nearest DEM pixel centre
    collected = []          # list of (cum_dist_m, pt_stream_crs)
    cum_dist  = 0.0
    prev_col  = None
    prev_row  = None

    for seg_i in range(len(vertices) - 1):
        pt_a = vertices[seg_i]
        pt_b = vertices[seg_i + 1]
        seg_len = da.measureLine(pt_a, pt_b)

        if seg_len < 1e-6:
            continue

        # Number of steps along this segment
        n_steps = max(1, int(math.ceil(seg_len / snap_m)))

        for step in range(n_steps):
            t  = step / n_steps
            pt = QgsPointXY(
                pt_a.x() + t * (pt_b.x() - pt_a.x()),
                pt_a.y() + t * (pt_b.y() - pt_a.y()),
            )

            # Snap to DEM pixel centre
            pt_dem = t_stream_to_dem.transform(pt) \
                if t_stream_to_dem else pt
            col, row = reader.world_to_pixel(pt_dem.x(), pt_dem.y())

            if col == -1:
                continue

            # Skip duplicate pixel — same pixel as previous step
            if col == prev_col and row == prev_row:
                continue

            prev_col = col
            prev_row = row

            # Cumulative distance — measure from previous vertex
            step_dist = da.measureLine(pt_a, pt) if step > 0 else 0.0
            if collected:
                # distance from last collected point
                last_pt = collected[-1][1]
                step_dist = cum_dist + da.measureLine(last_pt, pt)
            else:
                step_dist = 0.0

            collected.append((step_dist, pt))
            cum_dist = step_dist

    # Recalculate cumulative distances properly
    # (the above is approximate — recalc from collected points)
    if len(collected) < MIN_RIVER_POINTS:
        return {"valid": False, "fac_valid": False}

    pts_stream = [c[1] for c in collected]
    cum_dists  = [0.0]
    for i in range(1, len(pts_stream)):
        d = da.measureLine(pts_stream[i - 1], pts_stream[i])
        cum_dists.append(cum_dists[-1] + max(d, 0.0))

    total_length_m = cum_dists[-1]

    # ── Step 3 : sample DEM elevation at each pixel centre ───────────
    elevations = []
    for pt in pts_stream:
        pt_dem = t_stream_to_dem.transform(pt) \
            if t_stream_to_dem else pt
        val = reader.sample_at(pt_dem.x(), pt_dem.y())
        elevations.append(val)

    # ── Step 4 : sample FAC ──────────────────────────────────────────
    pixel_area_m2 = _pixel_size_metres(fac_layer) ** 2
    raw_fac       = []
    for pt in pts_stream:
        pt_fac = t_stream_to_fac.transform(pt) \
            if t_stream_to_fac else pt
        val, ok = fac_layer.dataProvider().sample(pt_fac, 1)
        raw_fac.append(float(val) if (ok and not math.isnan(val)) else np.nan)

    area_m2   = np.array(raw_fac, dtype=np.float64) * pixel_area_m2
    fac_valid = np.sum(~np.isnan(area_m2)) >= (len(area_m2) * 0.5)
    area_m2   = _fill_nan(area_m2)
    area_m2   = np.where(area_m2 > 0, area_m2, pixel_area_m2)

    # ── Step 5 : convert to numpy, remove NaN elevation ──────────────
    distances_m = np.array(cum_dists,  dtype=np.float64)
    elevations  = np.array(elevations, dtype=np.float64)

    valid_mask  = ~np.isnan(elevations)
    distances_m = distances_m[valid_mask]
    elevations  = elevations[valid_mask]
    area_m2     = area_m2[valid_mask]

    if len(elevations) < MIN_RIVER_POINTS:
        return {"valid": False, "fac_valid": False}

    # ── Step 6 : local slope — centred differences on NATIVE pixels ──
    n     = len(elevations)
    slope = np.empty(n, dtype=np.float64)

    dz = elevations[2:] - elevations[:-2]
    dl = distances_m[2:] - distances_m[:-2]
    slope[1:-1] = np.where(dl > 1e-6, np.abs(dz / dl), 1e-8)

    dl0 = distances_m[1] - distances_m[0]
    slope[0] = abs(elevations[1] - elevations[0]) / dl0 \
        if dl0 > 1e-6 else 1e-8

    dl1 = distances_m[-1] - distances_m[-2]
    slope[-1] = abs(elevations[-1] - elevations[-2]) / dl1 \
        if dl1 > 1e-6 else 1e-8

    slope = np.where(slope > 1e-8, slope, 1e-8)

    return {
        # Mirror of sample_river_hydraulics() output
        "valid":          True,
        "fac_valid":      fac_valid,
        "distances_m":    distances_m,
        "elevations":     elevations,
        "area_m2":        area_m2,
        "slope_local":    slope,
        "total_length_m": total_length_m,
        "n_points":       len(elevations),
    }