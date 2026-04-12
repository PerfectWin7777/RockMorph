# core/exporter.py

"""
core/exporter.py

RockMorphExporter — centralized export engine for all RockMorph tools.

Handles:
  - Resolution dialog (presets + custom DPI/size)
  - File path dialog (per format)
  - Image writing (PNG, JPG, SVG) from Plotly dataURL
  - CSV and JSON export
  - QGIS message bar feedback

Design
------
Each panel calls a single method:

    self._exporter.export_from_webview(self.webview, fmt, self._last_data, parent=self)

The exporter owns the full export pipeline:
    1. Ask resolution (for image formats)
    2. Ask file path
    3. Trigger JS export on the webview
    4. Receive dataURL via callback
    5. Write to disk

For CSV/JSON, the panel passes the data dict directly:

    self._exporter.export_tabular(data, fmt, parent=self)

Authors : RockMorph contributors /Tony winter 

"""

import os
import csv
import base64
import math
import json as json_module
from urllib.parse import unquote

from PyQt5.QtWidgets import ( # type: ignore
    QDialog, QVBoxLayout, QHBoxLayout, QFormLayout,
    QLabel, QComboBox, QSpinBox, QPushButton,
    QDialogButtonBox, QGroupBox, QFileDialog,
    QSizePolicy
)
from PyQt5.QtCore import QCoreApplication, Qt, QByteArray,QSizeF  # type: ignore
from PyQt5.QtGui import QFont, QImage, QPainter            # type: ignore
from PyQt5.QtSvg import QSvgRenderer                       # type: ignore
from PyQt5.QtPrintSupport import QPrinter  # type: ignore
            

def tr(message):
    return QCoreApplication.translate("RockMorph", message)


# DPI presets — replaces pixel resolution presets
DPI_PRESETS = [
    (tr("Screen  —  96 dpi"),  96,  tr("Good for presentations and web embedding.")),
    (tr("Draft   — 150 dpi"), 150,  tr("Suitable for reports. Internal documents.")),
    (tr("Print   — 300 dpi"), 300,  tr("Standard publication quality.")),
    (tr("High    — 600 dpi"), 600,  tr("Highest print quality. Journal submission.")),
    (tr("Custom…"),            -1,  tr("Define your own DPI.")),
]

# File dialog filters per format
FORMAT_FILTERS = {
    "png":  "PNG Image (*.png)",
    "jpg":  "JPEG Image (*.jpg)",
    "jpeg": "JPEG Image (*.jpg)",
    "svg":  "SVG Vector (*.svg)",
    "pdf":  "PDF Document (*.pdf)",
    "csv":  "CSV Spreadsheet (*.csv)",
    "json": "JSON File (*.json)",
}


# ---------------------------------------------------------------------------
# Resolution dialog
# ---------------------------------------------------------------------------

