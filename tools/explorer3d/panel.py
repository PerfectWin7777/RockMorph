# tools/explorer3d/panel.py

"""
PyQt panel UI for the 3D Explorer.
Manages layer selection, UI controls, and view swapping inside QGIS.

Authors: RockMorph contributors
"""

import json
from qgis.PyQt.QtWidgets import (  # type: ignore
    QWidget, QVBoxLayout, QHBoxLayout, QLabel, QPushButton, 
    QGroupBox, QSlider, QComboBox, QCheckBox, QFrame
)
from qgis.PyQt.QtCore import Qt, QUrl  # type: ignore
from qgis.core import QgsMapLayerProxyModel  # type: ignore
from qgis.gui import QgsMapLayerComboBox  # type: ignore

from ...base.base_panel import BasePanel
from .engine import Explorer3DEngine

from qgis.PyQt.QtWebEngineWidgets import QWebEnginePage  # type: ignore

class DebugWebEnginePage(QWebEnginePage):
    """Overridden page to redirect JS console.log directly to QGIS Python Console."""
    def javaScriptConsoleMessage(self, level, message, lineNumber, sourceID):
        print(f"[3D WebGL Console] Line {lineNumber} in {sourceID}: {message}")


class Explorer3DPanel(BasePanel):
    """
    Control panel for the 3D Explorer.
    Manages the 3D WebGL viewport embedded in QGIS.
    """

    def _html_file(self) -> str:
        return "explorer3d.html"

    def _build_ui(self) -> None:
        """Construct the PyQt user interface inside the panel dock."""
        self.engine = Explorer3DEngine()
        self.is_3d_active = False

        # Main layout of the PyQt inner widget
        layout = QVBoxLayout(self._inner)
        layout.setContentsMargins(10, 10, 10, 10)
        layout.setSpacing(10)

        # ── Group 1: Layer Selection ─────────────────────────────────
        group_layers = QGroupBox("Input Data")
        layout_layers = QVBoxLayout(group_layers)

        layout_layers.addWidget(QLabel("Main Elevation Raster (DEM):"))
        self.combo_raster = QgsMapLayerComboBox()
        self.combo_raster.setFilters(QgsMapLayerProxyModel.RasterLayer)  # Filter for rasters only [1]
        layout_layers.addWidget(self.combo_raster)

        layout.addWidget(group_layers)

        # ── Group 2: View Controls (Central Swap Trigger) ──────────────
        group_view = QGroupBox("3D Viewport Controller")
        layout_view = QVBoxLayout(group_view)

        self.btn_toggle_3d = QPushButton("Activate 3D View")
        self.btn_toggle_3d.setCheckable(True)
        self.btn_toggle_3d.setStyleSheet("background-color: #27ae60; color: white; font-weight: bold;")
        self.btn_toggle_3d.clicked.connect(self._slot_toggle_3d_view)
        layout_view.addWidget(self.btn_toggle_3d)

        layout.addWidget(group_view)

        # ── Group 3: Real-time Aesthetic Adjustments ──────────────────
        self.group_render = QGroupBox("Render & Styling")
        self.group_render.setEnabled(False)  # Disabled until 3D is active
        layout_render = QVBoxLayout(self.group_render)

        # Z-Scale Slider
        layout_render.addWidget(QLabel("Vertical Exaggeration (Z-Scale):"))
        self.slider_z_scale = QSlider(Qt.Horizontal)
        self.slider_z_scale.setRange(0, 50)  # Represents 0.0x to 5.0x
        self.slider_z_scale.setValue(15)  # Default 1.5x
        self.slider_z_scale.valueChanged.connect(self._slot_update_z_scale)
        layout_render.addWidget(self.slider_z_scale)

        # Render Shading Model
        layout_render.addWidget(QLabel("Shading Mode:"))
        self.combo_shading = QComboBox()
        self.combo_shading.addItems(["Smooth Shading", "Flat Shading", "Wireframe"])
        self.combo_shading.currentIndexChanged.connect(self._slot_update_shading_mode)
        layout_render.addWidget(self.combo_shading)

        # Show/Hide toggles
        self.chk_walls = QCheckBox("Show Lateral Block Walls")
        self.chk_walls.setChecked(True)
        self.chk_walls.stateChanged.connect(self._slot_toggle_walls)
        layout_render.addWidget(self.chk_walls)

        layout.addWidget(self.group_render)
        layout.addStretch()

        # Cache reference to the QGIS map canvas parent container for layout swapping
        self.canvas_2d = self.iface.mapCanvas()
        self.central_container = self.canvas_2d.parentWidget()
        self.central_layout = self.central_container.layout()

        # Insert our QWebEngineView into the central layout of QGIS main window
        self.central_layout.addWidget(self.webview)

        # Redirect all JavaScript errors and console.logs to the QGIS console
        debug_page = DebugWebEnginePage(self.webview)
        self.webview.setPage(debug_page)
        
        # RE-ATTACH THE WEBCHANNEL TO THE NEW DEBUG PAGE [2]
        debug_page.setWebChannel(self._channel)

        

    def _slot_toggle_3d_view(self, checked: bool) -> None:
        """Handles the clean swap between QGIS 2D Canvas and our 3D WebGL view."""
        if checked:
            # Check if a valid layer is selected
            selected_layer = self.combo_raster.currentLayer()
            if not selected_layer:
                self.btn_toggle_3d.setChecked(False)
                self.show_error("Please select a valid DEM layer first.")
                return

            # Step 1: Hide 2D Canvas, show WebGL Viewport
            self.canvas_2d.hide()
            self.webview.show()
            self.is_3d_active = True
            
            self.btn_toggle_3d.setText("Deactivate 3D View")
            self.btn_toggle_3d.setStyleSheet("background-color: #c0392b; color: white; font-weight: bold;")
            self.group_render.setEnabled(True)

            # Step 2: Extract data and send to WebGL Scene [2]
            dem_data = self.engine.prepare_dem(selected_layer)
            command = {
                "action": "set_main_raster",
                "payload": dem_data.to_dict()
            }
            # Send serialized command over the WebChannel bridge [2]
            self.webview.page().runJavaScript(f"processPythonCommand({json.dumps(command)});")

        else:
            # Restore native 2D Canvas view
            self.webview.hide()
            self.canvas_2d.show()
            self.is_3d_active = False

            self.btn_toggle_3d.setText("Activate 3D View")
            self.btn_toggle_3d.setStyleSheet("background-color: #27ae60; color: white; font-weight: bold;")
            self.group_render.setEnabled(False)
            self.canvas_2d.refresh()

    def _slot_update_z_scale(self, value: int) -> None:
        """Send dynamic Z-scale value directly to the Three.js viewport."""
        scale_factor = value / 10.0  # Convert integer 0-50 to float 0.0x-5.0x
        command = {
            "action": "update_z_scale",
            "payload": {"scale": scale_factor}
        }
        self.webview.page().runJavaScript(f"processPythonCommand({json.dumps(command)});")

    def _slot_update_shading_mode(self, index: int) -> None:
        """Send requested shading style directly to the Three.js viewport."""
        shading_modes = ["smooth", "flat", "wireframe"]
        command = {
            "action": "set_shading_mode",
            "payload": {"mode": shading_modes[index]}
        }
        self.webview.page().runJavaScript(f"processPythonCommand({json.dumps(command)});")

    def _slot_toggle_walls(self, state: int) -> None:
        """Send show/hide instruction for the block walls directly to the WebGL scene."""
        visible = (state == Qt.Checked)
        command = {
            "action": "toggle_walls",
            "payload": {"visible": visible}
        }
        self.webview.page().runJavaScript(f"processPythonCommand({json.dumps(command)});")

    def _on_compute(self) -> None:
        """Unused default override since computing is done dynamically here."""
        pass

    def _on_result(self, data: dict) -> None:
        """Unused default override since results are piped in real-time."""
        pass

    def cleanup(self) -> None:
        """Secure cleanup of QGIS central window widgets during plugin unload."""
        if self.is_3d_active:
            self._slot_toggle_3d_view(False)
        if self.webview:
            self.central_layout.removeWidget(self.webview)