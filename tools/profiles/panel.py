"""
tools/profiles/panel.py

Sidebar panel of the Topographic Profiles tool.

Workflow
--------
1. Data       – a DEM, then the lines: a line layer (all or selected
                features, optional label field) and/or lines DRAWN on the
                map with the "Draw on map" tool (left click = vertex,
                right click = finish, Esc = cancel).
2. Profiles   – "Along the lines" or "Serial cross-sections" (perpendicular
                to each line, every N metres, L metres long); sampling step
                (0 = DEM cell size).
3. Crossings  – optional line/polygon layer whose crossings (faults,
                contacts, rivers…) are marked on the profiles.
4. Display    – vertical exaggeration (0 = fit), stacked sections, map
                hover link.

Hover works both ways:
* chart → map: hovering a profile (2D or 3D) moves a red point on the map
  and highlights that profile line;
* map → chart: moving the mouse near a profile line on the map moves the
  cursor on the chart (within 8 screen pixels).

Outputs: PNG/SVG/PDF of the chart (PNG for the 3D view), CSV of every
sampled point, and a "profile lines" layer with the statistics.

Authors: RockMorph contributors / Tony
"""

from __future__ import annotations

import csv
import json
from typing import List, Optional

from qgis.PyQt.QtCore import QCoreApplication, Qt, QVariant  # type: ignore
from qgis.PyQt.QtGui import QColor  # type: ignore
from qgis.PyQt.QtWidgets import (  # type: ignore
    QCheckBox, QComboBox, QDoubleSpinBox, QFileDialog, QFormLayout, QHBoxLayout,
    QLabel, QPushButton, QSizePolicy, QSpinBox, QVBoxLayout,
)
from qgis.core import (  # type: ignore
    QgsCoordinateReferenceSystem, QgsCoordinateTransform, QgsFeature, QgsField,
    QgsGeometry, QgsMapLayerProxyModel, QgsPointXY, QgsProject, QgsVectorLayer,
    QgsWkbTypes,
)
from qgis.gui import (  # type: ignore
    QgsCollapsibleGroupBox, QgsFieldComboBox, QgsMapLayerComboBox, QgsMapTool,
    QgsRubberBand,
)

from ...base.base_panel import BasePanel, ComputeWorker
from ...widgets.export_group import RockMorphExportGroup
from .engine import MODE_LINES, MODE_SECTIONS, ProfilesEngine


def tr(message: str) -> str:
    return QCoreApplication.translate("RockMorph", message)


HOVER_TOLERANCE_PX = 8
LINE_COLOR = QColor(42, 120, 214, 170)
HIGHLIGHT_COLOR = QColor(208, 59, 59)


# ---------------------------------------------------------------------------
# Map tool: draw a profile line
# ---------------------------------------------------------------------------

class ProfileDrawTool(QgsMapTool):
    """
    Click vertices on the map; right click (or double click) finishes the
    line, Esc cancels it. ``on_finished(QgsGeometry, crs)`` receives the
    line in the canvas CRS.
    """

    def __init__(self, canvas, on_finished):
        super().__init__(canvas)
        self._on_finished = on_finished
        self._points: List[QgsPointXY] = []
        self._band = QgsRubberBand(canvas, QgsWkbTypes.LineGeometry)
        self._band.setColor(HIGHLIGHT_COLOR)
        self._band.setWidth(2)
        self.setCursor(Qt.CrossCursor)

    def canvasPressEvent(self, event):
        if event.button() == Qt.RightButton:
            self._finish()
            return
        self._points.append(self.toMapCoordinates(event.pos()))
        self._redraw()

    def canvasDoubleClickEvent(self, event):
        self._finish()

    def canvasMoveEvent(self, event):
        if self._points:
            self._redraw(self.toMapCoordinates(event.pos()))

    def keyPressEvent(self, event):
        if event.key() == Qt.Key_Escape:
            self._reset()

    def deactivate(self):
        self._reset()
        super().deactivate()

    def _redraw(self, floating: Optional[QgsPointXY] = None):
        points = self._points + ([floating] if floating is not None else [])
        self._band.setToGeometry(QgsGeometry.fromPolylineXY(points), None)

    def _finish(self):
        if len(self._points) >= 2:
            geometry = QgsGeometry.fromPolylineXY(self._points)
            self._on_finished(geometry, self.canvas().mapSettings().destinationCrs())
        self._reset()

    def _reset(self):
        self._points = []
        self._band.reset(QgsWkbTypes.LineGeometry)


