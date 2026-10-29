"""
tools/synthesis/common.py

Shared base of the synthesis tools (Iat, AHP, composite index, statistics &
machine learning).

These tools work on ANY vector layer holding numeric attributes: typically
the output of the Morphometric Indices tool, but also a table prepared in a
spreadsheet and joined to basins, or results from another software. The user
chooses the layer, the identifier, and the fields; nothing is imposed.

What the base class provides
----------------------------
* the "Layer" and "Fields" groups (numeric fields only; a field named like a
  catalogue index shows the index name next to it);
* a readiness checklist that keeps Run disabled while something is missing;
* reading the attribute table on the main thread into a ``TableData``;
* the output: a NEW memory layer (source geometry + id/label + input fields +
  result fields). The user's layer is never modified;
* the results window (shared with the morphometry tool) and CSV/GeoPackage
  export.

Computations are fast (attribute tables of tens to thousands of basins), so
they run on the main thread; t-SNE/UMAP on very large tables are the only
slow cases and show a busy cursor.

Subclasses implement ``_build_settings``, ``_settings_issues`` and
``_compute`` (see ``SynthesisPanel``).

Authors: RockMorph contributors / Tony
"""

from __future__ import annotations

import csv
import math
from dataclasses import dataclass, field
from typing import Dict, List, Optional, Sequence, Tuple

import numpy as np  # type: ignore
from qgis.PyQt.QtCore import QCoreApplication, Qt, QVariant, pyqtSignal  # type: ignore
from qgis.PyQt.QtGui import QCursor  # type: ignore
from qgis.PyQt.QtWidgets import (  # type: ignore
    QApplication, QCheckBox, QComboBox, QDoubleSpinBox, QFileDialog, QFormLayout,
    QGroupBox, QHBoxLayout, QHeaderView, QLabel, QListWidget, QListWidgetItem,
    QPushButton, QScrollArea, QSizePolicy, QTableWidget, QTableWidgetItem, QToolButton,
    QVBoxLayout, QWidget,
)
from qgis.core import (  # type: ignore
    QgsCategorizedSymbolRenderer, QgsCoordinateTransformContext, QgsFeature,
    QgsFeatureRequest, QgsField, QgsFillSymbol, QgsMapLayerProxyModel, QgsProject,
    QgsRendererCategory, QgsVectorFileWriter, QgsVectorLayer, QgsWkbTypes,
)
from qgis.gui import QgsFieldComboBox, QgsMapLayerComboBox  # type: ignore

from ...core.indices import INDEX_BY_KEY
from ...widgets.results_window import ResultsWindow


def tr(message: str) -> str:
    return QCoreApplication.translate("RockMorph", message)


OUTPUT_GROUP = "RockMorph — synthesis"
NUMERIC_TYPES = (QVariant.Int, QVariant.UInt, QVariant.LongLong, QVariant.ULongLong, QVariant.Double)
# Ordinal class colours shared with the morphometry tool (class 1 darkest).
CLASS_COLORS = {1: "#184f95", 2: "#2a78d6", 3: "#6da7ec", 4: "#9ec5f4", 5: "#cde2fb"}


# ---------------------------------------------------------------------------
# Data
# ---------------------------------------------------------------------------

@dataclass
class TableData:
    """Attribute table read on the main thread."""

    ids: List[str]
    labels: List[str]
    feature_ids: List[int]
    columns: Dict[str, np.ndarray] = field(default_factory=dict)

    @property
    def size(self) -> int:
        return len(self.ids)

    def matrix(self, names: Sequence[str]) -> np.ndarray:
        """(basins × fields) float matrix, NaN for missing values."""
        return np.column_stack([self.columns[n] for n in names]) if names else np.empty((self.size, 0))


@dataclass
class OutputColumn:
    """
    One result field.

    kind: "double", "int" or "text". ``classification`` (label list with
    scores) is passed to the viewer so that it can colour classes.
    """

    name: str
    values: Sequence
    kind: str = "double"
    title: str = ""
    unit: str = ""
    classes: Optional[Sequence[Tuple[Optional[str], Optional[int]]]] = None
    class_rules: Optional[List[dict]] = None
    class_scheme: str = ""


