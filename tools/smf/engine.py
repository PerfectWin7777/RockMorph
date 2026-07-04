"""
tools/smf/engine.py

Mountain Front Sinuosity (Smf) Analysis Engine for RockMorph.

Provides:
- Geodesic length measurement (Lf and Lr) on the ellipsoid.
- PCA-based baseline calculation for complex structures.
- Mountain-Flank Skeleton Detector (MFSD) for automatic scarp extraction.

Mountain-Flank Skeleton Detector (MFSD) Workflow:
--------------------------------------------------
1. Repairs nodata gaps via nearest-valid extrapolation (never a global mean),
   so the detector cannot lock onto the raster's own border shape.
2. Computes slope magnitude and regional relief contrast to classify
   mountain-flank pixels (same slope x relief-step logic used manually).
3. Restricts classification to a "safe" zone far enough from any nodata
   pixel that slope/relief windows never touched it.
4. Cleans the resulting mask morphologically (gap closing sized in real
   metres, opening, minimum real area in km^2).
5. Skeletonizes the cleaned mask to a 1-pixel-thick flank line.
6. For each skeleton component, detects whether it is a closed ring
   (isolated massif) or an open chain (linear front) and traces it
   accordingly via graph diameter (longest simple path) - this avoids
   the short/near-closed fragment artifacts of a naive perimeter walk.
7. Merges fragments that skeletonize still split apart (small mask gaps,
   tiny branch points) back into continuous fronts, using an endpoint
   proximity + collinearity test.

Authors: RockMorph contributors / Tony
"""

import math
from collections import deque

import numpy as np  # type: ignore

from qgis.core import (  # type: ignore
    QgsVectorLayer, QgsRasterLayer, QgsProject,
    QgsCoordinateTransform, QgsGeometry, QgsWkbTypes,
    QgsSpatialIndex, QgsDistanceArea, QgsPointXY
)
from PyQt5.QtCore import QCoreApplication  # type: ignore

from ...base.base_engine import BaseEngine
from ...core.raster import RasterReader


def tr(message):
    return QCoreApplication.translate("RockMorph", message)


