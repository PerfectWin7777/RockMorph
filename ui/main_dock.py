# ui/main_dock.py

from qgis.PyQt.QtWidgets import ( # type: ignore
    QDockWidget, QWidget, QVBoxLayout,
    QHBoxLayout, QLabel, QComboBox
)
from qgis.PyQt.QtCore import Qt, QCoreApplication # type: ignore

from ..tools.rose.panel import RosePanel
from ..tools.swath.panel import SwathPanel
from ..tools.hypsometry.panel import HypsometryPanel
from ..tools.ncp.panel import NCPPanel
from ..tools.fluvial.panel import FluvialPanel


def tr(message):
    return QCoreApplication.translate("RockMorph", message)


# Registry — add new tools here only
TOOLS = [
    ("Rose Diagram",   RosePanel),
    ("Swath Profile",  SwathPanel),
    ("Hypsometry",      HypsometryPanel),
    ("Normalized Channel Profile", NCPPanel),
    ("Fluvial Toolbox", FluvialPanel)
]


class RockMorphDock(QDockWidget):
    """
    Main dock widget.
    Owns the tool selector (QComboBox) and the active panel.
    """

    def __init__(self, iface, parent=None):
        super().__init__(tr("RockMorph"), parent)
        self.iface = iface
        self._panels = {}          # cache — panel instances keyed by index
        self.setMinimumWidth(420)
        self._build_ui()

    def _build_ui(self):
        container = QWidget()
        root = QVBoxLayout(container)
        root.setContentsMargins(0, 0, 0, 0)
        root.setSpacing(0)

        # --- Header bar : title + combo ---
        header = QWidget()
        header.setFixedHeight(40)
        header.setStyleSheet("background-color: #2b2b2b;")
        header_layout = QHBoxLayout(header)
        header_layout.setContentsMargins(10, 0, 10, 0)

        title_label = QLabel("⛰ RockMorph")
        title_label.setStyleSheet("color: #fff; font-weight: bold; font-size: 13px;")
        header_layout.addWidget(title_label)

        header_layout.addStretch()

        self.tool_combo = QComboBox()
        self.tool_combo.setFixedWidth(160)
        self.tool_combo.setStyleSheet("""
            QComboBox {
                background-color: #3a3a3a;
                color: #ccc;
                border: 1px solid #555;
                border-radius: 4px;
                padding: 2px 8px;
            }
            QComboBox::drop-down { border: none; }
            QComboBox QAbstractItemView {
                background-color: #2b2b2b;
                color: #ccc;
                selection-background-color: #3a7fc1;
            }
        """)
        for name, _ in TOOLS:
            self.tool_combo.addItem(tr(name))
        self.tool_combo.currentIndexChanged.connect(self._switch_tool)
        header_layout.addWidget(self.tool_combo)

        root.addWidget(header)

        # --- Panel container ---
        self.panel_container = QWidget()
        self.panel_layout = QVBoxLayout(self.panel_container)
        self.panel_layout.setContentsMargins(0, 0, 0, 0)
        root.addWidget(self.panel_container)

        self.setWidget(container)

        # Load first tool
        self.tool_combo.setCurrentIndex(4)
        self._switch_tool(4)

    def _switch_tool(self, index: int):
        """
        Lazily instantiates panels — only created when first selected.
        Removes current panel from layout and inserts the new one.
        """
        # Remove current panel from layout
        while self.panel_layout.count():
            item = self.panel_layout.takeAt(0)
            if item.widget():
                item.widget().hide()

        # Lazy instantiation
        if index not in self._panels:
            _, PanelClass = TOOLS[index]
            panel = PanelClass(self.iface, self.panel_container)
            self._panels[index] = panel

        panel = self._panels[index]
        self.panel_layout.addWidget(panel)
        panel.show()