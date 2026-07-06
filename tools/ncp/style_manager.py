# tools/ncp/style_manager.py

"""
tools/ncp/style_manager.py

NCPStyleWidget — Standardized style manager for the Normalized Channel Profile tool.
Uses QGIS-native RockMorphColorButton to ensure visual coherence.

Authors: RockMorph contributors / Tony
"""

from PyQt5.QtWidgets import QGridLayout, QLabel, QCheckBox, QDoubleSpinBox, QVBoxLayout, QWidget # type: ignore
from PyQt5.QtGui import QColor  # type: ignore 
from PyQt5.QtCore import pyqtSignal, QCoreApplication, Qt # type: ignore
from qgis.gui import QgsCollapsibleGroupBox  # type: ignore

from ...widgets.style_widgets import RockMorphColorButton

def tr(message: str) -> str:
    return QCoreApplication.translate("RockMorph", message)


class NCPStyleWidget(QgsCollapsibleGroupBox):
    """
    Control group for NCP visualization styles.
    Emits styleChanged when any parameter is modified.
    """
    styleChanged = pyqtSignal()

    def __init__(self, parent=None):
        super().__init__(tr("Visualization Style"), parent)
        
        # Default styling colors
        self._default_colors = {
            "diagonal": "#f0220b",
            "curve":    "#0631ef",
            "concave":  "#f1c40f",
            "convex":   "#e67e22",
            "basal":    "#a924b3"
        }
        self._build_ui()

    def _build_ui(self):
        grid = QGridLayout(self)
        grid.setSpacing(10)
        grid.setContentsMargins(10, 15, 10, 10)

        # ── 1. Equilibrium Line (Diagonal) ──
        self.diag_show = QCheckBox(tr("Show Diagonal"))
        self.diag_show.setChecked(True)
        self.diag_show.stateChanged.connect(self.styleChanged.emit)
        
        self.diag_color_btn = RockMorphColorButton()
        self.diag_color_btn.setColor(QColor(self._default_colors["diagonal"]))
        self.diag_color_btn.colorChanged.connect(self.styleChanged.emit)

        self.diag_width = QDoubleSpinBox()
        self.diag_width.setRange(0.5, 5.0)
        self.diag_width.setValue(1.0)
        self.diag_width.setSingleStep(0.1)
        self.diag_width.setSuffix(" pt")
        self.diag_width.setFixedWidth(65)
        self.diag_width.valueChanged.connect(self.styleChanged.emit)

        grid.addWidget(self.diag_show, 0, 0, 1, 1, Qt.AlignLeft | Qt.AlignVCenter)
        grid.addWidget(QLabel(tr("Color:")), 0, 1, Qt.AlignLeft | Qt.AlignVCenter)
        grid.addWidget(self.diag_color_btn, 0, 2, Qt.AlignLeft | Qt.AlignVCenter)
        grid.addWidget(QLabel(tr("Width:")), 0, 3, Qt.AlignLeft | Qt.AlignVCenter)
        grid.addWidget(self.diag_width, 0, 4, Qt.AlignLeft | Qt.AlignVCenter)

        # ── 2. NCP Main Curve ──
        self.curve_color_btn = RockMorphColorButton()
        self.curve_color_btn.setColor(QColor(self._default_colors["curve"]))
        self.curve_color_btn.colorChanged.connect(self.styleChanged.emit)

        self.curve_width = QDoubleSpinBox()
        self.curve_width.setRange(0.5, 5.0)
        self.curve_width.setValue(1.0)
        self.curve_width.setSingleStep(0.1)
        self.curve_width.setSuffix(" pt")
        self.curve_width.setFixedWidth(65)
        self.curve_width.valueChanged.connect(self.styleChanged.emit)

        grid.addWidget(QLabel(tr("Main Curve:")), 1, 0, 1, 1, Qt.AlignLeft | Qt.AlignVCenter)
        grid.addWidget(QLabel(tr("Color:")), 1, 1, Qt.AlignLeft | Qt.AlignVCenter)
        grid.addWidget(self.curve_color_btn, 1, 2, Qt.AlignLeft | Qt.AlignVCenter)
        grid.addWidget(QLabel(tr("Width:")), 1, 3, Qt.AlignLeft | Qt.AlignVCenter)
        grid.addWidget(self.curve_width, 1, 4, Qt.AlignLeft | Qt.AlignVCenter)

        # ── 3. Deviation Fill ──
        self.fill_dev_show = QCheckBox(tr("Enable Deviation Fill"))
        self.fill_dev_show.setChecked(True)
        self.fill_dev_show.stateChanged.connect(self.styleChanged.emit)

        self.color_concave = RockMorphColorButton()
        self.color_concave.setColor(QColor(self._default_colors["concave"]))
        self.color_concave.colorChanged.connect(self.styleChanged.emit)

        self.color_convex = RockMorphColorButton()
        self.color_convex.setColor(QColor(self._default_colors["convex"]))
        self.color_convex.colorChanged.connect(self.styleChanged.emit)

        grid.addWidget(self.fill_dev_show, 2, 0, 1, 1, Qt.AlignLeft | Qt.AlignVCenter)
        grid.addWidget(QLabel(tr("Concave:")), 2, 1, Qt.AlignLeft | Qt.AlignVCenter)
        grid.addWidget(self.color_concave, 2, 2, Qt.AlignLeft | Qt.AlignVCenter)
        grid.addWidget(QLabel(tr("Convex:")), 2, 3, Qt.AlignLeft | Qt.AlignVCenter)
        grid.addWidget(self.color_convex, 2, 4, Qt.AlignLeft | Qt.AlignVCenter)

        # ── 4. Basal Fill ──
        self.fill_basal_show = QCheckBox(tr("Enable Basal Fill"))
        self.fill_basal_show.setChecked(False)
        self.fill_basal_show.stateChanged.connect(self.styleChanged.emit)

        self.color_basal = RockMorphColorButton()
        self.color_basal.setColor(QColor(self._default_colors["basal"]))
        self.color_basal.colorChanged.connect(self.styleChanged.emit)

        grid.addWidget(self.fill_basal_show, 3, 0, 1, 1, Qt.AlignLeft | Qt.AlignVCenter)
        grid.addWidget(QLabel(tr("Color:")), 3, 1, Qt.AlignLeft | Qt.AlignVCenter)
        grid.addWidget(self.color_basal, 3, 2, Qt.AlignLeft | Qt.AlignVCenter)

        # ── 5. Labels & Annotations ──
        lbl_annot = QLabel(f"<b>{tr('Labels & Annotations')}</b>")
        lbl_annot.setStyleSheet("color: #555; margin-top: 5px;")
        grid.addWidget(lbl_annot, 4, 0, 1, 5)

        self.show_arrows_check = QCheckBox(tr("Show dL / MaxC arrows"))
        self.show_arrows_check.setChecked(True)
        self.show_arrows_check.stateChanged.connect(self.styleChanged.emit)

        self.show_info_box_check = QCheckBox(tr("Show top-right info box"))
        self.show_info_box_check.setChecked(True)
        self.show_info_box_check.stateChanged.connect(self.styleChanged.emit)

        grid.addWidget(self.show_arrows_check, 5, 0, 1, 3, Qt.AlignLeft | Qt.AlignVCenter)
        grid.addWidget(self.show_info_box_check, 5, 3, 1, 2, Qt.AlignLeft | Qt.AlignVCenter)

        # Force tight alignment to the left
        grid.setColumnStretch(0, 0)
        grid.setColumnStretch(1, 0)
        grid.setColumnStretch(2, 0)
        grid.setColumnStretch(3, 0)
        grid.setColumnStretch(4, 1)

    def get_style_dict(self):
        """Build and serialize the parameters mapping."""
        return {
            "diag_show":     self.diag_show.isChecked(),
            "diag_color":    self.diag_color_btn.color().name(),
            "diag_width":    round(self.diag_width.value(), 1),
            "curve_color":   self.curve_color_btn.color().name(),
            "curve_width":   round(self.curve_width.value(), 1),
            "fill_dev":      self.fill_dev_show.isChecked(),
            "color_concave": self.color_concave.color().name(),
            "color_convex":  self.color_convex.color().name(),
            "fill_basal":    self.fill_basal_show.isChecked(),
            "color_basal":   self.color_basal.color().name(),
            "show_arrows":   self.show_arrows_check.isChecked(),
            "show_info_box": self.show_info_box_check.isChecked()
        }