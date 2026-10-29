"""
tools/morphometry/engine.py

Computation side of the Morphometric Indices tool.

The tool is split in three steps so that QGIS layers are only touched on the
main thread (QgsMapLayer objects are not thread-safe):

1. ``collect_request``  (main thread) — reads the layers chosen by the user,
   picks the metric work CRS, copies the basin and stream geometries into it
   and freezes everything into a ``MorphometryRequest`` made of plain data.
2. ``run_morphometry``  (any thread)  — opens the DEM with GDAL, builds one
   ``BasinContext`` per basin, computes and classifies every selected index.
   Used by ``MorphometryTask`` (a ``QgsTask``) in QGIS and directly by tests.
3. ``build_result_layer`` / ``build_channel_layer`` (main thread) — turn the
   result into memory layers the user can style, inspect and save.

``readiness_issues`` checks the inputs before anything runs; the panel shows
its messages and keeps the Run button disabled while the list is not empty.

Authors: RockMorph contributors / Tony
"""

from __future__ import annotations

import math
import os
import time
from dataclasses import dataclass, field
from typing import Callable, Dict, List, Optional, Sequence, Tuple

import numpy as np  # type: ignore
from qgis.PyQt.QtCore import QCoreApplication, QVariant, pyqtSignal  # type: ignore
from qgis.core import (  # type: ignore
    QgsCoordinateReferenceSystem, QgsCoordinateTransform, QgsFeature,
    QgsFeatureRequest, QgsField, QgsGeometry, QgsPointXY, QgsProject,
    QgsRasterLayer, QgsTask, QgsVectorLayer, QgsWkbTypes,
)

from ...base.base_engine import BaseEngine
from ...core.basin_context_builder import (
    BasinContextBuilder, BasinFeature, StreamSegment, choose_work_crs,
    options_for, prepare_work_dem,
)
from ...core.indices import compute_index, get
from ...core.indices.profiles import sl_reaches
from ...core.indices.spec import Classification


def tr(message: str) -> str:
    return QCoreApplication.translate("RockMorph", message)


# Longest channel profile kept per basin for the charts. Profiles are
# decimated (not smoothed) above this size: the chart only needs the shape.
MAX_PROFILE_POINTS = 600


# ---------------------------------------------------------------------------
# Request and result data
# ---------------------------------------------------------------------------

@dataclass
class IndexSelection:
    """
    One index chosen by the user.

    params         : parameter values from the panel (coerced again by the
                     spec before use).
    classification : scheme used to label the values; may be a user-edited
                     copy of a published scheme. None = no classification.
    """

    key: str
    params: Dict[str, object] = field(default_factory=dict)
    classification: Optional[Classification] = None


@dataclass
class MorphometryRequest:
    """Everything ``run_morphometry`` needs, as plain thread-safe data."""

    dem_path: str
    dem_crs: QgsCoordinateReferenceSystem
    work_crs: QgsCoordinateReferenceSystem
    basins: List[BasinFeature]
    selections: List[IndexSelection]
    segments: List[StreamSegment] = field(default_factory=list)
    channel_threshold_km2: float = 1.0
    source_name: str = ""


@dataclass
class BasinResult:
    """Values, classes and diagnostics of one basin."""

    basin_id: object
    label: str
    geometry: QgsGeometry
    values: Dict[str, Optional[float]] = field(default_factory=dict)
    classes: Dict[str, Tuple[Optional[str], Optional[int]]] = field(default_factory=dict)
    warnings: List[str] = field(default_factory=list)
    channel: Optional[Dict[str, list]] = None


@dataclass
class MorphometryResult:
    """Output of one run."""

    rows: List[BasinResult]
    selections: List[IndexSelection]
    work_crs: QgsCoordinateReferenceSystem
    elapsed_s: float = 0.0
    cancelled: bool = False
    source_name: str = ""

    def table(self) -> Tuple[List[str], List[list]]:
        """Column names and rows (values only) for CSV export and display."""
        columns = ["basin_id", "label"]
        for selection in self.selections:
            columns.append(selection.key)
            if selection.classification is not None:
                columns.append(f"{selection.key}_class")
                if selection.classification.is_scored:
                    columns.append(f"{selection.key}_score")
        rows = []
        for basin in self.rows:
            row = [basin.basin_id, basin.label]
            for selection in self.selections:
                row.append(basin.values.get(selection.key))
                if selection.classification is not None:
                    label, score = basin.classes.get(selection.key, (None, None))
                    row.append(label)
                    if selection.classification.is_scored:
                        row.append(score)
            rows.append(row)
        return columns, rows


