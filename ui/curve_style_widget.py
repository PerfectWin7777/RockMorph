# ui/curve_style_widget.py
"""


CurveStyleWidget  — controls for a single curve (color, width, dash)
CurveStyleManager — manages a collection of CurveStyleWidgets

Reusable across all RockMorph tools that render Plotly line traces.
"""

from PyQt5.QtWidgets import ( # type: ignore
    QWidget, QHBoxLayout, QVBoxLayout, QFormLayout,
    QLabel, QPushButton, QDoubleSpinBox, QComboBox,
    QGroupBox, QSizePolicy,QColorDialog,QCheckBox,
    QGridLayout
)
from PyQt5.QtGui import QColor # type: ignore
from PyQt5.QtCore import Qt, pyqtSignal, QCoreApplication # type: ignore


def tr(message):
    return QCoreApplication.translate("RockMorph", message)


# Plotly dash styles
DASH_OPTIONS = [
    ("Solid",    "solid"),
    ("Dash",     "dash"),
    ("Dot",      "dot"),
    ("Dash-Dot", "dashdot"),
]

# Default styles per curve name
CURVE_DEFAULTS = {
    "mean":    {"color": "#2c3e50", "width": 1.5, "dash": "solid", "fill": False},
    "min":     {"color": "#3498db", "width": 1.0, "dash": "dot",   "fill": False},
    "max":     {"color": "#e74c3c", "width": 1.0, "dash": "dot",   "fill": True}, # Fills towards Min
    "q1":      {"color": "#e67e22", "width": 1.0, "dash": "dash",  "fill": False},
    "q3":      {"color": "#e67e22", "width": 1.0, "dash": "dash",  "fill": True}, # Fills towards Q1
    "relief":  {"color": "#27ae60", "width": 1.5, "dash": "solid", "fill": True}, # Fills to zero
    "hyps":    {"color": "#8e44ad", "width": 1.0, "dash": "solid", "fill": False},
}


class CurveStyleWidget(QWidget):
    """
    Controls for a single curve: color, line width, dash style.
    Emits style_changed when any control is modified.
    """

    style_changed = pyqtSignal()

    def __init__(self, curve_id: str, label: str, parent=None):
        super().__init__(parent)
        self.curve_id = curve_id

        defaults = CURVE_DEFAULTS.get(curve_id, {
            "color": "#333333",
            "width": 1.0,
            "dash":  "solid",
            "fill":  False
            
        })
        self._color = defaults["color"]
        self._create_widgets(label, defaults)

    # ------------------------------------------------------------------
    # Public API
    # ------------------------------------------------------------------

    def get_style(self) -> dict:
        """Return current style as dict for JSON serialization."""
        dash_idx = self.dash_combo.currentIndex()
        return {
            "color": self._color,
            "width": round(self.width_spin.value(), 1),
            "dash":  DASH_OPTIONS[dash_idx][1],
            "fill":  self.fill_check.isChecked()
        }

    def set_visible(self, visible: bool):
        """Show or hide this widget."""
        self.name_lbl.setVisible(visible)
        self.color_btn.setVisible(visible)
        self.width_spin.setVisible(visible)
        self.dash_combo.setVisible(visible)
        self.fill_check.setVisible(visible)

    # ------------------------------------------------------------------
    # UI
    # ------------------------------------------------------------------
    def _create_widgets(self, label: str, defaults: dict):
        """Create widgets — no layout here, Manager places them in grid."""

        # Name label
        self.name_lbl = QLabel(label)
        self.name_lbl.setStyleSheet("font-size: 11px;")

        # Color button
        self.color_btn = QPushButton()
        self.color_btn.setFixedSize(24, 22)
        self.color_btn.setCursor(Qt.PointingHandCursor)
        self.color_btn.setToolTip(tr("Click to change color"))
        self._refresh_color_btn()
        self.color_btn.clicked.connect(self._pick_color)

        # Width spinner
        self.width_spin = QDoubleSpinBox()
        self.width_spin.setRange(0.5, 6.0)
        self.width_spin.setSingleStep(0.5)
        self.width_spin.setValue(defaults["width"])
        self.width_spin.setDecimals(1)
        self.width_spin.setSuffix(" pt")
        self.width_spin.setToolTip(tr("Line width"))
        self.width_spin.valueChanged.connect(self.style_changed.emit)

        # Dash combo
        self.dash_combo = QComboBox()
        for lbl_dash, _ in DASH_OPTIONS:
            self.dash_combo.addItem(lbl_dash)
        default_dash = defaults.get("dash", "solid")
        for i, (_, val) in enumerate(DASH_OPTIONS):
            if val == default_dash:
                self.dash_combo.setCurrentIndex(i)
                break
        self.dash_combo.setToolTip(tr("Line style"))
        self.dash_combo.currentIndexChanged.connect(self.style_changed.emit)

        # Fill checkbox
        self.fill_check = QCheckBox()
        self.fill_check.setChecked(defaults.get("fill", False))
        self.fill_check.setToolTip(tr("Fill area under curve"))
        self.fill_check.stateChanged.connect(self.style_changed.emit)

    # ------------------------------------------------------------------
    # Private
    # ------------------------------------------------------------------

    def _pick_color(self):
        color = QColor(self._color)
        picked = QColorDialog.getColor(color, self, tr(f"Color — {self.curve_id}"))
        if picked.isValid():
            self._color = picked.name()
            self._refresh_color_btn()
            self.style_changed.emit()

    def _refresh_color_btn(self):
        self.color_btn.setStyleSheet(
            f"background-color: {self._color}; "
            f"border: 1px solid #888; border-radius: 3px;"
        )

