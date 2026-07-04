"""
ui/main_dock.py

Main Dock Widget for RockMorph.

Integrates:
- Dynamic main QGIS Plugins sub-menu generation based on central registry.
- Autocomplete search bar (QLineEdit + QCompleter) replacing the old QComboBox.
- Safe asynchronous lazy loading of modules upon active tool switching.

Authors: RockMorph contributors / Tony
"""

import importlib

from qgis.PyQt.QtWidgets import (  # type: ignore
    QDockWidget, QWidget, QVBoxLayout, QHBoxLayout, 
    QLabel, QLineEdit, QCompleter, QAction, QMenu
)
from qgis.PyQt.QtCore import Qt, QCoreApplication, QStringListModel  # type: ignore

from ..base.registry import TOOL_REGISTRY, get_all_tools


def tr(message):
    return QCoreApplication.translate("RockMorph", message)


class RockMorphDock(QDockWidget):
    """
    Main Sidebar Dock for RockMorph.
    Houses the active tool UI and exposes the quick search navigation bar.
    """

    def __init__(self, iface, parent=None):
        super().__init__(tr("RockMorph"), parent)
        self.iface = iface
        self._panels = {}             # Cached panel instances (lazy loaded)
        self._active_tool_id = None
        self._flat_tools = get_all_tools()
        
        self.setMinimumWidth(420)
        
        # 1. Build the side dock UI layout
        self._build_ui()
        
        # 2. Build the top QGIS plugin menus dynamically
        self._build_qgis_menus()

    def _build_ui(self):
        container = QWidget()
        root = QVBoxLayout(container)
        root.setContentsMargins(0, 0, 0, 0)
        root.setSpacing(0)

        # ── HEADER BAR : Title + Dynamic Search ──────────────────────────
        header = QWidget()
        header.setFixedHeight(44)
        header.setStyleSheet("background-color: #2b2b2b;")
        header_layout = QHBoxLayout(header)
        header_layout.setContentsMargins(10, 0, 10, 0)

        title_label = QLabel("⛰ RockMorph")
        title_label.setStyleSheet("color: #fff; font-weight: bold; font-size: 13px;")
        header_layout.addWidget(title_label)
        header_layout.addStretch()

        # Quick Search Bar (QLineEdit)
        self.search_bar = QLineEdit()
        self.search_bar.setPlaceholderText(tr("🔍 Quick search..."))
        self.search_bar.setFixedWidth(180)
        self.search_bar.setStyleSheet("""
            QLineEdit {
                background-color: #3a3a3a;
                color: #fff;
                border: 1px solid #555;
                border-radius: 4px;
                padding: 2px 8px;
                font-size: 11px;
            }
            QLineEdit:focus {
                border-color: #3a7fc1;
            }
        """)
        
        # Setup QCompleter for instant tool autocomplete
        tool_names = [info["name"] for info in self._flat_tools.values()]
        self.completer = QCompleter(tool_names, self.search_bar)
        self.completer.setCaseSensitivity(Qt.CaseInsensitive)
        self.completer.setFilterMode(Qt.MatchContains)
        self.completer.activated[str].connect(self._on_search_selected)
        self.search_bar.setCompleter(self.completer)

        header_layout.addWidget(self.search_bar)
        root.addWidget(header)

        # ── TOOL CONTAINER (Active Tool UI is loaded here) ───────────────
        self.panel_container = QWidget()
        self.panel_layout = QVBoxLayout(self.panel_container)
        self.panel_layout.setContentsMargins(0, 0, 0, 0)
        root.addWidget(self.panel_container)

        self.setWidget(container)

        # Initial launch tool: Open Mountain Front Sinuosity by default
        self.switch_to_tool("explorer3d")

    def _build_qgis_menus(self):
        """
        Dynamically registers RockMorph categories and actions in the main QGIS top menu
        under 'Plugins' using the centralized TOOL_REGISTRY dictionary.
        """
        # ── AUTO-CLEANUP / DE-DUPLICATION ─────────────────────────────
        # Scan existing Plugin menu actions, find any leftover "RockMorph" menu,
        # and remove/delete it before building the new one. This prevents
        # menu duplication when reloading the plugin.
        for action in self.iface.pluginMenu().actions():
            if action.text() == "RockMorph" or (action.menu() and action.menu().title() == "RockMorph"):
                self.iface.pluginMenu().removeAction(action)
                if action.menu():
                    action.menu().deleteLater()

        # Create a submenu under QGIS Plugins
        self.qgis_menu = QMenu("RockMorph", self.iface.mainWindow().menuBar())
        
        for cat_id, cat_info in TOOL_REGISTRY.items():
            # Create a submenu for this family category
            cat_menu = self.qgis_menu.addMenu(cat_info["name"])
            
            for tool_id, tool_info in cat_info["tools"].items():
                # Create an action for each individual tool
                action = QAction(tool_info["name"], self)
                action.setStatusTip(tool_info["desc"])
                
                # Connect action triggers dynamically using lambda closures
                action.triggered.connect(lambda checked, t_id=tool_id: self.switch_to_tool(t_id))
                cat_menu.addAction(action)

        # Add RockMorph directly into the QGIS top-level Plugins menu without nesting duplicates
        self.iface.pluginMenu().addMenu(self.qgis_menu)

    def _on_search_selected(self, selected_name: str):
        """Triggered when the user selects a tool from the search autocomplete dropdown."""
        # Find the matching tool ID based on the display name
        for tool_id, info in self._flat_tools.items():
            if info["name"] == selected_name:
                self.switch_to_tool(tool_id)
                self.search_bar.clear()  # Reset search input on launch
                break

    def switch_to_tool(self, tool_id: str):
        """
        Switches the active sidebar layout to the specified tool.
        Implements clean lazy loading of Python classes on-demand to protect startup speeds.
        """
        if tool_id not in self._flat_tools:
            return

        # Clear existing active panel from layout
        while self.panel_layout.count():
            item = self.panel_layout.takeAt(0)
            if item.widget():
                item.widget().hide()

        # Dynamic Lazy Loading: import module and class only on-demand
        if tool_id not in self._panels:
            info = self._flat_tools[tool_id]
            try:
                module = importlib.import_module(info["module_path"])
                PanelClass = getattr(module, info["class_name"])
                panel_instance = PanelClass(self.iface, self.panel_container)
                self._panels[tool_id] = panel_instance
            except Exception as e:
                import traceback
                traceback.print_exc()
                self.iface.messageBar().pushWarning("RockMorph", f"Could not load tool '{info['name']}': {e}")
                return

        # Load and show the selected tool UI
        panel = self._panels[tool_id]
        self.panel_layout.addWidget(panel)
        panel.show()
        
        self._active_tool_id = tool_id
        
        # Ensure the dock widget is visible to the user
        self.show()

    def unload(self):
        """Cleans up the dynamically registered top menus on plugin unload."""
        self.iface.pluginMenu().removeAction(self.qgis_menu.menuAction())