def _to_float(value) -> float:
    if value is None:
        return math.nan
    try:
        if hasattr(value, "isNull") and value.isNull():
            return math.nan
        return float(value)
    except (TypeError, ValueError):
        return math.nan


def read_table(layer: QgsVectorLayer, id_field: str, label_field: str,
               fields: Sequence[str], selected_only: bool) -> TableData:
    """Read ids, labels and the numeric ``fields`` (main thread only)."""
    request = QgsFeatureRequest().setFlags(QgsFeatureRequest.NoGeometry)
    features = layer.getSelectedFeatures(request) if selected_only else layer.getFeatures(request)
    ids, labels, fids, values = [], [], [], {name: [] for name in fields}
    for feature in features:
        identifier = feature[id_field] if id_field else feature.id()
        ids.append(str(identifier))
        label = feature[label_field] if label_field else None
        labels.append(str(label) if label not in (None, "") and not (hasattr(label, "isNull") and label.isNull())
                      else str(identifier))
        fids.append(feature.id())
        for name in fields:
            values[name].append(_to_float(feature[name]))
    return TableData(ids, labels, fids, {k: np.asarray(v, dtype=float) for k, v in values.items()})


def section_label(text: str) -> QLabel:
    """Word-wrapped rich-text heading: never forces the dock wider."""
    label = QLabel(text)
    label.setWordWrap(True)
    return label


def numeric_field_names(layer: Optional[QgsVectorLayer]) -> List[str]:
    if layer is None:
        return []
    return [f.name() for f in layer.fields() if f.type() in NUMERIC_TYPES]


def field_title(name: str) -> str:
    """'SL — Stream length-gradient index SL' when ``name`` is a catalogue key."""
    spec = INDEX_BY_KEY.get(name)
    return f"{name} — {spec.name}" if spec else name


# ---------------------------------------------------------------------------
# Output layer
# ---------------------------------------------------------------------------

_QTYPES = {"double": QVariant.Double, "int": QVariant.Int, "text": QVariant.String}


def build_output_layer(source: QgsVectorLayer, table: TableData, inputs: Sequence[str],
                       outputs: Sequence[OutputColumn], name: str) -> QgsVectorLayer:
    """
    Memory copy of the processed features: geometry (source CRS), id,
    label, the input fields and the result fields.
    """
    geometry_name = QgsWkbTypes.displayString(source.wkbType()) or "Polygon"
    layer = QgsVectorLayer(f"{geometry_name}?crs={source.crs().authid()}", name, "memory")
    if not source.crs().authid():
        layer.setCrs(source.crs())
    provider = layer.dataProvider()
    fields = [QgsField("basin_id", QVariant.String), QgsField("label", QVariant.String)]
    fields += [QgsField(n, QVariant.Double) for n in inputs]
    fields += [QgsField(o.name, _QTYPES[o.kind]) for o in outputs]
    provider.addAttributes(fields)
    layer.updateFields()

    geometries = {f.id(): f.geometry() for f in source.getFeatures(QgsFeatureRequest(table.feature_ids))}
    features = []
    for row in range(table.size):
        feature = QgsFeature(layer.fields())
        geometry = geometries.get(table.feature_ids[row])
        if geometry is not None:
            feature.setGeometry(geometry)
        attributes = [table.ids[row], table.labels[row]]
        attributes += [_cell(table.columns[n][row], "double") for n in inputs]
        attributes += [_cell(o.values[row], o.kind) for o in outputs]
        feature.setAttributes(attributes)
        features.append(feature)
    provider.addFeatures(features)
    layer.updateExtents()
    return layer


def _cell(value, kind: str):
    if value is None:
        return None
    if kind == "text":
        return str(value)
    number = _to_float(value)
    if not math.isfinite(number):
        return None
    return int(round(number)) if kind == "int" else number


