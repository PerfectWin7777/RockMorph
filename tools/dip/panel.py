"""
tools/dip/panel.py — Three-Point Dip panel

Measure the orientation of a bed, a contact or a fault from three or more
points, then see where that plane should crop out.

Workflow
    1. Choose the DEM.
    2. Click points on the map ("Pick on map": left click adds a point,
       right click or Esc stops) or load the selected features of a point
       layer (elevation from a field, or from the DEM).
    3. Compute: dip / dip direction (e.g. 30/120), strike (right-hand
       rule), RMS and largest distance of the points to the plane.
    4. Outputs added to the project: the measurement as a point (layer
       "Dip measurements", one feature per computation, labelled 30/120)
       and the predicted outcrop trace (red dashed line), which can also be
       draped in the 3D Explorer.

Authors: RockMorph contributors / Tony
"""

from __future__ import annotations

from typing import List, Optional, Tuple

from qgis.PyQt.QtCore import Qt, QCoreApplication  # type: ignore
from qgis.PyQt.QtGui import QColor  # type: ignore
from qgis.PyQt.QtWidgets import (  # type: ignore
    QCheckBox, QDoubleSpinBox, QFormLayout, QHBoxLayout, QHeaderView, QLabel, QPushButton,
    QTableWidget, QTableWidgetItem, QVBoxLayout,
)
from qgis.core import (  # type: ignore
    QgsCoordinateTransform, QgsFeature, QgsField, QgsFieldProxyModel, QgsGeometry,
    QgsMapLayerProxyModel, QgsPalLayerSettings, QgsPointXY, QgsProject, QgsTextFormat,
    QgsVectorLayer, QgsVectorLayerSimpleLabeling, QgsWkbTypes, QgsLineSymbol, QgsMarkerSymbol,
)
from qgis.gui import (  # type: ignore
    QgsCollapsibleGroupBox, QgsFieldComboBox, QgsMapLayerComboBox, QgsMapToolEmitPoint, QgsRubberBand,
)
from qgis.PyQt.QtCore import QVariant  # type: ignore

from ...base.base_panel import BasePanel, ComputeWorker
from ...core.raster import RasterReader
from .engine import DipEngine


def tr(message: str) -> str:
    return QCoreApplication.translate("RockMorph", message)


MEASUREMENTS_LAYER_NAME = "Dip measurements"