# ---------------------------------------------------------------------------
# Readiness
# ---------------------------------------------------------------------------

def readiness_issues(dem_layer: Optional[QgsRasterLayer],
                     basin_layer: Optional[QgsVectorLayer],
                     id_field: str,
                     selected_keys: Sequence[str],
                     selected_only: bool,
                     stream_layer: Optional[QgsVectorLayer] = None,
                     invalid_parameters: Sequence[str] = ()) -> List[str]:
    """
    Plain-language list of what prevents the run, in the order the user
    should fix it. An empty list means the Run button can be enabled.

    Only checks that are cheap and certain are done here; problems that
    depend on the data of one basin (too small, outside the DEM...) are
    reported per basin after the run instead of blocking everything.
    """
    issues: List[str] = []
    if dem_layer is None:
        issues.append(tr("Choose a DEM (elevation raster)."))
    elif not dem_layer.isValid():
        issues.append(tr("The selected DEM cannot be read."))
    elif dem_layer.bandCount() < 1:
        issues.append(tr("The selected raster has no band."))
    elif dem_layer.providerType() != "gdal":
        # The computation opens the DEM with GDAL in a background thread, so
        # web services and virtual rasters must be saved to a file first.
        issues.append(tr("The DEM must be a raster file read by GDAL (export it to "
                         "GeoTIFF first)."))

    if basin_layer is None:
        issues.append(tr("Choose the basin (watershed) polygon layer."))
    elif not basin_layer.isValid():
        issues.append(tr("The basin layer cannot be read."))
    elif QgsWkbTypes.geometryType(basin_layer.wkbType()) != QgsWkbTypes.PolygonGeometry:
        issues.append(tr("The basin layer must contain polygons."))
    else:
        count = basin_layer.selectedFeatureCount() if selected_only else basin_layer.featureCount()
        if count == 0:
            issues.append(tr("No basin selected: select features on the map or untick "
                             "'Selected features only'.") if selected_only
                          else tr("The basin layer is empty."))
        if not id_field:
            issues.append(tr("Choose the field that identifies each basin."))

    if stream_layer is not None and (
            not stream_layer.isValid()
            or QgsWkbTypes.geometryType(stream_layer.wkbType()) != QgsWkbTypes.LineGeometry):
        issues.append(tr("The stream network must be a valid line layer (or leave it empty "
                         "to derive the network from the DEM)."))

    if not selected_keys:
        issues.append(tr("Tick at least one index in the catalogue (or apply a preset)."))
    for message in invalid_parameters:
        issues.append(message)

    if (dem_layer is not None and basin_layer is not None and dem_layer.isValid()
            and basin_layer.isValid() and not issues):
        transform = QgsCoordinateTransform(basin_layer.crs(), dem_layer.crs(), QgsProject.instance())
        try:
            extent = transform.transformBoundingBox(basin_layer.extent())
        except Exception:  # transformation failure = CRS problem worth reporting
            issues.append(tr("The basin layer cannot be transformed to the DEM CRS; "
                             "check both layer CRSs."))
        else:
            if not extent.intersects(dem_layer.extent()):
                issues.append(tr("The basins do not overlap the DEM. Check the layers and "
                                 "their CRS."))
    return issues


# ---------------------------------------------------------------------------
# Step 1 — main thread: freeze the inputs
# ---------------------------------------------------------------------------

