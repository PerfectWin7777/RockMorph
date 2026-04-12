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
from PyQt5.QtCore import QCoreApplication, Qt # type: ignore
from PyQt5.QtGui import QFont # type: ignore


def tr(message):
    return QCoreApplication.translate("RockMorph", message)


# ---------------------------------------------------------------------------
# Resolution presets
# Each preset defines a (label, width_px, height_px, dpi, description) tuple.
# width/height are the Plotly toImage dimensions.
# dpi is informational — shown to the user for context.
# ---------------------------------------------------------------------------

RESOLUTION_PRESETS = [
    (
        tr("Screen — 720p"),
        1280, 720, 96,
        tr("Good for presentations and web embedding.")
    ),
    (
        tr("Screen — 1080p"),
        1920, 1080, 96,
        tr("High-resolution screen export.")
    ),
    (
        tr("Print — A4 landscape, 150 DPI"),
        1754, 1240, 150,
        tr("A4 landscape at 150 DPI. Suitable for reports.")
    ),
    (
        tr("Print — A4 portrait, 300 DPI"),
        2480, 3508, 300,
        tr("A4 portrait at 300 DPI. Publication quality.")
    ),
    (
        tr("Print — A4 landscape, 300 DPI"),
        3508, 2480, 300,
        tr("A4 landscape at 300 DPI. Journal-ready.")
    ),
    (
        tr("Print — A4 portrait, 600 DPI"),
        4961, 7016, 600,
        tr("A4 portrait at 600 DPI. Highest print quality.")
    ),
    (
        tr("Print — A4 landscape, 600 DPI"),
        7016, 4961, 600,
        tr("A4 landscape at 600 DPI. Highest print quality.")
    ),
    (
        tr("Custom…"),
        -1, -1, -1,
        tr("Define your own width and height in pixels.")
    ),
]

# File dialog filters per format
FORMAT_FILTERS = {
    "png":  "PNG Image (*.png)",
    "jpg":  "JPEG Image (*.jpg)",
    "jpeg": "JPEG Image (*.jpg)",
    "svg":  "SVG Vector (*.svg)",
    "csv":  "CSV Spreadsheet (*.csv)",
    "json": "JSON File (*.json)",
}


# ---------------------------------------------------------------------------
# Resolution dialog
# ---------------------------------------------------------------------------