class SMFEngine(BaseEngine):
    """
    Computes Mountain Front Sinuosity (Smf) and associated relief indices.
    Supports both manual vector layers and high-precision automatic detection.
    """

    def validate(self, **kwargs) -> bool:
        dem = kwargs.get("dem_layer")
        scarp_layer = kwargs.get("scarp_layer")
        if dem is None or not dem.isValid():
            return False
        if scarp_layer is not None and not scarp_layer.isValid():
            return False
        return True

    def compute(self, **kwargs) -> dict:
        dem_layer = kwargs["dem_layer"]
        scarp_layer = kwargs.get("scarp_layer")
        unit_field = kwargs.get("unit_field")
        baseline_method = kwargs.get("baseline_method", "endpoints")
        progress_cb = kwargs.get("progress_callback")

        results = []
        skipped = []
        warnings = []

        # Coordinate system geodesic tools on the ellipsoid
        da = QgsDistanceArea()
        da.setSourceCrs(dem_layer.crs(), QgsProject.instance().transformContext())
        da.setEllipsoid('WGS84')

        # Open DEM raster reader for true elevation profile sampling
        reader = RasterReader(dem_layer)

        features_to_process = []
        if scarp_layer and scarp_layer.isValid():
            if progress_cb:
                progress_cb(10, tr("Loading input scarp vector layer..."))

            transform = None
            if scarp_layer.crs() != dem_layer.crs():
                transform = QgsCoordinateTransform(
                    scarp_layer.crs(), dem_layer.crs(), QgsProject.instance()
                )

            for feat in scarp_layer.getFeatures():
                geom = feat.geometry()
                if geom is None or geom.isEmpty():
                    skipped.append((feat.id(), "Empty geometry"))
                    continue

                if transform:
                    geom_copy = QgsGeometry(geom)
                    geom_copy.transform(transform)
                    geom = geom_copy

                features_to_process.append((feat.id(), feat, geom))
        else:
            if progress_cb:
                progress_cb(10, tr("No scarp layer provided. Initializing mountain-flank detector..."))
            detected_geoms = self._detect_scarps(dem_layer, reader, kwargs, progress_cb, da)
            for idx, geom in enumerate(detected_geoms):
                features_to_process.append((idx, None, geom))

        if not features_to_process:
            return {
                "results": [],
                "skipped": skipped,
                "warnings": [tr("No mountain fronts detected. Try adjusting parameters.")]
            }

        total_feats = len(features_to_process)

        for i, (fid, feat, geom) in enumerate(features_to_process):
            if progress_cb:
                pct = 20 + int((i / total_feats) * 70)
                progress_cb(pct, tr(f"Analyzing scarp {i+1}/{total_feats}..."))

            try:
                unit_name = "Global"
                if feat and unit_field:
                    fields = [f.name() for f in scarp_layer.fields()]
                    if unit_field in fields:
                        val = feat.attribute(unit_field)
                        if val is not None and str(val).strip() != "":
                            unit_name = str(val).strip()

                vertices = [QgsPointXY(v) for v in geom.vertices()]
                if len(vertices) < 2:
                    skipped.append((fid, "Segment contains insufficient coordinates"))
                    continue

                # A. Sinuous length (Lf)
                lf_m = 0.0
                distances = [0.0]
                for v_idx in range(1, len(vertices)):
                    segment_len = da.measureLine(vertices[v_idx - 1], vertices[v_idx])
                    lf_m += segment_len
                    distances.append(lf_m)

                if lf_m < 1e-3:
                    skipped.append((fid, "Sinuous length is near zero"))
                    continue

                # B. Straight-line baseline (Lr)
                lr_m = self._calculate_pca_baseline(vertices, da) if baseline_method == "pca" else da.measureLine(vertices[0], vertices[-1])

                if lr_m < 1e-3:
                    skipped.append((fid, "Baseline length is near zero"))
                    continue

                # C. Compute Smf and classes
                smf = lf_m / lr_m
                tectonic_class = 3
                if smf < 1.4:
                    tectonic_class = 1
                elif smf < 1.8:
                    tectonic_class = 2

                # D. Sample true elevations from raster along vertices
                elevations = []
                for v in vertices:
                    z = reader.sample_at(v.x(), v.y())
                    elevations.append(z if not np.isnan(z) else 0.0)

                results.append({
                    "fid": fid,
                    "label": f"E{len(results) + 1}",
                    "unit": unit_name,
                    "lf_km": round(lf_m / 1000.0, 3),
                    "lr_km": round(lr_m / 1000.0, 3),
                    "smf": round(smf, 3),
                    "class": tectonic_class,
                    "elevations": elevations,
                    "distances": distances,
                    "geom": geom  # Retained for QGIS RubberBand rendering
                })

            except Exception as e:
                skipped.append((fid, str(e)))
                warnings.append(tr(f"Error processing front {fid}: {e}"))

        if progress_cb:
            progress_cb(100, tr("Computation completed."))

        return {
            "results": results,
            "skipped": skipped,
            "warnings": warnings
        }

    def _calculate_pca_baseline(self, vertices: list, da: QgsDistanceArea) -> float:
        coords = np.array([[pt.x(), pt.y()] for pt in vertices], dtype=np.float64)
        mean = np.mean(coords, axis=0)
        centered = coords - mean
        cov = np.cov(centered, rowvar=False)
        eigenvalues, eigenvectors = np.linalg.eigh(cov)
        pc1 = eigenvectors[:, np.argmax(eigenvalues)]
        projections = np.dot(centered, pc1)
        min_proj = np.min(projections)
        max_proj = np.max(projections)
        pt_start = mean + min_proj * pc1
        pt_end = mean + max_proj * pc1
        return da.measureLine(QgsPointXY(pt_start[0], pt_start[1]), QgsPointXY(pt_end[0], pt_end[1]))

    # ------------------------------------------------------------------
    # Automatic detection: Mountain-Flank Skeleton Detector (MFSD)
    # ------------------------------------------------------------------

    def _odd(self, n: int) -> int:
        return n if n % 2 == 1 else n + 1

    def _disk_structure(self, radius_px: int) -> np.ndarray:
        radius_px = max(1, int(radius_px))
        y, x = np.ogrid[-radius_px:radius_px + 1, -radius_px:radius_px + 1]
        return (x ** 2 + y ** 2) <= radius_px ** 2

    def _detect_scarps(self, dem_layer: QgsRasterLayer, reader: RasterReader, params: dict, progress_cb, da: QgsDistanceArea) -> list:
        """
        Mountain-flank detector: slope x regional-relief classification,
        cleaned morphologically, then traced via skeleton topology
        (closed loop vs longest open path) and finally re-merged where
        the skeleton still split one real front into several fragments.
        """
        from scipy import ndimage  # type: ignore
        try:
            from skimage.morphology import skeletonize  # type: ignore
        except ImportError:
            if progress_cb:
                progress_cb(100, tr("Auto-detect failed: scikit-image is not installed "
                                     "(pip install scikit-image in QGIS's Python)."))
            return []

        min_slope = params.get("min_slope_pct", 15.0)
        min_relief = params.get("min_relief_m", 100.0)
        relief_window_m = params.get("relief_window_m", 1500.0)
        min_front_area_km2 = params.get("min_front_area_km2", 15.3)
        min_front_length_m = params.get("min_front_length_m", 5000.0)
        resample_spacing_m = params.get("resample_spacing_m", 350.0)
        max_gap_m = params.get("max_gap_m", 2000.0)
        max_merge_angle_deg = params.get("max_merge_angle_deg", 60.0)

        arr = reader.array
        if arr is None:
            return []

        valid_mask = ~np.isnan(arr)
        if not np.any(valid_mask):
            return []
        invalid_mask = ~valid_mask

        dx = reader.pixel_size_x
        dy = reader.pixel_size_y

        if progress_cb:
            progress_cb(10, tr("Auto-detect: repairing nodata gaps..."))

        # Nearest-valid extrapolation. CRITICAL: filling with a global mean
        # manufactures a fake elevation cliff right at the nodata boundary,
        # which is what previously made the detector trace the raster's
        # own (possibly sinusoidal/mosaic) edge as a "scarp".
        if np.any(invalid_mask):
            nearest_idx = ndimage.distance_transform_edt(
                invalid_mask, return_distances=False, return_indices=True
            )
            arr_filled = arr[tuple(nearest_idx)]
        else:
            arr_filled = arr.copy()

        if progress_cb:
            progress_cb(20, tr("Auto-detect: computing slope..."))

        arr_smoothed = ndimage.gaussian_filter(arr_filled, sigma=1.5)
        dz_dy, dz_dx = np.gradient(arr_smoothed)
        dz_dx /= dx
        dz_dy /= dy
        slope_pct = np.hypot(dz_dx, dz_dy) * 100.0

        if progress_cb:
            progress_cb(35, tr("Auto-detect: computing regional relief..."))

        win_px_x = self._odd(max(3, int(round(relief_window_m / dx))))
        win_px_y = self._odd(max(3, int(round(relief_window_m / dy))))
        z_max = ndimage.maximum_filter(arr_filled, size=(win_px_y, win_px_x), mode='nearest')
        z_min = ndimage.minimum_filter(arr_filled, size=(win_px_y, win_px_x), mode='nearest')
        relief = z_max - z_min

        # Exclude any pixel whose slope/relief window could have touched
        # nodata. This makes the whole detector immune to the raster
        # border's shape.
        radius_px = max(win_px_x, win_px_y) // 2 + 5
        if np.any(invalid_mask):
            struct = np.ones((2 * radius_px + 1, 2 * radius_px + 1), dtype=bool)
            safe_mask = ndimage.binary_erosion(valid_mask, structure=struct)
        else:
            safe_mask = valid_mask

        if not np.any(safe_mask):
            return []

        if progress_cb:
            progress_cb(50, tr("Auto-detect: classifying mountain-flank zones..."))

        closing_radius_px = max(1, int(round((max_gap_m / 2.0) / max(dx, dy))))

        mountain_mask = (slope_pct >= min_slope) & (relief >= min_relief) & safe_mask
        mountain_mask = ndimage.binary_closing(mountain_mask, structure=self._disk_structure(closing_radius_px))
        mountain_mask = ndimage.binary_opening(mountain_mask, structure=np.ones((3, 3)))
        mountain_mask &= safe_mask  # closing must never re-invade the unsafe border band

        labeled, n_comp = ndimage.label(mountain_mask, structure=np.ones((3, 3)))
        pixel_area_km2 = abs(dx * dy) / 1e6
        if n_comp > 0:
            sizes = ndimage.sum(mountain_mask, labeled, index=list(range(1, n_comp + 1)))
            keep_labels = {i + 1 for i, s in enumerate(sizes) if s * pixel_area_km2 >= min_front_area_km2}
            mountain_mask = np.isin(labeled, list(keep_labels)) if keep_labels else np.zeros_like(mountain_mask)

        if not np.any(mountain_mask):
            return []

        if progress_cb:
            progress_cb(65, tr("Auto-detect: extracting flank-line skeleton..."))

        skeleton = skeletonize(mountain_mask)
        labeled_skel, n_skel = ndimage.label(skeleton, structure=np.ones((3, 3)))

        min_length_px = max(10, int(min_front_length_m / max(dx, dy) / 1.3))
        geometries = []

        if progress_cb:
            progress_cb(80, tr("Auto-detect: tracing individual fronts..."))

        for label_id in range(1, n_skel + 1):
            rows, cols = np.where(labeled_skel == label_id)
            if len(rows) < min_length_px:
                continue
            coords = set(zip(rows.tolist(), cols.tolist()))

            path, is_closed = self._classify_and_trace_component(coords)
            if len(path) < 5:
                continue

            world_pts = [QgsPointXY(*reader.pixel_to_world(c, r)) for r, c in path]
            if is_closed and world_pts[0] != world_pts[-1]:
                world_pts.append(world_pts[0])

            resampled = self._resample_points(world_pts, resample_spacing_m, da)
            if len(resampled) < 5:
                continue

            geom = QgsGeometry.fromPolylineXY(resampled)
            geom = geom.simplify(max(dx, dy) * 1.5)

            geometries.append(geom)

        if progress_cb:
            progress_cb(90, tr("Auto-detect: merging adjacent fragments into continuous fronts..."))

        geometries = self._merge_fragmented_fronts(
            geometries,
            max_gap_m=max_gap_m,
            max_angle_deg=max_merge_angle_deg
        )

        geometries = [g for g in geometries if g.length() >= min_front_length_m]

        if progress_cb:
            progress_cb(95, tr("Auto-detect: finalizing geometries..."))

        return geometries

    def _classify_and_trace_component(self, coords: set) -> tuple:
        """
        Determines whether a skeleton connected component is a simple
        closed loop (isolated massif bounded on all sides by lowland) or
        an open chain (linear/sinuous range front), and returns an
        ordered trace accordingly.
        """
        neighbors = {}
        has_endpoint = False
        only_degree_two = True
        for (r, c) in coords:
            nbrs = [(r + dr, c + dc) for dr in (-1, 0, 1) for dc in (-1, 0, 1)
                    if not (dr == 0 and dc == 0) and (r + dr, c + dc) in coords]
            neighbors[(r, c)] = nbrs
            deg = len(nbrs)
            if deg == 1:
                has_endpoint = True
            if deg != 2:
                only_degree_two = False

        if not has_endpoint and only_degree_two and len(coords) >= 3:
            # Simple closed ring: walk it directly.
            start = next(iter(coords))
            path = [start]
            prev = None
            current = start
            while True:
                nbrs = neighbors[current]
                nxt = nbrs[0] if nbrs[0] != prev else (nbrs[1] if len(nbrs) > 1 else None)
                if nxt is None or nxt == start:
                    break
                path.append(nxt)
                prev, current = current, nxt
            return path, True

        # Open chain (or messy junction fallback): longest simple path
        # via double BFS (graph diameter). Naturally ignores short
        # side-spurs from skeletonization noise.
        def bfs_farthest(start):
            parent = {start: None}
            dist = {start: 0}
            q = deque([start])
            far = start
            while q:
                cur = q.popleft()
                for nb in neighbors[cur]:
                    if nb not in parent:
                        parent[nb] = cur
                        dist[nb] = dist[cur] + 1
                        if dist[nb] > dist[far]:
                            far = nb
                        q.append(nb)
            return far, parent

        start = next(iter(coords))
        end_a, _ = bfs_farthest(start)
        end_b, parents = bfs_farthest(end_a)
        path = []
        node = end_b
        while node is not None:
            path.append(node)
            node = parents[node]
        path.reverse()
        return path, False

    def _resample_points(self, pts: list, spacing_m: float, da: QgsDistanceArea) -> list:
        """
        Resamples a polyline to evenly-spaced vertices, avoiding hundreds
        of near-duplicate pixel-accurate vertices feeding downstream
        processing.
        """
        if len(pts) < 2:
            return pts

        cumulative = [0.0]
        for i in range(1, len(pts)):
            d = da.measureLine(pts[i - 1], pts[i])
            cumulative.append(cumulative[-1] + max(d, 0.0))

        total_length_m = cumulative[-1]
        if total_length_m < spacing_m:
            return [pts[0], pts[-1]]

        n_samples = max(10, int(total_length_m / spacing_m) + 1)
        sample_dists = np.linspace(0.0, total_length_m, n_samples)

        coords = np.array([[p.x(), p.y()] for p in pts], dtype=np.float64)

        # Geodesically-spaced coordinate interpolation
        x_interp = np.interp(sample_dists, cumulative, coords[:, 0])
        y_interp = np.interp(sample_dists, cumulative, coords[:, 1])

        return [QgsPointXY(x, y) for x, y in zip(x_interp, y_interp)]

    # ------------------------------------------------------------------
    # Fragment merging
    # ------------------------------------------------------------------

    def _merge_fragmented_fronts(self, geometries: list, max_gap_m: float, max_angle_deg: float) -> list:
        """
        Iteratively joins line fragments that are close enough at one of
        their endpoints and roughly collinear across that gap, into
        single continuous fronts. Fixes skeletonize splitting one real
        scarp into many short pieces at tiny mask discontinuities.
        """
        fragments = []
        for g in geometries:
            pts = [QgsPointXY(v) for v in g.vertices()]
            if len(pts) >= 2:
                fragments.append(pts)

        cos_thresh = math.cos(math.radians(max_angle_deg))

        merged_any = True
        while merged_any and len(fragments) > 1:
            merged_any = False
            n = len(fragments)
            best = None  # (gap, i, j, end_i, end_j)

            for i in range(n):
                for j in range(i + 1, n):
                    for end_i in ('start', 'end'):
                        for end_j in ('start', 'end'):
                            pt_i = fragments[i][0] if end_i == 'start' else fragments[i][-1]
                            pt_j = fragments[j][0] if end_j == 'start' else fragments[j][-1]
                            gap = pt_i.distance(pt_j)
                            if gap > max_gap_m:
                                continue
                            if not self._is_continuation(fragments[i], end_i, fragments[j], end_j, cos_thresh):
                                continue
                            if best is None or gap < best[0]:
                                best = (gap, i, j, end_i, end_j)

            if best is not None:
                _, i, j, end_i, end_j = best
                new_frag = self._join_fragments(fragments[i], end_i, fragments[j], end_j)
                for idx in sorted((i, j), reverse=True):
                    fragments.pop(idx)
                fragments.append(new_frag)
                merged_any = True

        return [QgsGeometry.fromPolylineXY(f) for f in fragments if len(f) >= 2]

    def _is_continuation(self, pts_a: list, end_a: str, pts_b: list, end_b: str, cos_thresh: float, lookback: int = 5) -> bool:
        """
        True if joining pts_a at end_a to pts_b at end_b continues in
        roughly the same direction on both sides (rejects
        perpendicular/unrelated fragments that merely have close endpoints).
        """
        pt_a = pts_a[0] if end_a == 'start' else pts_a[-1]
        pt_b = pts_b[0] if end_b == 'start' else pts_b[-1]
        gap_vec = np.array([pt_b.x() - pt_a.x(), pt_b.y() - pt_a.y()])
        if np.linalg.norm(gap_vec) < 1e-6:
            return True  # endpoints coincide

        k_a = min(lookback, len(pts_a) - 1)
        k_b = min(lookback, len(pts_b) - 1)

        trend_a = (np.array([pt_a.x(), pt_a.y()]) -
                   np.array([pts_a[k_a].x(), pts_a[k_a].y()]))
        trend_b = (np.array([pts_b[k_b].x(), pts_b[k_b].y()]) -
                   np.array([pt_b.x(), pt_b.y()])) if end_b == 'start' else \
                  (np.array([pt_b.x(), pt_b.y()]) -
                   np.array([pts_b[k_b].x(), pts_b[k_b].y()]))

        def cos_angle(u, v):
            nu, nv = np.linalg.norm(u), np.linalg.norm(v)
            if nu < 1e-6 or nv < 1e-6:
                return 1.0
            return np.dot(u, v) / (nu * nv)

        return cos_angle(gap_vec, trend_a) >= cos_thresh and cos_angle(gap_vec, trend_b) >= cos_thresh

    def _join_fragments(self, pts_a: list, end_a: str, pts_b: list, end_b: str) -> list:
        """Concatenates two fragments in the correct order/orientation to form one continuous polyline."""
        a = pts_a if end_a == 'end' else list(reversed(pts_a))
        b = pts_b if end_b == 'start' else list(reversed(pts_b))
        return a + b