def collect_request(dem_layer: QgsRasterLayer, basin_layer: QgsVectorLayer,
                    id_field: str, label_field: str, selected_only: bool,
                    selections: List[IndexSelection],
                    stream_layer: Optional[QgsVectorLayer] = None,
                    order_field: str = "",
                    channel_threshold_km2: float = 1.0) -> MorphometryRequest:
    """
    Read the layers (main thread only) into a ``MorphometryRequest``.

    The work CRS is the DEM CRS when it is projected in metres; otherwise the
    UTM zone of the basins (see ``choose_work_crs``). Basin and stream
    geometries are transformed into it here, once.
    """
    project = QgsProject.instance()
    dem_crs = dem_layer.crs()
    to_wgs84 = QgsCoordinateTransform(basin_layer.crs(),
                                      QgsCoordinateReferenceSystem("EPSG:4326"), project)
    centre = to_wgs84.transformBoundingBox(basin_layer.extent()).center()
    work_crs = choose_work_crs(dem_crs, (centre.x(), centre.y()))

    to_work = QgsCoordinateTransform(basin_layer.crs(), work_crs, project)
    features = basin_layer.getSelectedFeatures() if selected_only else basin_layer.getFeatures()
    basins: List[BasinFeature] = []
    for feature in features:
        geometry = QgsGeometry(feature.geometry())
        if geometry.isEmpty():
            continue
        geometry.transform(to_work)
        basin_id = feature[id_field] if id_field else feature.id()
        label = str(feature[label_field]) if label_field and feature[label_field] is not None \
            else str(basin_id)
        basins.append(BasinFeature(_plain(basin_id), label, geometry))

    segments: List[StreamSegment] = []
    if stream_layer is not None:
        to_work_streams = QgsCoordinateTransform(stream_layer.crs(), work_crs, project)
        # Only streams near the basins are copied (large national networks).
        area = QgsGeometry.unaryUnion([b.geometry for b in basins]).boundingBox() if basins else None
        request = QgsFeatureRequest()
        if area is not None:
            back = QgsCoordinateTransform(work_crs, stream_layer.crs(), project)
            request.setFilterRect(back.transformBoundingBox(area))
        for feature in stream_layer.getFeatures(request):
            geometry = QgsGeometry(feature.geometry())
            if geometry.isEmpty():
                continue
            geometry.transform(to_work_streams)
            order = None
            if order_field:
                try:
                    order = int(feature[order_field])
                except (TypeError, ValueError):
                    order = None
            segments.append(StreamSegment(geometry, order if order and order > 0 else None))

    source = dem_layer.source().split("|")[0]
    return MorphometryRequest(
        dem_path=source, dem_crs=dem_crs, work_crs=work_crs, basins=basins,
        selections=selections, segments=segments,
        channel_threshold_km2=channel_threshold_km2,
        source_name=basin_layer.name(),
    )


def _plain(value):
    """Convert QVariant-ish attribute values to plain Python for tables/JSON."""
    if value is None:
        return None
    if isinstance(value, (int, float, str)):
        return value
    try:
        if value.isNull():  # QVariant NULL
            return None
    except AttributeError:
        pass
    return str(value)


# ---------------------------------------------------------------------------
# Step 2 — any thread: compute
# ---------------------------------------------------------------------------

def run_morphometry(request: MorphometryRequest,
                    progress: Optional[Callable[[float, str], None]] = None,
                    is_cancelled: Optional[Callable[[], bool]] = None) -> MorphometryResult:
    """
    Compute every selected index for every basin of ``request``.

    Never raises for a bad basin: problems are stored in
    ``BasinResult.warnings``. Raises only when the DEM itself cannot be
    opened or reprojected (then nothing can be computed).
    """
    started = time.time()
    progress = progress or (lambda value, message: None)
    is_cancelled = is_cancelled or (lambda: False)

    specs = [get(s.key) for s in request.selections]
    params = {s.key: specs[i].resolve_parameters(s.params) for i, s in enumerate(request.selections)}
    requirements = set().union(*[set(spec.requires) for spec in specs]) if specs else set()
    options = options_for(requirements, params, request.channel_threshold_km2)
    want_profiles = "channel" in requirements

    progress(0.0, tr("Preparing the DEM…"))
    dem_path, temporary = prepare_work_dem(request.dem_path, request.dem_crs, request.work_crs)
    rows: List[BasinResult] = []
    cancelled = False
    try:
        with BasinContextBuilder(dem_path, options, request.segments) as builder:
            total = max(1, len(request.basins))
            for number, basin in enumerate(request.basins):
                if is_cancelled():
                    cancelled = True
                    break
                progress(100.0 * number / total,
                         tr("Basin {0} ({1}/{2})").format(basin.label, number + 1, total))
                rows.append(_compute_basin(builder, basin, request.selections, params, want_profiles))
    finally:
        if temporary:
            try:
                os.remove(dem_path)
            except OSError:
                pass
    progress(100.0, tr("Done"))
    return MorphometryResult(rows=rows, selections=request.selections,
                             work_crs=request.work_crs, elapsed_s=time.time() - started,
                             cancelled=cancelled, source_name=request.source_name)


