"""
core/basin_context_builder.py

Extraction of ``BasinContext`` objects from GIS data (QGIS + GDAL side).

The morphometric-indices tool hands this module plain data collected on the
main thread (basin geometries already in the work CRS, the DEM path, optional
stream segments) and receives one ``BasinContext`` per basin. Nothing here
touches a QgsMapLayer: QGIS layers are not thread-safe, so the builder only
uses GDAL datasets it opens itself and standalone ``QgsGeometry`` objects,
which is safe inside a ``QgsTask``.

Pipeline for one basin
----------------------
1. Polygon metrics: area, perimeter, oriented minimum bounding rectangle
   (Lb, Bw).
2. DEM window around the basin (buffered for valley cross-profiles), read
   once; basin mask rasterised on that window (cell centres inside).
3. When a requirement needs flow routing ("terrain", "channel", "network",
   "asymmetry", "sections", "valley"): Priority-Flood filling, D8 and
   accumulation restricted to the basin (``core.indices.flow``).
4. Trunk stream from the outlet to the divide; longitudinal profile.
5. Network: DEM-derived (channel-initiation threshold) or user vector
   segments clipped to the basin.
6. AF: basin split by the trunk stream extended straight at both ends.
7. T sections and Vf cross-profiles.

Work CRS
--------
Every metric must be computed in metres. ``choose_work_crs`` keeps the DEM
CRS when it is projected in metres, otherwise picks the UTM zone of the study
area (as GSV-Studio did); ``prepare_work_dem`` reprojects the DEM with
bilinear resampling when needed.

Authors: RockMorph contributors / Tony
"""

from __future__ import annotations

import math
import os
import tempfile
import uuid
from dataclasses import dataclass, field
from typing import Callable, Dict, Iterable, List, Optional, Sequence, Set, Tuple

import numpy as np  # type: ignore
from osgeo import gdal, ogr, osr  # type: ignore
from qgis.core import (  # type: ignore
    QgsCoordinateReferenceSystem, QgsGeometry, QgsPointXY, QgsUnitTypes,
)

from .indices.context import (
    BasinContext, ChannelProfile, CrossProfile, NetworkSummary,
    TerrainWindow, TransverseSection,
)
from .indices.flow import (
    d8_receivers, flow_accumulation, horn_slope, network_from_segments,
    priority_flood_fill, step_lengths, strahler_network, trunk_stream,
)
from .indices.profiles import bilinear_sample

# Requirements that need flow routing on the basin.
FLOW_REQUIREMENTS = {"terrain", "channel", "network", "asymmetry", "sections", "valley"}

# Below this fraction of the polygon area covered by valid DEM cells, the
# basin is flagged (cells partly inside the polygon make ~1-2 % differences
# on normal basins, so 95 % only catches real gaps).
MIN_DEM_COVERAGE = 0.95


# ---------------------------------------------------------------------------
# Inputs
# ---------------------------------------------------------------------------

@dataclass
class BasinFeature:
    """One basin, collected on the main thread (geometry in the work CRS)."""

    basin_id: object
    label: str
    geometry: QgsGeometry


@dataclass
class StreamSegment:
    """One vector stream segment (geometry in the work CRS)."""

    geometry: QgsGeometry
    order: Optional[int] = None


@dataclass
class BuildOptions:
    """
    Settings of the extraction, derived from the selected indices.

    channel_threshold_m2 : drainage area at which a channel starts, for the
                           DEM-derived network (GSV-Studio default 1 km²).
    sections             : number of T sections (Cox 1994).
    vf_profiles, vf_start_fraction, vf_end_fraction, vf_width_m, vf_sampling_m:
                           geometry of the Vf cross-profiles.
    """

    requirements: Set[str] = field(default_factory=set)
    channel_threshold_m2: float = 1.0e6
    sections: int = 10
    vf_profiles: int = 10
    vf_start_fraction: float = 0.05
    vf_end_fraction: float = 0.95
    vf_width_m: float = 500.0
    vf_sampling_m: float = 0.0


# ---------------------------------------------------------------------------
# Work CRS and DEM preparation
# ---------------------------------------------------------------------------

