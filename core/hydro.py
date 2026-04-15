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