class DipPanel(BasePanel):
    """Three-point problem on a DEM, with predicted outcrop trace."""

    def _html_file(self) -> str:
        return "empty.html"

    def _load_html(self) -> None:
        pass

    # ── UI ─────────────────────────────────────────────────────────────

    def _build_ui(self) -> None:
        self.engine = DipEngine()
        self._points: List[Tuple[float, float, Optional[float]]] = []   # DEM CRS
        self._map_tool: Optional[QgsMapToolEmitPoint] = None
        self._previous_tool = None
        self._markers: Optional[QgsRubberBand] = None
        self._measurements_layer_id: Optional[str] = None

        root = QVBoxLayout(self._inner)
        root.setContentsMargins(8, 8, 8, 8)
        root.setSpacing(10)

        intro = QLabel(tr("Orientation of a bed, contact or fault from ≥ 3 points (three-point problem), "
                          "and where that plane should crop out."))
        intro.setWordWrap(True)
        intro.setStyleSheet("color: #555;")
        root.addWidget(intro)

        data = QgsCollapsibleGroupBox(tr("Data"))
        form = QFormLayout(data)
        self.dem_combo = QgsMapLayerComboBox()
        self.dem_combo.setFilters(QgsMapLayerProxyModel.RasterLayer)
        form.addRow(tr("DEM:"), self.dem_combo)
        root.addWidget(data)

        pts = QgsCollapsibleGroupBox(tr("Points"))
        pts_layout = QVBoxLayout(pts)
        buttons = QHBoxLayout()
        self.btn_pick = QPushButton(tr("✚ Pick on map"))
        self.btn_pick.setCheckable(True)
        self.btn_pick.setToolTip(tr("Left click adds a point (elevation read on the DEM); "
                                    "right click or Esc stops picking."))
        self.btn_undo = QPushButton(tr("Remove last"))
        self.btn_clear = QPushButton(tr("Clear"))
        buttons.addWidget(self.btn_pick)
        buttons.addWidget(self.btn_undo)
        buttons.addWidget(self.btn_clear)
        pts_layout.addLayout(buttons)

        self.table = QTableWidget(0, 4)
        self.table.setHorizontalHeaderLabels([tr("X"), tr("Y"), tr("Z (m)"), tr("Residual (m)")])
        self.table.horizontalHeader().setSectionResizeMode(QHeaderView.Stretch)
        self.table.setMinimumHeight(130)
        self.table.setToolTip(tr("Z can be edited (e.g. a borehole depth converted to elevation); "
                                 "an empty Z is read on the DEM."))
        pts_layout.addWidget(self.table)

        load_row = QFormLayout()
        self.point_layer_combo = QgsMapLayerComboBox()
        self.point_layer_combo.setFilters(QgsMapLayerProxyModel.PointLayer)
        self.point_layer_combo.setAllowEmptyLayer(True)
        self.point_layer_combo.setLayer(None)
        load_row.addRow(tr("Point layer:"), self.point_layer_combo)
        self.z_field_combo = QgsFieldComboBox()
        self.z_field_combo.setFilters(QgsFieldProxyModel.Numeric)
        self.z_field_combo.setAllowEmptyFieldName(True)
        load_row.addRow(tr("Z field (empty = DEM):"), self.z_field_combo)
        self.btn_load = QPushButton(tr("Load selected features"))
        load_row.addRow(self.btn_load)
        pts_layout.addLayout(load_row)
        root.addWidget(pts)

        opts = QgsCollapsibleGroupBox(tr("Outcrop prediction"))
        opts_form = QFormLayout(opts)
        self.chk_outcrop = QCheckBox(tr("Trace where the plane meets the topography"))
        self.chk_outcrop.setChecked(True)
        opts_form.addRow(self.chk_outcrop)
        self.spin_radius = QDoubleSpinBox()
        self.spin_radius.setRange(0.0, 500.0)
        self.spin_radius.setValue(5.0)
        self.spin_radius.setSuffix(" km")
        self.spin_radius.setSpecialValueText(tr("whole DEM"))
        self.spin_radius.setToolTip(tr("Search the trace within this distance of the points (0 = whole DEM)."))
        opts_form.addRow(tr("Search radius:"), self.spin_radius)
        root.addWidget(opts)

        self.btn_compute = QPushButton(tr("⚙  Compute plane"))
        self.btn_compute.setFixedHeight(34)
        self.btn_compute.setStyleSheet(
            "QPushButton { background-color: #6d4c41; color: white; font-weight: bold; border-radius: 4px; }"
            "QPushButton:disabled { background-color: #aaa; }")
        root.addWidget(self.btn_compute)
        root.addWidget(self._progress_container)

        results = QgsCollapsibleGroupBox(tr("Result"))
        res_form = QFormLayout(results)
        self.lbl_notation = QLabel("—")
        self.lbl_notation.setStyleSheet("font-size: 15px; font-weight: bold; color: #6d4c41;")
        self.lbl_strike = QLabel("—")
        self.lbl_fit = QLabel("—")
        self.lbl_outcrop = QLabel("—")
        self.lbl_outcrop.setWordWrap(True)
        res_form.addRow(tr("Dip / dip direction:"), self.lbl_notation)
        res_form.addRow(tr("Strike (right-hand rule):"), self.lbl_strike)
        res_form.addRow(tr("Fit:"), self.lbl_fit)
        res_form.addRow(tr("Outcrop:"), self.lbl_outcrop)
        root.addWidget(results)
        root.addStretch()

        self.btn_pick.toggled.connect(self._on_pick_toggled)
        self.btn_undo.clicked.connect(self._on_undo)
        self.btn_clear.clicked.connect(self._on_clear)
        self.btn_load.clicked.connect(self._on_load_selected)
        self.point_layer_combo.layerChanged.connect(self.z_field_combo.setLayer)
        self.btn_compute.clicked.connect(self._on_compute)
        self.table.itemChanged.connect(self._on_table_edited)

    # ── Points ─────────────────────────────────────────────────────────

    def _dem(self):
        layer = self.dem_combo.currentLayer()
        return layer if layer is not None and layer.isValid() else None

    def _on_pick_toggled(self, checked: bool) -> None:
        canvas = self.iface.mapCanvas()
        if checked:
            if self._dem() is None:
                self.show_error(tr("Choose the DEM first."))
                self.btn_pick.setChecked(False)
                return
            self._previous_tool = canvas.mapTool()
            self._map_tool = QgsMapToolEmitPoint(canvas)
            self._map_tool.canvasClicked.connect(self._on_canvas_clicked)
            self._map_tool.deactivated.connect(lambda: self.btn_pick.setChecked(False))
            canvas.setMapTool(self._map_tool)
        elif self._map_tool is not None and canvas.mapTool() is self._map_tool:
            canvas.unsetMapTool(self._map_tool)
            if self._previous_tool is not None:
                canvas.setMapTool(self._previous_tool)

    def _on_canvas_clicked(self, point: QgsPointXY, button) -> None:
        if button == Qt.RightButton:
            self.btn_pick.setChecked(False)
            return
        dem = self._dem()
        if dem is None:
            return
        canvas_crs = self.iface.mapCanvas().mapSettings().destinationCrs()
        if canvas_crs != dem.crs():
            point = QgsCoordinateTransform(canvas_crs, dem.crs(), QgsProject.instance()).transform(point)
        self._add_point(point.x(), point.y(), None)

    def _add_point(self, x: float, y: float, z: Optional[float]) -> None:
        dem = self._dem()
        shown_z = z
        if z is None and dem is not None:
            sampled = RasterReader(dem).sample_at(x, y)
            shown_z = sampled if sampled == sampled else None
        self._points.append((x, y, z))
        self.table.blockSignals(True)
        row = self.table.rowCount()
        self.table.insertRow(row)
        for column, value in enumerate((x, y)):
            item = QTableWidgetItem(f"{value:.6f}" if dem is not None and dem.crs().isGeographic() else f"{value:.1f}")
            item.setFlags(item.flags() & ~Qt.ItemIsEditable)
            self.table.setItem(row, column, item)
        self.table.setItem(row, 2, QTableWidgetItem("" if shown_z is None else f"{shown_z:.1f}"))
        residual = QTableWidgetItem("")
        residual.setFlags(residual.flags() & ~Qt.ItemIsEditable)
        self.table.setItem(row, 3, residual)
        self.table.blockSignals(False)
        self._redraw_markers()

    def _on_table_edited(self, item) -> None:
        """An edited Z overrides the DEM value; an emptied one goes back to the DEM."""
        if item.column() != 2 or item.row() >= len(self._points):
            return
        x, y, _ = self._points[item.row()]
        text = item.text().strip().replace(",", ".")
        try:
            z = float(text) if text else None
        except ValueError:
            z = None
        self._points[item.row()] = (x, y, z)

    def _on_undo(self) -> None:
        if self._points:
            self._points.pop()
            self.table.removeRow(self.table.rowCount() - 1)
            self._redraw_markers()

    def _on_clear(self) -> None:
        self._points.clear()
        self.table.setRowCount(0)
        self._redraw_markers()

    def _on_load_selected(self) -> None:
        layer = self.point_layer_combo.currentLayer()
        dem = self._dem()
        if layer is None or dem is None:
            self.show_error(tr("Choose the DEM and a point layer."))
            return
        features = list(layer.getSelectedFeatures()) or list(layer.getFeatures())
        if len(features) > 200:
            self.show_error(tr("Select the points to use (at most 200)."))
            return
        transform = QgsCoordinateTransform(layer.crs(), dem.crs(), QgsProject.instance())
        field = self.z_field_combo.currentField()
        for feature in features:
            geometry = feature.geometry()
            if geometry.isEmpty():
                continue
            point = transform.transform(geometry.asPoint() if not geometry.isMultipart()
                                        else geometry.asMultiPoint()[0])
            z = None
            if field:
                value = feature[field]
                z = float(value) if isinstance(value, (int, float)) else None
            self._add_point(point.x(), point.y(), z)

    def _redraw_markers(self) -> None:
        canvas = self.iface.mapCanvas()
        if self._markers is None:
            self._markers = QgsRubberBand(canvas, QgsWkbTypes.PointGeometry)
            self._markers.setColor(QColor(109, 76, 65))
            self._markers.setIconSize(10)
            self._markers.setWidth(2)
        self._markers.reset(QgsWkbTypes.PointGeometry)
        dem = self._dem()
        if dem is None:
            return
        canvas_crs = canvas.mapSettings().destinationCrs()
        transform = QgsCoordinateTransform(dem.crs(), canvas_crs, QgsProject.instance())
        for x, y, _ in self._points:
            self._markers.addPoint(transform.transform(QgsPointXY(x, y)))
        self._markers.show()

    # ── Compute ────────────────────────────────────────────────────────

    def _on_compute(self) -> None:
        dem = self._dem()
        if dem is None or len(self._points) < 3:
            self.show_error(tr("Choose the DEM and give at least three points."))
            return
        params = {
            "dem_layer": dem,
            "points": list(self._points),
            "predict_outcrop": self.chk_outcrop.isChecked(),
            "radius_m": self.spin_radius.value() * 1000.0,
        }
        self.btn_compute.setEnabled(False)
        self.set_loading_state(True, tr("Fitting the plane..."), total=100)
        self._worker = ComputeWorker(self.engine, params)
        self._worker.progress.connect(self.update_progress)
        self._worker.finished.connect(self._on_compute_finished)
        self._worker.error.connect(self._on_compute_error)
        self._worker.start()

    def _on_compute_error(self, message: str) -> None:
        self.btn_compute.setEnabled(True)
        self.set_loading_state(False)
        self.show_error(message)

    def _on_compute_finished(self, result: dict) -> None:
        self.btn_compute.setEnabled(True)
        self.set_loading_state(False)
        self._last_data = {k: v for k, v in result.items() if k != "outcrop_layer"}
        self.lbl_notation.setText(result["notation"])
        self.lbl_strike.setText(f"{result['strike']:03.0f}°  (dip {result['dip']:.1f}° towards "
                                f"{result['dip_direction']:.1f}°)")
        self.lbl_fit.setText(tr("RMS {:.2f} m, largest {:.2f} m ({} points)").format(
            result["rms_m"], result["max_residual_m"], len(result["points"])))
        self.table.blockSignals(True)
        for row, (_x, _y, z, residual) in enumerate(result["points"]):
            if row < self.table.rowCount():
                self.table.item(row, 2).setText(f"{z:.1f}")
                self.table.item(row, 3).setText(f"{residual:+.2f}")
        self.table.blockSignals(False)

        dem = self._dem()
        self._add_measurement(result, dem)
        outcrop = result.get("outcrop_layer")
        if outcrop is not None and outcrop.isValid():
            symbol = QgsLineSymbol.createSimple({"color": "214,39,40", "width": "0.7", "line_style": "dash"})
            outcrop.renderer().setSymbol(symbol)
            QgsProject.instance().addMapLayer(outcrop)
            total = sum(f["length_m"] or 0 for f in outcrop.getFeatures())
            self.lbl_outcrop.setText(tr("{} trace(s), {:.1f} km — layer \"{}\".").format(
                outcrop.featureCount(), total / 1000.0, outcrop.name()))
        else:
            self.lbl_outcrop.setText(" ".join(result.get("warnings", [])) or "—")

    def _add_measurement(self, result: dict, dem) -> None:
        """Appends the measurement to the 'Dip measurements' point layer (created once)."""
        project = QgsProject.instance()
        layer = project.mapLayer(self._measurements_layer_id) if self._measurements_layer_id else None
        if layer is None:
            layer = QgsVectorLayer("Point", tr(MEASUREMENTS_LAYER_NAME), "memory")
            layer.setCrs(dem.crs())
            layer.dataProvider().addAttributes([
                QgsField("notation", QVariant.String), QgsField("dip", QVariant.Double),
                QgsField("dip_dir", QVariant.Double), QgsField("strike", QVariant.Double),
                QgsField("rms_m", QVariant.Double), QgsField("n_points", QVariant.Int)])
            layer.updateFields()
            layer.renderer().setSymbol(QgsMarkerSymbol.createSimple(
                {"name": "triangle", "color": "109,76,65", "size": "3.2", "angle": "0"}))
            labels = QgsPalLayerSettings()
            labels.fieldName = "notation"
            text = QgsTextFormat()
            text.setSize(9)
            labels.setFormat(text)
            layer.setLabeling(QgsVectorLayerSimpleLabeling(labels))
            layer.setLabelsEnabled(True)
            project.addMapLayer(layer)
            self._measurements_layer_id = layer.id()
        feature = QgsFeature(layer.fields())
        feature.setGeometry(QgsGeometry.fromPointXY(QgsPointXY(*result["centroid"])))
        feature.setAttributes([result["notation"], round(result["dip"], 2), round(result["dip_direction"], 2),
                               round(result["strike"], 2), round(result["rms_m"], 3), len(result["points"])])
        layer.dataProvider().addFeatures([feature])
        layer.updateExtents()
        layer.triggerRepaint()

    def _on_result(self, data: dict) -> None:
        pass

    def cleanup(self) -> None:
        canvas = self.iface.mapCanvas()
        if self._map_tool is not None and canvas.mapTool() is self._map_tool:
            canvas.unsetMapTool(self._map_tool)
        if self._markers is not None:
            canvas.scene().removeItem(self._markers)
            self._markers = None
