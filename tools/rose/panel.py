
# tools/rose/panel.py

from PyQt5.QtWidgets import ( # type: ignore
    QVBoxLayout, QHBoxLayout, QFormLayout,
    QLabel, QPushButton, QSpinBox, QComboBox,
    QCheckBox, QSlider, QLineEdit, QGroupBox,
    QColorDialog, QSizePolicy,QDoubleSpinBox,
    QInputDialog
)
from PyQt5.QtWebEngineWidgets import QWebEngineView # type: ignore
from PyQt5.QtWebChannel import QWebChannel # type: ignore
from PyQt5.QtWebEngineWidgets import QWebEngineSettings # type: ignore
from PyQt5.QtCore import Qt, QUrl, QObject, pyqtSlot, QCoreApplication # type: ignore
from PyQt5.QtGui import QColor # type: ignore
from qgis.PyQt.QtWidgets import QFileDialog # type: ignore
from qgis.gui import QgsMapLayerComboBox # type: ignore
from qgis.core import QgsMapLayerProxyModel # type: ignore


import json
import os

from ...base.base_panel import BasePanel
from ...core.exporter import RockMorphExporter
from .engine import RoseEngine


def tr(message):
    return QCoreApplication.translate("RockMorph", message)


class RoseBridge(QObject):
    """
    QObject exposed to JavaScript via QWebChannel.
    Handles callbacks from the HTML side (export data).
    """

    def __init__(self, panel, parent=None):
        super().__init__(parent)
        self._panel = panel

    @pyqtSlot(str)
    def receive_export(self, data_url: str):
        """Called by JS after Plotly.toImage() — receives base64 image or SVG."""
        self._panel._save_export(data_url)