def style_by_class(layer: QgsVectorLayer, field_name: str, rules: Sequence[dict], title: str) -> None:
    """
    Categorised renderer on an integer class field. Ordinal classes use the
    class ramp (class 1 darkest); a rule may carry its own "color" (used for
    categorical results such as clusters).
    """
    categories = []
    for rule in rules:
        score = rule.get("score")
        color = rule.get("color") or CLASS_COLORS.get(score, "#cccccc")
        symbol = QgsFillSymbol.createSimple({"color": color,
                                             "outline_color": "#ffffff", "outline_width": "0.3"})
        categories.append(QgsRendererCategory(score, symbol, f"{title} — {rule['label']}"))
    symbol = QgsFillSymbol.createSimple({"color": "#e6e5e0", "outline_color": "#ffffff"})
    categories.append(QgsRendererCategory(None, symbol, tr("{0} — no value").format(title)))
    if QgsWkbTypes.geometryType(layer.wkbType()) == QgsWkbTypes.PolygonGeometry:
        layer.setRenderer(QgsCategorizedSymbolRenderer(field_name, categories))
        layer.setOpacity(0.85)


def json_number(value) -> Optional[float]:
    number = _to_float(value)
    return number if math.isfinite(number) else None


def table_payload(title: str, subtitle: str, table: TableData, inputs: Sequence[str],
                  outputs: Sequence[OutputColumn], figures: Optional[List[dict]] = None,
                  default_figure: Optional[str] = None,
                  warnings: Optional[Dict[int, List[str]]] = None) -> dict:
    """Viewer payload (format documented in web/js/morphometry.js)."""
    indices, columns, classes = [], {}, {}
    for name in inputs:
        spec = INDEX_BY_KEY.get(name)
        indices.append({"key": name, "name": spec.name if spec else name,
                        "unit": spec.unit if spec else "", "classification": None, "classes": None})
        columns[name] = table.columns[name]
    for output in outputs:
        if output.kind == "text":
            continue
        indices.append({"key": output.name, "name": output.title or output.name, "unit": output.unit,
                        "classification": output.class_scheme or None, "classes": output.class_rules})
        columns[output.name] = output.values
        if output.classes is not None:
            classes[output.name] = output.classes
    basins = []
    for row in range(table.size):
        basins.append({
            "id": table.ids[row], "label": table.labels[row],
            "values": {k: json_number(v[row]) for k, v in columns.items()},
            "classes": {k: [c[row][0], c[row][1]] for k, c in classes.items()},
            "warnings": (warnings or {}).get(row, []),
            "profile": None,
        })
    return {"title": title, "subtitle": subtitle, "indices": indices, "basins": basins,
            "figures": figures or [], "defaultFigure": default_figure}


# ---------------------------------------------------------------------------
# Base panel
# ---------------------------------------------------------------------------

