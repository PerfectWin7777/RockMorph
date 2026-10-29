"""
tools/morphometry/panel.py

Sidebar panel of the Morphometric Indices tool.

Workflow shown to the user, top to bottom:

  1. Data        – the user chooses their own DEM, basin polygons (with the
                   identifier field) and, optionally, a stream network. No
                   study area is imposed.
  2. Indices     – searchable catalogue with presets (e.g. El Hamdouni Iat).
  3. Settings    – for the focused index: formula and references, its
                   parameters and its classification (published thresholds,
                   editable).
  4. Readiness   – a plain-language checklist; Run stays disabled until it is
                   empty, so a run can never start with a missing input.
  5. Run         – background QgsTask with progress and Cancel; the result
                   layer, the trunk-stream layer and the results window open
                   automatically. Basins that could not be computed explain
                   why in the Diagnostics tab and in the layer's "warnings"
                   field.

Authors: RockMorph contributors / Tony
"""

from __future__ import annotations

import csv
from typing import Dict, List, Optional

from qgis.PyQt.QtCore import QCoreApplication, Qt  # type: ignore
from qgis.PyQt.QtWidgets import (  # type: ignore
    QCheckBox, QDoubleSpinBox, QFileDialog, QFormLayout, QGroupBox, QHBoxLayout,
    QLabel, QProgressBar, QPushButton, QScrollArea, QVBoxLayout, QWidget,
)
from qgis.core import (  # type: ignore
    QgsApplication, QgsCategorizedSymbolRenderer, QgsCoordinateTransformContext,
    QgsFeatureRequest, QgsFillSymbol, QgsMapLayerProxyModel,
    QgsProject, QgsRendererCategory, QgsVectorFileWriter,
)
from qgis.gui import QgsFieldComboBox, QgsMapLayerComboBox  # type: ignore

from ...core.indices import get
from ...widgets.index_widgets import (
    ClassificationEditor, IndexCatalogTree, IndexInfoCard, ParameterForm,
)
from ...widgets.results_window import ResultsWindow
from .engine import (
    IndexSelection, MorphometryResult, MorphometryTask, build_channel_layer,
    build_result_layer, collect_request, readiness_issues, result_payload,
)


def tr(message: str) -> str:
    return QCoreApplication.translate("RockMorph", message)


# Ordinal class colours (class 1 = darkest), the same ramp as the viewer.
CLASS_COLORS = {1: "#184f95", 2: "#2a78d6", 3: "#6da7ec", 4: "#9ec5f4"}
OUTPUT_GROUP = "RockMorph — morphometric indices"


