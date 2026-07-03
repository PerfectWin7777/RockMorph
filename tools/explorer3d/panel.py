"""
PyQt5 panel for the RockMorph 3D Explorer.

Architecture
------------
- Icon toolbar (QToolButton strip) + QStackedWidget: Blender-style navigation.
- Explicit data model for lights (_lights list) before any Qt widget.
- All signals wired in a single _connect_all_signals() call at the end of
  _build_ui(), so connectivity is auditable at a glance.
- Every user-visible string goes through tr() for i18n readiness.
- Vector layers are sent as a single batched JSON payload, not N round-trips.

Authors: RockMorph contributors
"""

import json, base64, os
from pathlib import Path
from typing import Optional

from qgis.PyQt.QtWidgets import (  # type: ignore
    QWidget, QVBoxLayout, QHBoxLayout, QLabel, QPushButton,
    QGroupBox, QSlider, QComboBox, QCheckBox, QFrame,
    QRadioButton, QStackedWidget, QListWidget, QListWidgetItem,
    QColorDialog, QDoubleSpinBox, QToolButton, QButtonGroup,
    QSizePolicy, QSpacerItem, QScrollArea, QMenu,
    QFileDialog
)
from qgis.PyQt.QtCore import Qt, QSize, QCoreApplication, QTimer  # type: ignore
from qgis.PyQt.QtGui import QColor, QIcon, QPixmap, QPainter  # type: ignore
from qgis.PyQt.QtWebEngineWidgets import QWebEnginePage  # type: ignore
from qgis.core import QgsMapLayerProxyModel  # type: ignore
from qgis.gui import QgsMapLayerComboBox  # type: ignore

from ...base.base_panel import BasePanel, ComputeWorker
from .engine import Explorer3DEngine

from ...widgets.colormap_combo import MatplotlibColorMapComboBox

json_file = (
    Path(__file__).parents[2]
    / "web"
    / "data"
    / "colormaps.json"
)



# ---------------------------------------------------------------------------
# Helpers
# ---------------------------------------------------------------------------

def tr(message: str) -> str:
    return QCoreApplication.translate("RockMorph", message)


def _color_pixmap(hex_color: str, size: int = 16) -> QPixmap:
    """Return a solid-color square pixmap for use on color-picker buttons."""
    px = QPixmap(size, size)
    px.fill(QColor(hex_color))
    return px


def _make_color_btn(label: str, default_hex: str) -> QPushButton:
    """
    Return a QPushButton whose left side shows a colored square swatch
    and whose right side shows a text label.
    """
    btn = QPushButton(f"  {label}")
    btn.setIcon(QIcon(_color_pixmap(default_hex)))
    btn.setIconSize(QSize(16, 16))
    btn.setProperty("color_hex", default_hex)
    btn.setStyleSheet("""
        QPushButton {
            text-align: left;
            padding: 4px 8px;
            border: 1px solid #aaa;
            border-radius: 4px;
            background: #fff;
        }
        QPushButton:hover { background: #f0f0f0; }
    """)
    return btn


def _apply_color_to_btn(btn: QPushButton, color: QColor) -> None:
    """Update a color-picker button's swatch and stored hex value."""
    btn.setIcon(QIcon(_color_pixmap(color.name())))
    btn.setProperty("color_hex", color.name())


# ---------------------------------------------------------------------------
# Debug WebEngine page
# ---------------------------------------------------------------------------

class DebugWebEnginePage(QWebEnginePage):
    """
    Redirects all JS console output (log / warn / error) to the QGIS
    Python Console so that WebGL issues are visible without DevTools.
    """
    def javaScriptConsoleMessage(self, level, message, lineNumber, sourceID):
        print(f"[3D WebGL] Line {lineNumber} | {sourceID}: {message}")


# ---------------------------------------------------------------------------
# Default light descriptor used as the authoritative data model
# ---------------------------------------------------------------------------

def _default_sun() -> dict:
    return {
        "id": "light_0",
        "label": "Sun_Directional",
        "type": "directional",
        "color": "#ffffff",
        "intensity": 1.0,
        "azimuth": 135,
        "altitude": 45,
        "gizmo": False,
    }


# ---------------------------------------------------------------------------
# Panel
# ---------------------------------------------------------------------------

