# tools/fluvial/style_manager.py

"""
tools/fluvial/style_manager.py

FluvialStyleWidget — Custom styling layout grouping our standardized 
RockMorphLineStyleRow components for the Fluvial tool.

Authors: RockMorph contributors / Tony
"""

from PyQt5.QtWidgets import QGridLayout, QLabel, QCheckBox, QHBoxLayout, QVBoxLayout, QWidget # type: ignore
from PyQt5.QtCore import pyqtSignal, QCoreApplication, Qt # type: ignore
from qgis.gui import QgsCollapsibleGroupBox  # type: ignore

from ...widgets.style_widgets import RockMorphLineStyleRow

def tr(message: str) -> str:
    return QCoreApplication.translate("RockMorph", message)

# Fluvial curve styling defaults
CURVE_DEFAULTS = {
    "z_profile":   {"color": "#1a5276", "width": 2.0, "dash": "solid"},
    "equil":       {"color": "#d35400", "width": 1.5, "dash": "dash"},
    "chi_curve":   {"color": "#148f77", "width": 2.0, "dash": "solid"},
    "sl_index":    {"color": "#1e8449", "width": 1.0, "dash": "solid"},
    "ksn_profile": {"color": "#884ea0", "width": 1.0, "dash": "solid"},
    "ksn_seg":     {"color": "#c0392b", "width": 3.0, "dash": "solid"},
    "knickpoint":  {"color": "#e74c3c", "width": 10.0} # No dash key implies point marker
}


class FluvialStyleWidget(QgsCollapsibleGroupBox):
    """
    Standardized styling layout for Fluvial profiles.
    """
    styleChanged = pyqtSignal()

    def __init__(self, parent=None):
        super().__init__(tr("Advanced Graph Styling"), parent)
        self._widgets = {}
        self._build_ui()

    def _build_ui(self):
        main_layout = QVBoxLayout(self)
        main_layout.setSpacing(6)

        # ── Grid Layout for standardized Style Rows ──
        grid_container = QWidget()
        grid = QGridLayout(grid_container)
        grid.setContentsMargins(0, 0, 0, 0)
        grid.setSpacing(6)

        # Columns Headers
        headers = [tr("Curve/Trace"), tr("Color"), tr("Width"), tr("Style")]
        for col, text in enumerate(headers):
            lbl = QLabel(text)
            lbl.setStyleSheet("font-size: 10px; font-weight: bold; color: ##242222;")
            lbl.setAlignment(Qt.AlignLeft | Qt.AlignVCenter)
            grid.addWidget(lbl, 0, col)

        grid.setColumnStretch(0, 3)
        grid.setColumnStretch(1, 1)
        grid.setColumnStretch(2, 2)
        grid.setColumnStretch(3, 2)

        # Fluvial specific style row keys & titles
        curves = [
            ("z_profile",   tr("Elevation (Z)")),
            ("equil",       tr("Equilibrium")),
            ("chi_curve",   tr("Chi Curve")),
            ("sl_index",    tr("SL Index")),
            ("ksn_profile", tr("kₛₙ Profile")),
            ("ksn_seg",     tr("kₛₙ Segments")),
            ("knickpoint",  tr("Knickpoints"))
        ]

        for row_idx, (curve_id, label) in enumerate(curves, start=1):
            defaults = CURVE_DEFAULTS.get(curve_id, {})
            w = RockMorphLineStyleRow(curve_id, label, defaults, self)
            w.styleChanged.connect(self.styleChanged.emit)
            self._widgets[curve_id] = w

            grid.addWidget(w.label, row_idx, 0, Qt.AlignLeft | Qt.AlignVCenter)
            grid.addWidget(w.color_btn, row_idx, 1, Qt.AlignLeft | Qt.AlignVCenter)
            grid.addWidget(w.width_spin, row_idx, 2, Qt.AlignLeft | Qt.AlignVCenter)
            grid.addWidget(w.dash_combo, row_idx, 3, Qt.AlignLeft | Qt.AlignVCenter)

        main_layout.addWidget(self.panel_container_or_widget(grid))

        # ── Grid Layout Options ──
        grid_opts_layout = QHBoxLayout()
        self.chk_x_grid = QCheckBox(tr("X Grid"))
        self.chk_x_grid.setChecked(True)
        self.chk_x_grid.stateChanged.connect(self.styleChanged.emit)
        
        self.chk_y_grid = QCheckBox(tr("Y Grid"))
        self.chk_y_grid.setChecked(True)
        self.chk_y_grid.stateChanged.connect(self.styleChanged.emit)

        grid_opts_layout.addWidget(self.chk_x_grid)
        grid_opts_layout.addWidget(self.chk_y_grid)
        main_layout.addLayout(grid_opts_layout)

    def panel_container_or_widget(self, layout):
        w = QWidget()
        w.setLayout(layout)
        return w

    def get_style_config(self) -> dict:
        config = {}
        for key, w in self._widgets.items():
            config[key] = w.get_style()
            
        config["layout"] = {
            "x_grid": self.chk_x_grid.isChecked(),
            "y_grid": self.chk_y_grid.isChecked()
        }
        return config