class SynthesisPanel(QWidget):
    """
    Base sidebar panel. Subclasses set ``TITLE``, ``INTRO``, ``MIN_FIELDS``
    and implement:

    _build_settings(layout)  add tool widgets to the "3 · Settings" group.
    _on_fields_changed()     optional: react to the checked field list.
    _settings_issues()       list of plain-language problems (blocks Run).
    _compute(table, fields)  return (outputs, figures, default_figure, style,
                             warnings, summary) where style is None or
                             (class field, class rules, legend title) and
                             warnings maps a row number to messages.
    """

    TITLE = "Synthesis"
    INTRO = ""
    MIN_FIELDS = 1
    OUTPUT_PREFIX = "Synthesis"

    def __init__(self, iface, parent=None):
        super().__init__(parent)
        self.iface = iface
        self._window: Optional[ResultsWindow] = None
        self._payload: Optional[dict] = None
        self._output_layer: Optional[QgsVectorLayer] = None
        self._last: Optional[Tuple[TableData, List[str], List[OutputColumn]]] = None
        self._build_ui()
        self._refresh()

    # -- UI -------------------------------------------------------------------

    def _build_ui(self) -> None:
        root = QVBoxLayout(self)
        root.setContentsMargins(0, 0, 0, 0)
        scroll = QScrollArea()
        scroll.setWidgetResizable(True)
        scroll.setHorizontalScrollBarPolicy(Qt.ScrollBarAlwaysOff)
        inner = QWidget()
        scroll.setWidget(inner)
        root.addWidget(scroll)
        layout = QVBoxLayout(inner)
        layout.setContentsMargins(10, 10, 10, 10)
        layout.setSpacing(10)

        if self.INTRO:
            intro = QLabel(self.INTRO)
            intro.setWordWrap(True)
            intro.setStyleSheet("color: #52514e;")
            layout.addWidget(intro)

        layer_group = QGroupBox(tr("1 · Layer"))
        form = QFormLayout(layer_group)
        self.layer_combo = QgsMapLayerComboBox()
        self.layer_combo.setFilters(QgsMapLayerProxyModel.VectorLayer)
        self.layer_combo.setAllowEmptyLayer(True)
        self.layer_combo.setLayer(None)
        self.layer_combo.setToolTip(tr("Any vector layer with numeric attributes, e.g. the output of "
                                       "the Morphometric Indices tool."))
        form.addRow(tr("Layer"), self.layer_combo)
        self.id_field = QgsFieldComboBox()
        form.addRow(tr("ID field"), self.id_field)
        self.label_field = QgsFieldComboBox()
        self.label_field.setAllowEmptyFieldName(True)
        form.addRow(tr("Label field"), self.label_field)
        self.selected_only = QCheckBox(tr("Selected features only"))
        form.addRow("", self.selected_only)
        layout.addWidget(layer_group)

        fields_group = QGroupBox(tr("2 · Fields"))
        fields_layout = QVBoxLayout(fields_group)
        row = QHBoxLayout()
        self.fields_hint = QLabel()
        self.fields_hint.setStyleSheet("color: #52514e;")
        row.addWidget(self.fields_hint, 1)
        for text, state in ((tr("All"), Qt.Checked), (tr("None"), Qt.Unchecked)):
            button = QToolButton()
            button.setText(text)
            button.clicked.connect(lambda _=False, s=state: self._check_all(s))
            row.addWidget(button)
        fields_layout.addLayout(row)
        self.field_list = QListWidget()
        self.field_list.setMinimumHeight(140)
        self.field_list.setSizePolicy(QSizePolicy.Ignored, QSizePolicy.Preferred)
        self.field_list.itemChanged.connect(self._fields_changed)
        fields_layout.addWidget(self.field_list)
        layout.addWidget(fields_group)

        settings_group = QGroupBox(tr("3 · Settings"))
        settings_layout = QVBoxLayout(settings_group)
        self._build_settings(settings_layout)
        layout.addWidget(settings_group)

        run_group = QGroupBox(tr("4 · Run"))
        run_layout = QVBoxLayout(run_group)
        self.readiness = QLabel()
        self.readiness.setWordWrap(True)
        self.readiness.setTextFormat(Qt.RichText)
        run_layout.addWidget(self.readiness)
        self.compute_btn = QPushButton(tr("Run"))
        self.compute_btn.clicked.connect(self._on_run)
        run_layout.addWidget(self.compute_btn)
        self.status = QLabel()
        self.status.setWordWrap(True)
        self.status.setStyleSheet("color: #52514e;")
        run_layout.addWidget(self.status)
        buttons = QHBoxLayout()
        self.show_btn = QPushButton(tr("Show results"))
        self.show_btn.setEnabled(False)
        self.show_btn.clicked.connect(self._show_results)
        buttons.addWidget(self.show_btn)
        self.export_btn = QPushButton(tr("Export table…"))
        self.export_btn.setEnabled(False)
        self.export_btn.clicked.connect(self._export)
        buttons.addWidget(self.export_btn)
        run_layout.addLayout(buttons)
        layout.addWidget(run_group)
        layout.addStretch()

        self.layer_combo.layerChanged.connect(self._on_layer)
        self.id_field.fieldChanged.connect(self._refresh)
        self.selected_only.toggled.connect(self._refresh)
        self._on_layer(None)

    # -- hooks ----------------------------------------------------------------

    def _build_settings(self, layout: QVBoxLayout) -> None:
        raise NotImplementedError

    def _on_fields_changed(self) -> None:
        pass

    def _settings_issues(self) -> List[str]:
        return []

    def _compute(self, table: TableData, fields: List[str]):
        raise NotImplementedError

    # -- fields -----------------------------------------------------------------

    def _on_layer(self, layer) -> None:
        self.id_field.setLayer(layer)
        self.label_field.setLayer(layer)
        if layer is not None:
            names = {f.name().lower(): f.name() for f in layer.fields()}
            for candidate in ("basin_id", "hybas_id", "id", "fid"):
                if candidate in names:
                    self.id_field.setField(names[candidate])
                    break
            if "label" in names:
                self.label_field.setField(names["label"])
        self.field_list.blockSignals(True)
        self.field_list.clear()
        for name in numeric_field_names(layer):
            if name.lower() in ("fid", "basin_id"):
                continue
            item = QListWidgetItem(field_title(name))
            item.setData(Qt.UserRole, name)
            item.setFlags(item.flags() | Qt.ItemIsUserCheckable)
            item.setCheckState(Qt.Unchecked)
            self.field_list.addItem(item)
        self.field_list.blockSignals(False)
        self.fields_hint.setText(tr("{0} numeric field(s)").format(self.field_list.count()) if layer
                                 else tr("Choose a layer first."))
        self._fields_changed()

    def checked_fields(self) -> List[str]:
        return [self.field_list.item(i).data(Qt.UserRole) for i in range(self.field_list.count())
                if self.field_list.item(i).checkState() == Qt.Checked]

    def set_checked_fields(self, names: Sequence[str]) -> None:
        wanted = set(names)
        self.field_list.blockSignals(True)
        for i in range(self.field_list.count()):
            item = self.field_list.item(i)
            item.setCheckState(Qt.Checked if item.data(Qt.UserRole) in wanted else Qt.Unchecked)
        self.field_list.blockSignals(False)
        self._fields_changed()

    def _check_all(self, state) -> None:
        self.field_list.blockSignals(True)
        for i in range(self.field_list.count()):
            self.field_list.item(i).setCheckState(state)
        self.field_list.blockSignals(False)
        self._fields_changed()

    def _fields_changed(self, *args) -> None:
        self._on_fields_changed()
        self._refresh()

    # -- readiness ------------------------------------------------------------------

    def _issues(self) -> List[str]:
        issues = []
        layer = self.layer_combo.currentLayer()
        if layer is None:
            issues.append(tr("Choose the layer that holds the index values."))
        else:
            count = layer.selectedFeatureCount() if self.selected_only.isChecked() else layer.featureCount()
            if count == 0:
                issues.append(tr("No feature selected: select features or untick 'Selected features only'.")
                              if self.selected_only.isChecked() else tr("The layer is empty."))
            if not self.id_field.currentField():
                issues.append(tr("Choose the field that identifies each basin."))
            if self.field_list.count() == 0:
                issues.append(tr("This layer has no numeric field."))
            elif len(self.checked_fields()) < self.MIN_FIELDS:
                issues.append(tr("Tick at least {0} field(s).").format(self.MIN_FIELDS))
        issues.extend(self._settings_issues())
        return issues

    def _refresh(self, *args) -> None:
        if not hasattr(self, "readiness"):
            return
        issues = self._issues()
        if issues:
            items = "".join(f"<li>{i}</li>" for i in issues)
            self.readiness.setText(f"<span style='color:#7a5a00'>⚠ {tr('Before running:')}</span>"
                                   f"<ul style='margin:2px 0 0 -20px'>{items}</ul>")
        else:
            self.readiness.setText(f"<span style='color:#0c7a0c'>✔ {tr('Ready')}</span>")
        self.compute_btn.setEnabled(not issues)

    # -- run ----------------------------------------------------------------------

    def _on_run(self) -> None:
        self._refresh()
        if not self.compute_btn.isEnabled():
            return
        layer = self.layer_combo.currentLayer()
        fields = self.checked_fields()
        table = read_table(layer, self.id_field.currentField(), self.label_field.currentField(),
                           fields, self.selected_only.isChecked())
        QApplication.setOverrideCursor(QCursor(Qt.WaitCursor))
        try:
            outputs, figures, default_figure, style, warnings, summary = self._compute(table, fields)
        except Exception as error:  # report, never crash QGIS
            QApplication.restoreOverrideCursor()
            self.status.setText(tr("Failed: {0}").format(error))
            self.iface.messageBar().pushCritical("RockMorph", f"{self.TITLE}: {error}")
            return
        QApplication.restoreOverrideCursor()

        name = f"{self.OUTPUT_PREFIX} — {layer.name()}"
        output = build_output_layer(layer, table, fields, outputs, name)
        if style is not None:
            style_by_class(output, *style)
        project = QgsProject.instance()
        root = project.layerTreeRoot()
        group = root.findGroup(OUTPUT_GROUP) or root.insertGroup(0, OUTPUT_GROUP)
        project.addMapLayer(output, False)
        group.insertLayer(0, output)
        self._output_layer = output
        self._last = (table, fields, outputs)
        self._payload = table_payload(f"{self.TITLE} — {layer.name()}", summary, table, fields, outputs,
                                      figures, default_figure, warnings)
        self.status.setText(summary)
        self.show_btn.setEnabled(True)
        self.export_btn.setEnabled(True)
        self._show_results()

    def _show_results(self) -> None:
        if self._payload is None:
            return
        if self._window is None:
            self._window = ResultsWindow(f"RockMorph — {self.TITLE}", self.iface.mainWindow())
            self._window.basinsSelected.connect(self._select_on_map)
            self._window.exportTableRequested.connect(self._export)
        self._window.show_payload(self._payload)

    def _select_on_map(self, ids: List[str]) -> None:
        layer = self._output_layer
        if layer is None or not ids:
            return
        try:
            layer.id()
        except RuntimeError:  # layer removed from the project
            self._output_layer = None
            return
        quoted = ",".join("'" + i.replace("'", "''") + "'" for i in ids)
        fids = [f.id() for f in layer.getFeatures(
            QgsFeatureRequest().setFilterExpression(f'"basin_id" IN ({quoted})'))]
        layer.selectByIds(fids)
        if fids:
            self.iface.mapCanvas().zoomToSelected(layer)
            self.iface.mapCanvas().refresh()

    def _export(self) -> None:
        if self._last is None:
            return
        table, fields, outputs = self._last
        path, chosen = QFileDialog.getSaveFileName(
            self, tr("Export the results"), f"{self.OUTPUT_PREFIX.lower().replace(' ', '_')}.csv",
            tr("CSV table (*.csv);;GeoPackage with geometries (*.gpkg)"))
        if not path:
            return
        if path.lower().endswith(".gpkg") or "gpkg" in chosen:
            if not path.lower().endswith(".gpkg"):
                path += ".gpkg"
            options = QgsVectorFileWriter.SaveVectorOptions()
            options.driverName = "GPKG"
            options.layerName = self.OUTPUT_PREFIX.lower().replace(" ", "_")
            error, message, *_ = QgsVectorFileWriter.writeAsVectorFormatV3(
                self._output_layer, path, QgsCoordinateTransformContext(), options)
            if error != QgsVectorFileWriter.NoError:
                self.iface.messageBar().pushCritical("RockMorph", message)
                return
        else:
            if not path.lower().endswith(".csv"):
                path += ".csv"
            with open(path, "w", newline="", encoding="utf-8-sig") as handle:
                writer = csv.writer(handle)
                writer.writerow(["basin_id", "label"] + list(fields) + [o.name for o in outputs])
                for row in range(table.size):
                    values = [_cell(table.columns[n][row], "double") for n in fields]
                    values += [_cell(o.values[row], o.kind) for o in outputs]
                    writer.writerow([table.ids[row], table.labels[row]]
                                    + ["" if v is None else v for v in values])
        self.iface.messageBar().pushSuccess("RockMorph", tr("Results saved: {0}").format(path))

    def cleanup(self) -> None:
        if self._window is not None:
            self._window.close()
            self._window.deleteLater()
            self._window = None


