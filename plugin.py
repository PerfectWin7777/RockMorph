# plugin.py

from qgis.PyQt.QtWidgets import QAction # type: ignore
from qgis.PyQt.QtCore import Qt, QCoreApplication # type: ignore
from .ui.main_dock import RockMorphDock


def tr(message):
    return QCoreApplication.translate("RockMorph", message)


class RockMorphPlugin:

    def __init__(self, iface):
        self.iface = iface
        self.dock = None

    def initGui(self):
        self.action = QAction(tr("Open RockMorph"), self.iface.mainWindow())
        self.action.triggered.connect(self.run)
        self.iface.addToolBarIcon(self.action)

    def unload(self):
        self.iface.removeToolBarIcon(self.action)
        if self.dock:
           # Cleanup all panels before removing dock
            for panel in self.dock._panels.values():
                if hasattr(panel, 'cleanup'):
                    print(f"Cleaning up panel: {type(panel).__name__}")
                    panel.cleanup()
            self.iface.removeDockWidget(self.dock)
            self.dock = None

    def run(self):
        if self.dock is None:
            self.dock = RockMorphDock(self.iface, self.iface.mainWindow())
            self.iface.addDockWidget(Qt.RightDockWidgetArea, self.dock)
        self.dock.show()