class ResolutionDialog(QDialog):
    """
    Professional resolution picker dialog.

    Shows a list of presets with descriptions.
    Falls back to a custom width/height spinner if the user selects "Custom".

    Returns (width, height) in pixels via .result_size attribute.
    """

    def __init__(self, parent=None):
        super().__init__(parent)
        self.setWindowTitle(tr("Export Resolution"))
        self.setMinimumWidth(400)
        self.result_size = None
        self._build_ui()

    def _build_ui(self):
        root = QVBoxLayout(self)
        root.setSpacing(12)

        # ── Preset selector ───────────────────────────────────
        preset_group = QGroupBox(tr("Preset"))
        preset_layout = QFormLayout(preset_group)

        self.preset_combo = QComboBox()
        for label, w, h, dpi, desc in RESOLUTION_PRESETS:
            self.preset_combo.addItem(label)
        self.preset_combo.currentIndexChanged.connect(self._on_preset_changed)
        preset_layout.addRow(tr("Format:"), self.preset_combo)

        self.desc_label = QLabel()
        self.desc_label.setWordWrap(True)
        self.desc_label.setStyleSheet("color: #666; font-size: 10px;")
        preset_layout.addRow("", self.desc_label)

        root.addWidget(preset_group)

        # ── Resolution info ───────────────────────────────────
        info_group = QGroupBox(tr("Output size"))
        info_layout = QFormLayout(info_group)

        self.size_label = QLabel()
        bold = QFont()
        bold.setBold(True)
        self.size_label.setFont(bold)
        info_layout.addRow(tr("Pixels:"), self.size_label)

        self.dpi_label = QLabel()
        self.dpi_label.setStyleSheet("color: #666;")
        info_layout.addRow(tr("DPI:"), self.dpi_label)

        root.addWidget(info_group)

        # ── Custom size (hidden by default) ───────────────────
        self.custom_group = QGroupBox(tr("Custom size"))
        custom_layout = QFormLayout(self.custom_group)

        self.custom_w = QSpinBox()
        self.custom_w.setRange(100, 10000)
        self.custom_w.setValue(1920)
        self.custom_w.setSuffix(" px")
        self.custom_w.valueChanged.connect(self._update_custom_info)
        custom_layout.addRow(tr("Width:"), self.custom_w)

        self.custom_h = QSpinBox()
        self.custom_h.setRange(100, 10000)
        self.custom_h.setValue(1080)
        self.custom_h.setSuffix(" px")
        self.custom_h.valueChanged.connect(self._update_custom_info)
        custom_layout.addRow(tr("Height:"), self.custom_h)

        self.custom_group.setVisible(False)
        root.addWidget(self.custom_group)

        # ── Buttons ───────────────────────────────────────────
        buttons = QDialogButtonBox(
            QDialogButtonBox.Ok | QDialogButtonBox.Cancel
        )
        buttons.accepted.connect(self._on_accept)
        buttons.rejected.connect(self.reject)
        root.addWidget(buttons)

        # Initialize display
        self._on_preset_changed(0)

    def _on_preset_changed(self, index: int):
        label, w, h, dpi, desc = RESOLUTION_PRESETS[index]
        self.desc_label.setText(desc)
        is_custom = (w == -1)
        self.custom_group.setVisible(is_custom)

        if is_custom:
            self._update_custom_info()
        else:
            self.size_label.setText(f"{w} × {h} px")
            self.dpi_label.setText(
                f"{dpi} DPI" if dpi > 0 else "—"
            )

    def _update_custom_info(self):
        w = self.custom_w.value()
        h = self.custom_h.value()
        self.size_label.setText(f"{w} × {h} px")
        self.dpi_label.setText("—")

    def _on_accept(self):
        index = self.preset_combo.currentIndex()
        _, w, h, dpi, _ = RESOLUTION_PRESETS[index]
        if w == -1:
            self.result_size = (self.custom_w.value(), self.custom_h.value())
        else:
            self.result_size = (w, h)
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
        ok, path, width, height = self._exporter.prepare_image_export(fmt, parent=self)
        if not ok:
            return

        # Step 2: panel triggers JS
        self.webview.page().runJavaScript(
            f"exportImage('{fmt}', {width}, {height})"
        )

        # Step 3: JS calls back bridge.receive_export(dataUrl)
        # Step 4: bridge calls panel._save_export(dataUrl)
        # Step 5: panel calls:
        self._exporter.save_image(data_url, path)

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
        (ok, path, width, height)
        ok=False if user cancelled at any step.
        SVG ignores resolution (vector format) — returns (ok, path, 1200, 800).
        """
        # SVG is vector — skip resolution dialog
        if fmt == "svg":
            path = self._ask_path(fmt, parent)
            if not path:
                return False, "", 1200, 800
            return True, path, 1200, 800

        # Ask resolution
        dialog = ResolutionDialog(parent)
        if dialog.exec_() != QDialog.Accepted:
            return False, "", 0, 0
        width, height = dialog.result_size

        # Security check for high-res
        if width > 5000 or height > 5000:
            self._info(tr("Extremely high resolution may take time or crash the view."))

        # Ask file path
        path = self._ask_path(fmt, parent)
        if not path:
            return False, "", 0, 0

        return True, path, width, height

    def save_image(self, data_url: str, path: str) -> None:
        """
        Write a Plotly-generated dataURL to disk.
        Handles PNG, JPG and both SVG encodings (base64 and raw utf-8).

        Parameters
        ----------
        data_url : str — dataURL string from Plotly.toImage()
        path     : str — absolute output file path
        """
        try:
            fmt, payload = self._parse_data_url(data_url)
            if fmt == "svg":
                self._write_svg(payload, path)
            else:
                self._write_binary(payload, path)
            self._info(tr(f"Exported → {os.path.basename(path)}"))
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
    # Internal — dataURL parsing
    # ------------------------------------------------------------------

    def _parse_data_url(self, data_url: str) -> tuple:
        """
        Parse a Plotly dataURL into (format, payload).

        Supported formats
        -----------------
        data:image/png;base64,<data>
        data:image/jpeg;base64,<data>
        data:image/svg+xml;base64,<data>
        data:image/svg+xml;utf8,<rawsvg>
        data:image/svg+xml;charset=utf-8,<rawsvg>
        """
        if "," not in data_url:
            raise ValueError(tr("Invalid dataURL — missing comma separator."))

        header, payload = data_url.split(",", 1)

        # Detect format
        if "svg" in header:
            fmt = "svg"
        elif "jpeg" in header or "jpg" in header:
            fmt = "jpg"
        else:
            fmt = "png"

         # Detect encoding
        is_base64 = "base64" in header
        return fmt, (payload, is_base64)

    # ------------------------------------------------------------------
    # Internal — writers
    # ------------------------------------------------------------------


    def _write_svg(self, payload: tuple, path: str) -> None:
        """Write SVG — handles both base64 and raw utf8 payloads."""
        raw, is_base64 = payload
        if is_base64:
            # Pad base64 if needed
            raw    = self._pad_base64(raw)
            data   = base64.b64decode(raw)
            mode, kw = "wb", {}
        else:
            data   = unquote(raw)
            mode, kw = "w", {"encoding": "utf-8"}

        with open(path, mode, **kw) as f:
            f.write(data)
    
    
    def _write_binary(self, payload: tuple, path: str) -> None:
        """Write PNG or JPG from base64 payload."""
        raw, is_base64 = payload
        if not is_base64:
            raise ValueError(tr("Expected base64 data for binary image format."))
        raw   = self._pad_base64(raw)
        data  = base64.b64decode(raw)
        with open(path, "wb") as f:
            f.write(data)
    
    @staticmethod
    def _pad_base64(raw: str) -> str:
        """Add missing base64 padding if needed."""
        remainder = len(raw) % 4
        if remainder:
            raw += "=" * (4 - remainder)
        return raw

    # ------------------------------------------------------------------
    # Internal — QGIS message bar
    # ------------------------------------------------------------------

    def _info(self, message: str) -> None:
        self.iface.messageBar().pushInfo("RockMorph", message)

    def _error(self, message: str) -> None:
        self.iface.messageBar().pushWarning("RockMorph", message)