# ---------------------------------------------------------------------------
# Criteria table (AHP, composite)
# ---------------------------------------------------------------------------

def default_direction(name: str) -> int:
    """
    +1 when a higher value means "more active / more important".

    Class scores (fields ending with _score / _iat / _class, and Iat itself)
    use 1 for the MOST active class, so their direction is -1. Among raw
    indices, low Vf (V-shaped valleys) and low Smf (straight fronts) mean
    activity. Everything else defaults to +1 and stays editable in the table.
    """
    lowered = name.lower()
    if lowered.endswith(("_score", "_iat", "_class")) or lowered in ("iat", "iat_class"):
        return -1
    if name in ("VF", "Smf"):
        return -1
    return 1


class CriteriaTable(QWidget):
    """
    One row per criterion: name, direction and (optionally) a weight.

    ``directions()`` -> {name: +1|-1}; ``weights()`` -> {name: weight}.
    """

    changed = pyqtSignal()

    def __init__(self, with_weights: bool = False, parent=None):
        super().__init__(parent)
        self.with_weights = with_weights
        layout = QVBoxLayout(self)
        layout.setContentsMargins(0, 0, 0, 0)
        headers = [tr("Criterion"), tr("Active when")] + ([tr("Weight")] if with_weights else [])
        self.table = QTableWidget(0, len(headers))
        self.table.setHorizontalHeaderLabels(headers)
        self.table.verticalHeader().setVisible(False)
        self.table.horizontalHeader().setSectionResizeMode(0, QHeaderView.Stretch)
        for column in range(1, len(headers)):
            self.table.horizontalHeader().setSectionResizeMode(column, QHeaderView.ResizeToContents)
        self.table.setToolTip(tr("Direction: '↑ high' when a HIGH value means more tectonic activity "
                                 "(or more importance); '↓ low' otherwise (class scores, Vf...). "
                                 "Values are rescaled to 0–1 so that 1 always means more active."))
        # Scroll inside the narrow dock instead of widening it.
        self.table.setSizePolicy(QSizePolicy.Ignored, QSizePolicy.Fixed)
        layout.addWidget(self.table)
        self._names: List[str] = []

    def set_criteria(self, names: Sequence[str]) -> None:
        previous_dir, previous_w = self.directions(), self.weights()
        self._names = list(names)
        self.table.setRowCount(len(names))
        for row, name in enumerate(names):
            item = QTableWidgetItem(name)
            item.setFlags(item.flags() & ~Qt.ItemIsEditable)
            self.table.setItem(row, 0, item)
            combo = QComboBox()
            combo.addItem(tr("↑ high"), 1)
            combo.addItem(tr("↓ low"), -1)
            combo.setCurrentIndex(0 if previous_dir.get(name, default_direction(name)) > 0 else 1)
            combo.currentIndexChanged.connect(self.changed)
            self.table.setCellWidget(row, 1, combo)
            if self.with_weights:
                spin = QDoubleSpinBox()
                spin.setRange(0.0, 100.0)
                spin.setDecimals(2)
                spin.setValue(previous_w.get(name, 1.0))
                spin.valueChanged.connect(self.changed)
                self.table.setCellWidget(row, 2, spin)
        height = self.table.horizontalHeader().sizeHint().height() + \
            self.table.verticalHeader().defaultSectionSize() * max(1, len(names)) + 4
        self.table.setFixedHeight(min(height, 260))
        self.changed.emit()

    def directions(self) -> Dict[str, int]:
        result = {}
        for row, name in enumerate(self._names):
            widget = self.table.cellWidget(row, 1)
            if widget is not None:
                result[name] = int(widget.currentData())
        return result

    def weights(self) -> Dict[str, float]:
        result = {}
        if not self.with_weights:
            return result
        for row, name in enumerate(self._names):
            widget = self.table.cellWidget(row, 2)
            if widget is not None:
                result[name] = float(widget.value())
        return result