def _compute_basin(builder: BasinContextBuilder, basin: BasinFeature,
                   selections: List[IndexSelection], params: Dict[str, dict],
                   want_profile: bool) -> BasinResult:
    """Context extraction + indices + classes for one basin (never raises)."""
    result = BasinResult(basin.basin_id, basin.label, QgsGeometry(basin.geometry))
    try:
        ctx = builder.build(basin)
    except Exception as error:  # GDAL/GEOS failure on one basin
        result.warnings.append(tr("Extraction failed: {0}").format(error))
        for selection in selections:
            result.values[selection.key] = None
            result.classes[selection.key] = (None, None)
        return result
    for selection in selections:
        value = compute_index(selection.key, ctx, params[selection.key])
        result.values[selection.key] = value
        scheme = selection.classification
        result.classes[selection.key] = scheme.classify(value) if scheme else (None, None)
    result.warnings.extend(ctx.warnings)
    if want_profile and ctx.channel is not None:
        result.channel = _profile_payload(ctx.channel, params.get("SL", {}))
    return result


def _profile_payload(channel, sl_params: dict) -> Dict[str, list]:
    """
    Decimated longitudinal profile (source -> outlet) plus SL reaches, for
    the profile chart and the trunk-stream layer.
    """
    n = channel.distance_m.size
    step = max(1, int(math.ceil(n / MAX_PROFILE_POINTS)))
    keep = np.unique(np.concatenate((np.arange(0, n, step), [n - 1])))
    reach = float(sl_params.get("reach_length_m", 200.0) or 200.0)
    reaches = sl_reaches(channel.distance_m, channel.elevation_m, reach)
    return {
        "distance": channel.distance_m[keep].round(1).tolist(),
        "elevation": channel.elevation_m[keep].round(2).tolist(),
        "area_km2": (channel.drainage_area_m2[keep] / 1e6).round(4).tolist(),
        "x": channel.x[keep].tolist(),
        "y": channel.y[keep].tolist(),
        "sl_mid": [round(r["mid_m"], 1) for r in reaches],
        "sl": [round(r["sl"], 2) for r in reaches],
    }


class MorphometryEngine(BaseEngine):
    """
    ``BaseEngine`` adapter so the tool follows the plugin convention
    (``compute(**kwargs) -> dict``).
    """

    def compute(self, **kwargs) -> dict:
        request: MorphometryRequest = kwargs["request"]
        callback = kwargs.get("progress_callback")
        result = run_morphometry(
            request,
            progress=(lambda value, message: callback(int(value), message)) if callback else None,
            is_cancelled=kwargs.get("is_cancelled"),
        )
        return {"result": result}


class MorphometryTask(QgsTask):
    """
    Background task running ``run_morphometry``.

    QgsTask (rather than a bare QThread) gives the QGIS task manager progress
    bar, cancellation from the status bar and safe shutdown when QGIS closes.
    ``finished`` runs on the main thread and emits ``completed`` with the
    result, or ``failed`` with a message.
    """

    completed = pyqtSignal(object)
    failed = pyqtSignal(str)
    message = pyqtSignal(float, str)

    def __init__(self, request: MorphometryRequest):
        super().__init__(tr("RockMorph — morphometric indices"), QgsTask.CanCancel)
        self.request = request
        self.result: Optional[MorphometryResult] = None
        self.error: str = ""

    def run(self) -> bool:
        def report(value: float, text: str) -> None:
            self.setProgress(value)
            self.message.emit(value, text)
        try:
            self.result = run_morphometry(self.request, report, self.isCanceled)
        except Exception as error:  # the whole run failed (DEM unreadable…)
            import traceback
            traceback.print_exc()
            self.error = str(error)
            return False
        return True

    def finished(self, success: bool) -> None:
        if success and self.result is not None:
            self.completed.emit(self.result)
        else:
            self.failed.emit(self.error or tr("The computation was cancelled."))


# ---------------------------------------------------------------------------
# Step 3 — main thread: output layers
# ---------------------------------------------------------------------------