class MorphometryPanel(QWidget):
    """Morphometric Indices tool (sidebar)."""

    def __init__(self, iface, parent=None):
        super().__init__(parent)
        self.iface = iface
        self._params: Dict[str, dict] = {}       # key -> parameter values edited by the user
        self._schemes: Dict[str, object] = {}    # key -> Classification | NONE_KEY | None (invalid)
        self._focus: Optional[str] = None
        self._task: Optional[MorphometryTask] = None
        self._result: Optional[MorphometryResult] = None
        self._result_layer = None
        self._window: Optional[ResultsWindow] = None
        self._build_ui()
        self._connect_project()
        self._refresh_readiness()

    # ------------------------------------------------------------------
    # UI
    # ------------------------------------------------------------------

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

        intro = QLabel(tr("Compute published morphometric and active-tectonics indices for your "
                          "own basins, classify them and compare them in tables and charts."))
        intro.setWordWrap(True)
        intro.setStyleSheet("color: #52514e;")
        layout.addWidget(intro)

        layout.addWidget(self._build_data_group())
        layout.addWidget(self._build_catalog_group())
        layout.addWidget(self._build_settings_group())
        layout.addWidget(self._build_run_group())
        layout.addStretch()

    def _build_data_group(self) -> QGroupBox:
        group = QGroupBox(tr("1 · Data"))
        form = QFormLayout(group)

        self.dem_combo = QgsMapLayerComboBox()
        self.dem_combo.setFilters(QgsMapLayerProxyModel.RasterLayer)
        self.dem_combo.setAllowEmptyLayer(True)
        self.dem_combo.setToolTip(tr("Digital elevation model in metres. Any CRS: a geographic DEM is "
                                     "reprojected to the UTM zone of the basins for the computation."))
        form.addRow(tr("DEM"), self.dem_combo)

        self.basin_combo = QgsMapLayerComboBox()
        self.basin_combo.setFilters(QgsMapLayerProxyModel.PolygonLayer)
        self.basin_combo.setAllowEmptyLayer(True)
        self.basin_combo.setToolTip(tr("One polygon per drainage basin (any CRS)."))
        form.addRow(tr("Basins"), self.basin_combo)

        self.id_field = QgsFieldComboBox()
        self.id_field.setToolTip(tr("Field that identifies each basin in the results."))
        form.addRow(tr("Basin ID field"), self.id_field)

        self.label_field = QgsFieldComboBox()
        self.label_field.setAllowEmptyFieldName(True)
        self.label_field.setToolTip(tr("Optional: readable name shown in tables and charts."))
        form.addRow(tr("Label field"), self.label_field)

        self.selected_only = QCheckBox(tr("Selected features only"))
        form.addRow("", self.selected_only)

        self.stream_combo = QgsMapLayerComboBox()
        self.stream_combo.setFilters(QgsMapLayerProxyModel.LineLayer)
        self.stream_combo.setAllowEmptyLayer(True, tr("Derive from the DEM"))
        self.stream_combo.setToolTip(tr("Optional stream network for the Horton–Strahler indices. "
                                        "When empty, the network is derived from the DEM with the "
                                        "channel-initiation area below."))
        form.addRow(tr("Stream network"), self.stream_combo)

        self.order_field = QgsFieldComboBox()
        self.order_field.setAllowEmptyFieldName(True)
        self.order_field.setToolTip(tr("Strahler order field of the network. Leave empty to "
                                       "recompute the orders from the network topology."))
        form.addRow(tr("Order field"), self.order_field)

        self.threshold = QDoubleSpinBox()
        self.threshold.setRange(0.001, 1000.0)
        self.threshold.setDecimals(3)
        self.threshold.setValue(1.0)
        self.threshold.setSuffix(" km²")
        self.threshold.setToolTip(tr("Drainage area at which a channel starts, for the network derived "
                                     "from the DEM (GSV-Studio default: 1 km²). Smaller values give "
                                     "denser networks and higher drainage density."))
        form.addRow(tr("Channel initiation"), self.threshold)

        self.basin_combo.layerChanged.connect(self._on_basin_layer)
        self.stream_combo.layerChanged.connect(self._on_stream_layer)
        for signal in (self.dem_combo.layerChanged, self.id_field.fieldChanged,
                       self.selected_only.toggled):
            signal.connect(self._refresh_readiness)
        self.dem_combo.setLayer(None)
        self.basin_combo.setLayer(None)
        self.stream_combo.setLayer(None)
        self._on_basin_layer(None)
        self._on_stream_layer(None)
        return group

    def _build_catalog_group(self) -> QGroupBox:
        group = QGroupBox(tr("2 · Indices"))
        layout = QVBoxLayout(group)
        self.catalog = IndexCatalogTree()
        self.catalog.selectionChanged.connect(self._on_selection_changed)
        self.catalog.focusChanged.connect(self._on_focus_changed)
        layout.addWidget(self.catalog)
        return group

    def _build_settings_group(self) -> QGroupBox:
        self.settings_group = QGroupBox(tr("3 · Index settings"))
        layout = QVBoxLayout(self.settings_group)
        self.info = IndexInfoCard()
        layout.addWidget(self.info)
        self.params_title = QLabel(tr("<b>Parameters</b>"))
        layout.addWidget(self.params_title)
        self.param_form = ParameterForm()
        self.param_form.changed.connect(self._store_focus_state)
        layout.addWidget(self.param_form)
        self.class_title = QLabel(tr("<b>Classification</b>"))
        layout.addWidget(self.class_title)
        self.class_editor = ClassificationEditor()
        self.class_editor.changed.connect(self._store_focus_state)
        layout.addWidget(self.class_editor)
        self._show_focus(None)
        return self.settings_group

    def _build_run_group(self) -> QGroupBox:
        group = QGroupBox(tr("4 · Run"))
        layout = QVBoxLayout(group)
        self.readiness = QLabel()
        self.readiness.setWordWrap(True)
        self.readiness.setTextFormat(Qt.RichText)
        layout.addWidget(self.readiness)

        buttons = QHBoxLayout()
        self.compute_btn = QPushButton(tr("Run"))
        self.compute_btn.setDefault(True)
        self.compute_btn.clicked.connect(self._on_compute)
        buttons.addWidget(self.compute_btn, 1)
        self.cancel_btn = QPushButton(tr("Cancel"))
        self.cancel_btn.setEnabled(False)
        self.cancel_btn.clicked.connect(self._on_cancel)
        buttons.addWidget(self.cancel_btn)
        layout.addLayout(buttons)

        self.progress = QProgressBar()
        self.progress.setRange(0, 100)
        self.progress.setTextVisible(False)
        self.progress.setFixedHeight(10)
        self.progress.hide()
        layout.addWidget(self.progress)
        self.progress_label = QLabel()
        self.progress_label.setStyleSheet("color: #52514e;")
        layout.addWidget(self.progress_label)

        results = QHBoxLayout()
        self.show_btn = QPushButton(tr("Show results"))
        self.show_btn.setEnabled(False)
        self.show_btn.clicked.connect(self._show_results)
        results.addWidget(self.show_btn)
        self.export_btn = QPushButton(tr("Export table…"))
        self.export_btn.setEnabled(False)
        self.export_btn.clicked.connect(self._export_table)
        results.addWidget(self.export_btn)
        layout.addLayout(results)
        return group

    # ------------------------------------------------------------------
    # Data inputs
    # ------------------------------------------------------------------

    def _connect_project(self) -> None:
        # A removed layer must not leave a dangling reference in the panel.
        QgsProject.instance().layersWillBeRemoved.connect(self._on_layers_removed)

    def _on_layers_removed(self, layer_ids) -> None:
        if self._result_layer is not None and self._result_layer.id() in layer_ids:
            self._result_layer = None
        self._refresh_readiness()

    def _on_basin_layer(self, layer) -> None:
        self.id_field.setLayer(layer)
        self.label_field.setLayer(layer)
        if layer is not None:
            # Preselect a sensible identifier: a field named like an id,
            # otherwise the first field.
            names = {f.name().lower(): f.name() for f in layer.fields()}
            preferred = ("basin_id", "hybas_id", "id", "name", "fid")
            guess = next((names[n] for n in preferred if n in names),
                         layer.fields()[0].name() if len(layer.fields()) else "")
            self.id_field.setField(guess)
        self._refresh_readiness()

    def _on_stream_layer(self, layer) -> None:
        self.order_field.setLayer(layer)
        self.order_field.setEnabled(layer is not None)
        self.threshold.setEnabled(layer is None)
        if layer is not None:
            names = [f.name() for f in layer.fields()]
            guess = next((n for n in names if "strahler" in n.lower() or n.lower() in ("ord_stra", "order")), "")
            self.order_field.setField(guess)
        self._refresh_readiness()

    # ------------------------------------------------------------------
    # Catalogue and index settings
    # ------------------------------------------------------------------

    def _on_selection_changed(self, keys: List[str]) -> None:
        if keys and (self._focus is None or self._focus not in keys):
            self.catalog.focus_key(keys[0])
        self._refresh_readiness()

    def _on_focus_changed(self, key: str) -> None:
        self._show_focus(key)

    def _show_focus(self, key: Optional[str]) -> None:
        self._focus = None  # suspend storing while widgets are rebuilt
        spec = get(key) if key else None
        self.info.show_spec(spec)
        self.settings_group.setTitle(tr("3 · Settings of {0}").format(spec.key) if spec
                                     else tr("3 · Index settings"))
        self.params_title.setVisible(spec is not None)
        self.param_form.setVisible(spec is not None)
        self.class_title.setVisible(spec is not None)
        self.class_editor.setVisible(spec is not None)
        if spec is not None:
            self.param_form.set_parameters(spec.parameters, self._params.get(key))
            self.class_editor.set_index(spec, self._schemes.get(key))
        self._focus = key
        self._store_focus_state()

    def _store_focus_state(self) -> None:
        if not self._focus:
            return
        self._params[self._focus] = self.param_form.values()
        self._schemes[self._focus] = self.class_editor.choice()
        self._refresh_readiness()

    def _selection_objects(self) -> List[IndexSelection]:
        selections = []
        for key in self.catalog.selected_keys():
            spec = get(key)
            state = self._schemes.get(key)
            if key not in self._schemes:
                scheme = spec.classification()              # never edited: published default
            elif state == ClassificationEditor.NONE_KEY:
                scheme = None
            else:
                scheme = state
            selections.append(IndexSelection(key, dict(self._params.get(key, {})), scheme))
        return selections

    def _parameter_issues(self) -> List[str]:
        issues = []
        for key in self.catalog.selected_keys():
            if key in self._schemes and self._schemes[key] is None:
                issues.append(tr("{0}: the edited classification is invalid (see the class table).")
                              .format(key))
        return issues

    # ------------------------------------------------------------------
    # Readiness
    # ------------------------------------------------------------------

    def _refresh_readiness(self, *args) -> None:
        if not hasattr(self, "readiness"):
            return
        running = self._task is not None
        issues = readiness_issues(
            self.dem_combo.currentLayer(), self.basin_combo.currentLayer(),
            self.id_field.currentField(), self.catalog.selected_keys(),
            self.selected_only.isChecked(), self.stream_combo.currentLayer(),
            self._parameter_issues())
        if issues:
            items = "".join(f"<li>{i}</li>" for i in issues)
            self.readiness.setText(f"<span style='color:#7a5a00'>⚠ {tr('Before running:')}</span>"
                                   f"<ul style='margin:2px 0 0 -20px'>{items}</ul>")
        else:
            layer = self.basin_combo.currentLayer()
            n = layer.selectedFeatureCount() if self.selected_only.isChecked() else layer.featureCount()
            self.readiness.setText(
                f"<span style='color:#0c7a0c'>✔ {tr('Ready')}</span>: "
                + tr("{0} basin(s) × {1} index(es).").format(n, len(self.catalog.selected_keys())))
        self.compute_btn.setEnabled(not issues and not running)

    # ------------------------------------------------------------------
    # Run
    # ------------------------------------------------------------------

    def _on_compute(self) -> None:
        self._refresh_readiness()
        if not self.compute_btn.isEnabled():
            return
        try:
            request = collect_request(
                self.dem_combo.currentLayer(), self.basin_combo.currentLayer(),
                self.id_field.currentField(), self.label_field.currentField(),
                self.selected_only.isChecked(), self._selection_objects(),
                self.stream_combo.currentLayer(), self.order_field.currentField(),
                self.threshold.value())
        except Exception as error:
            self.iface.messageBar().pushCritical("RockMorph", tr("Cannot read the inputs: {0}").format(error))
            return
        if not request.basins:
            self.iface.messageBar().pushWarning("RockMorph", tr("No basin geometry to process."))
            return
        self._task = MorphometryTask(request)
        self._task.message.connect(self._on_progress)
        self._task.completed.connect(self._on_completed)
        self._task.failed.connect(self._on_failed)
        self.progress.setValue(0)
        self.progress.show()
        self.progress_label.setText(tr("Starting…"))
        self.cancel_btn.setEnabled(True)
        self.compute_btn.setEnabled(False)
        QgsApplication.taskManager().addTask(self._task)

    def _on_cancel(self) -> None:
        if self._task is not None:
            self._task.cancel()
            self.progress_label.setText(tr("Cancelling after the current basin…"))

    def _on_progress(self, value: float, message: str) -> None:
        self.progress.setValue(int(value))
        self.progress_label.setText(message)

    def _finish_run(self) -> None:
        self._task = None
        self.progress.hide()
        self.cancel_btn.setEnabled(False)
        self._refresh_readiness()

    def _on_failed(self, message: str) -> None:
        self._finish_run()
        self.progress_label.setText(tr("Failed: {0}").format(message))
        self.iface.messageBar().pushCritical("RockMorph", tr("Morphometric indices: {0}").format(message))

    def _on_completed(self, result: MorphometryResult) -> None:
        self._finish_run()
        self._result = result
        source = result.source_name or tr("basins")
        layer = build_result_layer(result, tr("Indices — {0}").format(source))
        channels = build_channel_layer(result, tr("Trunk streams — {0}").format(source))
        self._style_result_layer(layer, result)
        project = QgsProject.instance()
        root = project.layerTreeRoot()
        group = root.findGroup(OUTPUT_GROUP) or root.insertGroup(0, OUTPUT_GROUP)
        project.addMapLayer(layer, False)
        group.insertLayer(0, layer)
        if channels is not None:
            project.addMapLayer(channels, False)
            group.insertLayer(0, channels)
        self._result_layer = layer
        with_warnings = sum(1 for r in result.rows if r.warnings)
        text = tr("{0} basin(s) computed in {1:.1f} s.").format(len(result.rows), result.elapsed_s)
        if with_warnings:
            text += " " + tr("{0} with remarks (see Diagnostics).").format(with_warnings)
        if result.cancelled:
            text += " " + tr("Cancelled: partial results.")
        self.progress_label.setText(text)
        self.show_btn.setEnabled(True)
        self.export_btn.setEnabled(True)
        self._show_results()

    # ------------------------------------------------------------------
    # Outputs
    # ------------------------------------------------------------------

    def _style_result_layer(self, layer, result: MorphometryResult) -> None:
        """
        Colour the basins by the class score of the first scored index
        (class 1 darkest). Other indices remain one click away in the layer
        styling panel: every value, class and score is a field.
        """
        scored = next((s for s in result.selections
                       if s.classification is not None and s.classification.is_scored), None)
        if scored is None:
            return
        field = f"{scored.key}_score"
        categories = []
        for rule in scored.classification.rules:
            symbol = QgsFillSymbol.createSimple({
                "color": CLASS_COLORS.get(rule.score, "#cccccc"), "outline_color": "#ffffff",
                "outline_width": "0.3"})
            categories.append(QgsRendererCategory(rule.score, symbol, f"{scored.key} — {rule.label}"))
        symbol = QgsFillSymbol.createSimple({"color": "#e6e5e0", "outline_color": "#ffffff"})
        categories.append(QgsRendererCategory(None, symbol, tr("{0} — no value").format(scored.key)))
        layer.setRenderer(QgsCategorizedSymbolRenderer(field, categories))
        layer.setOpacity(0.85)

    def _show_results(self) -> None:
        if self._result is None:
            return
        if self._window is None:
            self._window = ResultsWindow(tr("RockMorph — morphometric indices"), self.iface.mainWindow())
            self._window.basinsSelected.connect(self._select_on_map)
            self._window.exportTableRequested.connect(self._export_table)
        self._window.show_payload(result_payload(self._result, tr("Morphometric indices — {0}")
                                                 .format(self._result.source_name)))

    def _select_on_map(self, ids: List[str]) -> None:
        layer = self._result_layer
        if layer is None or not ids:
            return
        quoted = ",".join("'" + i.replace("'", "''") + "'" for i in ids)
        request = QgsFeatureRequest().setFilterExpression(f'"basin_id" IN ({quoted})')
        fids = [f.id() for f in layer.getFeatures(request)]
        layer.selectByIds(fids)
        if fids:
            canvas = self.iface.mapCanvas()
            canvas.zoomToSelected(layer)
            canvas.refresh()

    def _export_table(self) -> None:
        if self._result is None:
            return
        path, chosen = QFileDialog.getSaveFileName(
            self, tr("Export the results"), "morphometric_indices.csv",
            tr("CSV table (*.csv);;GeoPackage with geometries (*.gpkg)"))
        if not path:
            return
        if path.lower().endswith(".gpkg") or "gpkg" in chosen:
            if not path.lower().endswith(".gpkg"):
                path += ".gpkg"
            layer = self._result_layer or build_result_layer(self._result, "indices")
            options = QgsVectorFileWriter.SaveVectorOptions()
            options.driverName = "GPKG"
            options.layerName = "morphometric_indices"
            error, message, *_ = QgsVectorFileWriter.writeAsVectorFormatV3(
                layer, path, QgsCoordinateTransformContext(), options)
            if error != QgsVectorFileWriter.NoError:
                self.iface.messageBar().pushCritical("RockMorph", message)
                return
        else:
            if not path.lower().endswith(".csv"):
                path += ".csv"
            columns, rows = self._result.table()
            columns.append("warnings")
            with open(path, "w", newline="", encoding="utf-8-sig") as handle:
                writer = csv.writer(handle)
                writer.writerow(columns)
                for basin, row in zip(self._result.rows, rows):
                    writer.writerow(["" if v is None else v for v in row] + [" | ".join(basin.warnings)])
        self.iface.messageBar().pushSuccess("RockMorph", tr("Results saved: {0}").format(path))

    # ------------------------------------------------------------------
    # Life cycle
    # ------------------------------------------------------------------

    def cleanup(self) -> None:
        """Called by the dock when the plugin unloads."""
        try:
            QgsProject.instance().layersWillBeRemoved.disconnect(self._on_layers_removed)
        except TypeError:
            pass
        if self._task is not None:
            self._task.cancel()
        if self._window is not None:
            self._window.close()
            self._window.deleteLater()
            self._window = None