# ---------------------------------------------------------------------------
# Panel
# ---------------------------------------------------------------------------

class ProfilesPanel(BasePanel):
    """Topographic Profiles tool (dock panel with embedded chart)."""

    def __init__(self, iface, parent=None):
        self.iface = iface
        self._engine = ProfilesEngine()
        self._drawn: List[QgsGeometry] = []          # in self._drawn_crs
        self._drawn_crs: Optional[QgsCoordinateReferenceSystem] = None
        self._drawn_bands: List[QgsRubberBand] = []
        self._draw_tool: Optional[ProfileDrawTool] = None
        self._previous_tool = None
        self._result: Optional[dict] = None
        self._work_geoms: List[QgsGeometry] = []     # profile lines in the work CRS
        self._work_crs: Optional[QgsCoordinateReferenceSystem] = None
        self._line_bands: List[QgsRubberBand] = []
        self._highlight: Optional[QgsRubberBand] = None
        self._marker: Optional[QgsRubberBand] = None
        self._map_hover_index: Optional[int] = None
        self._running = False
        super().__init__(iface, parent)
        canvas = iface.mapCanvas()
        self._marker = QgsRubberBand(canvas, QgsWkbTypes.PointGeometry)
        self._marker.setIcon(QgsRubberBand.ICON_CIRCLE)
        self._marker.setIconSize(10)
        self._marker.setColor(HIGHLIGHT_COLOR)
        self._marker.setFillColor(QColor(255, 255, 255))
        self._marker.setWidth(3)
        self._marker.hide()
        self._highlight = QgsRubberBand(canvas, QgsWkbTypes.LineGeometry)
        self._highlight.setColor(HIGHLIGHT_COLOR)
        self._highlight.setWidth(3)
        canvas.xyCoordinates.connect(self._on_map_mouse_move)

    # -- BasePanel hooks ------------------------------------------------------

    def _html_file(self) -> str:
        return "profiles.html"

    def _build_ui(self) -> None:
        root = QVBoxLayout(self._inner)
        root.setContentsMargins(8, 8, 8, 8)
        root.setSpacing(6)

        # 1 · Data
        data_group = QgsCollapsibleGroupBox(tr("1 · Data"))
        form = QFormLayout(data_group)
        self.dem_combo = QgsMapLayerComboBox()
        self.dem_combo.setFilters(QgsMapLayerProxyModel.RasterLayer)
        self.dem_combo.setToolTip(tr("Elevation raster (any CRS; distances are always in metres)."))
        form.addRow(tr("DEM:"), self.dem_combo)
        self.line_combo = QgsMapLayerComboBox()
        self.line_combo.setFilters(QgsMapLayerProxyModel.LineLayer)
        self.line_combo.setAllowEmptyLayer(True, tr("None (draw on the map)"))
        self.line_combo.setToolTip(tr("Lines to profile, or baselines of the cross-sections."))
        form.addRow(tr("Line layer:"), self.line_combo)
        self.label_field = QgsFieldComboBox()
        self.label_field.setAllowEmptyFieldName(True)
        form.addRow(tr("Label field:"), self.label_field)
        self.selected_only = QCheckBox(tr("Selected features only"))
        form.addRow("", self.selected_only)
        draw_row = QHBoxLayout()
        self.draw_btn = QPushButton(tr("✎ Draw on map"))
        self.draw_btn.setCheckable(True)
        self.draw_btn.setToolTip(tr("Left click: add a vertex · right click or double click: finish · "
                                    "Esc: cancel the line being drawn."))
        draw_row.addWidget(self.draw_btn)
        self.clear_drawn_btn = QPushButton(tr("Clear drawn"))
        draw_row.addWidget(self.clear_drawn_btn)
        form.addRow(draw_row)
        self.drawn_label = QLabel(tr("No line drawn."))
        self.drawn_label.setStyleSheet("color: #52514e;")
        form.addRow(self.drawn_label)
        root.addWidget(data_group)

        # 2 · Profiles
        prof_group = QgsCollapsibleGroupBox(tr("2 · Profiles"))
        form = QFormLayout(prof_group)
        self.mode_combo = QComboBox()
        self.mode_combo.addItem(tr("Along the lines"), MODE_LINES)
        self.mode_combo.addItem(tr("Serial cross-sections"), MODE_SECTIONS)
        self.mode_combo.setToolTip(tr("Serial cross-sections are perpendicular to each line (valley axis, "
                                      "fault, river), centred on it, numbered from its start."))
        form.addRow(tr("Mode:"), self.mode_combo)
        self.spacing_spin = self._metres_spin(10, 100000, 1000, tr("Distance between two cross-sections."))
        form.addRow(tr("Section spacing:"), self.spacing_spin)
        self.length_spin = self._metres_spin(20, 200000, 4000, tr("Total length of each cross-section."))
        form.addRow(tr("Section length:"), self.length_spin)
        self.max_sections = QSpinBox()
        self.max_sections.setRange(2, 500)
        self.max_sections.setValue(60)
        self.max_sections.setToolTip(tr("Upper limit; the spacing is widened if the line is longer."))
        form.addRow(tr("Max sections per line:"), self.max_sections)
        self.step_spin = self._metres_spin(0, 10000, 0, tr("Distance between samples along a profile; "
                                                           "0 = the DEM cell size."))
        self.step_spin.setSpecialValueText(tr("DEM cell size"))
        form.addRow(tr("Sampling step:"), self.step_spin)
        root.addWidget(prof_group)

        # 3 · Crossings
        cross_group = QgsCollapsibleGroupBox(tr("3 · Crossings (optional)"))
        cross_group.setCollapsed(True)
        form = QFormLayout(cross_group)
        self.cross_combo = QgsMapLayerComboBox()
        self.cross_combo.setFilters(QgsMapLayerProxyModel.LineLayer | QgsMapLayerProxyModel.PolygonLayer)
        self.cross_combo.setAllowEmptyLayer(True, tr("None"))
        self.cross_combo.setToolTip(tr("Faults, geological contacts, rivers… Their crossings are marked "
                                       "on the profiles (polygons: their boundaries)."))
        form.addRow(tr("Layer:"), self.cross_combo)
        self.cross_field = QgsFieldComboBox()
        self.cross_field.setAllowEmptyFieldName(True)
        form.addRow(tr("Label field:"), self.cross_field)
        root.addWidget(cross_group)

        # Readiness + compute
        self.readiness = QLabel()
        self.readiness.setWordWrap(True)
        root.addWidget(self.readiness)
        self.compute_btn = QPushButton(tr("Compute profiles"))
        self.compute_btn.clicked.connect(self._on_compute)
        root.addWidget(self.compute_btn)
        root.addWidget(self._progress_container)

        # 4 · Display
        disp = QHBoxLayout()
        disp.addWidget(QLabel(tr("V.E.:")))
        self.ve_spin = QDoubleSpinBox()
        self.ve_spin.setRange(0.0, 50.0)
        self.ve_spin.setSingleStep(0.5)
        self.ve_spin.setValue(0.0)
        self.ve_spin.setSpecialValueText(tr("fit"))
        self.ve_spin.setToolTip(tr("Vertical exaggeration of the chart (1 = true scale, 0 = fit the panel). "
                                   "The 3D view uses it too (2 when 'fit')."))
        disp.addWidget(self.ve_spin)
        self.stack_check = QCheckBox(tr("Stack sections"))
        self.stack_check.setToolTip(tr("Offset the profiles vertically so serial cross-sections do not overlap."))
        disp.addWidget(self.stack_check)
        self.link_check = QCheckBox(tr("Map link"))
        self.link_check.setChecked(True)
        self.link_check.setToolTip(tr("Hover the chart to follow the profile on the map, and the map to "
                                      "follow it on the chart."))
        disp.addWidget(self.link_check)
        disp.addStretch()
        root.addLayout(disp)

        self.webview.setMinimumHeight(420)
        self.webview.setSizePolicy(QSizePolicy.Expanding, QSizePolicy.Expanding)
        root.addWidget(self.webview, 1)

        outputs = QHBoxLayout()
        self.layer_btn = QPushButton(tr("Add profile lines to map"))
        self.layer_btn.setEnabled(False)
        outputs.addWidget(self.layer_btn)
        root.addLayout(outputs)
        self.export_widget = RockMorphExportGroup(formats=["png", "svg", "pdf", "csv"], parent=self)
        self.export_widget.exportRequested.connect(self._on_export)
        root.addWidget(self.export_widget)

        # Signals
        self.line_combo.layerChanged.connect(self._on_line_layer)
        self.cross_combo.layerChanged.connect(self.cross_field.setLayer)
        self.dem_combo.layerChanged.connect(self._refresh)
        self.mode_combo.currentIndexChanged.connect(self._on_mode)
        self.selected_only.toggled.connect(self._refresh)
        self.draw_btn.toggled.connect(self._toggle_draw_tool)
        self.clear_drawn_btn.clicked.connect(self._clear_drawn)
        self.ve_spin.valueChanged.connect(self._push_display_options)
        self.stack_check.toggled.connect(self._push_display_options)
        self.layer_btn.clicked.connect(self._add_lines_layer)
        self.line_combo.setLayer(None)
        self.cross_combo.setLayer(None)
        self._on_line_layer(None)
        self._on_mode()

    @staticmethod
    def _metres_spin(minimum, maximum, value, tip) -> QDoubleSpinBox:
        spin = QDoubleSpinBox()
        spin.setRange(minimum, maximum)
        spin.setDecimals(0)
        spin.setSingleStep(max(1, value / 10 or 10))
        spin.setValue(value)
        spin.setSuffix(" m")
        spin.setToolTip(tip)
        return spin

    # -- inputs ---------------------------------------------------------------

    def _on_line_layer(self, layer) -> None:
        self.label_field.setLayer(layer)
        self.selected_only.setEnabled(layer is not None)
        self._refresh()

    def _on_mode(self, *args) -> None:
        sections = self.mode_combo.currentData() == MODE_SECTIONS
        for widget in (self.spacing_spin, self.length_spin, self.max_sections):
            widget.setEnabled(sections)
        self.stack_check.setChecked(sections)
        self._refresh()

    def _issues(self) -> List[str]:
        issues = []
        if self.dem_combo.currentLayer() is None:
            issues.append(tr("Choose a DEM."))
        layer = self.line_combo.currentLayer()
        count = 0
        if layer is not None:
            count = layer.selectedFeatureCount() if self.selected_only.isChecked() else layer.featureCount()
            if count == 0:
                issues.append(tr("No feature selected in the line layer.") if self.selected_only.isChecked()
                              else tr("The line layer is empty."))
        if layer is None and not self._drawn:
            issues.append(tr("Choose a line layer or draw a line on the map."))
        if self.mode_combo.currentData() == MODE_SECTIONS and self.spacing_spin.value() <= 0:
            issues.append(tr("Set the spacing of the cross-sections."))
        return issues

    def _refresh(self, *args) -> None:
        if not hasattr(self, "readiness"):
            return
        issues = self._issues()
        if issues:
            self.readiness.setText("<span style='color:#7a5a00'>⚠ " + "<br>⚠ ".join(issues) + "</span>")
        else:
            self.readiness.setText("<span style='color:#0c7a0c'>✔ " + tr("Ready") + "</span>")
        # A flag rather than QThread.isRunning(): the worker emits its result
        # just BEFORE its thread ends, which left the button disabled.
        self.compute_btn.setEnabled(not issues and not self._running)

    # -- drawing on the map -----------------------------------------------------

    def _toggle_draw_tool(self, on: bool) -> None:
        canvas = self.iface.mapCanvas()
        if on:
            self._previous_tool = canvas.mapTool()
            self._draw_tool = ProfileDrawTool(canvas, self._on_line_drawn)
            self._draw_tool.deactivated.connect(lambda: self.draw_btn.setChecked(False))
            canvas.setMapTool(self._draw_tool)
        elif self._draw_tool is not None and canvas.mapTool() is self._draw_tool:
            canvas.unsetMapTool(self._draw_tool)
            if self._previous_tool is not None:
                canvas.setMapTool(self._previous_tool)

    def _on_line_drawn(self, geometry: QgsGeometry, crs: QgsCoordinateReferenceSystem) -> None:
        if self._drawn_crs is not None and crs != self._drawn_crs:
            geometry.transform(QgsCoordinateTransform(crs, self._drawn_crs, QgsProject.instance()))
        elif self._drawn_crs is None:
            self._drawn_crs = crs
        self._drawn.append(geometry)
        band = QgsRubberBand(self.iface.mapCanvas(), QgsWkbTypes.LineGeometry)
        band.setColor(QColor(235, 104, 52))
        band.setWidth(2)
        band.setToGeometry(geometry, self._drawn_crs)
        self._drawn_bands.append(band)
        self.drawn_label.setText(tr("{0} line(s) drawn — right click finishes a line.").format(len(self._drawn)))
        self._refresh()

    def _clear_drawn(self) -> None:
        for band in self._drawn_bands:
            band.reset(QgsWkbTypes.LineGeometry)
        self._drawn_bands, self._drawn, self._drawn_crs = [], [], None
        self.drawn_label.setText(tr("No line drawn."))
        self._refresh()

    # -- compute ------------------------------------------------------------------

    def _on_compute(self) -> None:
        self._refresh()
        if not self.compute_btn.isEnabled():
            return
        params = dict(
            dem_layer=self.dem_combo.currentLayer(),
            line_layer=self.line_combo.currentLayer(),
            selected_only=self.selected_only.isChecked(),
            label_field=self.label_field.currentField(),
            drawn_wkt=[g.asWkt() for g in self._drawn],
            drawn_crs=self._drawn_crs.toWkt() if self._drawn_crs else "",
            mode=self.mode_combo.currentData(),
            step_m=self.step_spin.value(),
            spacing_m=self.spacing_spin.value(),
            section_length_m=self.length_spin.value(),
            max_sections=self.max_sections.value(),
            crossing_layer=self.cross_combo.currentLayer(),
            crossing_label_field=self.cross_field.currentField(),
            surface=True,
        )
        self._worker = ComputeWorker(self._engine, params)
        self._worker.progress.connect(self.update_progress)
        self._worker.finished.connect(self._on_result)
        self._worker.error.connect(self._on_error)
        self.set_loading_state(True, tr("Sampling profiles…"), 100)
        self._running = True
        self._worker.start()

    def _on_error(self, message: str) -> None:
        self._running = False
        self.set_loading_state(False)
        self.show_error(tr("Profiles: {0}").format(message))
        self._refresh()

    def _on_result(self, data: dict) -> None:
        self._running = False
        self.set_loading_state(False)
        self._on_plot_leave()             # markers of the previous result
        self._result = data
        self._last_data = data
        self._work_crs = QgsCoordinateReferenceSystem.fromWkt(data["work_crs"])
        self._work_geoms = [QgsGeometry.fromWkt(p["wkt"]) for p in data["profiles"]]
        self._draw_profile_lines()
        payload = {"profiles": [{k: v for k, v in p.items() if k != "wkt"} for p in data["profiles"]],
                   "surface": data["surface"], "mode": data["mode"]}
        self.webview.page().runJavaScript(f"renderProfiles({json.dumps(payload)});")
        self._push_display_options()
        self.layer_btn.setEnabled(True)
        for warning in data.get("warnings", [])[:3]:
            self.show_info(warning)
        self._refresh()

    def _push_display_options(self, *args) -> None:
        options = {"exaggeration": self.ve_spin.value(), "stacked": self.stack_check.isChecked()}
        self.webview.page().runJavaScript(f"setOptions({json.dumps(options)});")

    # -- map display and hover ------------------------------------------------------

    def _canvas_transform(self) -> Optional[QgsCoordinateTransform]:
        canvas_crs = self.iface.mapCanvas().mapSettings().destinationCrs()
        if self._work_crs is None or canvas_crs == self._work_crs:
            return None
        return QgsCoordinateTransform(self._work_crs, canvas_crs, QgsProject.instance())

    def _draw_profile_lines(self) -> None:
        for band in self._line_bands:
            band.reset(QgsWkbTypes.LineGeometry)
        self._line_bands = []
        for geometry in self._work_geoms:
            band = QgsRubberBand(self.iface.mapCanvas(), QgsWkbTypes.LineGeometry)
            band.setColor(LINE_COLOR)
            band.setWidth(2)
            band.setToGeometry(geometry, self._work_crs)
            self._line_bands.append(band)

    def _on_profile_hover(self, index: int, distance: float) -> None:
        """Chart → map: red point at ``distance`` m along profile ``index``."""
        if not self.link_check.isChecked() or not (0 <= index < len(self._work_geoms)):
            return
        geometry = self._work_geoms[index]
        point = geometry.interpolate(distance)
        if point.isEmpty():
            return
        self._marker.setToGeometry(point, self._work_crs)
        self._marker.show()
        self._highlight.setToGeometry(geometry, self._work_crs)
        self._highlight.show()

    def _on_plot_leave(self) -> None:
        self._marker.hide()
        self._highlight.hide()

    def _on_map_mouse_move(self, point: QgsPointXY) -> None:
        """Map → chart: cursor on the profile passing within 8 pixels."""
        if not self.link_check.isChecked() or not self._work_geoms or not self.isVisible():
            return
        canvas = self.iface.mapCanvas()
        canvas_crs = canvas.mapSettings().destinationCrs()
        tolerance = HOVER_TOLERANCE_PX * canvas.mapUnitsPerPixel()
        if canvas_crs.isGeographic():
            tolerance *= 111320.0                # degrees → metres (work CRS is metric)
        work_point = QgsPointXY(point)
        if canvas_crs != self._work_crs:
            try:
                work_point = QgsCoordinateTransform(canvas_crs, self._work_crs,
                                                    QgsProject.instance()).transform(point)
            except Exception:
                return
        probe = QgsGeometry.fromPointXY(work_point)
        best, best_distance = None, tolerance
        for index, geometry in enumerate(self._work_geoms):
            gap = geometry.distance(probe)
            if gap <= best_distance:
                best, best_distance = index, gap
        if best is None:
            if self._map_hover_index is not None:
                self._map_hover_index = None
                self.webview.page().runJavaScript("hideCursor();")
                self._highlight.hide()
            return
        self._map_hover_index = best
        along = self._work_geoms[best].lineLocatePoint(probe)
        self.webview.page().runJavaScript(f"showCursor({best}, {along:.1f});")
        self._highlight.setToGeometry(self._work_geoms[best], self._work_crs)
        self._highlight.show()

    # -- outputs --------------------------------------------------------------------------

    def _add_lines_layer(self) -> None:
        """Memory layer of the profile lines with their statistics."""
        if not self._result:
            return
        layer = QgsVectorLayer(f"LineString?crs={self._work_crs.authid()}", tr("Topographic profiles"), "memory")
        if not self._work_crs.authid():
            layer.setCrs(self._work_crs)
        names = ["length_m", "length_3d_m", "z_min", "z_max", "relief_m", "mean_slope_deg",
                 "max_slope_deg", "ascent_m", "descent_m"]
        fields = [QgsField("profile_id", QVariant.Int), QgsField("label", QVariant.String),
                  QgsField("along_m", QVariant.Double)] + [QgsField(n, QVariant.Double) for n in names]
        layer.dataProvider().addAttributes(fields)
        layer.updateFields()
        features = []
        for profile, geometry in zip(self._result["profiles"], self._work_geoms):
            feature = QgsFeature(layer.fields())
            feature.setGeometry(geometry)
            feature.setAttributes([profile["id"], profile["label"], profile["along_m"]]
                                  + [profile["stats"].get(n) for n in names])
            features.append(feature)
        layer.dataProvider().addFeatures(features)
        layer.updateExtents()
        QgsProject.instance().addMapLayer(layer)

    def _on_export(self, fmt: str) -> None:
        fmt = fmt.lower()
        if not self._result:
            self.show_error(tr("No profile yet — run Compute first."))
            return
        if fmt == "csv":
            path, _ = QFileDialog.getSaveFileName(self, tr("Export profile points"), "profiles.csv",
                                                  tr("CSV (*.csv)"))
            if not path:
                return
            with open(path, "w", newline="", encoding="utf-8-sig") as handle:
                writer = csv.writer(handle)
                writer.writerow(["profile_id", "label", "distance_m", "x", "y", "z_m", "work_crs"])
                crs_name = self._result.get("work_crs_name", "")
                for p in self._result["profiles"]:
                    for d, x, y, z in zip(p["distance"], p["x"], p["y"], p["z"]):
                        writer.writerow([p["id"], p["label"], d, x, y, "" if z is None else z, crs_name])
            self.show_info(tr("Profiles saved: {0}").format(path))
            return
        ok, path, dpi = self._exporter.prepare_image_export(fmt, parent=self)
        if not ok:
            return

        def export(tab):
            target = path
            if tab == "three" and target.lower().endswith(".svg"):
                # WebGL 3D scenes can only be exported as raster images.
                target = target[:-4] + ".png"
                self.show_info(tr("The 3D view is exported as PNG: {0}").format(target))
            self._pending_export_path = target
            self._pending_export_dpi = dpi
            self.webview.page().runJavaScript("exportActiveChart();")

        self.webview.page().runJavaScript("activeTab();", export)

    # -- life cycle ---------------------------------------------------------------------------

    def cleanup(self) -> None:
        canvas = self.iface.mapCanvas()
        try:
            canvas.xyCoordinates.disconnect(self._on_map_mouse_move)
        except TypeError:
            pass
        if self._draw_tool is not None and canvas.mapTool() is self._draw_tool:
            canvas.unsetMapTool(self._draw_tool)
        for band in self._line_bands + self._drawn_bands + [self._marker, self._highlight]:
            if band is not None:
                canvas.scene().removeItem(band)
