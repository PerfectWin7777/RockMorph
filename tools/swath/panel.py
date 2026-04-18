"""
tools/swath/panel.py

SwathPanel — UI for the Swath Profile tool.
Inherits BasePanel.
"""

from PyQt5.QtWidgets import ( # type: ignore
    QVBoxLayout, QHBoxLayout, QFormLayout,
    QLabel, QPushButton, QSpinBox, QDoubleSpinBox,
    QCheckBox, QLineEdit, QGroupBox, QSizePolicy
)
from PyQt5.QtGui import QColor # type: ignore
from PyQt5.QtWebEngineWidgets import QWebEngineView # type: ignore
from PyQt5.QtCore import Qt, QCoreApplication # type: ignore
from qgis.gui import QgsMapLayerComboBox, QgsRubberBand  # type: ignore
from qgis.core import (  # type: ignore
    QgsMapLayerProxyModel, QgsWkbTypes,Qgis,
    QgsPointXY, QgsGeometry, QgsProject,QgsCoordinateTransform
)
import json
import os
import math
from ...base.base_panel import BasePanel, ComputeWorker
from ...core.exporter import RockMorphExporter
from ...ui.curve_style_widget import CurveStyleManager
from .engine import SwathEngine


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
        self._canvas_connected = False
        self.swath_rubber_band  = None
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
        input_group = QGroupBox(tr("Input"))
        input_layout = QFormLayout(input_group)

        self.dem_combo = QgsMapLayerComboBox()
        self.dem_combo.setFilters(QgsMapLayerProxyModel.RasterLayer)
        input_layout.addRow(tr("DEM layer:"), self.dem_combo)

        self.line_combo = QgsMapLayerComboBox()
        self.line_combo.setFilters(QgsMapLayerProxyModel.LineLayer)
        input_layout.addRow(tr("Profile line:"), self.line_combo)

        root.addWidget(input_group)

        # --- Parameters group ---
        params_group = QGroupBox(tr("Parameters"))
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
        options_group = QGroupBox(tr("Options"))
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
        export_group = QGroupBox(tr("Export"))
        export_layout = QHBoxLayout(export_group)
        for fmt in ["PNG", "JPG", "SVG", "PDF", "CSV", "JSON"]:
            btn = QPushButton(fmt)
            btn.setFixedHeight(28)
            btn.clicked.connect(lambda checked, f=fmt: self._on_export(f))
            export_layout.addWidget(btn)
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
        canvas = self.iface.mapCanvas()
        if state == Qt.Checked:
            canvas.xyCoordinates.connect(self._on_canvas_move)
            self._canvas_connected = True
            self._update_swath_rubber_band() # Optional: show swath area on canvas using rubber band
        else:
            if self._canvas_connected:
                canvas.xyCoordinates.disconnect(self._on_canvas_move)
                self._canvas_connected = False
                if self.swath_rubber_band:
                    self.swath_rubber_band.hide()
                    self.swath_rubber_band.reset(QgsWkbTypes.PolygonGeometry)
                    self.iface.mapCanvas().scene().removeItem(self.swath_rubber_band)
                    self.swath_rubber_band = None
                # Hide cursor on plot
                self.iface.mapCanvas().refresh()
                self.webview.page().runJavaScript("updateCursor(null)")

    def _update_swath_rubber_band(self):
        """Draws a transparent polygon on the map to visualize the swath width."""
        line_layer = self.line_combo.currentLayer()
        if not line_layer:
            return

        feature = next(line_layer.getFeatures(), None)
        if not feature:
            return

        if not self.swath_rubber_band:
            self.swath_rubber_band = QgsRubberBand(self.iface.mapCanvas(), QgsWkbTypes.PolygonGeometry)
            # Nice semi-transparent blue for scientific UI
            self.swath_rubber_band.setColor(QColor(45, 106, 159, 40))
            self.swath_rubber_band.setStrokeColor(QColor(45, 106, 159, 180))
            self.swath_rubber_band.setWidth(1)
        else:
            self.swath_rubber_band.reset(QgsWkbTypes.PolygonGeometry)  # ← reset instead of creating a new one each time

        canvas_crs = self.iface.mapCanvas().mapSettings().destinationCrs()
        line_crs   = line_layer.crs()
    
        # Create a buffer (rectangle) around the line representing the swath width
        geom = feature.geometry()
        width = self.width_spin.value()
        if width <= 0: return

        # Reproject line to canvas CRS if needed
        if line_crs != canvas_crs:
            xform = QgsCoordinateTransform(
                line_crs, canvas_crs, QgsProject.instance()
            )
            geom.transform(xform)

        # Now buffer in canvas CRS units
        # If canvas CRS is geographic — convert metres to degrees approx
        if canvas_crs.isGeographic():
            centroid = geom.centroid().asPoint()
            
            lat_rad   = math.radians(centroid.y())
            width_deg = width / (111320.0 * math.cos(lat_rad))
            buffer_dist = width_deg
        else:
            buffer_dist = width

        
        # 3. Create the buffer geometry — using Flat end caps to create a rectangular swath instead of rounded
        # CapFlat creates a rectangular buffer instead of rounded ends
        # (distance, segments, cap, join, miter)
        swath_buffer = geom.buffer(buffer_dist, 5, Qgis.EndCapStyle.Flat, Qgis.JoinStyle.Miter, 2.0)

        # 4. Set geometry and CRITICAL: ensure it's shown
        self.swath_rubber_band.setToGeometry(swath_buffer, canvas_crs)
        self.swath_rubber_band.show() # Force visibility
        
        # 5. Force QGIS to redraw the canvas so the blue box appears immediately
        self.iface.mapCanvas().refresh()

    
    def _on_canvas_move(self, point: QgsPointXY):
        """
        Handles mouse movement on map canvas. 
        Calculates distance along line and checks proximity.
        """
        if self._last_data is None:
            return

        line_layer = self.line_combo.currentLayer()
        dem_layer = self.dem_combo.currentLayer()
        if not line_layer or not dem_layer:
            return

        feature = next(line_layer.getFeatures(), None)
        if not feature:
            return

        # 1. Coordinate Transforms
        # We need everything in the DEM CRS (usually metric) for accurate distances
        canvas_crs = self.iface.mapCanvas().mapSettings().destinationCrs()
        line_crs = line_layer.crs()
        target_crs = dem_layer.crs()

        # Transform line geometry to target CRS
        line_geom = feature.geometry()
        if line_crs != target_crs:
            line_xform = QgsCoordinateTransform(line_crs, target_crs, QgsProject.instance())
            line_geom.transform(line_xform)

        # Transform mouse point to target CRS
        if canvas_crs != target_crs:
            point_xform = QgsCoordinateTransform(canvas_crs, target_crs, QgsProject.instance())
            try:
                point = point_xform.transform(point)
            except:
                return

        # 2. Distance and Proximity Check
        mouse_geom = QgsGeometry.fromPointXY(point)
        
        # Calculate perpendicular distance from mouse to the profile line
        perpendicular_dist = line_geom.distance(mouse_geom)
        
        # Set a threshold slightly larger than the half-width for better UX
        threshold = self.width_spin.value() * 1.1
        
        if perpendicular_dist > threshold:
            # Mouse is outside the swath zone: hide the cursor
            self.webview.page().runJavaScript("updateCursor(null)")
        else:
            # Mouse is inside: calculate distance from start of the line
            dist_along_line = line_geom.lineLocatePoint(mouse_geom)
            self.webview.page().runJavaScript(f"updateCursor({dist_along_line})")

    def cleanup(self):
        """
        Called when panel is destroyed or plugin unloaded.
        Removes rubber band from canvas scene.
        """
        # Disconnect canvas tracking if active
        if self._canvas_connected:
            try:
                self.iface.mapCanvas().xyCoordinates.disconnect(
                    self._on_canvas_move
                )
            except:
                pass
            self._canvas_connected = False

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

        div_id = 'plot-main'  # div ID for plotly export
        self.webview.page().runJavaScript(f"exportViaSvg('{div_id}')")
    
    
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