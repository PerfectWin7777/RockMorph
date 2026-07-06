# rockmorph/widgets/export_group.py

"""
rockmorph/widgets/export_group.py

A unified and compact export widget for RockMorph panels.
Consolidates image, tabular, and spatial GIS exports into a single 
dropdown button to maintain a narrow, responsive sidebar in QGIS.

Authors: RockMorph contributors / Tony
"""

from PyQt5.QtWidgets import QWidget, QHBoxLayout, QPushButton, QMenu, QAction # type:ignore
from PyQt5.QtCore import pyqtSignal, QCoreApplication, Qt # type:ignore
from PyQt5.QtGui import QIcon # type:ignore

def tr(message: str) -> str:
    return QCoreApplication.translate("RockMorph", message)


class RockMorphExportGroup(QWidget):
    """
    A reusable export control that encapsulates different output formats
    under a professional, single-click dropdown interface.
    """
    # Emitted when a format option is selected from the menu
    exportRequested = pyqtSignal(str)

    def __init__(self, formats: list = None, parent=None):
        """
        Parameters
        ----------
        formats : list of str, optional
            List of format keys to display (e.g., ['png', 'svg', 'csv', 'gpkg']).
            Defaults to ['png', 'jpg', 'svg', 'pdf', 'csv', 'json'].
        parent : QWidget, optional
            The parent widget.
        """
        super().__init__(parent)
        
        # Default fallback formats if none specified
        self._formats = formats if formats is not None else ["png", "jpg", "svg", "pdf", "csv", "json"]
        
        # Mapping of format keys to localized labels and icon emojis
        self._format_registry = {
            "png": (tr("Export high-resolution PNG image (.png)..."), "📸"),
            "jpg": (tr("Export JPEG image (.jpg)..."), "🖼"),
            "jpeg": (tr("Export JPEG image (.jpg)..."), "🖼"),
            "svg": (tr("Export SVG vector graphic (.svg)..."), "📐"),
            "pdf": (tr("Export PDF publication document (.pdf)..."), "📄"),
            "csv": (tr("Export tabular metrics to CSV (.csv)..."), "📊"),
            "json": (tr("Export parameters or metadata to JSON (.json)..."), "📁"),
            "gpkg": (tr("Export spatial layers to GeoPackage (.gpkg)..."), "🌍"),
            "shp": (tr("Export spatial layers to Shapefile (.shp)..."), "🗺"),
            "shapefile": (tr("Export spatial layers to Shapefile (.shp)..."), "🗺"),
            "html": (tr("Export standalone interactive HTML (.html)..."), "🌐"),
            "stl": (tr("Export 3D printable mesh (.stl)..."), "📦")
        }
        
        self._init_ui()

    def _init_ui(self):
        layout = QHBoxLayout(self)
        layout.setContentsMargins(0, 4, 0, 4)
        layout.setSpacing(0)

        # Primary dropdown button
        self.export_btn = QPushButton(tr("💾  Export Results..."))
        self.export_btn.setMinimumHeight(32)
        
        # Modern neutral dark styling to complement QGIS panels
        self.export_btn.setStyleSheet("""
            QPushButton {
                background-color: #2b2b2b;
                color: #ffffff;
                border: 1px solid #555555;
                border-radius: 4px;
                padding: 6px 12px;
                font-weight: bold;
                text-align: left;
            }
            QPushButton:hover {
                background-color: #3d3d3d;
                border-color: #3a7fc1;
            }
            QPushButton:pressed {
                background-color: #1f1f1f;
            }
            QPushButton::menu-indicator {
                subcontrol-origin: padding;
                subcontrol-position: right center;
                right: 12px;
            }
        """)

        # Construct the context menu
        self.export_menu = QMenu(self)
        self.export_menu.setStyleSheet("""
            QMenu {
                background-color: #ffffff;
                color: #333333;
                border: 1px solid #cccccc;
                font-size: 12px;
            }
            QMenu::item {
                padding: 6px 24px 6px 12px;
            }
            QMenu::item:selected {
                background-color: #3a7fc1;
                color: #ffffff;
            }
        """)

        # Dynamically append actions based on chosen capabilities
        for fmt_key in self._formats:
            fmt_clean = fmt_key.lower().strip()
            if fmt_clean in self._format_registry:
                label, emoji = self._format_registry[fmt_clean]
                action_text = f"{emoji}  {label}"
                
                action = QAction(action_text, self)
                # Keep target format in action data for retrieval
                action.setData(fmt_clean)
                action.triggered.connect(self._on_action_triggered)
                self.export_menu.addAction(action)

        # Assign menu directly to button
        self.export_btn.setMenu(self.export_menu)
        layout.addWidget(self.export_btn)

    def _on_action_triggered(self):
        """Extract the target format identifier from the triggered action."""
        action = self.sender()
        if isinstance(action, QAction):
            format_id = action.data()
            if format_id:
                self.exportRequested.emit(format_id)