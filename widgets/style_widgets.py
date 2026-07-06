# rockmorph/widgets/style_widgets.py

"""
rockmorph/widgets/style_widgets.py

Standardized, QGIS-native style components for RockMorph panels.
Wraps qgis.gui elements to provide a seamless visual integration.

Authors: RockMorph contributors / Tony
"""

from PyQt5.QtWidgets import QWidget, QHBoxLayout, QDoubleSpinBox, QComboBox, QLabel, QCheckBox  # type: ignore
from PyQt5.QtCore import pyqtSignal, QCoreApplication, Qt  # type: ignore
from PyQt5.QtGui import QColor  # type: ignore
from qgis.gui import QgsColorButton  # type: ignore

def tr(message: str) -> str:
    return QCoreApplication.translate("RockMorph", message)


class RockMorphColorButton(QgsColorButton):
    """
    A unified QGIS-native color picker button with alpha channel support,
    context memory, and standard sizing.
    """
    def __init__(self, parent=None):
        super().__init__(parent)
        self.setAllowOpacity(True)
        self.setContext("RockMorph")
        self.setMinimumHeight(24)
        # self.setMaximumHeight(24)
        # self.setFixedWidth(36)


class RockMorphLineStyleRow(QWidget):
    """
    A compact horizontal row grouping a label, color picker (with opacity),
    line thickness spinbox, line dash combobox, and optional fill checkbox.
    Emits styleChanged when any styling property is modified.
    """
    styleChanged = pyqtSignal()

    def __init__(self, curve_id: str, label_text: str, defaults: dict, parent=None):
        """
        Parameters
        ----------
        curve_id : str
            Unique identifier of the curve/trace (e.g., 'mean', 'max').
        label_text : str
            Localized name of the curve shown in the UI.
        defaults : dict
            Default styles containing 'color', 'width', 'dash', and optionally 'fill'.
        """
        super().__init__(parent)
        self.curve_id = curve_id
        
        # Check properties support
        self._supports_fill = "fill" in defaults
        self._supports_dash = "dash" in defaults and defaults["dash"] is not None
        
        layout = QHBoxLayout(self)
        layout.setContentsMargins(0, 0, 0, 0)
        layout.setSpacing(6)
        
        # 1. Label
        self.label = QLabel(label_text)
        self.label.setStyleSheet("font-size: 11px;")
        layout.addWidget(self.label)
        
        # 2. QGIS-native Color button
        self.color_btn = RockMorphColorButton()
        self.color_btn.setColor(QColor(defaults.get("color", "#333333")))
        self.color_btn.colorChanged.connect(self.styleChanged.emit)
        layout.addWidget(self.color_btn)
        
        # 3. Width/Size spinbox
        self.width_spin = QDoubleSpinBox()
        self.width_spin.setRange(0.5, 20.0)
        self.width_spin.setSingleStep(0.5)
        self.width_spin.setValue(defaults.get("width", 1.0))
        self.width_spin.setDecimals(1)
        self.width_spin.setSuffix(" pt" if self._supports_dash else " px")
        self.width_spin.setFixedWidth(65)
        self.width_spin.valueChanged.connect(self.styleChanged.emit)
        layout.addWidget(self.width_spin)
        
        # 4. Dash style combobox (only if supported)
        self.dash_combo = QComboBox()
        self.dash_options = [
            ("Solid", "solid"),
            ("Dash", "dash"),
            ("Dot", "dot"),
            ("Dash-Dot", "dashdot")
        ]
        for label, value in self.dash_options:
            self.dash_combo.addItem(label, value)
        
        default_dash = defaults.get("dash", "solid")
        for i in range(self.dash_combo.count()):
            if self.dash_combo.itemData(i) == default_dash:
                self.dash_combo.setCurrentIndex(i)
                break
                
        self.dash_combo.setFixedWidth(80)
        self.dash_combo.currentIndexChanged.connect(self.styleChanged.emit)
        layout.addWidget(self.dash_combo)
        
        if not self._supports_dash:
            self.dash_combo.setVisible(False)
        
        # 5. Fill checkbox (only if supported)
        self.fill_check = QCheckBox()
        self.fill_check.setChecked(defaults.get("fill", False))
        self.fill_check.stateChanged.connect(self.styleChanged.emit)
        layout.addWidget(self.fill_check)
        
        if not self._supports_fill:
            self.fill_check.setVisible(False)

    def get_style(self) -> dict:
        """Serialize row settings to a dictionary ready for Plotly/JS."""
        return {
            "color": self.color_btn.color().name(),
            "opacity": self.color_btn.color().alphaF(),
            "width": round(self.width_spin.value(), 1),
            "dash": self.dash_combo.currentData() if self._supports_dash else None,
            "fill": self.fill_check.isChecked() if self._supports_fill else False
        }

    def set_visible(self, visible: bool):
        """Toggle visibility of the entire row components."""
        self.label.setVisible(visible)
        self.color_btn.setVisible(visible)
        self.width_spin.setVisible(visible)
        if self._supports_dash:
            self.dash_combo.setVisible(visible)
        if self._supports_fill:
            self.fill_check.setVisible(visible)