class ResolutionDialog(QDialog):
    """
    Professional resolution picker dialog.

    Shows a list of presets_dpi with descriptions.
    Falls back to a custom dpi spinner if the user selects "Custom".

    Returns selected DPI via ResolutionDialog.result_dpi attribute.
    """

    def __init__(self, parent=None):
        super().__init__(parent)
        self.setWindowTitle(tr("Export Quality"))
        self.setMinimumWidth(380)
        self.result_dpi = 300
        self._build_ui()

    def _build_ui(self):
        root = QVBoxLayout(self)
        root.setSpacing(12)

        group        = QGroupBox(tr("Resolution"))
        group_layout = QFormLayout(group)

        self.combo = QComboBox()
        for label, dpi, _ in DPI_PRESETS:
            self.combo.addItem(label, dpi)
        self.combo.setCurrentIndex(2)   # 300 dpi default
        self.combo.currentIndexChanged.connect(self._on_changed)
        group_layout.addRow(tr("Quality:"), self.combo)

        self.desc_label = QLabel(DPI_PRESETS[2][2])
        self.desc_label.setWordWrap(True)
        self.desc_label.setStyleSheet("color: #666; font-size: 12px;")
        group_layout.addRow("", self.desc_label)

        self.custom_spin = QSpinBox()
        self.custom_spin.setRange(72, 2000)
        self.custom_spin.setValue(300)
        self.custom_spin.setSingleStep(10)
        self.custom_spin.setSuffix(" dpi")
        self.custom_spin.setVisible(False)
        self.custom_spin.valueChanged.connect(self._on_spin_changed)
        group_layout.addRow(" ", self.custom_spin)

        self.result_label = QLabel(" 300 dpi")
        bold = QFont()
        bold.setBold(True)
        self.result_label.setFont(bold)
        group_layout.addRow("Selected DPI:", self.result_label)

        root.addWidget(group)

        buttons = QDialogButtonBox(
            QDialogButtonBox.Ok | QDialogButtonBox.Cancel
        )
        buttons.accepted.connect(self._on_accept)
        buttons.rejected.connect(self.reject)
        root.addWidget(buttons)

    def _on_changed(self, idx):
        _, dpi, desc = DPI_PRESETS[idx]
        self.desc_label.setText(desc)
        is_custom = (dpi == -1)
        self.custom_spin.setVisible(is_custom)
        if not is_custom:
            self.result_label.setText(f" {dpi} dpi")
        else:
            self.result_label.setText(f" {self.custom_spin.value()} dpi")

    def _on_spin_changed(self, val):
        self.result_label.setText(f" {val} dpi")

    def _on_accept(self):
        idx       = self.combo.currentIndex()
        _, dpi, _ = DPI_PRESETS[idx]
        self.result_dpi = self.custom_spin.value() if dpi == -1 else dpi
        self.accept()


# ---------------------------------------------------------------------------
# Main exporter
# ---------------------------------------------------------------------------

