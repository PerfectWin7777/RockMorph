"""
tools/swath/panel.py

SwathPanel — UI for the Swath Profile tool.
Inherits BasePanel.
"""

from qgis.PyQt.QtWidgets import ( # type: ignore
    QVBoxLayout, QHBoxLayout, QFormLayout,
    QLabel, QPushButton, QSpinBox, QDoubleSpinBox,
    QCheckBox, QLineEdit, QSizePolicy, QComboBox
)
from qgis.PyQt.QtGui import QColor # type: ignore
from ...base.webengine import QWebEngineView
from qgis.PyQt.QtCore import Qt, QCoreApplication # type: ignore
from qgis.gui import QgsMapLayerComboBox, QgsRubberBand, QgsCollapsibleGroupBox   # type: ignore
from qgis.core import (  # type: ignore
    QgsMapLayerProxyModel, QgsWkbTypes,Qgis,
    QgsPointXY, QgsGeometry, QgsProject,QgsCoordinateTransform,
    QgsCoordinateReferenceSystem, QgsExpression, QgsExpressionContext,
    QgsExpressionContextUtils, QgsFeatureRequest,
)
import bisect
import json
import os
import math
from ...base.base_panel import BasePanel, ComputeWorker
from ...core.exporter import RockMorphExporter
from ...widgets.export_group import RockMorphExportGroup
from .engine import SwathEngine
from .style_manager import CurveStyleManager


def tr(message):
    return QCoreApplication.translate("RockMorph", message)