class Explorer3DPanel(BasePanel):
    """
    Control panel for the RockMorph 3D Explorer.

    Internal state
    --------------
    self._lights            : list[dict]  – authoritative light descriptors
    self._selected_light_idx: int         – index into self._lights
    self._loaded_raster_id  : str | None  – layer.id() of the active DEM
    self._colors            : dict        – hex values for walls/base/sky
    """

    # ── BasePanel required hook ──────────────────────────────────────────

    def _html_file(self) -> str:
        return "explorer3d.html"

    # ── Top-level UI assembly ────────────────────────────────────────────

    def _build_ui(self) -> None:
        # ── Data model (always before widgets) ──────────────────────────
        self.engine = Explorer3DEngine()
        self.is_3d_active = False
        # Pending WebEngine command queue to prevent initialization race conditions
        self._web_ready = False
        self._pending_commands = []
        self._active_dem_data = None
        self._loaded_vectors = []
        
        # Initialize the style debounce timer first to prevent initialization crashes [Fix 1]
        self._style_debounce_timer = QTimer()
        self._style_debounce_timer.setSingleShot(True)
        self._style_debounce_timer.setInterval(50)  # 50ms buffer
        self._style_debounce_timer.timeout.connect(self._slot_apply_vector_style_update)

        # Cached values for classified rendering
        self.active_dem_min_z = 0.0
        self.active_dem_max_z = 1.0
        self._class_colors = []
        self._class_bounds = []
        self._loaded_raster_id: Optional[str] = None
        self._selected_light_idx: int = 0
        self._lights: list = [_default_sun()]
        self._colors: dict = {
           "walls":       "#909090",  
            "base":       "#3d3d3d", 
            "sky_top":    "#1a2a3a",
            "sky_bottom": "#3d6080",
        }

        # ── Root layout ──────────────────────────────────────────────────
        root = QVBoxLayout(self._inner)
        root.setContentsMargins(8, 8, 8, 8)
        root.setSpacing(6)

        # View switcher (always visible, above the tabs)
        root.addWidget(self._build_view_switcher())

        # Icon toolbar strip
        root.addWidget(self._build_icon_toolbar())

        # Thin separator line between toolbar and page content
        sep = QFrame()
        sep.setFrameShape(QFrame.HLine)
        sep.setStyleSheet("color: #ccc;")
        root.addWidget(sep)

        # Pages managed by the stacked widget
        self._stack = QStackedWidget()
        self._stack.addWidget(self._build_page_layers())       # 0
        self._stack.addWidget(self._build_page_symbology())    # 1
        self._stack.addWidget(self._build_page_lighting())     # 2
        self._stack.addWidget(self._build_page_aesthetics())   # 3
        self._stack.addWidget(self._build_page_export())       # 4
        root.addWidget(self._stack)
        root.addStretch()

        # ── QGIS canvas wiring ───────────────────────────────────────────
        # ── QGIS canvas wiring ───────────────────────────────────────────
        self.canvas_2d = self.iface.mapCanvas()
        self.central_container = self.canvas_2d.parentWidget()
        
        # Make the webview a direct overlay child of the central container
        # instead of inserting it into QGIS's strict grid layout.
        self.webview.setParent(self.central_container)
        self.webview.hide() 

        # Install an event filter to capture real-time resizing of the central area
        self.central_container.installEventFilter(self)


        # Redirect JS console to QGIS Python Console
        debug_page = DebugWebEnginePage(self.webview)
        self.webview.setPage(debug_page)
        debug_page.setWebChannel(self._channel)

        # ── Wire ALL signals in one place ────────────────────────────────
        self._connect_all_signals()

        # Initial UI state
        self._slot_view_mode_changed()        
        self._slot_selected_raster_changed()

        self._add_layer_item(tr("🌐  Reference Grid"), element_id="scene_grid")
        self._add_layer_item(tr("📍  Orientation Axes (X, Y, Z)"), element_id="scene_axes")


    # ── Navigation widgets ───────────────────────────────────────────────

    def _build_view_switcher(self) -> QWidget:
        """
        Two radio buttons that swap QGIS 2D canvas ↔ WebGL 3D viewport.
        Always visible; independent of the tab selection.
        """
        container = QWidget()
        layout = QHBoxLayout(container)
        layout.setContentsMargins(0, 0, 0, 0)
        layout.setSpacing(8)

        label = QLabel(tr("Viewport:"))
        label.setStyleSheet("font-weight: bold; color: #333;")
        layout.addWidget(label)

        self.rad_view_2d = QRadioButton(tr("QGIS 2D"))

        self.rad_view_3d = QRadioButton(tr("3D Terrain")) 
        self.rad_view_3d.setChecked(True)

        layout.addWidget(self.rad_view_2d)
        layout.addWidget(self.rad_view_3d)
        layout.addStretch()
        return container

    def _build_icon_toolbar(self) -> QWidget:
        """
        Compact horizontal strip of QToolButtons.
        Each button switches one page of self._stack.
        """
        container = QWidget()
        container.setStyleSheet("""
            QWidget {
                background-color: #2b2b2b;
                border-radius: 6px;
            }
        """)
        layout = QHBoxLayout(container)
        layout.setContentsMargins(6, 4, 6, 4)
        layout.setSpacing(2)

        # (icon_text, tooltip, page_index)
        pages = [
            ("⛰",  tr("Layers & Scene"),           0),
            ("🎨",  tr("Symbology & Colormaps"),    1),
            ("💡",  tr("Light Manager"),            2),
            ("🧱",  tr("Block & Environment"),      3),
            ("💾",  tr("Export"),                   4),
        ]

        self._tab_buttons: list[QToolButton] = []
        self._tab_btn_group = QButtonGroup()
        self._tab_btn_group.setExclusive(True)

        for icon_text, tooltip, idx in pages:
            btn = QToolButton()
            btn.setText(icon_text)
            btn.setToolTip(tooltip)
            btn.setCheckable(True)
            btn.setFixedSize(40, 32)
            btn.setStyleSheet("""
                QToolButton {
                    color: #aaa;
                    font-size: 16px;
                    border: none;
                    border-radius: 4px;
                    background: transparent;
                }
                QToolButton:hover  { background: #3d3d3d; color: #fff; }
                QToolButton:checked {
                    background: #3a7fc1;
                    color: #fff;
                    border-radius: 4px;
                }
            """)
            self._tab_btn_group.addButton(btn, idx)
            self._tab_buttons.append(btn)
            layout.addWidget(btn)

        layout.addStretch()

        # Activate first button by default
        self._tab_buttons[0].setChecked(True)
        return container

    # ── Page 1 — Layers & Scene ──────────────────────────────────────────

    def _build_page_layers(self) -> QWidget:
        page = QWidget()
        layout = QVBoxLayout(page)
        layout.setContentsMargins(0, 8, 0, 0)
        layout.setSpacing(8)

        # 1. DEM selection
        lbl_dem = QLabel(tr("Elevation DEM layer:"))
        lbl_dem.setStyleSheet("font-weight: bold;")
        layout.addWidget(lbl_dem)

        self.combo_raster = QgsMapLayerComboBox()
        self.combo_raster.setFilters(QgsMapLayerProxyModel.RasterLayer)
        self.combo_raster.setToolTip(tr("Select the raster layer to use as terrain surface."))
        layout.addWidget(self.combo_raster)

        self.btn_render_terrain = QPushButton(tr("⛰  Render 3D Terrain"))
        self.btn_render_terrain.setStyleSheet("""
            QPushButton {
                background-color: #27ae60;
                color: white;
                font-weight: bold;
                padding: 6px;
                border-radius: 4px;
            }
            QPushButton:disabled { background-color: #aaa; color: #eee; }
            QPushButton:hover:!disabled { background-color: #219a52; }
        """)
        layout.addWidget(self.btn_render_terrain)

        # Separator
        sep1 = QFrame()
        sep1.setFrameShape(QFrame.HLine)
        layout.addWidget(sep1)

        # 2. Scene registry list with Delete controls
        lbl_scene = QLabel(tr("Scene objects:"))
        lbl_scene.setStyleSheet("font-weight: bold;")
        layout.addWidget(lbl_scene)

        list_row = QHBoxLayout()
        self.list_layers = QListWidget()
        self.list_layers.setStyleSheet("""
            QListWidget {
                border: 1px solid #ccc;
                border-radius: 4px;
                background: #fafafa;
            }
            QListWidget::item { padding: 4px; }
            QListWidget::item:selected { background: #d0e8ff; color: #000; }
        """)
        self.list_layers.setMaximumHeight(120)
        list_row.addWidget(self.list_layers)

        # Small vertical panel for list actions (Delete)
        list_actions = QVBoxLayout()
        self.btn_delete_object = QPushButton(tr("🗑"))
        self.btn_delete_object.setFixedSize(32, 32)
        self.btn_delete_object.setToolTip(tr("Delete the selected layer from the 3D scene."))
        self.btn_delete_object.setStyleSheet("""
            QPushButton {
                background-color: #e74c3c;
                color: white;
                font-weight: bold;
                border-radius: 4px;
            }
            QPushButton:hover { background-color: #c0392b; }
        """)
        list_actions.addWidget(self.btn_delete_object)
        list_actions.addStretch()
        list_row.addLayout(list_actions)
        layout.addLayout(list_row)

        # Separator
        sep2 = QFrame()
        sep2.setFrameShape(QFrame.HLine)
        layout.addWidget(sep2)

        # 3. Vector Overlay Section
        lbl_vec = QLabel(tr("Drape vector layer onto terrain:"))
        lbl_vec.setStyleSheet("font-weight: bold;")
        layout.addWidget(lbl_vec)

        row_vec = QHBoxLayout()
        self.combo_vector = QgsMapLayerComboBox()
        self.combo_vector.setFilters(QgsMapLayerProxyModel.VectorLayer)
        row_vec.addWidget(self.combo_vector)

        self.btn_add_vector = QPushButton(tr("+ Add"))
        self.btn_add_vector.setFixedWidth(60)
        self.btn_add_vector.setToolTip(tr("Project this vector layer onto the 3D terrain surface."))
        row_vec.addWidget(self.btn_add_vector)
        layout.addLayout(row_vec)

        # 4. Collapse-ready Vector Styling Settings Group Box
        self.group_vector_style = QGroupBox(tr("Vector Styling Settings"))
        vector_style_layout = QVBoxLayout(self.group_vector_style)
        vector_style_layout.setSpacing(6)

        # Drape Mode Selector
        vector_style_layout.addWidget(QLabel(tr("Drape visualization mode:")))
        self.combo_drape_mode = QComboBox()
        self.combo_drape_mode.addItems([
            tr("Line (Default)"),
            tr("Curtain (Geological Fault)"),
            tr("Polygon Outline"),
            tr("Polygon Filled (Transparent)"),
            tr("Point Markers")
        ])
        vector_style_layout.addWidget(self.combo_drape_mode)

        # Dynamic Mode-Specific Settings Stack
        self.stacked_vector_settings = QStackedWidget()

        # Page 0: Line/Outline settings [Line Width Slider] [New]
        page_line = QWidget()
        layout_line = QHBoxLayout(page_line)
        layout_line.setContentsMargins(0, 0, 0, 0)
        layout_line.addWidget(QLabel(tr("Line Width:")))
        self.slider_line_width = QSlider(Qt.Horizontal)
        self.slider_line_width.setRange(1, 10)
        self.slider_line_width.setValue(2)
        layout_line.addWidget(self.slider_line_width)
        self.lbl_line_width_val = QLabel("2 px")
        layout_line.addWidget(self.lbl_line_width_val)
        self.stacked_vector_settings.addWidget(page_line)

        # Page 1: Curtain depth slider
        page_curtain = QWidget()
        layout_curtain = QHBoxLayout(page_curtain)
        layout_curtain.setContentsMargins(0, 0, 0, 0)
        layout_curtain.addWidget(QLabel(tr("Depth:")))
        self.slider_curtain_depth = QSlider(Qt.Horizontal)
        self.slider_curtain_depth.setRange(1, 2000)
        self.slider_curtain_depth.setValue(250)
        layout_curtain.addWidget(self.slider_curtain_depth)
        self.lbl_curtain_depth_val = QLabel("250 m")
        layout_curtain.addWidget(self.lbl_curtain_depth_val)
        self.stacked_vector_settings.addWidget(page_curtain)

        # Page 2: Polygon opacity slider
        page_opacity = QWidget()
        layout_opacity = QHBoxLayout(page_opacity)
        layout_opacity.setContentsMargins(0, 0, 0, 0)
        layout_opacity.addWidget(QLabel(tr("Opacity:")))
        self.slider_polygon_opacity = QSlider(Qt.Horizontal)
        self.slider_polygon_opacity.setRange(10, 100)
        self.slider_polygon_opacity.setValue(50)
        layout_opacity.addWidget(self.slider_polygon_opacity)
        self.lbl_polygon_opacity_val = QLabel("50 %")
        layout_opacity.addWidget(self.lbl_polygon_opacity_val)
        self.stacked_vector_settings.addWidget(page_opacity)

        # Page 3: Point size slider
        page_point = QWidget()
        layout_point = QHBoxLayout(page_point)
        layout_point.setContentsMargins(0, 0, 0, 0)
        layout_point.addWidget(QLabel(tr("Marker Size:")))
        self.slider_point_size = QSlider(Qt.Horizontal)
        self.slider_point_size.setRange(1, 50)
        self.slider_point_size.setValue(15)
        layout_point.addWidget(self.slider_point_size)
        self.lbl_point_size_val = QLabel("1.5 m")
        layout_point.addWidget(self.lbl_point_size_val)
        self.stacked_vector_settings.addWidget(page_point)

        vector_style_layout.addWidget(self.stacked_vector_settings)

        vector_style_layout.addWidget(QLabel(tr("Vertical height offset:")))
        row_offset = QHBoxLayout()
        self.slider_height_offset = QSlider(Qt.Horizontal)
        self.slider_height_offset.setRange(-200, 1000)  
        self.slider_height_offset.setValue(10)         
        row_offset.addWidget(self.slider_height_offset)
        self.lbl_height_offset_val = QLabel("10 m")
        row_offset.addWidget(self.lbl_height_offset_val)
        vector_style_layout.addLayout(row_offset)


        # Color Styling Selector
        vector_style_layout.addWidget(QLabel(tr("Coloring method:")))
        self.combo_color_styling = QComboBox()
        self.combo_color_styling.addItems([
            tr("Fixed Uniform Color"),
            tr("Color by Attribute Value")
        ])
        vector_style_layout.addWidget(self.combo_color_styling)

        # Dynamic Color Settings Stack
        self.stacked_color_settings = QStackedWidget()

        # Page 0: Simple fixed color picker
        page_fixed_color = QWidget()
        layout_fixed = QVBoxLayout(page_fixed_color)
        layout_fixed.setContentsMargins(0, 0, 0, 0)
        self.btn_vector_fixed_color = _make_color_btn(tr("Select color"), "#3498db")
        layout_fixed.addWidget(self.btn_vector_fixed_color)
        self.stacked_color_settings.addWidget(page_fixed_color)

        # Page 1: Attribute categorization + colormap selector
        page_attribute_color = QWidget()
        layout_attrib = QVBoxLayout(page_attribute_color)
        layout_attrib.setContentsMargins(0, 0, 0, 0)
        layout_attrib.setSpacing(4)

        row_attr = QHBoxLayout()
        row_attr.addWidget(QLabel(tr("Attribute Field:")))
        self.combo_vector_attribute = QComboBox()
        row_attr.addWidget(self.combo_vector_attribute)
        layout_attrib.addLayout(row_attr)

        row_cmap = QHBoxLayout()
        row_cmap.addWidget(QLabel(tr("Palette Ramp:")))
        self.combo_vector_colormap = QComboBox()
        self.combo_vector_colormap.addItems(["terrain", "viridis", "magma", "plasma", "cividis"])
        row_cmap.addWidget(self.combo_vector_colormap)
        layout_attrib.addLayout(row_cmap)

        self.stacked_color_settings.addWidget(page_attribute_color)
        vector_style_layout.addWidget(self.stacked_color_settings)

        layout.addWidget(self.group_vector_style)
        layout.addStretch()
        return page

    # ── Page 2 — Symbology ───────────────────────────────────────────────

    def _build_page_symbology(self) -> QWidget:
        page = QWidget()
        layout = QVBoxLayout(page)
        layout.setContentsMargins(0, 8, 0, 0)
        layout.setSpacing(8)

        # 1. Unified Render Mode Selector (Renamed for clarity)
        lbl_mode = QLabel(tr("Symbology render mode:"))
        lbl_mode.setStyleSheet("font-weight: bold;")
        layout.addWidget(lbl_mode)

        self.combo_symbology_render_mode = QComboBox()
        self.combo_symbology_render_mode.addItems([
            tr("Continuous Colormap (Scientific Pseudocolor)"),
            tr("Classified Intervals (Discrete Brackets)"),
            tr("Solid Uniform Fill")
        ])
        layout.addWidget(self.combo_symbology_render_mode)

        # 2. Stacked Settings Area 
        self.stacked_symbology_settings = QStackedWidget()
        
        # --- Page 0: Continuous Settings ---
        page_continuous = QWidget()
        layout_cont = QVBoxLayout(page_continuous)
        layout_cont.setContentsMargins(0, 4, 0, 4)
        layout_cont.setSpacing(6)
        
        lbl_cmap = QLabel(tr("Scientific colormap:"))
        lbl_cmap.setStyleSheet("font-weight: bold;")
        layout_cont.addWidget(lbl_cmap)

        self.combo_colormap = MatplotlibColorMapComboBox(json_file)
        self.combo_colormap.setToolTip(
            tr("All names match Matplotlib conventions — use the same name in figure captions.")
        )
        layout_cont.addWidget(self.combo_colormap)

        self.chk_reverse_cmap = QCheckBox(tr("Reverse color ramp"))
        layout_cont.addWidget(self.chk_reverse_cmap)
        layout_cont.addStretch()
        self.stacked_symbology_settings.addWidget(page_continuous)

        # --- Page 1: Classified Settings ---
        page_classified = QWidget()
        layout_class = QVBoxLayout(page_classified)
        layout_class.setContentsMargins(0, 4, 0, 4)
        layout_class.setSpacing(6)

        row_count = QHBoxLayout()
        row_count.addWidget(QLabel(tr("Number of classes:")))
        self.spin_class_count = QDoubleSpinBox()
        self.spin_class_count.setRange(2, 12)
        self.spin_class_count.setDecimals(0)
        self.spin_class_count.setValue(5)
        row_count.addWidget(self.spin_class_count)
        layout_class.addLayout(row_count)

        # Scroll area to contain custom class range pickers
        scroll_classes = QScrollArea()
        scroll_classes.setWidgetResizable(True)
        scroll_classes.setMaximumHeight(150)
        scroll_classes.setStyleSheet("QScrollArea { border: 1px solid #ccc; border-radius: 4px; }")
        
        self.widget_classes_list = QWidget()
        self.layout_classes_list = QVBoxLayout(self.widget_classes_list)
        self.layout_classes_list.setContentsMargins(4, 4, 4, 4)
        self.layout_classes_list.setSpacing(4)
        scroll_classes.setWidget(self.widget_classes_list)
        layout_class.addWidget(scroll_classes)
        self.stacked_symbology_settings.addWidget(page_classified)

        # --- Page 2: Solid Fill Settings ---
        page_solid = QWidget()
        layout_solid = QVBoxLayout(page_solid)
        layout_solid.setContentsMargins(0, 4, 0, 4)
        layout_solid.setSpacing(6)

        self.btn_solid_color = _make_color_btn(tr("Terrain color"), "#4a90d9")
        layout_solid.addWidget(self.btn_solid_color)
        layout_solid.addStretch()
        self.stacked_symbology_settings.addWidget(page_solid)

        layout.addWidget(self.stacked_symbology_settings)

        sep = QFrame()
        sep.setFrameShape(QFrame.HLine)
        layout.addWidget(sep)

        # 3. Dynamic bounds settings group (Hides automatically in solid mode) [Bug B Fix]
        self.bounds_configuration_panel = QFrame()
        self.bounds_configuration_panel.setFrameShape(QFrame.NoFrame)
        layout_bounds = QVBoxLayout(self.bounds_configuration_panel)
        layout_bounds.setContentsMargins(0, 0, 0, 0)
        layout_bounds.setSpacing(6)

        lbl_bounds = QLabel(tr("Elevation classification bounds:"))
        lbl_bounds.setStyleSheet("font-weight: bold;")
        layout_bounds.addWidget(lbl_bounds)

        bounds_row = QHBoxLayout()
        self.rad_scale_auto = QRadioButton(tr("Auto (DEM range)"))
        self.rad_scale_auto.setChecked(True)
        self.rad_scale_manual = QRadioButton(tr("Custom range"))
        self.scale_btn_group = QButtonGroup(self)
        self.scale_btn_group.addButton(self.rad_scale_auto)
        self.scale_btn_group.addButton(self.rad_scale_manual)
        bounds_row.addWidget(self.rad_scale_auto)
        bounds_row.addWidget(self.rad_scale_manual)
        layout_bounds.addLayout(bounds_row)

        spin_row = QHBoxLayout()
        spin_row.addWidget(QLabel(tr("Min Z:")))
        self.spin_min_z = QDoubleSpinBox()
        self.spin_min_z.setRange(-99999, 99999)
        self.spin_min_z.setEnabled(False)
        spin_row.addWidget(self.spin_min_z)

        spin_row.addWidget(QLabel(tr("Max Z:")))
        self.spin_max_z = QDoubleSpinBox()
        self.spin_max_z.setRange(-99999, 99999)
        self.spin_max_z.setEnabled(False)
        spin_row.addWidget(self.spin_max_z)
        layout_bounds.addLayout(spin_row)

        layout.addWidget(self.bounds_configuration_panel)
        layout.addStretch()
        return page

    # ── Page 3 — Light Manager ───────────────────────────────────────────

    def _build_page_lighting(self) -> QWidget:
        page = QWidget()
        layout = QVBoxLayout(page)
        layout.setContentsMargins(0, 8, 0, 0)
        layout.setSpacing(8)

        # Main Lighting Mode Selector
        lbl_mode = QLabel(tr("Illumination algorithm:"))
        lbl_mode.setStyleSheet("font-weight: bold;")
        layout.addWidget(lbl_mode)

        self.chk_multidirectional = QCheckBox(tr("Enable multidirectional shading (4-Axis GIS mode)"))
        self.chk_multidirectional.setToolTip(
            tr("Combines 4 orthogonal light sources to eliminate structural shadows. "
               "Perfect for fault-scarp lineament mapping.")
        )
        layout.addWidget(self.chk_multidirectional)

        sep = QFrame()
        sep.setFrameShape(QFrame.HLine)
        layout.addWidget(sep)

        # Primary Sun properties (authoritative source)
        self.group_light_props = QGroupBox(tr("Primary solar parameters"))
        props_layout = QVBoxLayout(self.group_light_props)
        props_layout.setSpacing(6)

        # Sun Color
        color_row = QHBoxLayout()
        color_row.addWidget(QLabel(tr("Sun color:")))
        self.btn_light_color = _make_color_btn(tr("Sun light"), "#ffffff")
        self.btn_light_color.setToolTip(tr("Color of the primary directional sun rays."))
        color_row.addWidget(self.btn_light_color)
        props_layout.addLayout(color_row)

        # Intensity
        self._add_labeled_slider(
            props_layout,
            tr("Direct sun intensity:"),
            "slider_light_intensity",
            min_val=0, max_val=30, default=15,
            tooltip=tr("Power of the primary sun (0 = fully overcast, 3.0 = direct sunlight).")
        )

        # Azimuth
        self._add_labeled_slider(
            props_layout,
            tr("Sun azimuth (0–360°):"),
            "slider_light_azimuth",
            min_val=0, max_val=360, default=135,
            tooltip=tr("Direction of the sun rays relative to North.")
        )

        # Altitude
        self._add_labeled_slider(
            props_layout,
            tr("Sun altitude (10–90°):"),
            "slider_light_altitude",
            min_val=10, max_val=90, default=45,
            tooltip=tr("Elevation of the sun above the horizon.")
        )

        layout.addWidget(self.group_light_props)
        layout.addStretch()
        return page

    # ── Page 4 — Block & Environment ────────────────────────────────────

    def _build_page_aesthetics(self) -> QWidget:
        page = QWidget()
        layout = QVBoxLayout(page)
        layout.setContentsMargins(0, 8, 0, 0)
        layout.setSpacing(8)

        # Camera Projection Selection
        lbl_cam = QLabel(tr("Camera projection mode:"))
        lbl_cam.setStyleSheet("font-weight: bold;")
        layout.addWidget(lbl_cam)

        self.combo_camera = QComboBox()
        self.combo_camera.addItems([tr("Perspective View"), tr("Orthographic View")])
        self.combo_camera.setToolTip(
            tr("Perspective: natural 3D depth. Orthographic: parallel projection, "
               "guaranteeing consistent scale across the viewport (ideal for publication maps).")
        )
        layout.addWidget(self.combo_camera)

        sep_cam = QFrame()
        sep_cam.setFrameShape(QFrame.HLine)
        layout.addWidget(sep_cam)

        # Z-Scale
        lbl_z = QLabel(tr("Vertical exaggeration (Z-scale):"))
        lbl_z.setStyleSheet("font-weight: bold;")
        layout.addWidget(lbl_z)

        self._add_labeled_slider(
            layout,
            label_text=None,        # label already added above
            attr_name="slider_z_scale",
            min_val=0, max_val=50, default=15,
            tooltip=tr(
                "Multiplier applied to terrain height. "
                "A dynamic base factor keeps the relief visible regardless of DEM extent."
            )
        )
        self.lbl_z_value = QLabel("1.5 ×")
        self.lbl_z_value.setAlignment(Qt.AlignRight)
        layout.addWidget(self.lbl_z_value)

        # Block base thickness
        lbl_thick = QLabel(tr("Block base thickness:"))
        lbl_thick.setStyleSheet("font-weight: bold;")
        layout.addWidget(lbl_thick)

        self._add_labeled_slider(
            layout,
            label_text=None,
            attr_name="slider_thickness",
            min_val=1, max_val=30, default=5,
            tooltip=tr("Depth of the rock block below the lowest terrain point, as % of terrain width.")
        )
        self.lbl_thick_value = QLabel("5 %")
        self.lbl_thick_value.setAlignment(Qt.AlignRight)
        layout.addWidget(self.lbl_thick_value)

        sep2 = QFrame()
        sep2.setFrameShape(QFrame.HLine)
        layout.addWidget(sep2)

        # Block colors
        lbl_block = QLabel(tr("Block color customization:"))
        lbl_block.setStyleSheet("font-weight: bold;")
        layout.addWidget(lbl_block)

        color_row = QHBoxLayout()
        self.btn_wall_color = _make_color_btn(tr("Walls"), self._colors["walls"])
        self.btn_wall_color.setToolTip(tr("Color applied to the four lateral faces of the block."))
        self.btn_base_color = _make_color_btn(tr("Base plate"), self._colors["base"])
        self.btn_base_color.setToolTip(tr("Color applied to the flat bottom of the block."))
        color_row.addWidget(self.btn_wall_color)
        color_row.addWidget(self.btn_base_color)
        layout.addLayout(color_row)

        # Sky gradient
        lbl_sky = QLabel(tr("Background sky gradient:"))
        lbl_sky.setStyleSheet("font-weight: bold;")
        layout.addWidget(lbl_sky)

        sky_row = QHBoxLayout()
        self.btn_sky_top = _make_color_btn(tr("Sky (top)"), self._colors["sky_top"])
        self.btn_sky_top.setToolTip(
            tr("Top color of the sky gradient. "
               "Use a deep blue for realistic renders or pure black for figure-quality backgrounds.")
        )
        self.btn_sky_bottom = _make_color_btn(tr("Horizon"), self._colors["sky_bottom"])
        self.btn_sky_bottom.setToolTip(
            tr("Bottom color of the sky gradient. "
               "Use white for thesis figures or a warm haze for landscape renders.")
        )
        sky_row.addWidget(self.btn_sky_top)
        sky_row.addWidget(self.btn_sky_bottom)
        layout.addLayout(sky_row)

        sep4 = QFrame()
        sep4.setFrameShape(QFrame.HLine)
        layout.addWidget(sep4)

        # Scientific Geomorphological Shading Properties
        lbl_shading = QLabel(tr("Topographic rendering enhancements:"))
        lbl_shading.setStyleSheet("font-weight: bold;")
        layout.addWidget(lbl_shading)

        # Roughness (Oren-Nayar)
        self._add_labeled_slider(
            layout,
            label_text=tr("Surface roughness (Oren-Nayar):"),
            attr_name="slider_roughness",
            min_val=0, max_val=100, default=50,
            tooltip=tr("Simulates diffuse scattering of matte rock and soil. Higher values remove plastic shine.")
        )
        
        # Slope contrast
        self._add_labeled_slider(
            layout,
            label_text=tr("Structural slope accentuation:"),
            attr_name="slider_slope_contrast",
            min_val=0, max_val=100, default=30,
            tooltip=tr("Darkens steep river incisions and structural escarpments to reveal tectonic lineaments.")
        )

        layout.addStretch()
        return page

    # ── Page 5 — Export ──────────────────────────────────────────────────

    def _build_page_export(self) -> QWidget:
        page = QWidget()
        layout = QVBoxLayout(page)
        layout.setContentsMargins(0, 8, 0, 0)
        layout.setSpacing(12)

        lbl = QLabel(tr("Publish output:"))
        lbl.setStyleSheet("font-weight: bold;")
        layout.addWidget(lbl)

        self.btn_export_img = QPushButton(tr("📸  Export high-resolution image"))
        self.btn_export_img.setToolTip(
            tr("Capture the current 3D viewport at the selected DPI (suitable for journal figures).")
        )
        self.btn_export_img.setStyleSheet("""
            QPushButton {
                padding: 10px;
                font-weight: bold;
                border-radius: 4px;
                background: #2980b9;
                color: white;
            }
            QPushButton:hover { background: #1f6fa0; }
        """)
        layout.addWidget(self.btn_export_img)

        self.btn_export_html = QPushButton(tr("🌐  Export as standalone interactive HTML"))
        self.btn_export_html.setToolTip(
            tr("Generate a self-contained HTML file with Three.js embedded — "
               "shareable without any software installation.")
        )
        self.btn_export_html.setStyleSheet("""
            QPushButton {
                padding: 10px;
                font-weight: bold;
                border-radius: 4px;
                background: #8e44ad;
                color: white;
            }
            QPushButton:hover { background: #7d3c98; }
        """)
        layout.addWidget(self.btn_export_html)

        self.btn_export_mesh = QPushButton(tr("📐  Export 3D Mesh (STL)..."))
        self.btn_export_mesh.setToolTip(tr("Export the terrain and block base as a solid STL file for 3D printing or Blender."))
        self.btn_export_mesh.setStyleSheet("""
            QPushButton {
                padding: 10px;
                font-weight: bold;
                border-radius: 4px;
                background: #e67e22;
                color: white;
            }
            QPushButton:hover { background: #d35400; }
        """)
        layout.addWidget(self.btn_export_mesh)

        layout.addStretch()
        return page

    # ── Signal wiring (single authoritative location) ────────────────────

    def _connect_all_signals(self) -> None:
        """
        Every signal connection lives here.
        Scanning this method is sufficient to understand all interactivity.
        """
         # ── WebEngine Lifecycle ──────────────────────────────────────────
        self.webview.loadFinished.connect(self._slot_web_load_finished)

        # ── View switcher ────────────────────────────────────────────────
        self.rad_view_3d.toggled.connect(self._slot_view_mode_changed)
        self.rad_view_2d.toggled.connect(self._slot_view_mode_changed)

        # ── Icon toolbar → stack pages ───────────────────────────────────
        self._tab_btn_group.idClicked.connect(self._stack.setCurrentIndex)

        # ── Page 1 — Layers ──────────────────────────────────────────────
        self.combo_raster.currentIndexChanged.connect(self._slot_selected_raster_changed)
        self.btn_render_terrain.clicked.connect(self._slot_render_terrain)
        self.list_layers.itemChanged.connect(self._slot_layer_visibility_changed)
        self.list_layers.currentItemChanged.connect(self._slot_scene_list_selection_changed)
        self.btn_add_vector.clicked.connect(self._slot_add_vector_layer)
        # ── Vector Layer Interactivity & Styling ─────────────────────────
        self.combo_vector.currentIndexChanged.connect(self._slot_selected_vector_changed)
        self.combo_drape_mode.currentIndexChanged.connect(self._slot_drape_mode_changed)
        self.combo_color_styling.currentIndexChanged.connect(self.stacked_color_settings.setCurrentIndex)
        
        self.slider_curtain_depth.valueChanged.connect(
            lambda v: self.lbl_curtain_depth_val.setText(f"{v} m")
        )
        self.slider_polygon_opacity.valueChanged.connect(
            lambda v: self.lbl_polygon_opacity_val.setText(f"{v} %")
        )
        self.slider_point_size.valueChanged.connect(
            lambda v: self.lbl_point_size_val.setText(f"{v/10.0:.1f} m")
        )
        
        self.btn_vector_fixed_color.clicked.connect(
            lambda: self._slot_pick_color_for("vector_fixed", self.btn_vector_fixed_color)
        )
        self.btn_delete_object.clicked.connect(self._slot_delete_scene_object)

        # Connect dynamic triggers to the debouncer [New]
        self.combo_drape_mode.currentIndexChanged.connect(self._slot_style_parameter_changed)
        self.slider_line_width.valueChanged.connect(self._slot_style_parameter_changed)
        self.slider_curtain_depth.valueChanged.connect(self._slot_style_parameter_changed)
        self.slider_polygon_opacity.valueChanged.connect(self._slot_style_parameter_changed)
        self.slider_point_size.valueChanged.connect(self._slot_style_parameter_changed)
        self.combo_color_styling.currentIndexChanged.connect(self._slot_style_parameter_changed)
        self.combo_vector_attribute.currentIndexChanged.connect(self._slot_style_parameter_changed)
        self.combo_vector_colormap.currentIndexChanged.connect(self._slot_style_parameter_changed)
        self.slider_height_offset.valueChanged.connect(self._slot_style_parameter_changed)
        
        # Connect labels
        self.slider_line_width.valueChanged.connect(
            lambda v: self.lbl_line_width_val.setText(f"{v} px")
        )
        self.slider_height_offset.valueChanged.connect(
            lambda v: self.lbl_height_offset_val.setText(f"{v} m")
        )
        
        # ── Page 2 — Symbology ───────────────────────────────────────────
        self.combo_symbology_render_mode.currentIndexChanged.connect(self._slot_render_mode_changed)
        self.spin_class_count.valueChanged.connect(self._rebuild_classified_brackets_ui)
        self.btn_solid_color.clicked.connect(
            lambda: self._slot_pick_color_for("solid_color", self.btn_solid_color)
        )
        self.combo_colormap.currentTextChanged.connect(self._slot_update_colormap)
        self.chk_reverse_cmap.stateChanged.connect(self._slot_update_colormap)
        self.rad_scale_manual.toggled.connect(self.spin_min_z.setEnabled)
        self.rad_scale_manual.toggled.connect(self.spin_max_z.setEnabled)
        self.spin_min_z.valueChanged.connect(self._slot_update_color_bounds)
        self.spin_max_z.valueChanged.connect(self._slot_update_color_bounds)
        self.rad_scale_auto.toggled.connect(self._slot_color_scale_mode_changed)

        # ── Page 3 — Lights ──────────────────────────────────────────────
        self.chk_multidirectional.stateChanged.connect(self._slot_toggle_multidirectional)
        self.btn_light_color.clicked.connect(
            lambda: self._slot_pick_color_for("light_color", self.btn_light_color)
        )
        self.slider_light_intensity.valueChanged.connect(self._slot_light_property_changed)
        self.slider_light_azimuth.valueChanged.connect(self._slot_light_property_changed)
        self.slider_light_altitude.valueChanged.connect(self._slot_light_property_changed)

        # ── Page 4 — Aesthetics ──────────────────────────────────────────
        self.slider_z_scale.valueChanged.connect(self._slot_update_z_scale)
        self.slider_thickness.valueChanged.connect(self._slot_update_base_thickness)
        self.btn_wall_color.clicked.connect(
            lambda: self._slot_pick_color_for("walls", self.btn_wall_color)
        )
        self.btn_base_color.clicked.connect(
            lambda: self._slot_pick_color_for("base", self.btn_base_color)
        )
        self.btn_sky_top.clicked.connect(
            lambda: self._slot_pick_color_for("sky_top", self.btn_sky_top)
        )
        self.btn_sky_bottom.clicked.connect(
            lambda: self._slot_pick_color_for("sky_bottom", self.btn_sky_bottom)
        )

        self.combo_camera.currentIndexChanged.connect(self._slot_camera_mode_changed)

        self.slider_roughness.valueChanged.connect(self._slot_update_aesthetic_shading)
        self.slider_slope_contrast.valueChanged.connect(self._slot_update_aesthetic_shading)

        # ── Page 5 — Export ──────────────────────────────────────────────
        self.btn_export_img.clicked.connect(self._slot_export_high_res_image)
        self.btn_export_html.clicked.connect(self._slot_export_interactive_html)
        self.btn_export_mesh.clicked.connect(self._slot_export_3d_mesh)

    # ── Slots ────────────────────────────────────────────────────────────

    # View switcher

    def _slot_view_mode_changed(self) -> None:
        """Swap QGIS 2D canvas and WebGL viewport."""
        if self.rad_view_3d.isChecked():
            # Copy the exact coordinates of the active 2D canvas and raise the WebGL scene
            self.webview.setGeometry(self.canvas_2d.geometry())
            self.webview.show()
            self.webview.raise_()
            self.is_3d_active = True
        else:
            self.webview.hide()
            self.canvas_2d.show()
            self.is_3d_active = False
            self.canvas_2d.refresh()

    # Layers page

    def _slot_selected_raster_changed(self) -> None:
        """Update render button label and style to reflect current selection."""
        layer = self.combo_raster.currentLayer()
        if not layer:
            self.btn_render_terrain.setEnabled(False)
            self.btn_render_terrain.setText(tr("No DEM layer selected"))
            return

        self.btn_render_terrain.setEnabled(True)
        if self._loaded_raster_id == layer.id():
            self.btn_render_terrain.setText(tr("✔  DEM active & loaded"))
            self.btn_render_terrain.setStyleSheet(
                "background-color:#7f8c8d; color:white; font-weight:bold; "
                "padding:6px; border-radius:4px;"
            )
        else:
            self.btn_render_terrain.setText(tr("⛰  Render 3D terrain"))
            self.btn_render_terrain.setStyleSheet(
                "background-color:#27ae60; color:white; font-weight:bold; "
                "padding:6px; border-radius:4px;"
            )

    def _slot_render_terrain(self) -> None:
        """
        Initiate the background computation worker to prepare the 3D terrain
        without freezing the QGIS main graphical thread.
        """
        if not self.is_3d_active:
            self.rad_view_3d.blockSignals(True)
            self.rad_view_3d.setChecked(True)
            self.rad_view_3d.blockSignals(False)
            self._slot_view_mode_changed()

        layer = self.combo_raster.currentLayer()
        if not layer:
            return
        if self._loaded_raster_id == layer.id():
            return

        self._loaded_raster_id = layer.id()
        # Wipe out python vector cache to prevent ghost vector overlays in standalone HTML exports
        self._loaded_vectors = []

        # Keep permanent system layers, only prune old vector overlays
        for i in range(self.list_layers.count() - 1, -1, -1):
            item = self.list_layers.item(i)
            elem_id = item.data(Qt.UserRole)
            if elem_id and elem_id.startswith("vector_"):
                self.list_layers.takeItem(i)

        # 1. Show the built-in professional progress feedback panel
        self.set_loading_state(True, tr("Initializing 3D computation worker..."), total=100)

        # 2. Setup and run the background thread worker
        self._worker = ComputeWorker(self.engine, {"dem_layer": layer})
        
        # Connect dynamic worker signals directly to UI slots
        self._worker.progress.connect(self.update_progress)
        self._worker.finished.connect(self._slot_on_dem_ready)
        self._worker.error.connect(self._slot_on_dem_failed)
        
        # Launch the asynchronous thread
        self._worker.start()

    
    def _slot_on_dem_ready(self, result: dict) -> None:
        """
        Triggered on the main thread when ComputeWorker finishes terrain preparation.
        Updates UI bounds and transmits the 3D payload to WebGL.
        """
        # 1. Close the progress feedback and unlock UI controls
        self.set_loading_state(False)
        
        dem_data = result.get("dem_data")
        if not dem_data:
            self.show_error(tr("Received empty dataset from computation thread."))
            return
            
        # 2. Cache and transmit the dataset to the WebGL rendering engine
        self._active_dem_data = dem_data.to_dict()
        self._js({"action": "set_main_raster", "payload": self._active_dem_data})
        
        # 3. Dynamically append structural Block control options
        self._add_layer_item(tr("🧱  Block base (walls & sole)"), element_id="block_base")
        
        # 4. Cache and update elevation boundary widgets for classified rendering
        self.active_dem_min_z = dem_data.z_min
        self.active_dem_max_z = dem_data.z_max

        self.spin_min_z.blockSignals(True)
        self.spin_max_z.blockSignals(True)
        self.spin_min_z.setValue(self.active_dem_min_z)
        self.spin_max_z.setValue(self.active_dem_max_z)
        self.spin_min_z.blockSignals(False)
        self.spin_max_z.blockSignals(False)

        # 5. Refresh colorization state based on active render mode
        if self.combo_symbology_render_mode.currentIndex() == 1:
            self._rebuild_classified_brackets_ui()
        else:
            self._slot_selected_raster_changed()

    def _slot_on_dem_failed(self, error_msg: str) -> None:
        """
        Triggered on the main thread if the background thread encounters a fatal exception.
        """
        self.set_loading_state(False)
        self._loaded_raster_id = None # Clear cached ID to allow re-trying
        self.show_error(tr(f"3D Terrain preparation failed: {error_msg}"))


    def _slot_layer_visibility_changed(self, item: QListWidgetItem) -> None:
        """Toggle visibility for a scene object identified by its stored element_id."""
        visible = (item.checkState() == Qt.Checked)
        element_id = item.data(Qt.UserRole)
        if element_id:
            self._js({
                "action": "toggle_visibility",
                "payload": {"element_id": element_id, "visible": visible}
            })

    def _slot_add_vector_layer(self) -> None:
        """
        Drape a vector layer onto the terrain surface using an asynchronous worker thread
        to keep QGIS responsive on heavy geometries.
        """
        dem_layer = self.combo_raster.currentLayer()
        vec_layer = self.combo_vector.currentLayer()
        if not vec_layer or not dem_layer or not self.is_3d_active:
            return

        # 1. Gather styling parameters directly from UI elements [No Hardcoding]
        drape_mode_idx = self.combo_drape_mode.currentIndex()
        drape_modes = ["line", "curtain", "outline", "filled", "point"]
        selected_drape_mode = drape_modes[drape_mode_idx]

        extrude_depth = 0.0
        if selected_drape_mode == "curtain":
            extrude_depth = float(self.slider_curtain_depth.value())

        polygon_opacity = 1.0
        if selected_drape_mode == "filled":
            polygon_opacity = float(self.slider_polygon_opacity.value() / 100.0)

        point_marker_size = 1.0
        if selected_drape_mode == "point":
            point_marker_size = float(self.slider_point_size.value() / 10.0)
        
        line_width = float(self.slider_line_width.value())
        height_offset = float(self.slider_height_offset.value())

        # Color configurations
        color_styling_idx = self.combo_color_styling.currentIndex()
        color_styling = "fixed" if color_styling_idx == 0 else "attribute"

        fixed_color_hex = self.btn_vector_fixed_color.property("color_hex") or "#3498db"
        
        attribute_field = ""
        vector_colormap = "terrain"
        if color_styling == "attribute":
            attribute_field = self.combo_vector_attribute.currentText()
            vector_colormap = self.combo_vector_colormap.currentText() or "terrain"

        # 2. Show progress panel feedback
        self.set_loading_state(True, tr("Draping complex geometries onto 3D terrain..."), total=100)

        # 3. Setup and dispatch the background task
        params = {
            "task_type": "prepare_vectors",
            "vector_layer": vec_layer,
            "dem_layer": dem_layer,
            "extrude_depth": extrude_depth,
            "attribute_field": attribute_field,
            # Packaging UI parameters so they return to main thread on completion (stateless)
            "style_params": {
                "selected_drape_mode": selected_drape_mode,
                "polygon_opacity": polygon_opacity,
                "point_marker_size": point_marker_size,
                "line_width": line_width,
                "height_offset": height_offset,
                "color_styling": color_styling,
                "fixed_color_hex": fixed_color_hex,
                "attribute_field": attribute_field,
                "vector_colormap": vector_colormap,
                "vec_layer_id": vec_layer.id(),
                "vec_layer_name": vec_layer.name()
            }
        }
        
        self._vector_worker = ComputeWorker(self.engine, params)
        self._vector_worker.progress.connect(self.update_progress)
        self._vector_worker.finished.connect(self._slot_on_vectors_ready)
        self._vector_worker.error.connect(self._slot_on_vectors_failed)
        self._vector_worker.start()


    def _slot_on_vectors_ready(self, result: dict) -> None:
        """
        Triggered on the main thread when ComputeWorker finishes vector draping.
        Normalizes and injects vector features into WebGL viewport.
        """
        self.set_loading_state(False)
        
        vectors = result.get("vectors")
        if not vectors:
            self.show_info(tr("No features found in the selected layer."))
            return
            
        style_params = result.get("style_params")
        if not style_params:
            return
            
        # Extract style parameters returned by the worker
        selected_drape_mode = style_params["selected_drape_mode"]
        polygon_opacity = style_params["polygon_opacity"]
        point_marker_size = style_params["point_marker_size"]
        line_width = style_params["line_width"]
        height_offset = style_params["height_offset"]
        color_styling = style_params["color_styling"]
        fixed_color_hex = style_params["fixed_color_hex"]
        attribute_field = style_params["attribute_field"]
        vector_colormap = style_params["vector_colormap"]
        vec_layer_id = style_params["vec_layer_id"]
        vec_layer_name = style_params["vec_layer_name"]

        # Calculate bounds in Python to allow immediate normalization in WebGL
        attr_min = 0.0
        attr_max = 1.0
        if color_styling == "attribute":
            valid_vals = [v.attribute_values[0] for v in vectors if v.attribute_values]
            if valid_vals:
                attr_min = min(valid_vals)
                attr_max = max(valid_vals)
                if attr_max <= attr_min:
                    attr_max = attr_min + 1.0

        # Serialize and package dynamic parameters for WebGL
        serialized_vectors = []
        for v in vectors:
            v_dict = v.to_dict()
            v_dict["geom_type"] = selected_drape_mode
            v_dict["polygon_opacity"] = polygon_opacity
            v_dict["point_marker_size"] = point_marker_size
            v_dict["line_width"] = line_width
            v_dict["height_offset"] = height_offset
            v_dict["color_styling"] = color_styling
            v_dict["vector_colormap"] = vector_colormap
            v_dict["attribute_bounds"] = {"min": attr_min, "max": attr_max}
            
            if color_styling == "fixed":
                v_dict["color"] = fixed_color_hex
                
            serialized_vectors.append(v_dict)
        
        # Cache vectors in Python for HTML export
        self._loaded_vectors.extend(serialized_vectors)

        # Single batch transaction to WebGL
        self._js({
            "action": "add_vector_batch",
            "payload": serialized_vectors
        })

        # Register in scene list
        self._add_layer_item(
            label=f"💧  {vec_layer_name}",
            element_id=f"vector_{vec_layer_id}"
        )

    def _slot_on_vectors_failed(self, error_msg: str) -> None:
        """
        Triggered on the main thread if vector draping fails.
        """
        self.set_loading_state(False)
        self.show_error(tr(f"Failed to drape vector layer: {error_msg}"))


    def _slot_selected_vector_changed(self) -> None:
        """Triggered when the selected vector layer changes. Populates field columns."""
        layer = self.combo_vector.currentLayer()
        self.combo_vector_attribute.clear()
        if not layer:
            return
        
        # Pull field names from the QGIS Vector layer provider
        fields = layer.fields()
        for field in fields:
            self.combo_vector_attribute.addItem(field.name())

    def _slot_drape_mode_changed(self, index: int) -> None:
        """Swap configuration pages based on the chosen drape visualization."""
        if index == 1:    # Curtain
            self.stacked_vector_settings.setCurrentIndex(1)
        elif index == 3:  # Polygon Filled
            self.stacked_vector_settings.setCurrentIndex(2)
        elif index == 4:  # Point Markers
            self.stacked_vector_settings.setCurrentIndex(3)
        else:             # Line (0) or Polygon Outline (2)
            self.stacked_vector_settings.setCurrentIndex(0)
            
        # Trigger an immediate style preview update
        self._slot_style_parameter_changed()
    
    def _slot_style_parameter_changed(self) -> None:
        """Debounce the change event before transmitting to WebGL."""
        if self._style_debounce_timer.isActive():
            self._style_debounce_timer.stop()
        self._style_debounce_timer.start()

    def _slot_apply_vector_style_update(self) -> None:
        """Identify selected scene object and push styling overrides to WebGL in real-time."""
        current_item = self.list_layers.currentItem()
        # Fallback: if no layer is selected, but there is exactly one vector layer, auto-select it [Fix 2]
        # Correctly filter actual vector layers (starting with 'vector_') to enable fluid automatic selection
        if not current_item and self.list_layers.count() > 0:
            items = [self.list_layers.item(i) for i in range(self.list_layers.count())]
            vector_items = [it for it in items if it.data(Qt.UserRole) and it.data(Qt.UserRole).startswith("vector_")]
            if len(vector_items) == 1:
                current_item = vector_items[0]
                self.list_layers.setCurrentItem(current_item)

        if not current_item or not self.is_3d_active:
            return

        element_id = current_item.data(Qt.UserRole)
        # Skip if system layout layers are selected
        if not element_id or element_id in ["block_base", "scene_grid", "scene_axes"]:
            return
            
        
        # Read values from widgets
        drape_mode_idx = self.combo_drape_mode.currentIndex()
        drape_modes = ["line", "curtain", "outline", "filled", "point"]
        selected_drape_mode = drape_modes[drape_mode_idx]

        # Explicitly isolate parameter values to prevent attribute leakage [Fix 1]
        extrude_depth = 0.0
        if selected_drape_mode == "curtain":
            extrude_depth = float(self.slider_curtain_depth.value())

        polygon_opacity = 1.0
        if selected_drape_mode == "filled":
            polygon_opacity = float(self.slider_polygon_opacity.value() / 100.0)

        point_marker_size = 1.0
        if selected_drape_mode == "point":
            point_marker_size = float(self.slider_point_size.value() / 10.0)

        line_width = float(self.slider_line_width.value())
        height_offset = float(self.slider_height_offset.value())

        # Colors
        color_styling_idx = self.combo_color_styling.currentIndex()
        color_styling = "fixed" if color_styling_idx == 0 else "attribute"
        fixed_color_hex = self.btn_vector_fixed_color.property("color_hex") or "#3498db"
        attribute_field = self.combo_vector_attribute.currentText()
        vector_colormap = self.combo_vector_colormap.currentText() or "terrain"

        self._js({
            "action": "update_vector_style",
            "payload": {
                "element_id": element_id,
                "geom_type": selected_drape_mode,
                "extrude_depth": extrude_depth,
                "polygon_opacity": polygon_opacity,
                "point_marker_size": point_marker_size,
                "line_width": line_width,
                "height_offset": height_offset,
                "color_styling": color_styling,
                "fixed_color": fixed_color_hex,
                "attribute_field": attribute_field,
                "vector_colormap": vector_colormap
            }
        })



    # ── Scene List Control and Safety Guards ─────────────────────────────

    def _slot_scene_list_selection_changed(self, current, previous) -> None:
        """Disable the delete control when system/permanent objects are selected."""
        if not current:
            self.btn_delete_object.setEnabled(False)
            return
            
        element_id = current.data(Qt.UserRole)
        # Protect permanent system elements from deletion
        if element_id in ["block_base", "scene_grid", "scene_axes"]:
            self.btn_delete_object.setEnabled(False)
            self.btn_delete_object.setStyleSheet("""
                QPushButton { background-color: #7f8c8d; color: #ccc; border-radius: 4px; }
            """)
        else:
            self.btn_delete_object.setEnabled(True)
            self.btn_delete_object.setStyleSheet("""
                QPushButton {
                    background-color: #e74c3c;
                    color: white;
                    font-weight: bold;
                    border-radius: 4px;
                }
                QPushButton:hover { background-color: #c0392b; }
            """)

    def _slot_delete_scene_object(self) -> None:
        """Delete the currently selected vector object from WebGL and UI."""
        current_item = self.list_layers.currentItem()
        if not current_item:
            return

        element_id = current_item.data(Qt.UserRole)
        if not element_id or element_id in ["block_base", "scene_grid", "scene_axes"]:
            return # Protect system layers from deletion

        # Send deletion transaction to the WebGL rendering engine
        self._js({
            "action": "remove_vector_layer",
            "payload": {"element_id": element_id}
        })

        # Remove from PyQt list
        self.list_layers.takeItem(self.list_layers.row(current_item))
        # Remove from Python export cache
        self._loaded_vectors = [v for v in self._loaded_vectors if not v["element_id"].startswith(element_id)]



    # Symbology page

    def _slot_update_colormap(self) -> None:
        if not self.is_3d_active:
            return
        self._js({
            "action": "set_colormap",
            "payload": {
                "name": self.combo_colormap.currentText(),
                "reverse": self.chk_reverse_cmap.isChecked(),
            }
        })

    def _slot_update_color_bounds(self) -> None:
        if not self.is_3d_active or self.rad_scale_auto.isChecked():
            return
        self._js({
            "action": "set_color_bounds",
            "payload": {
                "min_z": self.spin_min_z.value(),
                "max_z": self.spin_max_z.value(),
            }
        })

    def _slot_color_mode_changed(self) -> None:
        """Triggered when switching between Colormap and Solid Color modes."""
        if not self.is_3d_active:
            return
            
        if self.rad_mode_solid.isChecked():
            hex_color = self.btn_solid_color.property("color_hex") or "#4a90d9"
            self._js({"action": "set_solid_color", "payload": {"color": hex_color}})
        else:
            self._slot_update_colormap()

    def _slot_color_scale_mode_changed(self) -> None:
        """Triggered when switching between Auto and Custom Z bounds."""
        if not self.is_3d_active:
            return
            
        if self.rad_scale_auto.isChecked():
            self._js({"action": "set_color_bounds_auto", "payload": {}})
        else:
            self._slot_update_color_bounds()
    
    # Symbology slots
    def _sample_active_colormap(self, position: float) -> str:
        """
        Sample a hex color from the active colormap loaded from colormaps.json.
        Position is a float between 0.0 and 1.0.
        """
        position = max(0.0, min(1.0, position))
        if self.chk_reverse_cmap.isChecked():
            position = 1.0 - position

        # Strip whitespace and convert to lowercase for deterministic lookup
        colormap_name = (self.combo_colormap.currentText() or "terrain").strip().lower()

        try:
            # 1. Lazy load the colormaps file once to optimize performance
            if not hasattr(self, "_colormaps_cache"):
                if json_file.exists():
                    with open(json_file, "r", encoding="utf-8") as f:
                        self._colormaps_cache = json.load(f)
                else:
                    self._colormaps_cache = {}

            # 2. Case-insensitive dictionary mapping to avoid case mismatch failures
            lowercase_cache = {key.lower(): val for key, val in self._colormaps_cache.items()}

            # Extract stops, falling back to lowercase 'terrain'
            stops = lowercase_cache.get(colormap_name) or lowercase_cache.get("terrain")
            if not stops:
                return self._fallback_terrain_gradient(position)

            # 3. Parse stops cleanly supporting multiple potential formats
            parsed_stops = []
            for item in stops:
                if isinstance(item, list) and len(item) == 2 and isinstance(item[1], list):
                    pos = float(item[0])
                    rgb = item[1]
                elif isinstance(item, list) and len(item) == 4:
                    pos = float(item[0])
                    rgb = item[1:4]
                elif isinstance(item, dict):
                    pos = float(item.get("pos", item.get("position", 0.0)))
                    rgb = item.get("rgb", [item.get("r", 0), item.get("g", 0), item.get("b", 0)])
                else:
                    continue

                # Scale float 0..1 RGB to 0..255 integers
                r = int(rgb[0] * 255) if isinstance(rgb[0], float) and rgb[0] <= 1.0 else int(rgb[0])
                g = int(rgb[1] * 255) if isinstance(rgb[1], float) and rgb[1] <= 1.0 else int(rgb[1])
                b = int(rgb[2] * 255) if isinstance(rgb[2], float) and rgb[2] <= 1.0 else int(rgb[2])
                parsed_stops.append((pos, (r, g, b)))

            parsed_stops.sort(key=lambda x: x[0])

            # 4. Handle boundary endpoints
            if position <= parsed_stops[0][0]:
                c = parsed_stops[0][1]
                return f"#{c[0]:02x}{c[1]:02x}{c[2]:02x}"
            if position >= parsed_stops[-1][0]:
                c = parsed_stops[-1][1]
                return f"#{c[0]:02x}{c[1]:02x}{c[2]:02x}"

            # 5. Linear interpolation
            for i in range(len(parsed_stops) - 1):
                p0, c0 = parsed_stops[i]
                p1, c1 = parsed_stops[i + 1]
                if p0 <= position <= p1:
                    t = (position - p0) / (p1 - p0) if (p1 - p0) > 0 else 0.0
                    r = int(c0[0] + (c1[0] - c0[0]) * t)
                    g = int(c0[1] + (c1[1] - c0[1]) * t)
                    b = int(c0[2] + (c1[2] - c0[2]) * t)
                    return f"#{r:02x}{g:02x}{b:02x}"

        except Exception as e:
            print(f"[RockMorph] Error sampling colormap, using fallback: {e}")

        return self._fallback_terrain_gradient(position)

    def _fallback_terrain_gradient(self, position: float) -> str:
        """
        Calculates a beautiful terrain profile (Blue -> Green -> Brown -> White)
        if the JSON cannot be loaded or parsed.
        """
        terrain_stops = [
            (0.000, (51, 102, 204)),    # Deep Blue
            (0.150, (65, 152, 175)),    # Aqua
            (0.250, (230, 240, 150)),   # Sand
            (0.400, (34, 139, 34)),     # Forest Green
            (0.650, (139, 115, 85)),    # Earth Brown
            (0.850, (90, 70, 50)),      # Rocky Dark Brown
            (1.000, (255, 255, 255))    # Snow White
        ]
        position = max(0.0, min(1.0, position))
        for i in range(len(terrain_stops) - 1):
            p0, c0 = terrain_stops[i]
            p1, c1 = terrain_stops[i + 1]
            if p0 <= position <= p1:
                t = (position - p0) / (p1 - p0) if (p1 - p0) > 0 else 0.0
                r = int(c0[0] + (c1[0] - c0[0]) * t)
                g = int(c0[1] + (c1[1] - c0[1]) * t)
                b = int(c0[2] + (c1[2] - c0[2]) * t)
                return f"#{r:02x}{g:02x}{b:02x}"
        return "#228b22"



    def _slot_render_mode_changed(self, index: int) -> None:
        """Swap active configuration page and show/hide bounds settings dynamically."""
        self.stacked_symbology_settings.setCurrentIndex(index)
        
        # Hide bounds UI group entirely in Solid Mode, show it for Continuous and Classified
        if index == 2:  # Solid Uniform Fill
            self.bounds_configuration_panel.setVisible(False)
        else:
            self.bounds_configuration_panel.setVisible(True)

        if not self.is_3d_active:
            return
            
        if index == 0:  # Continuous Colormap
            self._slot_color_scale_mode_changed()
            self._slot_update_colormap()
        elif index == 1:  # Classified
            self._slot_color_scale_mode_changed()
            self._rebuild_classified_brackets_ui()
        elif index == 2:  # Solid Color
            hex_color = self.btn_solid_color.property("color_hex") or "#4a90d9"
            self._js({"action": "set_solid_color", "payload": {"color": hex_color}})

    def _rebuild_classified_brackets_ui(self) -> None:
        """Regenerate dynamic rows dividing the Z span into equal intervals, sampling the active colormap."""
        min_z = self.active_dem_min_z if self.rad_scale_auto.isChecked() else self.spin_min_z.value()
        max_z = self.active_dem_max_z if self.rad_scale_auto.isChecked() else self.spin_max_z.value()
        
        num_classes = int(self.spin_class_count.value())
        if max_z <= min_z:
            max_z = min_z + 1.0

        interval = (max_z - min_z) / num_classes

        # Clear existing rows in the UI layout
        while self.layout_classes_list.count():
            item = self.layout_classes_list.takeAt(0)
            if item.widget():
                item.widget().deleteLater()

        # Adjust and generate default colors using the SELECTED colormap [Bug C Fix]
        if len(self._class_colors) != num_classes:
            old_colors = self._class_colors
            self._class_colors = []
            for i in range(num_classes):
                if i < len(old_colors):
                    self._class_colors.append(old_colors[i])
                else:
                    t = i / max(1, num_classes - 1)
                    sampled_hex = self._sample_active_colormap(t)
                    self._class_colors.append(sampled_hex)

        self._class_bounds = []

        for i in range(num_classes):
            c_min = min_z + i * interval
            c_max = min_z + (i + 1) * interval
            self._class_bounds.append(c_max)

            active_hex = self._class_colors[i]

            row = QWidget()
            row_layout = QHBoxLayout(row)
            row_layout.setContentsMargins(0, 2, 0, 2)

            label_text = f"Bracket {i+1}: {c_min:.1f}m - {c_max:.1f}m"
            row_layout.addWidget(QLabel(label_text))

            btn_color = _make_color_btn(tr("Select"), active_hex)
            btn_color.setProperty("class_idx", i)
            btn_color.clicked.connect(self._slot_pick_class_color)
            row_layout.addWidget(btn_color)

            self.layout_classes_list.addWidget(row)

        self._slot_update_classified_shading()

    def _slot_update_colormap(self) -> None:
        if not self.is_3d_active:
            return
            
        # If currently in Classified Mode, reset and rebuild classes with the new colormap
        if self.combo_symbology_render_mode.currentIndex() == 1:
            self._class_colors = []
            self._rebuild_classified_brackets_ui()
        else:
            self._js({
                "action": "set_colormap",
                "payload": {
                    "name": self.combo_colormap.currentText(),
                    "reverse": self.chk_reverse_cmap.isChecked(),
                }
            })

    def _slot_update_color_bounds(self) -> None:
        """Route bounds change to the correct render mode [Bug A Fix]"""
        if not self.is_3d_active or self.rad_scale_auto.isChecked():
            return
            
        render_mode = self.combo_symbology_render_mode.currentIndex()
        if render_mode == 0:  # Continuous Colormap
            self._js({
                "action": "set_color_bounds",
                "payload": {
                    "min_z": self.spin_min_z.value(),
                    "max_z": self.spin_max_z.value(),
                }
            })
        elif render_mode == 1:  # Classified
            self._rebuild_classified_brackets_ui()

    def _slot_color_scale_mode_changed(self) -> None:
        """Triggered when switching between Auto and Custom Z bounds [Bug A Fix]"""
        if not self.is_3d_active:
            return
            
        render_mode = self.combo_symbology_render_mode.currentIndex()
        if render_mode == 0:  # Continuous Colormap
            if self.rad_scale_auto.isChecked():
                self._js({"action": "set_color_bounds_auto", "payload": {}})
            else:
                self._slot_update_color_bounds()
        elif render_mode == 1:  # Classified
            self._rebuild_classified_brackets_ui()

    def _slot_pick_class_color(self) -> None:
        """Open native picker to customize a specific class's color."""
        button = self.sender()
        if not button:
            return
        class_idx = button.property("class_idx")
        current_hex = self._class_colors[class_idx]
        color = QColorDialog.getColor(QColor(current_hex), self, tr("Select class color"))
        if not color.isValid():
            return

        _apply_color_to_btn(button, color)
        self._class_colors[class_idx] = color.name()
        # Use renamed method
        self._slot_update_classified_shading()

    def _slot_update_classified_shading(self) -> None:
        """Serialize current bounds and color mapping, then push to the WebGL rendering thread."""
        if not self.is_3d_active or self.combo_symbology_render_mode.currentIndex() != 1:
            return
        # Bounds has length N-1
        self._js({
            "action": "set_classified_colors",
            "payload": {
                "bounds": self._class_bounds[:-1],
                "colors": self._class_colors
            }
        })

    def _slot_update_classified_colors(self) -> None:
        """Fallback alias to prevent crashes from any external references."""
        self._slot_update_classified_shading()


    # Lighting page
    def _slot_light_property_changed(self) -> None:
        """Read sun parameters and update the authoritative light representation."""
        if not self.is_3d_active:
            return
            
        light = {
            "id": "light_0",
            "color": self.btn_light_color.property("color_hex") or "#ffffff",
            "intensity": self.slider_light_intensity.value() / 10.0,
            "azimuth": self.slider_light_azimuth.value(),
            "altitude": self.slider_light_altitude.value(),
            "gizmo": False,
        }
        self._js({"action": "update_light", "payload": light})

    def _slot_toggle_multidirectional(self, state: int) -> None:
        """Enable or disable 4-Axis GIS hillshading."""
        if not self.is_3d_active:
            return
        enabled = (state == Qt.Checked)
        self._js({
            "action": "set_multidirectional_shading",
            "payload": {"enabled": enabled}
        })
    
    # Aesthetics page

    def _slot_update_z_scale(self, value: int) -> None:
        scale = value / 10.0
        self.lbl_z_value.setText(f"{scale:.1f} ×")
        if not self.is_3d_active:
            return
        self._js({"action": "update_z_scale", "payload": {"scale": scale}})

    def _slot_update_base_thickness(self, value: int) -> None:
        self.lbl_thick_value.setText(f"{value} %")
        if not self.is_3d_active:
            return
        self._js({"action": "update_base_thickness", "payload": {"thickness": value / 100.0}})

    def _slot_update_aesthetic_shading(self) -> None:
        """Read topography rendering enhancements and send parameters to the GLSL shader."""
        if not self.is_3d_active:
            return
        roughness = self.slider_roughness.value() / 100.0
        slope_contrast = self.slider_slope_contrast.value() / 100.0
        self._js({
            "action": "update_aesthetic_shading",
            "payload": {
                "roughness": roughness,
                "slope_contrast": slope_contrast
            }
        })

    def _slot_camera_mode_changed(self) -> None:
        """Trigger camera projection swap between perspective and orthographic parallel modes."""
        if not self.is_3d_active:
            return
        mode = "ortho" if self.combo_camera.currentIndex() == 1 else "persp"
        self._js({
            "action": "set_camera_projection",
            "payload": {"mode": mode}
        })
        if mode == "ortho":
            self.show_info(tr("Orthographic projection enabled. Distances are now dimensionally consistent."))

    # Export page

    def _slot_export_high_res_image(self) -> None:
        """Trigger the standard image export pipeline for the 3D WebGL viewport."""
        # 1. Open a clean QMenu popup on the button to let the user select the format
        menu = QMenu(self)
        png_action = menu.addAction(tr("Export as PNG Image (.png)..."))
        jpg_action = menu.addAction(tr("Export as JPEG Image (.jpg)..."))
        pdf_action = menu.addAction(tr("Export as PDF Document (.pdf)..."))
        
        chosen = menu.exec_(self.btn_export_img.mapToGlobal(self.btn_export_img.rect().bottomLeft()))
        if not chosen:
            return
            
        fmt = "png"
        if chosen == jpg_action:
            fmt = "jpg"
        elif chosen == pdf_action:
            fmt = "pdf"
            
        # 2. Call the centralized parent exporter to handle quality and file dialogues
        ok, path, dpi = self._exporter.prepare_image_export(fmt, parent=self)
        if not ok:
            return
            
        self._pending_export_path = path
        self._pending_export_dpi = dpi
        
        # 3. Request the WebGL engine thread to render and return the viewport image
        self._js({
            "action": "export_viewport",
            "payload": {"format": fmt, "dpi": dpi}
        })


    # ── High-Quality Exporters ───────────────────────────────────────────

    def _slot_export_3d_mesh(self) -> None:
        """Trigger solid STL mesh export."""
        path, _ = QFileDialog.getSaveFileName(
            self, tr("Export 3D Mesh"), "rockmorph_terrain_model.stl", "STL Files (*.stl)"
        )
        if not path:
            return
            
        self._pending_export_path = path
        
        self._js({
            "action": "export_stl"
        })

   
    def _slot_export_interactive_html(self) -> None:
        """Export the active 3D scene as a standalone, double-clickable interactive HTML file."""
        if not self._active_dem_data:
            layer = self.combo_raster.currentLayer()
            if layer:
                dem_data = self.engine.prepare_dem(layer)
                self._active_dem_data = dem_data.to_dict()
            else:
                self.show_error(tr("No active terrain loaded. Please render a MNT first."))
                return

        path, _ = QFileDialog.getSaveFileName(
            self, tr("Export Interactive HTML"), "rockmorph_3d_scene.html", "HTML Files (*.html)"
        )
        if not path:
            return

        # 1. Test internet connection to switch dynamically between CDN and offline bundling
        has_net = False
        import urllib.request
        try:
            urllib.request.urlopen("https://cdnjs.cloudflare.com", timeout=1.5)
            has_net = True
        except Exception:
            pass

        # 2. Extract ONLY the active colormap stops in Python to minimize HTML weight
        current_cmap = self.combo_colormap.currentText() or "terrain"
        active_ramp = []
        try:
            with open(json_file, "r", encoding="utf-8") as f:
                colormaps_dict = json.load(f)
            stops = colormaps_dict.get(current_cmap.lower()) or colormaps_dict.get("terrain")
            if stops:
                for item in stops:
                    pos = 0.0
                    rgb = [0.0, 0.0, 0.0]
                    
                    # Format A: [position, [r, g, b]]
                    if isinstance(item, list) and len(item) == 2 and isinstance(item[1], list):
                        pos = float(item[0])
                        rgb = item[1]
                    # Format B: [position, r, g, b]
                    elif isinstance(item, list) and len(item) == 4:
                        pos = float(item[0])
                        rgb = item[1:4]
                    # Format C: Dictionary object {"pos": position, "rgb": [r, g, b]} or direct keys
                    elif isinstance(item, dict):
                        pos = float(item.get("pos", item.get("position", 0.0)))
                        if "rgb" in item:
                            rgb = item["rgb"]
                        else:
                            rgb = [item.get("r", 0.0), item.get("g", 0.0), item.get("b", 0.0)]
                    else:
                        continue
                    
                    # Normalize RGB values strictly to [0.0, 1.0] floats for WebGL / Three.js
                    r = float(rgb[0] / 255.0) if isinstance(rgb[0], (int, float)) and rgb[0] > 1.0 else float(rgb[0])
                    g = float(rgb[1] / 255.0) if isinstance(rgb[1], (int, float)) and rgb[1] > 1.0 else float(rgb[1])
                    b = float(rgb[2] / 255.0) if isinstance(rgb[2], (int, float)) and rgb[2] > 1.0 else float(rgb[2])
                    
                    active_ramp.append({
                        "pos": pos,
                        "r": r,
                        "g": g,
                        "b": b
                    })
        except Exception as e:
            print(f"[RockMorph] Error parsing active colormap for standalone export: {e}")

        # 3. Compile the structured 3D scene dataset (Now featuring the lightweight active ramp)
        scene_data = {
            "raster": self._active_dem_data,
            "vectors": self._loaded_vectors,
            "style": {
                "z_scale": self.slider_z_scale.value() / 10.0,
                "thickness": self.slider_thickness.value() / 100.0,
                "walls_color": self._colors["walls"],
                "base_color": self._colors["base"],
                "sky_top": self._colors["sky_top"],
                "sky_bottom": self._colors["sky_bottom"],
                "colormap": current_cmap,
                "reverse_cmap": self.chk_reverse_cmap.isChecked(),
                "color_mode": "colormap" if self.combo_symbology_render_mode.currentIndex() == 0 else ("classified" if self.combo_symbology_render_mode.currentIndex() == 1 else "solid"),
                "solid_color": self.btn_solid_color.property("color_hex") or "#4a90d9",
                "class_colors": self._class_colors,
                "class_bounds": self._class_bounds,
                "active_ramp": active_ramp,  # Direct, lightweight palette injection
                "roughness": self.slider_roughness.value() / 100.0,
                "slope_contrast": self.slider_slope_contrast.value() / 100.0,
                "multidirectional": self.chk_multidirectional.isChecked(),
            }
        }

        # 4. Read template HTML
        web_dir = self._web_dir()
        template_path = os.path.join(web_dir, "explorer3d.html")
        with open(template_path, "r", encoding="utf-8") as f:
            html = f.read()

        # Inject the single active dataset script into the header
        data_script = f"\n    <script>const ACTIVE_SCENE_DATA = {json.dumps(scene_data)};</script>\n"
        html = html.replace("<head>", f"<head>{data_script}")

        # 5. Regex-based script inliner (automatically clears offline bridging assets)
        import re
        pattern = re.compile(
            r'<\s*script\s+src=["\']js/([^"\']+)["\']\s*>\s*<\s*/\s*script\s*>',
            re.IGNORECASE
        )

        def replacer(match):
            js_filename = match.group(1)
            # Remove bridging and debugging files from standalone compilation
            if js_filename in ["qwebchannel.js", "bridge.js", "lil-gui.umd.min.js", "OrbitControls.js", "colormaps_data.js"]:
                return ""

            if has_net:
                if js_filename == "three.min.js":
                    return '<script src="https://cdnjs.cloudflare.com/ajax/libs/three.js/r128/three.min.js"></script>'
                elif js_filename == "TrackballControls.js":
                    return '<script src="https://cdn.jsdelivr.net/npm/three@0.128.0/examples/js/controls/TrackballControls.js"></script>'

            # Inline remaining local files
            js_path = os.path.join(web_dir, "js", js_filename)
            if os.path.exists(js_path):
                try:
                    with open(js_path, "r", encoding="utf-8") as js_f:
                        return f'<script>\n{js_f.read()}\n</script>'
                except Exception:
                    pass
            return ""

        html = pattern.sub(replacer, html)

        # 6. Save compiled bundle
        try:
            with open(path, "w", encoding="utf-8") as f:
                f.write(html)
            self.show_info(tr(f"Standalone interactive HTML exported successfully → {os.path.basename(path)}"))
        except Exception as e:
            self.show_error(tr(f"HTML Export failed: {e}"))



    def _save_export(self, data_url: str) -> None:
        """
        Override to intercept STL exports (including high-volume chunked streams),
        and safely delegate standard image/PDF exports to BasePanel.
        """
        try:
            fmt = os.path.splitext(self._pending_export_path)[1].lower().lstrip('.')
            
            if fmt == 'stl':
                if ',' in data_url:
                    header, payload = data_url.split(',', 1)
                    
                    # Case A: Chunked transmission to bypass QWebChannel limits on large meshes
                    if ';chunk' in header:
                        meta = {}
                        for part in header.split(';'):
                            if '=' in part:
                                key, val = part.split('=', 1)
                                meta[key] = int(val)
                                
                        idx = meta.get('index', 0)
                        total = meta.get('total', 1)
                        
                        # Open file in write mode ('w') for the first chunk, append mode ('a') for the rest
                        mode = 'w' if idx == 0 else 'a'
                        
                        from urllib.parse import unquote
                        raw_ascii_stl = unquote(payload)
                        with open(self._pending_export_path, mode, encoding='utf-8') as f:
                            f.write(raw_ascii_stl)
                            
                        # Show non-blocking progress inside the QGIS message bar
                        if idx == total - 1:
                            self.show_info(tr(f"3D Model successfully exported to STL: {os.path.basename(self._pending_export_path)}"))
                        else:
                            self.show_info(tr(f"Exporting 3D Model: processing block {idx+1}/{total}..."))
                        return
                    
                    # Case B: Legacy fallback (Direct Base64 encoded payload)
                    elif 'base64' in header:
                        stl_bytes = base64.b64decode(payload)
                        with open(self._pending_export_path, 'wb') as f:
                            f.write(stl_bytes)
                        self.show_info(tr(f"3D Model successfully exported to STL: {os.path.basename(self._pending_export_path)}"))
                        return
                    
                    # Case C: Standard single raw payload
                    else:
                        from urllib.parse import unquote
                        raw_ascii_stl = unquote(payload)
                        with open(self._pending_export_path, 'w', encoding='utf-8') as f:
                            f.write(raw_ascii_stl)
                        self.show_info(tr(f"3D Model successfully exported to STL: {os.path.basename(self._pending_export_path)}"))
                        return
                        
            # Let the parent base class (BasePanel / RockMorphExporter) save images/PDFs natively
            super()._save_export(data_url)
        except Exception as e:
            self.show_error(tr(f"Export failed: {e}"))



    # ── Shared color picker ──────────────────────────────────────────────

    def _slot_pick_color_for(self, target: str, button: QPushButton) -> None:
        """
        Unified color picker.
        `target` is either a key in self._colors or a special role ('light_color',
        'solid_color').
        """
        current_hex = button.property("color_hex") or "#ffffff"
        color = QColorDialog.getColor(QColor(current_hex), self, tr("Select color"))
        if not color.isValid():
            return

        _apply_color_to_btn(button, color)

        if target in self._colors:
            self._colors[target] = color.name()
            self._js({
                "action": f"set_{target}_color",
                "payload": {"color": color.name()}
            })
        elif target == "light_color":
            idx = self._selected_light_idx
            if 0 <= idx < len(self._lights):
                self._lights[idx]["color"] = color.name()
                if self.is_3d_active:
                    self._js({"action": "update_light", "payload": self._lights[idx]})
        elif target == "solid_color":
            if self.is_3d_active:
                self._js({"action": "set_solid_color", "payload": {"color": color.name()}})
        
        elif target == "vector_fixed":
            # Immediately notify WebGL of the updated swatch color [Fix 1]
            self._slot_style_parameter_changed()

    # ── Internal helpers ─────────────────────────────────────────────────

    # ── Shared WebEngine Communication and Queue Management ──────────────

    def _slot_web_load_finished(self, ok: bool) -> None:
        """Handler for WebEngine view load completion. Flushes deferred commands."""
        if ok:
            self._web_ready = True
            print(f"[RockMorph] WebEngine ready. Flushing {len(self._pending_commands)} pending commands.")
            for cmd in self._pending_commands:
                self._js_direct(cmd)
            self._pending_commands.clear()
        else:
            print("[RockMorph] Failed to load WebGL WebEngine page.")

    def _js_direct(self, command: dict) -> None:
        """Executes javascript call immediately on the WebEngine page thread."""
        self.webview.page().runJavaScript(
            f"processPythonCommand({json.dumps(command)});"
        )

    def _js(self, command: dict) -> None:
        """Send a JSON command to the WebGL viewport, queuing it if the WebEngine is still initializing."""
        print(f"[Python -> JS Command] Dispatching: {command.get('action')}")
        if not self._web_ready:
            print(f"[RockMorph] WebEngine offline/loading. Queueing action: {command.get('action')}")
            self._pending_commands.append(command)
        else:
            self._js_direct(command)


    def _add_layer_item(self, label: str, element_id: str) -> QListWidgetItem:
        """Add a checkable item to the scene registry list and return it."""
        item = QListWidgetItem(label)
        item.setFlags(item.flags() | Qt.ItemIsUserCheckable)
        item.setCheckState(Qt.Checked)
        item.setData(Qt.UserRole, element_id)   # authoritative ID used in JS commands
        self.list_layers.addItem(item)
        return item


    def _add_labeled_slider(
        self,
        parent_layout,
        label_text: Optional[str],
        attr_name: str,
        min_val: int,
        max_val: int,
        default: int,
        tooltip: str = "",
    ) -> None:
        """
        Create a QSlider, attach it to self.<attr_name>, and optionally add
        a label above it. Centralises slider construction to avoid repetition.
        """
        if label_text:
            lbl = QLabel(label_text)
            parent_layout.addWidget(lbl)

        slider = QSlider(Qt.Horizontal)
        slider.setRange(min_val, max_val)
        slider.setValue(default)
        if tooltip:
            slider.setToolTip(tooltip)

        setattr(self, attr_name, slider)
        parent_layout.addWidget(slider)

    
    # ── Real-Time Layout Synchronization ──────────────────────────────────

    def eventFilter(self, obj, event) -> bool:
        """Intercept central container resize events to synchronize WebGL container geometry."""
        from qgis.PyQt.QtCore import QEvent  # type: ignore
        if obj == self.central_container and event.type() == QEvent.Resize:
            if self.is_3d_active:
                # Force the webview to perfectly match the size and position of the 2D canvas
                self.webview.setGeometry(self.canvas_2d.geometry())
        return super().eventFilter(obj, event)


    # ── BasePanel required overrides ─────────────────────────────────────
    
    def _on_compute(self) -> None:
        """Satisfy the abstract base class requirement without blocking QGIS."""
        pass  # Computation is driven by UI events in our background worker threads

    def _on_result(self, data: dict) -> None:
        pass  # Results are piped in real-time via _js().

    def cleanup(self) -> None:
        """Restore QGIS 2D canvas and detach the WebGL widget on plugin unload."""
        if self.is_3d_active:
            self.rad_view_2d.setChecked(True)
        if self.central_container:
            self.central_container.removeEventFilter(self)
        if self.webview:
            self.webview.setParent(None)