class RosePanel(BasePanel):
    """
    UI panel for the Rose Diagram tool.
    Owns the input form, WebEngineView, and export logic.
    """

    def __init__(self, iface, parent=None):
        self._engine   = RoseEngine()
        self._exporter = RockMorphExporter(iface) 
        self._color    = "#4a9eff"
        self._pending_export_path = None
        super().__init__(iface, parent)

    # ------------------------------------------------------------------
    # BasePanel interface
    # ------------------------------------------------------------------

    def _build_ui(self):
        # Build into self._inner, not self
        root = QVBoxLayout(self._inner)
        root.setContentsMargins(8, 8, 8, 8)
        root.setSpacing(6)

        # --- Input group ---
        input_group = QGroupBox(tr("Input"))
        input_layout = QFormLayout(input_group)

        self.layer_combo = QgsMapLayerComboBox()
        self.layer_combo.setFilters(QgsMapLayerProxyModel.LineLayer)
        input_layout.addRow(tr("Line layer:"), self.layer_combo)

        self.sectors_spin = QSpinBox()
        self.sectors_spin.setRange(8, 72)
        self.sectors_spin.setValue(36)
        self.sectors_spin.setToolTip(tr(
            "Number of sectors — more sectors = finer angular resolution"
        ))
        input_layout.addRow(tr("Sectors:"), self.sectors_spin)

        self.mode_combo = QComboBox()
        self.mode_combo.addItems([tr("Count"), tr("Length"), tr("Frequency %")])
        self.mode_combo.setCurrentIndex(1)
        input_layout.addRow(tr("Mode:"), self.mode_combo)

        self.half_rose_check = QCheckBox(tr("Half rose (0–180°)"))
        self.half_rose_check.setChecked(False)
        input_layout.addRow("", self.half_rose_check)

        self.rectitude_spin = QDoubleSpinBox()
        self.rectitude_spin.setRange(0.0, 1.0)
        self.rectitude_spin.setSingleStep(0.05)
        self.rectitude_spin.setValue(0.0)
        self.rectitude_spin.setDecimals(2)
        self.rectitude_spin.setToolTip(tr(
            "Rectitude filter — 0 = no filter, 0.85 = only straight features\n"
            "Applied at feature level before segment extraction."
        ))
        input_layout.addRow(tr("Min rectitude:"), self.rectitude_spin)

        root.addWidget(input_group)

        # --- Style group ---
        style_group = QGroupBox(tr("Style"))
        style_layout = QFormLayout(style_group)

        # Color picker
        color_row = QHBoxLayout()
        self.color_preview = QPushButton()
        self.color_preview.setFixedSize(32, 24)
        self._update_color_preview()
        self.color_preview.clicked.connect(self._pick_color)
        color_row.addWidget(self.color_preview)
        color_row.addStretch()
        style_layout.addRow(tr("Petal color:"), color_row)

        # Opacity slider
        opacity_row = QHBoxLayout()
        self.opacity_slider = QSlider(Qt.Horizontal)
        self.opacity_slider.setRange(0, 100)
        self.opacity_slider.setValue(100)
        self.opacity_label = QLabel("100%")
        self.opacity_slider.valueChanged.connect(
            lambda v: self.opacity_label.setText(f"{v}%")
        )
        opacity_row.addWidget(self.opacity_slider)
        opacity_row.addWidget(self.opacity_label)
        style_layout.addRow(tr("Opacity:"), opacity_row)

        self.grid_check = QCheckBox(tr("Show grid"))
        self.grid_check.setChecked(False)
        style_layout.addRow("", self.grid_check)

        self.title_edit = QLineEdit(tr("Rose Diagram"))
        style_layout.addRow(tr("Title:"), self.title_edit)

        root.addWidget(style_group)

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
            QPushButton:hover { background-color: #3a7fc1; }
            QPushButton:pressed { background-color: #1f4f7a; }
        """)
        self.compute_btn.clicked.connect(self._on_compute)
        root.addWidget(self.compute_btn)
        root.addSpacing(8)

        # --- WebEngineView ---
        self.webview = QWebEngineView()
        self.webview.setMinimumHeight(380)   
        self.webview.setSizePolicy(
            QSizePolicy.Expanding, QSizePolicy.Expanding
        )
        self._setup_webchannel()
        self._load_html()
        root.addWidget(self.webview)

        # --- Export buttons ---
        export_group = QGroupBox(tr("Export"))
        export_layout = QHBoxLayout(export_group)
        for fmt in ["PNG", "JPG", "SVG", "CSV"]:
            btn = QPushButton(fmt)
            btn.setFixedHeight(25)
            btn.clicked.connect(lambda checked, f=fmt: self._on_export(f))
            export_layout.addWidget(btn)
        root.addWidget(export_group)

    def _on_compute(self):
        layer = self.layer_combo.currentLayer()
        if layer is None:
            self.show_error(tr("Please select a line layer."))
            return

        mode_map = {0: "count", 1: "length", 2: "frequency"}

        params = {
            "layer":     layer,
            "n_sectors": self.sectors_spin.value(),
            "mode":      mode_map[self.mode_combo.currentIndex()],
            "half_rose": self.half_rose_check.isChecked(),
            "color":     self._color,
            "opacity":   self.opacity_slider.value() / 100.0,
            "show_grid": self.grid_check.isChecked(),
            "title":     self.title_edit.text(),
            "min_rectitude": self.rectitude_spin.value(),
        }

        if not self._engine.validate(**params):
            self.show_error(tr("Invalid layer — please select a line layer."))
            return

        try:
            data = self._engine.compute(**params)
            # print("=== ENGINE OUTPUT ===")
            # print("azimuths:", data["azimuths"][:5])
            # print("values:",   data["values"][:5])
            # print("stats:",    data["stats"])
            # print("====================")
            self._on_result(data)
        except Exception as e:
            import traceback
            print("=== ENGINE ERROR ===")
            traceback.print_exc()
            self.show_error(str(e))

    def _on_result(self, data: dict):
        self._last_data = data   
        json_data = json.dumps(data)
        # Use double quotes wrapper to avoid conflicts with JSON content
        js = f'updatePlot({json.dumps(json_data)})'
        self.webview.page().runJavaScript(js)
    # ------------------------------------------------------------------
    # WebChannel setup
    # ------------------------------------------------------------------

    def _setup_webchannel(self):
        # Allow local file access — required for plotly.min.js
        settings = self.webview.settings()
        settings.setAttribute(QWebEngineSettings.LocalContentCanAccessFileUrls, True)
        settings.setAttribute(QWebEngineSettings.LocalContentCanAccessRemoteUrls, True)
        settings.setAttribute(QWebEngineSettings.AllowRunningInsecureContent, True)
        settings.setAttribute(QWebEngineSettings.JavascriptEnabled, True)

        self._bridge = RoseBridge(self)
        self._channel = QWebChannel()
        self._channel.registerObject("bridge", self._bridge)
        self.webview.page().setWebChannel(self._channel)

    def _load_html(self):
        web_dir = os.path.join(
            os.path.dirname(os.path.dirname(os.path.dirname(__file__))),
            "web"
        )
        html_path = os.path.join(web_dir, "rose.html")
        js_dir = os.path.join(web_dir, "js").replace("\\", "/")

        # Write a temp HTML with absolute paths next to rose.html
        with open(html_path, "r", encoding="utf-8") as f:
            html_content = f.read()

        html_content = html_content.replace(
            'src="js/qwebchannel.js"',
            f'src="file:///{js_dir}/qwebchannel.js"'
        ).replace(
            'src="js/plotly.min.js"',
            f'src="file:///{js_dir}/plotly.min.js"'
        ).replace(
            'src="js/bridge.js"',
            f'src="file:///{js_dir}/bridge.js"'
        )

        # Write resolved HTML to temp file and load it
        # temp_path = os.path.join(web_dir, "_rose_temp.html")
        # with open(temp_path, "w", encoding="utf-8") as f:
        #     f.write(html_content)

        # print("Loading temp HTML:", temp_path)
        self.webview.load(QUrl.fromLocalFile(html_path))
        self.webview.loadFinished.connect(self._on_load_finished)

    def _on_load_finished(self, ok):
        # print("Page loaded:", ok)
        
        self.webview.page().runJavaScript(
            "typeof Plotly !== 'undefined' ? 'Plotly OK' : 'Plotly MISSING'",
            lambda result: print("Plotly check:", result)
        )

        self.webview.page().runJavaScript(
            "typeof updatePlot !== 'undefined' ? 'updatePlot OK' : 'updatePlot MISSING'",
            lambda result: print("updatePlot check:", result)
        )

    # ------------------------------------------------------------------
    # Style helpers
    # ------------------------------------------------------------------

    def _pick_color(self):
        color = QColorDialog.getColor(QColor(self._color), self, tr("Petal color"))
        if color.isValid():
            self._color = color.name()
            self._update_color_preview()

    def _update_color_preview(self):
        self.color_preview.setStyleSheet(
            f"background-color: {self._color}; border: 1px solid #555;"
        )

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

        # Image formats
        ok, path, width, height = self._exporter.prepare_image_export(
            fmt_lower, parent=self
        )
        if not ok:
            return

        self._pending_export_path = path

        if fmt_lower == "svg":
            self.webview.page().runJavaScript("exportSvg()")
        else:
            plotly_fmt = "jpeg" if fmt_lower == "jpg" else fmt_lower
            self.webview.page().runJavaScript(
                f"exportImage('{plotly_fmt}', {width}, {height})"
            )

    def _csv_headers(self) -> list:
        return ["azimuth", "value"]

    def _build_csv_rows(self) -> list:
        if self._last_data is None:
            return []
        return [
            {"azimuth": az, "value": val}
            for az, val in zip(
                self._last_data["azimuths"],
                self._last_data["values"]
            )
        ]
        
    def _save_export(self, data_url: str) -> None:
        """Called by JS bridge after Plotly.toImage()."""
        self._exporter.save_image(data_url, self._pending_export_path)