class SwathPanel(BasePanel):
    """
    UI panel for the Swath Profile tool.
    Two subplots: main profile (min/mean/max) + secondary (relief or hyps).
    """

    def __init__(self, iface, parent=None):
        self._engine   = SwathEngine()
        self._exporter = RockMorphExporter(iface)
        self._last_data = None
        # self._canvas_connected = False
        self.swath_rubber_band  = None
    
        # Initialize a dedicated RubberBand for the map marker (a red circle)
        self.map_marker = QgsRubberBand(iface.mapCanvas(), QgsWkbTypes.PointGeometry)
        self.map_marker.setIcon(QgsRubberBand.ICON_CIRCLE)
        self.map_marker.setIconSize(10)
        self.map_marker.setColor(QColor(231, 76, 60)) # Plotly Red
        self.map_marker.setStrokeColor(QColor(255, 255, 255)) # White outline
        self.map_marker.hide()

         #  Call super().__init__ LAST (it triggers UI build and HTML load)
        super().__init__(iface, parent)

        # Ensure tracking is off on init
        if hasattr(self, 'tracking_check'):
            self.tracking_check.setChecked(False)
    
    def _html_file(self) -> str:
        return "swath.html"

    # ------------------------------------------------------------------
    # BasePanel interface
    # ------------------------------------------------------------------

    def _build_ui(self):
        root = QVBoxLayout(self._inner)
        root.setContentsMargins(8, 8, 8, 8)
        root.setSpacing(6)

        # --- Input group ---
        input_group = QgsCollapsibleGroupBox(tr("Input"))
        input_layout = QFormLayout(input_group)

        self.dem_combo = QgsMapLayerComboBox()
        self.dem_combo.setFilters(QgsMapLayerProxyModel.RasterLayer)
        input_layout.addRow(tr("DEM layer:"), self.dem_combo)

        self.line_combo = QgsMapLayerComboBox()
        self.line_combo.setFilters(QgsMapLayerProxyModel.LineLayer)
        input_layout.addRow(tr("Profile line:"), self.line_combo)

        # Which feature of the line layer is profiled (the tool used to take
        # the first feature silently). Defaults to the selected feature.
        self.feature_combo = QComboBox()
        self.feature_combo.setToolTip(tr("Line of the layer used for the swath. By default the "
                                         "feature selected on the map, otherwise the first one."))
        input_layout.addRow(tr("Line feature:"), self.feature_combo)
        self.line_combo.layerChanged.connect(self._on_line_layer_changed)
        self._on_line_layer_changed(self.line_combo.currentLayer())

        root.addWidget(input_group)

        # --- Parameters group ---
        params_group = QgsCollapsibleGroupBox(tr("Parameters"))
        params_layout = QFormLayout(params_group)

        self.stations_spin = QSpinBox()
        self.stations_spin.setRange(50, 1000)
        self.stations_spin.setSingleStep(50)
        self.stations_spin.setValue(400)
        self.stations_spin.setToolTip(tr(
            "Number of sample points along the profile line."
        ))
        params_layout.addRow(tr("Stations:"), self.stations_spin)

        self.width_spin = QDoubleSpinBox()
        self.width_spin.setRange(10.0, 50000.0)
        self.width_spin.setValue(1000.0)
        self.width_spin.setSingleStep(100.0)
        self.width_spin.setSuffix(" m")
        self.width_spin.setDecimals(0)
        self.width_spin.setToolTip(tr(
            "Half-width of swath band in metres.\n"
            "Total band width = 2 x this value."
        ))
        params_layout.addRow(tr("Half-width:"), self.width_spin)

        self.transversal_spin = QSpinBox()
        self.transversal_spin.setRange(10, 1000)
        self.transversal_spin.setValue(100)
        self.transversal_spin.setSingleStep(20)
        self.transversal_spin.setToolTip(tr(
            "Number of sample points per transversal.\n"
            "More points = slower but more accurate stats."
        ))
        params_layout.addRow(tr("Transversal pts:"), self.transversal_spin)

        self.smooth_spin = QSpinBox()
        self.smooth_spin.setRange(0, 99)
        self.smooth_spin.setValue(0)  # 0 = no smoothing
        self.smooth_spin.setSingleStep(2)  # Step of 2 to skip more easily from odd numbers to odd numbers
        self.smooth_spin.setSuffix(" pts")
        self.smooth_spin.setToolTip(tr(
            "Window size for Hanning smoothing (must be >= 3). Set to 0 to disable."
        ))
        params_layout.addRow(tr("Smoothing window:"), self.smooth_spin)

        

        root.addWidget(params_group)

        # --- Options group ---
        options_group = QgsCollapsibleGroupBox(tr("Options"))
        options_layout = QVBoxLayout(options_group)

        self.q_check = QCheckBox(tr("Show Q1/Q3 envelope"))
        self.q_check.setToolTip(tr(
            "Add 25th and 75th percentile envelopes.\n"
            "More robust than min/max for noisy DEMs."
        ))
        options_layout.addWidget(self.q_check)

        self.relief_check = QCheckBox(tr("Show local relief"))
        self.relief_check.setChecked(True)
        self.relief_check.setToolTip(tr(
            "Second subplot: local relief = max - min per transversal."
        ))
        options_layout.addWidget(self.relief_check)

        self.hyps_check = QCheckBox(tr("Show transversal hypsometry"))
        self.hyps_check.setChecked(False)
        self.hyps_check.setToolTip(tr(
            "Second subplot: (mean - min) / (max - min) per transversal.\n"
            "Close to 1 = young/active relief. Close to 0 = mature relief."
        ))
        options_layout.addWidget(self.hyps_check)

        
        self.reorient_check = QCheckBox(tr("Force High-to-Low orientation"))
        self.reorient_check.setToolTip(tr(
            "Ensure the profile starts at the highest point (useful for river longitudinal profiles)."
        ))
        options_layout.addWidget(self.reorient_check)

        self.tracking_check = QCheckBox(tr("Canvas tracking"))
        self.tracking_check.setChecked(False)
        self.tracking_check.setToolTip(tr(
            "Move cursor on map canvas to see position on profile."
        ))
        self.tracking_check.stateChanged.connect(self._toggle_tracking)
        options_layout.addWidget(self.tracking_check)

         # --- Title ---
        title_layout = QFormLayout()
        options_layout.addLayout(title_layout)
        self.title_edit = QLineEdit(tr("Swath Profile"))
        title_layout.addRow(tr("Title:"), self.title_edit)

        root.addWidget(options_group)

    
        # --- Curve styles ---
        self.style_manager = CurveStyleManager(
            [
            ("mean",   "Mean"),
            ("min",    "Min"),
            ("max",    "Max"),
            ("q1",     "Q1"),
            ("q3",     "Q3"),
            ("relief", "Relief"),
            ("hyps",   "Hypsometry"),
        ], 
        self._apply_styles,
        self._inner
        )

        # Q1/Q3 hidden by default
        self.style_manager.set_visible("q1", False)
        self.style_manager.set_visible("q3", False)

        # Connect checkbox to show/hide Q1/Q3 style widgets
        self.q_check.stateChanged.connect(
            lambda state: [
                self.style_manager.set_visible("q1", state == Qt.Checked),
                self.style_manager.set_visible("q3", state == Qt.Checked),
            ]
        )

        # Connect relief/hyps checkboxes
        self.relief_check.stateChanged.connect(
            lambda state: self.style_manager.set_visible(
                "relief", state == Qt.Checked
            )
        )
        self.hyps_check.stateChanged.connect(
            lambda state: self.style_manager.set_visible(
                "hyps", state == Qt.Checked
            )
        )

        root.addWidget(self.style_manager)


        # --- Compute button ---
        self.compute_btn = QPushButton(tr("Compute"))
        self.compute_btn.setFixedHeight(36)
        self.compute_btn.setStyleSheet("""
            QPushButton {
                background-color: #2d6a9f;
                color: white;
                border-radius: 4px;
                font-weight: bold;
            }
            QPushButton:hover   { background-color: #3a7fc1; }
            QPushButton:pressed { background-color: #1f4f7a; }
        """)
        self.compute_btn.clicked.connect(self._on_compute)
        root.addWidget(self.compute_btn)

       # add a progress bar container (hidden by default, shown during computation)
        root.addWidget(self._progress_container)

        # --- WebEngineView ---
        self.webview = QWebEngineView()
        self.webview.setMinimumHeight(420)
        self.webview.setSizePolicy(
            QSizePolicy.Expanding, QSizePolicy.Expanding
        )
        self._setup_webchannel()
        self._load_html()
        root.addWidget(self.webview)

        # --- Export group ---
        export_group = QgsCollapsibleGroupBox(tr("Export"))
        export_layout = QVBoxLayout(export_group)  # Note

        self.export_widget = RockMorphExportGroup(
            formats=["png", "jpg", "svg", "pdf", "csv", "json"],
            parent=self
        )
        self.export_widget.exportRequested.connect(self._on_export)
        export_layout.addWidget(self.export_widget)
        root.addWidget(export_group)

    # ------------------------------------------------------------------
    # Compute
    # ------------------------------------------------------------------

    def _on_compute(self):
        dem_layer  = self.dem_combo.currentLayer()
        line_layer = self.line_combo.currentLayer()

        params = {
            "dem_layer":      dem_layer,
            "line_layer":     line_layer,
            "n_stations":     self.stations_spin.value(),
            "width_m":        self.width_spin.value(),
            "n_transversal":  self.transversal_spin.value(),
            "smooth_window":  self.smooth_spin.value(), 
            "force_high_to_low": self.reorient_check.isChecked(),
            "compute_q":      self.q_check.isChecked(),
            "compute_relief": self.relief_check.isChecked(),
            "compute_hyps":   self.hyps_check.isChecked(),
            "feature_id":     self.feature_combo.currentData(),
        }

        if not self._engine.validate(**params):
            self.show_error(tr(
                "Please select a valid DEM layer and a line layer."
            ))
            return
        
         # Show progress bar and disable compute button during processing
        self.set_loading_state(True, tr("Sampling DEM data..."), total=100)
        try:
            # Run computation in background thread to keep UI responsive
            self._worker = ComputeWorker(self._engine, params)

            # Connexions for progress, error, and result signals
            self._worker.progress.connect(self.update_progress)
            self._worker.error.connect(self._on_compute_error)
            self._worker.finished.connect(self._on_compute_finished)
            
            self._worker.start()

        except Exception as e:
            import traceback
            traceback.print_exc()
            self.show_error(str(e))
        # finally:
        #     # hide progress bar and re-enable button
        #     self.set_loading_state(False)
        #     self.compute_btn.setEnabled(True)
        #     self.compute_btn.setText(tr("Compute"))
    
    def _on_compute_error(self, err_msg):
        self.set_loading_state(False)
        self.show_error(f"Engine Error: {err_msg}")

    def _on_compute_finished(self, data):
        self.set_loading_state(False)
        self._on_result(data)

    def _apply_styles(self):
        """Send updated styles to HTML without recomputing."""
        if self._last_data is None:
            self.show_error(tr("No data — run Compute first."))
            return
        self._last_data["styles"] = self.style_manager.get_all_styles()
        json_data = json.dumps(self._last_data)
        self.webview.page().runJavaScript(
            f'updatePlot({json.dumps(json_data)})'
        )


    def _on_result(self, data: dict):
        self._last_data = data
        for warning in data.get("warnings", []):
            self.show_info(warning)
        if self.tracking_check.isChecked():
            self._draw_swath_rubber_band()
        data["title"] = self.title_edit.text()
        # Inject current styles into data
        data["styles"] = self.style_manager.get_all_styles()
        json_data = json.dumps(data)
        js = f'updatePlot({json.dumps(json_data)})'
        self.webview.page().runJavaScript(
            js,
            # lambda result: print("JS result:", result)
        )
       

    # ------------------------------------------------------------------
    # Canvas tracking
    # ------------------------------------------------------------------

    def _toggle_tracking(self, state: int):
        """
        Controls the tracking system.
        Note: We no longer connect to canvas.xyCoordinates.
        """
        is_checked = (state == Qt.Checked)
        
        if is_checked:
            # Show the swath area (blue box) to give context
            self._draw_swath_rubber_band()
        else:
            # Hide everything when unchecked
            if self.swath_rubber_band:
                self.swath_rubber_band.hide()
            if self.map_marker:
                self.map_marker.hide()
            
            # Reset the cursor on the Plotly chart via JavaScript
            # self.webview.page().runJavaScript("updateCursor(null)")

    def _on_plot_hover(self, distance):
        """
        Chart → map: red point at ``distance`` metres along the swath.

        The position is interpolated between the two stations that bracket
        the distance, using the station coordinates returned by the engine
        (DEM CRS). This is exact for any CRS and follows a reversed
        (high-to-low) profile; the former code interpolated metres along the
        first feature in DEM CRS units (degrees for a geographic DEM).
        """
        if not self.tracking_check.isChecked() or not self._last_data:
            return
        data = self._last_data
        xs, ys, ds = data.get("station_x"), data.get("station_y"), data.get("distances")
        if not xs or not ds:
            return
        i = bisect.bisect_left(ds, distance)
        i = min(max(i, 1), len(ds) - 1)
        span = ds[i] - ds[i - 1]
        t = 0.0 if span <= 0 else min(max((distance - ds[i - 1]) / span, 0.0), 1.0)
        point = QgsPointXY(xs[i - 1] + t * (xs[i] - xs[i - 1]), ys[i - 1] + t * (ys[i] - ys[i - 1]))
        crs = QgsCoordinateReferenceSystem.fromWkt(data["station_crs"])
        self.map_marker.setToGeometry(QgsGeometry.fromPointXY(point), crs)
        self.map_marker.show()

    def _on_swath_point(self, distance: float, offset: float) -> None:
        """
        3D block → map: the sampled point at ``distance`` along the line and
        ``offset`` across it (metres, + = left of the line direction), i.e.
        between the right and left ends of the transversal at that station.
        """
        if not self.tracking_check.isChecked() or not self._last_data:
            return
        data = self._last_data
        left, right, ds = data.get("band_left"), data.get("band_right"), data.get("distances")
        if not left or not ds:
            return
        i = bisect.bisect_left(ds, distance)
        i = min(max(i, 1), len(ds) - 1)
        span = ds[i] - ds[i - 1]
        t = 0.0 if span <= 0 else min(max((distance - ds[i - 1]) / span, 0.0), 1.0)

        def lerp(a, b, f):
            return (a[0] + f * (b[0] - a[0]), a[1] + f * (b[1] - a[1]))

        # Transversal at this distance, from its right end to its left end.
        r = lerp(right[i - 1], right[i], t)
        l = lerp(left[i - 1], left[i], t)
        width = data.get("width_m") or 1.0
        f = min(max((offset + width) / (2.0 * width), 0.0), 1.0)
        x, y = lerp(r, l, f)
        crs = QgsCoordinateReferenceSystem.fromWkt(data["station_crs"])
        self.map_marker.setToGeometry(QgsGeometry.fromPointXY(QgsPointXY(x, y)), crs)
        self.map_marker.show()

    def _on_plot_leave(self):
        """Hides the map marker when mouse leaves the plot area."""
        if self.map_marker:
            self.map_marker.hide()

    def _draw_swath_rubber_band(self):
        """
        Outline of the band actually sampled: the ends of every transversal,
        returned by the engine in the DEM CRS (the former buffer was redrawn
        around the FIRST feature, whatever line had been computed).
        """
        band = (self._last_data or {}).get("band")
        if not band:
            return
        if not self.swath_rubber_band:
            self.swath_rubber_band = QgsRubberBand(self.iface.mapCanvas(), QgsWkbTypes.PolygonGeometry)
            self.swath_rubber_band.setColor(QColor(45, 106, 159, 40))
            self.swath_rubber_band.setStrokeColor(QColor(45, 106, 159, 180))
            self.swath_rubber_band.setWidth(1)
        else:
            self.swath_rubber_band.reset(QgsWkbTypes.PolygonGeometry)
        ring = [QgsPointXY(x, y) for x, y in band]
        ring.append(ring[0])
        crs = QgsCoordinateReferenceSystem.fromWkt(self._last_data["station_crs"])
        self.swath_rubber_band.setToGeometry(QgsGeometry.fromPolygonXY([ring]), crs)
        self.swath_rubber_band.show()
        self.iface.mapCanvas().refresh()

    def _on_line_layer_changed(self, layer=None) -> None:
        """List the features of the line layer; preselect the selected one."""
        previous = getattr(self, "_watched_line_layer", None)
        if previous is not None:
            try:
                previous.selectionChanged.disconnect(self._fill_feature_combo)
            except (TypeError, RuntimeError):
                pass
        self._watched_line_layer = layer
        if layer is not None:
            layer.selectionChanged.connect(self._fill_feature_combo)
        self._fill_feature_combo()

    def _fill_feature_combo(self, *args) -> None:
        self.feature_combo.blockSignals(True)
        self.feature_combo.clear()
        layer = self.line_combo.currentLayer()
        if layer is not None:
            context = QgsExpressionContext(QgsExpressionContextUtils.globalProjectLayerScopes(layer))
            display = QgsExpression(layer.displayExpression())
            for count, feature in enumerate(layer.getFeatures(QgsFeatureRequest().setNoAttributes()
                                                              if not display.referencedColumns() else
                                                              QgsFeatureRequest())):
                if count >= 5000:     # very large layers: list the first 5000
                    break
                context.setFeature(feature)
                name = display.evaluate(context)
                text = f"#{feature.id()}" + (f" — {name}" if name not in (None, "") else "")
                self.feature_combo.addItem(text, feature.id())
            selected = layer.selectedFeatureIds()
            if selected:
                index = self.feature_combo.findData(selected[0])
                if index >= 0:
                    self.feature_combo.setCurrentIndex(index)
        self.feature_combo.blockSignals(False)

    # def _on_canvas_move(self, point: QgsPointXY):
    #     """
    #     Handles mouse movement on map canvas. 
    #     Calculates distance along line and checks proximity.
    #     """
    #     if self._last_data is None:
    #         return

    #     line_layer = self.line_combo.currentLayer()
    #     dem_layer = self.dem_combo.currentLayer()
    #     if not line_layer or not dem_layer:
    #         return

    #     feature = next(line_layer.getFeatures(), None)
    #     if not feature:
    #         return

    #     # 1. Coordinate Transforms
    #     # We need everything in the DEM CRS (usually metric) for accurate distances
    #     canvas_crs = self.iface.mapCanvas().mapSettings().destinationCrs()
    #     line_crs = line_layer.crs()
    #     target_crs = dem_layer.crs()

    #     # Transform line geometry to target CRS
    #     line_geom = feature.geometry()
    #     if line_crs != target_crs:
    #         line_xform = QgsCoordinateTransform(line_crs, target_crs, QgsProject.instance())
    #         line_geom.transform(line_xform)

    #     # Transform mouse point to target CRS
    #     if canvas_crs != target_crs:
    #         point_xform = QgsCoordinateTransform(canvas_crs, target_crs, QgsProject.instance())
    #         try:
    #             point = point_xform.transform(point)
    #         except:
    #             return

    #     # 2. Distance and Proximity Check
    #     mouse_geom = QgsGeometry.fromPointXY(point)
        
    #     # Calculate perpendicular distance from mouse to the profile line
    #     perpendicular_dist = line_geom.distance(mouse_geom)
        
    #     # Set a threshold slightly larger than the half-width for better UX
    #     threshold = self.width_spin.value() * 1.1
        
    #     if perpendicular_dist > threshold:
    #         # Mouse is outside the swath zone: hide the cursor
    #         self.webview.page().runJavaScript("updateCursor(null)")
    #     else:
    #         # Mouse is inside: calculate distance from start of the line
    #         dist_along_line = line_geom.lineLocatePoint(mouse_geom)
    #         self.webview.page().runJavaScript(f"updateCursor({dist_along_line})")

    def cleanup(self):
        """
        Called when panel is destroyed or plugin unloaded.
        Removes rubber band from canvas scene.
        """
        # Disconnect canvas tracking if active
        # if self._canvas_connected:
        #     try:
        #         self.iface.mapCanvas().xyCoordinates.disconnect(
        #             self._on_canvas_move
        #         )
        #     except:
        #         pass
        #     self._canvas_connected = False

        # Remove rubber band from canvas
        if self.swath_rubber_band:
            self.swath_rubber_band.hide()
            self.swath_rubber_band.reset(QgsWkbTypes.PolygonGeometry)
            try:
                self.iface.mapCanvas().scene().removeItem(
                    self.swath_rubber_band
                )
            except:
                pass
            self.swath_rubber_band = None
        
        if self.map_marker:
            self.iface.mapCanvas().scene().removeItem(self.map_marker)
            self.map_marker = None

        self.iface.mapCanvas().refresh()

    

    # ------------------------------------------------------------------
    # Export
    # ------------------------------------------------------------------

    def _on_export(self, fmt: str):
        fmt_lower = fmt.lower()
        # Tabular formats
        if fmt_lower == "csv":
            if self._last_data is None:
                self.show_error(tr("No data — run Compute first."))
                return
            self._exporter.export_csv(
                self._build_csv_rows(),
                self._csv_headers(),
                parent=self
            )
            return
        
        if fmt_lower == "json":
            if self._last_data is None:
                self.show_error(tr("No data — run Compute first."))
                return
            self._exporter.export_json(
                self._last_data,
                parent=self
            )
            return
        

        # Image formats
        ok, path, dpi = self._exporter.prepare_image_export(fmt_lower, parent=self)
        if not ok:
            return

        self._pending_export_path = path
        self._pending_export_dpi  = dpi

        def export(view):
            # The WebGL 3D block can only be exported as a raster image.
            if view == "3d" and self._pending_export_path.lower().endswith((".svg", ".pdf")):
                self._pending_export_path = self._pending_export_path.rsplit(".", 1)[0] + ".png"
                self.show_info(tr("The 3D block is exported as PNG: {0}").format(self._pending_export_path))
            self.webview.page().runJavaScript("exportSwathActive()")

        self.webview.page().runJavaScript("activeView()", export)
    
    
    def _csv_headers(self) -> list:
        headers = ["distance_m", "mean", "min", "max"]
        if self._last_data and self._last_data.get("q1"):
            headers += ["q1", "q3"]
        if self._last_data and self._last_data.get("relief"):
            headers.append("relief")
        if self._last_data and self._last_data.get("hyps"):
            headers.append("hypsomtry")
        return headers

    def _build_csv_rows(self) -> list:
        if self._last_data is None:
            return []
        data    = self._last_data
        headers = self._csv_headers()
        rows    = []
        for i, d in enumerate(data["distances"]):
            row = {
                "distance_m": d,
                "mean":       data["mean"][i],
                "min":        data["min"][i],
                "max":        data["max"][i],
            }
            if "q1" in headers:
                row["q1"] = data["q1"][i]
                row["q3"] = data["q3"][i]
            if "relief" in headers:
                row["relief"] = data["relief"][i]
            if "hyps" in headers:
                row["hyps"] = data["hyps"][i]
            rows.append(row)
        return rows

    def closeEvent(self, event):
        """Cleanup when panel is closed."""
        if hasattr(self, 'cleanup'):
            self.cleanup()
        super().closeEvent(event)