def choose_work_crs(dem_crs: QgsCoordinateReferenceSystem,
                    lon_lat_centre: Tuple[float, float]) -> QgsCoordinateReferenceSystem:
    """
    Metric CRS used for all measurements.

    Keep the DEM CRS when it is projected with metre units (no resampling);
    otherwise use the WGS 84 / UTM zone containing the study-area centre.
    """
    if (not dem_crs.isGeographic()
            and dem_crs.mapUnits() == QgsUnitTypes.DistanceMeters):
        return dem_crs
    lon, lat = lon_lat_centre
    zone = int((lon + 180.0) // 6.0) + 1
    epsg = (32600 if lat >= 0 else 32700) + zone
    return QgsCoordinateReferenceSystem(f"EPSG:{epsg}")


def prepare_work_dem(dem_path: str, dem_crs: QgsCoordinateReferenceSystem,
                     work_crs: QgsCoordinateReferenceSystem) -> Tuple[str, bool]:
    """
    Return ``(path, is_temporary)`` of a DEM in the work CRS.

    The DEM is reprojected with bilinear resampling (elevation is continuous)
    and a square cell close to the original resolution. The temporary file
    is deleted by the caller.
    """
    if dem_crs == work_crs:
        return dem_path, False
    target = os.path.join(tempfile.gettempdir(), f"rockmorph_dem_{uuid.uuid4().hex[:8]}.tif")
    srs = osr.SpatialReference()
    srs.ImportFromWkt(work_crs.toWkt())
    gdal.Warp(target, dem_path, dstSRS=srs.ExportToWkt(), resampleAlg="bilinear",
              dstNodata=-9999.0, creationOptions=["TILED=YES", "COMPRESS=DEFLATE"])
    dataset = gdal.Open(target)
    if dataset is None:
        raise RuntimeError("The DEM could not be reprojected to a metric CRS.")
    # Force square cells: use the mean of the two cell sizes produced by Warp.
    gt = dataset.GetGeoTransform()
    cell = (abs(gt[1]) + abs(gt[5])) / 2.0
    dataset = None
    if abs(abs(gt[1]) - abs(gt[5])) > 1e-6 * cell:
        square = target.replace(".tif", "_sq.tif")
        gdal.Warp(square, target, xRes=cell, yRes=cell, resampleAlg="bilinear")
        os.remove(target)
        target = square
    return target, True


# ---------------------------------------------------------------------------
# Raster access
# ---------------------------------------------------------------------------

class DemSource:
    """
    Read-only access to a single-band DEM opened with GDAL in this thread.

    Windows are returned as float64 arrays with NoData converted to NaN.
    """

    def __init__(self, path: str):
        self.dataset = gdal.Open(path, gdal.GA_ReadOnly)
        if self.dataset is None:
            raise IOError(f"GDAL cannot open the DEM: {path}")
        self.band = self.dataset.GetRasterBand(1)
        self.geotransform = self.dataset.GetGeoTransform()
        if abs(self.geotransform[2]) > 0 or abs(self.geotransform[4]) > 0:
            raise ValueError("Rotated rasters are not supported; please warp the DEM first.")
        self.cell_size = (abs(self.geotransform[1]) + abs(self.geotransform[5])) / 2.0
        self.nodata = self.band.GetNoDataValue()
        self.projection = self.dataset.GetProjection()
        self.cols = self.dataset.RasterXSize
        self.rows = self.dataset.RasterYSize

    def window_for(self, xmin: float, ymin: float, xmax: float, ymax: float):
        """Pixel window ``(col0, row0, ncols, nrows)`` covering a map extent."""
        x0, dx, _, y0, _, dy = self.geotransform
        col0 = int(math.floor((xmin - x0) / dx))
        col1 = int(math.ceil((xmax - x0) / dx))
        row0 = int(math.floor((ymax - y0) / dy))
        row1 = int(math.ceil((ymin - y0) / dy))
        col0, row0 = max(col0, 0), max(row0, 0)
        col1, row1 = min(col1, self.cols), min(row1, self.rows)
        if col1 <= col0 or row1 <= row0:
            return None
        return col0, row0, col1 - col0, row1 - row0

    def read(self, window) -> Tuple[np.ndarray, Tuple[float, ...]]:
        """Read ``window``; return the array and its own geotransform."""
        col0, row0, ncols, nrows = window
        raw = self.band.ReadRaster(col0, row0, ncols, nrows, ncols, nrows, gdal.GDT_Float64)
        array = np.frombuffer(raw, dtype=np.float64).reshape(nrows, ncols).copy()
        if self.nodata is not None:
            array[array == self.nodata] = np.nan
        array[~np.isfinite(array)] = np.nan
        x0, dx, _, y0, _, dy = self.geotransform
        return array, (x0 + col0 * dx, dx, 0.0, y0 + row0 * dy, 0.0, dy)

    def close(self) -> None:
        self.band = None
        self.dataset = None


def rasterize_mask(geometry: QgsGeometry, shape: Tuple[int, int],
                   geotransform: Tuple[float, ...], projection_wkt: str = "") -> np.ndarray:
    """
    Boolean mask of the cells whose centre lies inside ``geometry``.

    ``projection_wkt`` is the DEM projection; the geometry is already in that
    CRS, so it is only attached to both sides to keep GDAL from warning.
    """
    rows, cols = shape
    driver = gdal.GetDriverByName("MEM")
    target = driver.Create("", cols, rows, 1, gdal.GDT_Byte)
    target.SetGeoTransform(geotransform)
    srs = None
    if projection_wkt:
        target.SetProjection(projection_wkt)
        srs = osr.SpatialReference()
        srs.ImportFromWkt(projection_wkt)
    vector = ogr.GetDriverByName("Memory").CreateDataSource("mask")
    layer = vector.CreateLayer("basin", srs=srs)
    feature = ogr.Feature(layer.GetLayerDefn())
    feature.SetGeometry(ogr.CreateGeometryFromWkb(bytes(geometry.asWkb())))
    layer.CreateFeature(feature)
    gdal.RasterizeLayer(target, [1], layer, burn_values=[1])
    band = target.GetRasterBand(1)
    raw = band.ReadRaster(0, 0, cols, rows, cols, rows, gdal.GDT_Byte)
    return np.frombuffer(raw, dtype=np.uint8).reshape(rows, cols).astype(bool)


def right_of_polyline(points: np.ndarray, polyline: np.ndarray,
                      budget: int = 2_000_000) -> np.ndarray:
    """
    Boolean array: True where a point lies on the right of ``polyline``
    (walking from its first to its last vertex), judged on the nearest
    segment. Vectorised in chunks of points so that the temporary
    (points x segments) arrays hold at most ``budget`` elements.
    """
    a = polyline[:-1]
    ab = polyline[1:] - a
    length2 = np.maximum((ab ** 2).sum(axis=1), 1e-12)
    chunk = max(1, budget // max(1, a.shape[0]))
    result = np.zeros(points.shape[0], dtype=bool)
    for start in range(0, points.shape[0], chunk):
        p = points[start:start + chunk, None, :]
        ap = p - a[None, :, :]
        t = np.clip((ap * ab[None]).sum(axis=2) / length2[None], 0.0, 1.0)
        nearest = a[None] + t[..., None] * ab[None]
        d2 = ((p - nearest) ** 2).sum(axis=2)
        best = np.argmin(d2, axis=1)
        rows = np.arange(best.size)
        cross = ab[best, 0] * ap[rows, best, 1] - ab[best, 1] * ap[rows, best, 0]
        result[start:start + chunk] = cross < 0  # y-up map axes: negative = right
    return result


# ---------------------------------------------------------------------------
# Builder
# ---------------------------------------------------------------------------

class BasinContextBuilder:
    """
    Build ``BasinContext`` objects for one DEM and one set of options.

    The builder keeps the DEM open between basins; call ``close()`` when
    done (or use it as a context manager).
    """

    def __init__(self, dem_path: str, options: BuildOptions,
                 segments: Optional[Sequence[StreamSegment]] = None):
        self.dem = DemSource(dem_path)
        self.options = options
        self.segments = list(segments or [])
        self.needs_flow = bool(options.requirements & FLOW_REQUIREMENTS)
        # Debug/visual products of the last basin, read by the tool to draw
        # the trunk stream on the map.
        self.last_channel_points: List[QgsPointXY] = []

    def __enter__(self):
        return self

    def __exit__(self, *exc):
        self.close()

    def close(self) -> None:
        self.dem.close()

    # -- main entry ----------------------------------------------------------

    def build(self, basin: BasinFeature) -> BasinContext:
        """Extract everything the selected indices need for ``basin``."""
        ctx = BasinContext(basin.basin_id, basin.label)
        self.last_channel_points = []
        geometry = QgsGeometry(basin.geometry)
        if geometry.isEmpty():
            ctx.warnings.append("Empty geometry.")
            return ctx
        if not geometry.isGeosValid():
            geometry = geometry.makeValid()

        self._polygon_metrics(ctx, geometry)

        needs_raster = bool(self.options.requirements - {"geometry"})
        if not needs_raster:
            return ctx

        margin = max(2 * self.dem.cell_size, self.options.vf_width_m / 2.0
                     if "valley" in self.options.requirements else 0.0)
        box = geometry.boundingBox()
        window = self.dem.window_for(box.xMinimum() - margin, box.yMinimum() - margin,
                                     box.xMaximum() + margin, box.yMaximum() + margin)
        if window is None:
            ctx.warnings.append("The basin lies outside the DEM.")
            return ctx
        dem, gt = self.dem.read(window)
        mask = rasterize_mask(geometry, dem.shape, gt, self.dem.projection) & np.isfinite(dem)
        if mask.sum() < 9:
            ctx.warnings.append("Fewer than 9 DEM cells inside the basin.")
            return ctx
        ctx.cell_size_m = self.dem.cell_size
        ctx.elevations = dem[mask]
        # A basin only partly covered by the DEM gives meaningless relief,
        # channel and network values: say so (the polygon metrics stay valid).
        coverage = mask.sum() * self.dem.cell_size ** 2 / ctx.area_m2 if ctx.area_m2 else 0.0
        if coverage < MIN_DEM_COVERAGE:
            ctx.warnings.append(
                f"The DEM covers only {100 * coverage:.0f} % of the basin; DEM-based "
                "indices describe the covered part only.")

        if self.needs_flow:
            self._flow_products(ctx, geometry, dem, mask, gt)
        return ctx

    # -- geometry -------------------------------------------------------------

    def _polygon_metrics(self, ctx: BasinContext, geometry: QgsGeometry) -> None:
        """Area, perimeter and the oriented minimum bounding rectangle."""
        ctx.area_m2 = geometry.area()
        ctx.perimeter_m = geometry.length()
        result = geometry.orientedMinimumBoundingBox()
        if result and len(result) >= 5 and result[3] > 0 and result[4] > 0:
            ctx.length_m = float(max(result[3], result[4]))
            ctx.width_m = float(min(result[3], result[4]))
            ctx.length_method = "oriented minimum bounding rectangle"

    # -- flow-based products --------------------------------------------------

    def _flow_products(self, ctx, geometry, dem, mask, gt) -> None:
        cell = self.dem.cell_size
        rows, cols = dem.shape
        filled = priority_flood_fill(dem, mask)
        receivers = d8_receivers(filled, mask, cell)
        accumulation = flow_accumulation(filled, receivers, mask)
        req = self.options.requirements

        if "terrain" in req:
            ctx.terrain = TerrainWindow(horn_slope(dem, cell), accumulation, mask, cell)

        # Trunk stream: outlet = cell with the largest accumulation.
        path = trunk_stream(receivers, accumulation)
        x0, dx, _, y0, _, dy = gt
        path_rows, path_cols = np.divmod(path, cols)
        xs = x0 + (path_cols + 0.5) * dx
        ys = y0 + (path_rows + 0.5) * dy
        steps = step_lengths(receivers, cols, cell)[path]
        distance = np.concatenate(([0.0], np.cumsum(steps[:-1])))
        channel = ChannelProfile(
            distance_m=distance,
            elevation_m=filled.ravel()[path],
            drainage_area_m2=accumulation.ravel()[path] * cell * cell,
            x=xs, y=ys,
        )
        if path.size >= 3:
            ctx.channel = channel
            self.last_channel_points = [QgsPointXY(float(a), float(b)) for a, b in zip(xs, ys)]
        else:
            ctx.warnings.append("The trunk stream is shorter than 3 cells.")

        if "network" in req:
            ctx.network = self._network(ctx, geometry, filled, receivers, accumulation, mask, gt)
        if ctx.channel is None:
            return
        if "asymmetry" in req:
            ctx.right_bank_area_m2 = self._right_bank_area(ctx, geometry, mask, gt)
        if "sections" in req:
            ctx.sections = self._sections(ctx, geometry, mask, gt)
        if "valley" in req:
            ctx.cross_profiles = self._cross_profiles(ctx, dem, gt)

    # -- network ------------------------------------------------------------

    def _network(self, ctx, geometry, filled, receivers, accumulation, mask, gt) -> Optional[NetworkSummary]:
        """DEM-derived or vector Horton-Strahler statistics."""
        cell = self.dem.cell_size
        cols = mask.shape[1]
        if not self.segments:
            stream_mask = mask & (accumulation * cell * cell >= self.options.channel_threshold_m2)
            if not np.any(stream_mask):
                ctx.warnings.append("No channel exceeds the channel-initiation area in this basin.")
                return None
            summary, _ = strahler_network(receivers, accumulation, stream_mask, cols, cell)
            return summary

        x0, dx, _, y0, _, dy = gt
        flat_filled = filled.ravel()
        flat_acc = accumulation.ravel()

        def cell_index(point: QgsPointXY) -> Optional[int]:
            col = int(math.floor((point.x() - x0) / dx))
            row = int(math.floor((point.y() - y0) / dy))
            if 0 <= row < mask.shape[0] and 0 <= col < cols and mask[row, col]:
                return row * cols + col
            return None

        clipped = []
        for segment in self.segments:
            if not segment.geometry.boundingBox().intersects(geometry.boundingBox()):
                continue
            part = segment.geometry.intersection(geometry)
            if part.isEmpty():
                continue
            lines = part.asMultiPolyline() if part.isMultipart() else [part.asPolyline()]
            for line in lines:
                if len(line) < 2:
                    continue
                start, end = line[0], line[-1]
                a, b = cell_index(start), cell_index(end)
                z_start = flat_filled[a] if a is not None else np.nan
                z_end = flat_filled[b] if b is not None else np.nan
                if np.isfinite(z_start) and np.isfinite(z_end) and z_start < z_end:
                    start, end, b = end, start, a  # orient upstream -> downstream
                length = QgsGeometry.fromPolylineXY(line).length()
                outlet_area = float(flat_acc[b] * cell * cell) if b is not None else None
                clipped.append({"start": (start.x(), start.y()), "end": (end.x(), end.y()),
                                "length": length, "order": segment.order,
                                "outlet_area": outlet_area})
        if not clipped:
            ctx.warnings.append("No stream segment of the vector network lies in this basin.")
            return None
        # Clipped ends of a segment shared by two basins never meet another
        # segment; snapping at one tenth of a cell joins true shared vertices
        # only.
        summary = network_from_segments(clipped, snap_m=max(0.01, 0.1 * cell))
        lowest = min(summary.stream_counts) if summary.stream_counts else None
        if lowest is not None and lowest > 1:
            # Typical of global products (e.g. HydroRIVERS ORD_STRA) whose
            # orders refer to the whole river system: Horton ratios computed
            # on such a truncated network are not comparable with a full one.
            ctx.warnings.append(
                f"The lowest stream order in this basin is {lowest}: the vector network "
                "does not start at first-order streams; Horton ratios and Dd describe a "
                "truncated network.")
        return summary

    # -- asymmetry factor -------------------------------------------------------

    def _extended_channel(self, ctx, geometry: QgsGeometry) -> List[QgsPointXY]:
        """
        Trunk stream polyline (source -> outlet) extended straight at both
        ends until it leaves the basin.

        End directions are estimated over the first and last ~5 cells, which
        is more stable than the last D8 step. Each extension stops one cell
        beyond its FIRST crossing of the basin boundary: an extension of a
        fixed (long) length can cut back across a curved channel and make the
        cutting line self-intersecting, which GEOS refuses to split with.
        """
        ch = ctx.channel
        pts = np.column_stack((ch.x, ch.y))
        k = min(5, len(pts) - 1)
        boundary = QgsGeometry(geometry.constGet().boundary())
        box = geometry.boundingBox()
        reach = 2.0 * math.hypot(box.width(), box.height())
        cell = self.dem.cell_size

        def exit_point(origin: np.ndarray, direction: np.ndarray) -> np.ndarray:
            direction = direction / max(np.linalg.norm(direction), 1e-9)
            ray = QgsGeometry.fromPolylineXY([QgsPointXY(*origin),
                                              QgsPointXY(*(origin + direction * reach))])
            hits = ray.intersection(boundary)
            distances = [math.hypot(v.x() - origin[0], v.y() - origin[1])
                         for v in hits.vertices()] if not hits.isEmpty() else []
            length = (min(distances) if distances else reach) + cell
            return origin + direction * length

        head = exit_point(pts[0], pts[0] - pts[k])
        tail = exit_point(pts[-1], pts[-1] - pts[-1 - k])
        coords = [head] + [p for p in pts] + [tail]
        return [QgsPointXY(float(x), float(y)) for x, y in coords]

    def _right_bank_area(self, ctx, geometry: QgsGeometry, mask=None, gt=None) -> Optional[float]:
        """
        Area of the basin on the right of the trunk stream (looking
        downstream), for Hare & Gardner's (1985) asymmetry factor.

        Primary method (exact): split the polygon along the extended trunk
        stream and assign each part to a bank with
        ``closestSegmentWithContext`` (positive = right of the line).
        Fallback (cell-based): when GEOS cannot split (rare, e.g. a channel
        hugging the divide), classify every basin cell by the side of its
        nearest segment of the extended channel and sum the cell areas.
        """
        line_points = self._extended_channel(ctx, geometry)
        line = QgsGeometry.fromPolylineXY(line_points)
        work = QgsGeometry(geometry)
        code, parts, _ = work.splitGeometry(line_points, False)
        pieces = [work] + list(parts)
        if code == 0 and len(pieces) >= 2:
            right = 0.0
            for piece in pieces:
                for single in (piece.asGeometryCollection() if piece.isMultipart() else [piece]):
                    probe = single.pointOnSurface().asPoint()
                    if line.closestSegmentWithContext(probe)[3] > 0:
                        right += single.area()
            return right
        if mask is None:
            ctx.warnings.append("AF: the trunk stream does not split the basin.")
            return None
        rows, cols = np.nonzero(mask)
        x0, dx, _, y0, _, dy = gt
        cells = np.column_stack((x0 + (cols + 0.5) * dx, y0 + (rows + 0.5) * dy))
        polyline = np.array([[p.x(), p.y()] for p in line_points])
        # The share of right-bank cells is applied to the polygon area so
        # that AF stays consistent with the exact (vector) basin area.
        share = np.count_nonzero(right_of_polyline(cells, polyline)) / cells.shape[0]
        return float(share * ctx.area_m2)

    # -- transverse symmetry ------------------------------------------------------

    def _principal_axis(self, mask: np.ndarray, gt) -> Tuple[np.ndarray, np.ndarray]:
        """
        Centroid and unit direction of the basin principal axis, from a PCA of
        the cell-centre coordinates (area-weighted). GSV-Studio used the
        polygon vertices, which over-weights densely digitised stretches.
        """
        rows, cols = np.nonzero(mask)
        x0, dx, _, y0, _, dy = gt
        xy = np.column_stack((x0 + (cols + 0.5) * dx, y0 + (rows + 0.5) * dy))
        centre = xy.mean(axis=0)
        cov = np.cov((xy - centre).T)
        values, vectors = np.linalg.eigh(cov)
        return centre, vectors[:, int(np.argmax(values))]

    def _sections(self, ctx, geometry, mask, gt) -> List[TransverseSection]:
        """
        Cox (1994) sections: at points spread along the trunk stream
        (5 %–95 % of its length), the distance Da from the midline and the
        distances Dd from the midline to the divide on each side.
        """
        centre, axis = self._principal_axis(mask, gt)
        ch = ctx.channel
        # Orient the axis downstream so that "left/right" match flow direction.
        flow = np.array([ch.x[-1] - ch.x[0], ch.y[-1] - ch.y[0]])
        if np.dot(axis, flow) < 0:
            axis = -axis
        normal_left = np.array([-axis[1], axis[0]])
        boundary = QgsGeometry(geometry.constGet().boundary())
        box = geometry.boundingBox()
        reach = 2.0 * math.hypot(box.width(), box.height())

        sections = []
        for fraction in np.linspace(0.05, 0.95, max(1, self.options.sections)):
            target = fraction * ch.length_m
            px = float(np.interp(target, ch.distance_m, ch.x))
            py = float(np.interp(target, ch.distance_m, ch.y))
            p = np.array([px, py])
            foot = centre + axis * np.dot(p - centre, axis)
            offset = p - foot
            da = float(np.linalg.norm(offset))
            side = 0 if da < 1e-6 else (1 if np.dot(offset, normal_left) > 0 else -1)
            dd = []
            for direction in (normal_left, -normal_left):
                ray = QgsGeometry.fromPolylineXY([QgsPointXY(*foot), QgsPointXY(*(foot + direction * reach))])
                hit = ray.intersection(boundary)
                points = hit.asMultiPoint() if hit.isMultipart() else ([hit.asPoint()] if not hit.isEmpty() else [])
                distances = [math.hypot(q.x() - foot[0], q.y() - foot[1]) for q in points]
                dd.append(min(distances) if distances else float("nan"))
            azimuth = math.degrees(math.atan2(offset[0], offset[1])) % 360.0
            sections.append(TransverseSection(da, dd[0], dd[1], side, azimuth))
        return sections

    # -- valley cross-profiles ------------------------------------------------------

    def _cross_profiles(self, ctx, dem, gt) -> List[CrossProfile]:
        """
        Cross-profiles perpendicular to the trunk stream for the Vf ratio,
        sampled by bilinear interpolation on the original (unfilled) DEM.
        """
        ch = ctx.channel
        opts = self.options
        step = opts.vf_sampling_m if opts.vf_sampling_m > 0 else self.dem.cell_size / 2.0
        half = opts.vf_width_m / 2.0
        offsets = np.arange(-half, half + step / 2.0, step)
        lookahead = max(2.0 * self.dem.cell_size, 0.01 * ch.length_m)
        start = min(opts.vf_start_fraction, opts.vf_end_fraction)
        end = max(opts.vf_start_fraction, opts.vf_end_fraction)
        fractions = [0.5 * (start + end)] if opts.vf_profiles == 1 else \
            np.linspace(start, end, opts.vf_profiles)

        profiles = []
        for fraction in fractions:
            d = fraction * ch.length_m
            before = max(d - lookahead, 0.0)
            after = min(d + lookahead, ch.length_m)
            x1, y1 = np.interp(before, ch.distance_m, ch.x), np.interp(before, ch.distance_m, ch.y)
            x2, y2 = np.interp(after, ch.distance_m, ch.x), np.interp(after, ch.distance_m, ch.y)
            direction = np.array([x2 - x1, y2 - y1])
            norm = np.linalg.norm(direction)
            if norm < 1e-9:
                continue
            perpendicular = np.array([-direction[1], direction[0]]) / norm  # points left
            cx, cy = np.interp(d, ch.distance_m, ch.x), np.interp(d, ch.distance_m, ch.y)
            # Profile from the left bank to the right bank (looking downstream).
            xs = cx + perpendicular[0] * (-offsets)
            ys = cy + perpendicular[1] * (-offsets)
            elevations = bilinear_sample(dem, gt, xs, ys)
            profiles.append(CrossProfile(offsets + half, elevations, half))
        return profiles


def options_for(requirements: Iterable[str], parameters: Dict[str, dict],
                channel_threshold_km2: float) -> BuildOptions:
    """
    Derive extraction options from the selected indices.

    ``parameters`` maps index keys to their resolved parameters; the T
    section count comes from TTSF (or TTSF_Azimuth) and the cross-profile
    geometry from VF.
    """
    options = BuildOptions(requirements=set(requirements),
                           channel_threshold_m2=channel_threshold_km2 * 1.0e6)
    t_params = parameters.get("TTSF") or parameters.get("TTSF_Azimuth") or {}
    if "sections" in t_params:
        options.sections = int(t_params["sections"])
    vf = parameters.get("VF") or {}
    if vf:
        options.vf_profiles = int(vf.get("profiles", options.vf_profiles))
        options.vf_start_fraction = float(vf.get("start_fraction", options.vf_start_fraction))
        options.vf_end_fraction = float(vf.get("end_fraction", options.vf_end_fraction))
        options.vf_width_m = float(vf.get("profile_width_m", options.vf_width_m))
        options.vf_sampling_m = float(vf.get("sampling_m", options.vf_sampling_m))
    return options
