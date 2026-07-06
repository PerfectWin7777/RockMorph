# tools/swath/style_manager.py

"""
tools/swath/style_manager.py

CurveStyleManager — Swath-specific styles layout grouping our standardized 
RockMorphLineStyleRow briques.

Authors: RockMorph contributors / Tony
"""

from PyQt5.QtWidgets import QGridLayout, QLabel, QPushButton # type: ignore
from PyQt5.QtCore import pyqtSignal, QCoreApplication, Qt # type: ignore
from qgis.gui import QgsCollapsibleGroupBox  # type: ignore

from ...widgets.style_widgets import RockMorphLineStyleRow

def tr(message: str) -> str:
    return QCoreApplication.translate("RockMorph", message)

# Swath default colors and parameters
CURVE_DEFAULTS = {
    "mean":    {"color": "#2c3e50", "width": 1.5, "dash": "solid", "fill": False},
    "min":     {"color": "#3498db", "width": 1.0, "dash": "dot",   "fill": False},
    "max":     {"color": "#e74c3c", "width": 1.0, "dash": "dot",   "fill": True},
    "q1":      {"color": "#e67e22", "width": 1.0, "dash": "dash",  "fill": False},
    "q3":      {"color": "#e67e22", "width": 1.0, "dash": "dash",  "fill": True},
    "relief":  {"color": "#27ae60", "width": 1.5, "dash": "solid", "fill": True},
    "hyps":    {"color": "#8e44ad", "width": 1.0, "dash": "solid", "fill": False},
}


class CurveStyleManager(QgsCollapsibleGroupBox):
    """
    Manages the grid layout of all Curve Styles for the Swath Profile.
    """
    styles_changed = pyqtSignal()

    def __init__(self, curves: list, apply_callback: callable, parent=None):
        super().__init__(tr("Curve Styles"), parent)
        self.apply_callback = apply_callback
        self._widgets = {}
        self._build_ui(curves)

    def _build_ui(self, curves: list):
        grid = QGridLayout(self)
        grid.setContentsMargins(8, 8, 8, 8)
        grid.setSpacing(6)

        headers = [tr("Curve"), tr("Color"), tr("Width"), tr("Style"), tr("Fill")]
        for col, text in enumerate(headers):
            lbl = QLabel(text)
            lbl.setStyleSheet("font-size: 10px; font-weight: bold; color: #242222;")
            if col in (0, 1, 2, 3):
                lbl.setAlignment(Qt.AlignLeft | Qt.AlignVCenter)
            else:
                lbl.setAlignment(Qt.AlignCenter)
            grid.addWidget(lbl, 0, col)

    
        grid.setColumnStretch(0, 1)
        grid.setColumnStretch(1, 0)
        grid.setColumnStretch(2, 0)
        grid.setColumnStretch(3, 0)
        grid.setColumnStretch(4, 1)

  
        for row, (curve_id, label) in enumerate(curves, start=1):
            defaults = CURVE_DEFAULTS.get(curve_id, {})
            w = RockMorphLineStyleRow(curve_id, tr(label), defaults, self)
            w.styleChanged.connect(self.styles_changed.emit)
            self._widgets[curve_id] = w

            grid.addWidget(w.label, row, 0, Qt.AlignLeft | Qt.AlignVCenter)
            grid.addWidget(w.color_btn, row, 1, Qt.AlignLeft | Qt.AlignVCenter)
            grid.addWidget(w.width_spin, row, 2, Qt.AlignLeft | Qt.AlignVCenter)
            grid.addWidget(w.dash_combo, row, 3, Qt.AlignLeft | Qt.AlignVCenter)
            grid.addWidget(w.fill_check, row, 4, Qt.AlignCenter)

        self.apply_btn = QPushButton(tr("Apply Styles"))
        self.apply_btn.setFixedHeight(28)
        self.apply_btn.clicked.connect(self.apply_callback)
        grid.addWidget(self.apply_btn, len(curves) + 1, 0, 1, 5)

    def get_style(self, curve_id: str) -> dict:
        w = self._widgets.get(curve_id)
        if w:
            return w.get_style()
        return {}

    def get_all_styles(self) -> dict:
        return {cid: w.get_style() for cid, w in self._widgets.items()}

    def set_visible(self, curve_id: str, visible: bool):
        w = self._widgets.get(curve_id)
        if w:
            w.set_visible(visible)