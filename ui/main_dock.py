# ui/main_dock.py

"""
ui/main_dock.py

Main Dock Widget for RockMorph.

Integrates:
- Centralized, DRY tool menu generation for both QGIS menubar and local switcher.
- Dynamic local tool switcher dropdown (QPushButton + QMenu) replacing static title.
- Autocomplete search bar (QLineEdit + QCompleter).
- Safe asynchronous lazy loading of modules upon active tool switching.

Authors: RockMorph contributors / Tony
"""

import importlib

from qgis.PyQt.QtWidgets import (  # type: ignore
    QDockWidget, QWidget, QVBoxLayout, QHBoxLayout, 
    QLabel, QLineEdit, QCompleter, QAction, QMenu, QPushButton
)
from qgis.PyQt.QtCore import Qt, QCoreApplication, QStringListModel  # type: ignore

from ..base.registry import TOOL_REGISTRY, get_all_tools


def tr(message):
    return QCoreApplication.translate("RockMorph", message)


class RockMorphDock(QDockWidget):
    """
    Main Sidebar Dock for RockMorph.
    Houses the active tool UI and exposes a local dropdown switcher in the header.
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

        # ── HEADER BAR : Title/Switcher + Dynamic Search ──────────────────
        header = QWidget()
        header.setFixedHeight(44)
        header.setStyleSheet("background-color: #2b2b2b;")
        header_layout = QHBoxLayout(header)
        header_layout.setContentsMargins(10, 0, 10, 0)

        # 🔄 DYNAMIC SWITCHER BUTTON (Replaces static QLabel)
        self.title_btn = QPushButton("⛰  RockMorph ▾")
        self.title_btn.setStyleSheet("""
            QPushButton {
                background-color: transparent;
                color: #ffffff;
                border: none;
                font-weight: bold;
                font-size: 13px;
                padding: 4px 8px;
                text-align: left;
            }
            QPushButton:hover {
                background-color: #3d3d3d;
                border-radius: 4px;
            }
            QPushButton::menu-indicator {
                image: none; /* Hide default arrow to handle layout with tr() custom arrow */
            }
        """)

        # Switcher menu styling (dark theme matching QGIS theme palette)
        self.switcher_menu = QMenu(self)
        # self.switcher_menu.setStyleSheet("""
        #     QMenu {
        #         background-color: #2b2b2b;
        #         color: #ffffff;
        #         border: 1px solid #555555;
        #     }
        #     QMenu::item {
        #         padding: 6px 20px;
        #         font-size: 12px;
        #     }
        #     QMenu::item:selected {
        #         background-color: #3a7fc1;
        #         color: #ffffff;
        #     }
        # """)
        
        # Connect dynamic menu directly to button
        self.title_btn.setMenu(self.switcher_menu)
        header_layout.addWidget(self.title_btn)
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
        self.switch_to_tool("terrainderivatives")

    def _populate_tool_menu(self, menu: QMenu):
        """
        Helper method to populate any QMenu with the tools hierarchy.
        Provides a DRY implementation for QGIS plugins menu and dock switcher.
        """
        for cat_id, cat_info in TOOL_REGISTRY.items():
            cat_menu = menu.addMenu(cat_info["name"])
            
            for tool_id, tool_info in cat_info["tools"].items():
                action = QAction(tool_info["name"], self)
                action.setStatusTip(tool_info["desc"])
                action.triggered.connect(
                    lambda checked, t_id=tool_id: self.switch_to_tool(t_id)
                )
                cat_menu.addAction(action)

    def _build_qgis_menus(self):
        """
        Dynamically registers RockMorph categories and actions in the main QGIS top menu
        using the centralized helper.
        """
        for action in self.iface.pluginMenu().actions():
            if action.text() == "RockMorph" or (action.menu() and action.menu().title() == "RockMorph"):
                self.iface.pluginMenu().removeAction(action)
                if action.menu():
                    action.menu().deleteLater()

        self.qgis_menu = QMenu("RockMorph", self.iface.mainWindow().menuBar())
        
        # Centralized build
        self._populate_tool_menu(self.qgis_menu)
        self.iface.pluginMenu().addMenu(self.qgis_menu)
        
        # Now populate the dock switcher local menu with the same architecture
        self.switcher_menu.clear()
        self._populate_tool_menu(self.switcher_menu)

    def _on_search_selected(self, selected_name: str):
        """Triggered when the user selects a tool from the search autocomplete dropdown."""
        for tool_id, info in self._flat_tools.items():
            if info["name"] == selected_name:
                self.switch_to_tool(tool_id)
                self.search_bar.clear()
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
        
        # 🔄 Update switcher text dynamically to show active tool
        info = self._flat_tools[tool_id]
        self.title_btn.setText(f"⛰  {info['name']} ▾")
        
        self.show()

    def unload(self):
        """Cleans up the dynamically registered top menus on plugin unload."""
        self.iface.pluginMenu().removeAction(self.qgis_menu.menuAction())