class CurveStyleManager(QGroupBox):
    """
    Manages a collection of CurveStyleWidgets inside a QGroupBox.
    Provides get_all_styles() for panel → JSON → HTML pipeline.
    Emits styles_changed when any curve style is modified.
    """

    styles_changed = pyqtSignal()

    def __init__(self, curves: list, fct: callable, parent=None):
        """
        Parameters
        ----------
        curves : list of (curve_id, label) tuples
            e.g. [("mean", "Mean"), ("min", "Min"), ...]
        """
        super().__init__(tr("Curve Styles"), parent)
        self.fct = fct
        self._widgets = {}
        self._build_ui(curves)

    # ------------------------------------------------------------------
    # Public API
    # ------------------------------------------------------------------

    def get_style(self, curve_id: str) -> dict:
        """Get style dict for a single curve."""
        w = self._widgets.get(curve_id)
        if w is None:
            return CURVE_DEFAULTS.get(curve_id, {
                "color": "#333333",
                "width": 1.5,
                "dash":  "solid"
            })
        return w.get_style()

    def get_all_styles(self) -> dict:
        """Get all styles as a dict keyed by curve_id."""
        return {cid: w.get_style() for cid, w in self._widgets.items()}

    def set_visible(self, curve_id: str, visible: bool):
        """Show or hide a specific curve style widget."""
        w = self._widgets.get(curve_id)
        if w:
            w.set_visible(visible)

    # ------------------------------------------------------------------
    # UI
    # ------------------------------------------------------------------

    def _build_ui(self, curves: list):
        grid = QGridLayout(self)
        grid.setContentsMargins(8, 8, 8, 8)
        grid.setSpacing(4)

        # ── Header row ────────────────────────────────────────
        headers = ["Curve", "Color", "Width", "Style", "Fill"]
        for col, text in enumerate(headers):
            lbl = QLabel(tr(text))
            lbl.setStyleSheet("font-size: 10px; font-weight: bold; color: #888;")
            lbl.setAlignment(Qt.AlignCenter if col > 0 else Qt.AlignLeft | Qt.AlignVCenter)
            grid.addWidget(lbl, 0, col)

        # ── Column stretch ────────────────────────────────────
        grid.setColumnStretch(0, 2)   # Curve name — plus large
        grid.setColumnStretch(1, 1)   # Color
        grid.setColumnStretch(2, 2)   # Width
        grid.setColumnStretch(3, 2)   # Style
        grid.setColumnStretch(4, 1)   # Fill

        # ── One row per curve ─────────────────────────────────
        for row, (curve_id, label) in enumerate(curves, start=1):
            w = CurveStyleWidget(curve_id, tr(label))
            w.style_changed.connect(self.styles_changed.emit)
            self._widgets[curve_id] = w

            # Place each control in its column
            grid.addWidget(w.name_lbl,    row, 0, Qt.AlignVCenter)
            grid.addWidget(w.color_btn,   row, 1, Qt.AlignCenter)
            grid.addWidget(w.width_spin,  row, 2, Qt.AlignCenter)
            grid.addWidget(w.dash_combo,  row, 3, Qt.AlignCenter)
            grid.addWidget(w.fill_check,  row, 4, Qt.AlignCenter)
        
        self.apply_style_btn = QPushButton(tr("Apply styles"))
        self.apply_style_btn.setFixedHeight(28)
        self.apply_style_btn.clicked.connect(self.fct)
        
        grid.addWidget(self.apply_style_btn, len(curves) + 2, 0, 1, 5)