class RockMorphExporter:
    """
    Centralized export engine for all RockMorph tool panels.

    Usage — image export (triggered from panel):
    --------------------------------------------
        # Step 1: panel calls this to get path + resolution
        ok, path, dpi = self._exporter.prepare_image_export(fmt, parent=self)
        if not ok:
            return

        # Step 2: panel triggers JS
        div_id = 'plot-main'   # div ID for plotly 
        self.webview.page().runJavaScript(f"exportViaSvg('{div_id}')")

        # Step 3: JS calls back bridge.receive_export(svg_data_url)
        # Step 4: bridge calls panel._save_export(svg_data_url)
        # Step 5: panel calls:
        self._exporter.save_image(svg_data_url, path, dpi)

    Usage — tabular export:
    -----------------------
        self._exporter.export_csv(rows, headers, parent=self)
        self._exporter.export_json(data, parent=self)
    """

    def __init__(self, iface):
        self.iface = iface
    
    # ------------------------------------------------------------------
    # Image export pipeline
    # ------------------------------------------------------------------

    def prepare_image_export(self, fmt: str, parent=None) -> tuple:
        """
        Show resolution dialog then file path dialog.

        Parameters
        ----------
        fmt    : str   — 'png' | 'jpg' | 'svg'
        parent : QWidget

        Returns
        -------
        (ok, path, dpi) where
        ok=False if user cancelled at any step.
        SVG/JSON ignores resolution  — returns (ok, path, 96).
        """
        # SVG is vector — skip resolution dialog. Same thing with json
        if fmt in ("svg", "json"):
            path = self._ask_path(fmt, parent)
            if not path:
                return False, "", 96
            return True, path, 96

        # Ask resolution
        dialog = ResolutionDialog(parent)
        if dialog.exec_() != QDialog.Accepted:
            return False, "", 0
        
        dpi  = dialog.result_dpi

        # Security check for high-res
        if dpi > 1000:
            self._info(tr("Extremely high resolution over than 1000 dpi."))
            self._info(tr("This may take time or crash the view."))

        # Ask file path
        path = self._ask_path(fmt, parent)
        if not path:
            return False, "", 0 

        return True, path, dpi

    # REMPLACER save_image entièrement par :

    def save_image(self, svg_data_url: str, path: str, dpi: int = 300) -> None:
        """
        Convert Plotly SVG dataURL to final format.
        Uses QSvgRenderer for PNG/JPG/PDF — no DOM manipulation.
        cairosvg used automatically if installed (better quality).

        Parameters
        ----------
        svg_data_url : str — SVG dataURL from Plotly.toImage(format='svg')
        path         : str — output file path
        dpi          : int — for raster formats
        """
        try:
            fmt       = os.path.splitext(path)[1].lower().lstrip('.')
            svg_bytes = self._extract_svg_bytes(svg_data_url)

            if fmt == 'svg':
                self._write_svg_bytes(svg_bytes, path)
            elif fmt == 'png':
                self._write_png(svg_bytes, path, dpi)
            elif fmt in ('jpg', 'jpeg'):
                self._write_jpg(svg_bytes, path, dpi)
            elif fmt == 'pdf':
                self._write_pdf(svg_bytes, path)
            else:
                self._error(tr(f"Unsupported format: {fmt}"))
        except Exception as e:
            self._error(tr(f"Export failed: {e}"))

    # ------------------------------------------------------------------
    # Tabular export
    # ------------------------------------------------------------------

    def export_csv(self, rows: list, headers: list, parent=None) -> None:
        """
        Show save dialog and write rows to CSV.

        Parameters
        ----------
        rows    : list of dicts — one dict per row
        headers : list of str  — column order and names
        parent  : QWidget
        """
        path = self._ask_path("csv", parent)
        if not path:
            return
        self.save_csv(rows, headers, path)

    def save_csv(self, rows: list, headers: list, path: str) -> None:
        """Write rows to CSV at path (no dialog)."""
        try:
            with open(path, "w", newline="", encoding="utf-8") as f:
                writer = csv.DictWriter(f, fieldnames=headers)
                writer.writeheader()
                writer.writerows(rows)
            self._info(tr(f"CSV → {os.path.basename(path)}"))
        except Exception as e:
            self._error(tr(f"CSV export failed: {e}"))

    def export_json(self, data: dict, parent=None) -> None:
        """Show save dialog and write data to JSON."""
        path = self._ask_path("json", parent)
        if not path:
            return
        self.save_json(data, path)

    def save_json(self, data: dict, path: str) -> None:
        """Write data to JSON at path (no dialog)."""
        try:
            with open(path, "w", encoding="utf-8") as f:
                json_module.dump(data, f, indent=2, ensure_ascii=False)
            self._info(tr(f"JSON → {os.path.basename(path)}"))
        except Exception as e:
            self._error(tr(f"JSON export failed: {e}"))

    # ------------------------------------------------------------------
    # Internal — file dialog
    # ------------------------------------------------------------------

    def _ask_path(self, fmt: str, parent=None) -> str:
        """
        Show a save file dialog for the given format.
        Returns the chosen path or empty string if cancelled.
        """
        fmt_key   = fmt.lower().replace("jpeg", "jpg")
        filter_   = FORMAT_FILTERS.get(fmt_key, f"{fmt.upper()} (*.{fmt})")
        caption   = tr(f"Export {fmt.upper()}")
        path, _   = QFileDialog.getSaveFileName(parent, caption, "", filter_)
        return path


    # ------------------------------------------------------------------
    # Internal — writers
    # ------------------------------------------------------------------
    

    def _write_svg_bytes(self, svg_bytes: bytes, path: str) -> None:
        with open(path, 'wb') as f:
            f.write(svg_bytes)
        self._info(tr(f"SVG → {os.path.basename(path)}"))

    def _write_png(self, svg_bytes: bytes, path: str, dpi: int) -> None:
        try:
            import cairosvg  # type: ignore
            cairosvg.svg2png(bytestring=svg_bytes, write_to=path, dpi=dpi)
            self._info(tr(f"PNG {dpi}dpi → {os.path.basename(path)}"))
            return
        except ImportError:
            pass
        image = self._rasterize_qt(svg_bytes, dpi)
        if image is None:
            self._error(tr("SVG rasterization failed."))
            return
        image.save(path, 'PNG')
        self._info(tr(f"PNG {dpi}dpi → {os.path.basename(path)}"))

    def _write_jpg(self, svg_bytes: bytes, path: str, dpi: int) -> None:
        try:
            import cairosvg  # type: ignore
            import io
            from PIL import Image  # type: ignore
            png_bytes = cairosvg.svg2png(bytestring=svg_bytes, dpi=dpi)
            img = Image.open(io.BytesIO(png_bytes)).convert('RGB')
            img.save(path, 'JPEG', quality=95, dpi=(dpi, dpi))
            self._info(tr(f"JPG {dpi}dpi → {os.path.basename(path)}"))
            return
        except ImportError:
            pass
        image = self._rasterize_qt(svg_bytes, dpi)
        if image is None:
            self._error(tr("SVG rasterization failed."))
            return
        image.save(path, 'JPEG', 90)
        self._info(tr(f"JPG {dpi}dpi → {os.path.basename(path)}"))

    def _write_pdf(self, svg_bytes: bytes, path: str) -> None:
        try:
            import cairosvg  # type: ignore
            cairosvg.svg2pdf(bytestring=svg_bytes, write_to=path)
            self._info(tr(f"PDF → {os.path.basename(path)}"))
            return
        except ImportError:
            pass
        try:
            
            renderer = QSvgRenderer()
            renderer.load(QByteArray(svg_bytes))
            if not renderer.isValid():
                self._error(tr("Invalid SVG for PDF export."))
                return
            printer = QPrinter(QPrinter.HighResolution)
            printer.setOutputFormat(QPrinter.PdfFormat)
            printer.setOutputFileName(path)
            sz = renderer.defaultSize()
            printer.setPageSizeMM(QSizeF(
                sz.width()  * 0.2646,
                sz.height() * 0.2646
            ))
            painter = QPainter(printer)
            renderer.render(painter)
            painter.end()
            self._info(tr(f"PDF → {os.path.basename(path)}"))
        except Exception as e:
            self._error(tr(f"PDF export failed: {e}"))

       
    def _rasterize_qt(self, svg_bytes: bytes, dpi: int) -> QImage: 
        """Rasterize SVG to QImage using QSvgRenderer."""
        renderer = QSvgRenderer()
        renderer.load(QByteArray(svg_bytes)) 
        if not renderer.isValid():
            return None
        scale = dpi / 96.0
        sz    = renderer.defaultSize()
        w     = int(sz.width()  * scale)
        h     = int(sz.height() * scale)
        image = QImage(w, h, QImage.Format_ARGB32)
        image.fill(Qt.white)
        image.setDotsPerMeterX(int(dpi / 0.0254))
        image.setDotsPerMeterY(int(dpi / 0.0254))
        painter = QPainter(image)
        painter.setRenderHint(QPainter.Antialiasing,             True)
        painter.setRenderHint(QPainter.TextAntialiasing,         True)
        painter.setRenderHint(QPainter.SmoothPixmapTransform,    True)
        renderer.render(painter)
        painter.end()
        return image

    @staticmethod
    def _extract_svg_bytes(data_url: str) -> bytes:
        """Extract raw SVG bytes from a Plotly SVG dataURL."""
        if ',' not in data_url:
            raise ValueError(tr("Invalid dataURL."))
        header, payload = data_url.split(',', 1)
        if 'base64' in header:
            rem = len(payload) % 4
            if rem:
                payload += '=' * (4 - rem)
            return base64.b64decode(payload)
        return unquote(payload).encode('utf-8')
    
    
  

    # ------------------------------------------------------------------
    # Internal — QGIS message bar
    # ------------------------------------------------------------------

    def _info(self, message: str) -> None:
        self.iface.messageBar().pushInfo("RockMorph", message)

    def _error(self, message: str) -> None:
        self.iface.messageBar().pushWarning("RockMorph", message)