def build_result_layer(result: MorphometryResult, name: str) -> QgsVectorLayer:
    """
    Memory polygon layer (work CRS) with one field per index, plus
    ``<key>_class`` (text) and ``<key>_score`` (integer) when classified.
    Field names equal the index keys so that the IAT / AHP / statistics tools
    can pick them directly.
    """
    layer = QgsVectorLayer(f"Polygon?crs={result.work_crs.authid() or 'EPSG:4326'}", name, "memory")
    if not result.work_crs.authid():
        layer.setCrs(result.work_crs)
    provider = layer.dataProvider()
    columns, rows = result.table()
    fields = [QgsField("basin_id", QVariant.String), QgsField("label", QVariant.String),
              QgsField("warnings", QVariant.String)]
    types = []
    for column in columns[2:]:
        if column.endswith("_class"):
            types.append(QVariant.String)
        elif column.endswith("_score"):
            types.append(QVariant.Int)
        else:
            types.append(QVariant.Double)
        fields.append(QgsField(column, types[-1]))
    provider.addAttributes(fields)
    layer.updateFields()
    features = []
    for basin, row in zip(result.rows, rows):
        feature = QgsFeature(layer.fields())
        feature.setGeometry(basin.geometry)
        attributes = [str(row[0]), row[1], " | ".join(basin.warnings)[:250] or None]
        for value, kind in zip(row[2:], types):
            if value is None:
                attributes.append(None)
            elif kind == QVariant.Int:
                attributes.append(int(value))
            elif kind == QVariant.Double:
                attributes.append(float(value))
            else:
                attributes.append(str(value))
        feature.setAttributes(attributes)
        features.append(feature)
    provider.addFeatures(features)
    layer.updateExtents()
    return layer


def build_channel_layer(result: MorphometryResult, name: str) -> Optional[QgsVectorLayer]:
    """Trunk streams (source -> outlet) used for the channel indices."""
    with_channel = [b for b in result.rows if b.channel]
    if not with_channel:
        return None
    layer = QgsVectorLayer(f"LineString?crs={result.work_crs.authid() or 'EPSG:4326'}", name, "memory")
    provider = layer.dataProvider()
    provider.addAttributes([QgsField("basin_id", QVariant.String), QgsField("label", QVariant.String),
                            QgsField("length_m", QVariant.Double)])
    layer.updateFields()
    features = []
    for basin in with_channel:
        points = [QgsPointXY(x, y) for x, y in zip(basin.channel["x"], basin.channel["y"])]
        feature = QgsFeature(layer.fields())
        feature.setGeometry(QgsGeometry.fromPolylineXY(points))
        feature.setAttributes([str(basin.basin_id), basin.label, float(basin.channel["distance"][-1])])
        features.append(feature)
    provider.addFeatures(features)
    layer.updateExtents()
    return layer


# ---------------------------------------------------------------------------
# Viewer payload
# ---------------------------------------------------------------------------

def _json_number(value) -> Optional[float]:
    """None for missing/NaN/inf so the payload is strict JSON."""
    if value is None:
        return None
    try:
        number = float(value)
    except (TypeError, ValueError):
        return None
    return number if math.isfinite(number) else None


def result_payload(result: MorphometryResult, title: str) -> dict:
    """Data for ``web/js/morphometry.js`` (format documented there)."""
    indices = []
    for selection in result.selections:
        spec = get(selection.key)
        scheme = selection.classification
        indices.append({
            "key": spec.key, "name": spec.name, "unit": spec.unit,
            "classification": scheme.label if scheme else None,
            "classes": [{"label": r.label, "score": r.score} for r in scheme.rules] if scheme else None,
        })
    basins = []
    for row in result.rows:
        basins.append({
            "id": str(row.basin_id), "label": row.label,
            "values": {k: _json_number(v) for k, v in row.values.items()},
            "classes": {k: [c[0], c[1]] for k, c in row.classes.items()},
            "warnings": list(row.warnings),
            "profile": row.channel,
        })
    crs = result.work_crs.authid() or result.work_crs.description()
    subtitle = tr("{0} basin(s) · {1} index(es) · work CRS {2} · {3:.1f} s").format(
        len(result.rows), len(result.selections), crs, result.elapsed_s)
    if result.cancelled:
        subtitle += " · " + tr("cancelled: partial results")
    return {"title": title, "subtitle": subtitle, "indices": indices, "basins": basins}
