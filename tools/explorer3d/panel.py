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

import json
from pathlib import Path
from typing import Optional

from qgis.PyQt.QtWidgets import (  # type: ignore
    QWidget, QVBoxLayout, QHBoxLayout, QLabel, QPushButton,
    QGroupBox, QSlider, QComboBox, QCheckBox, QFrame,
    QRadioButton, QStackedWidget, QListWidget, QListWidgetItem,
    QColorDialog, QDoubleSpinBox, QToolButton, QButtonGroup,
    QSizePolicy, QSpacerItem, QScrollArea
)
from qgis.PyQt.QtCore import Qt, QSize, QCoreApplication  # type: ignore
from qgis.PyQt.QtGui import QColor, QIcon, QPixmap, QPainter  # type: ignore
from qgis.PyQt.QtWebEngineWidgets import QWebEnginePage  # type: ignore
from qgis.core import QgsMapLayerProxyModel  # type: ignore
from qgis.gui import QgsMapLayerComboBox  # type: ignore

from ...base.base_panel import BasePanel
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
        self.canvas_2d = self.iface.mapCanvas()
        self.central_container = self.canvas_2d.parentWidget()
        self.central_layout = self.central_container.layout()
        self.central_layout.addWidget(self.webview)
        self.webview.hide() 

        # Redirect JS console to QGIS Python Console
        debug_page = DebugWebEnginePage(self.webview)
        self.webview.setPage(debug_page)
        debug_page.setWebChannel(self._channel)

        # ── Wire ALL signals in one place ────────────────────────────────
        self._connect_all_signals()

        # Initial UI state
        self._slot_view_mode_changed()        
        self._slot_selected_raster_changed()
        # self._refresh_light_panel()

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

        # DEM selection
        lbl_dem = QLabel(tr("Elevation DEM layer:"))
        lbl_dem.setStyleSheet("font-weight: bold;")
        layout.addWidget(lbl_dem)

        self.combo_raster = QgsMapLayerComboBox()
        self.combo_raster.setFilters(QgsMapLayerProxyModel.RasterLayer)
        self.combo_raster.setToolTip(tr("Select the raster layer to use as terrain surface."))
        layout.addWidget(self.combo_raster)

        self.btn_render_terrain = QPushButton(tr("⛰  Render 3D Terrain"))
        self.btn_render_terrain.setToolTip(tr("Load the selected DEM into the 3D viewport."))
        self.btn_render_terrain.setStyleSheet("""
            QPushButton {
                background-color: #27ae60;
                color: white;
                font-weight: bold;
                padding: 6px;
                border-radius: 4px;
            }
            QPushButton:disabled {
                background-color: #aaa;
                color: #eee;
            }
            QPushButton:hover:!disabled { background-color: #219a52; }
        """)
        layout.addWidget(self.btn_render_terrain)

        # Separator
        sep = QFrame()
        sep.setFrameShape(QFrame.HLine)
        layout.addWidget(sep)

        # Scene registry
        lbl_scene = QLabel(tr("Scene objects:"))
        lbl_scene.setStyleSheet("font-weight: bold;")
        layout.addWidget(lbl_scene)

        self.list_layers = QListWidget()
        self.list_layers.setToolTip(tr("Check or uncheck to toggle visibility of each scene object."))
        self.list_layers.setStyleSheet("""
            QListWidget {
                border: 1px solid #ccc;
                border-radius: 4px;
                background: #fafafa;
            }
            QListWidget::item { padding: 4px; }
            QListWidget::item:selected { background: #d0e8ff; color: #000; }
        """)
        self.list_layers.setMaximumHeight(140)

        # Default permanent entry
        self._add_layer_item(tr("🧱  Block base (walls & sole)"), element_id="block_base")
        layout.addWidget(self.list_layers)

        # Vector overlay
        sep2 = QFrame()
        sep2.setFrameShape(QFrame.HLine)
        layout.addWidget(sep2)

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

        layout.addStretch()
        return page

    # ── Page 2 — Symbology ───────────────────────────────────────────────

    def _build_page_symbology(self) -> QWidget:
        page = QWidget()
        layout = QVBoxLayout(page)
        layout.setContentsMargins(0, 8, 0, 0)
        layout.setSpacing(8)

        # 1. Unified Render Mode Selector
        lbl_mode = QLabel(tr("Symbology render mode:"))
        lbl_mode.setStyleSheet("font-weight: bold;")
        layout.addWidget(lbl_mode)

        self.combo_render_mode = QComboBox()
        self.combo_render_mode.addItems([
            tr("Continuous Colormap (Scientific Pseudocolor)"),
            tr("Classified Intervals (Discrete Brackets)"),
            tr("Solid Uniform Fill")
        ])
        layout.addWidget(self.combo_render_mode)

        # 2. Stacked Settings Area for cleaner layout
        self.stacked_symbology = QStackedWidget()
        
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
        self.stacked_symbology.addWidget(page_continuous)

        # --- Page 1: Classified Settings ---
        page_classified = QWidget()
        layout_class = QVBoxLayout(page_classified)
        layout_class.setContentsMargins(0, 4, 0, 4)
        layout_class.setSpacing(6)

        row_count = QHBoxLayout()
        row_count.addWidget(QLabel(tr("Number of classes:")))
        self.spin_class_count = QDoubleSpinBox() # Using DoubleSpinBox mapped as integer for consistency
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
        self.stacked_symbology.addWidget(page_classified)

        # --- Page 2: Solid Fill Settings ---
        page_solid = QWidget()
        layout_solid = QVBoxLayout(page_solid)
        layout_solid.setContentsMargins(0, 4, 0, 4)
        layout_solid.setSpacing(6)

        self.btn_solid_color = _make_color_btn(tr("Terrain color"), "#4a90d9")
        layout_solid.addWidget(self.btn_solid_color)
        layout_solid.addStretch()
        self.stacked_symbology.addWidget(page_solid)

        layout.addWidget(self.stacked_symbology)

        sep = QFrame()
        sep.setFrameShape(QFrame.HLine)
        layout.addWidget(sep)

        # 3. Shared value bounds (Z range)
        lbl_bounds = QLabel(tr("Elevation classification bounds:"))
        lbl_bounds.setStyleSheet("font-weight: bold;")
        layout.addWidget(lbl_bounds)

        bounds_row = QHBoxLayout()
        self.rad_scale_auto = QRadioButton(tr("Auto (DEM range)"))
        self.rad_scale_auto.setChecked(True)
        self.rad_scale_manual = QRadioButton(tr("Custom range"))
        self.scale_btn_group = QButtonGroup(self)
        self.scale_btn_group.addButton(self.rad_scale_auto)
        self.scale_btn_group.addButton(self.rad_scale_manual)
        bounds_row.addWidget(self.rad_scale_auto)
        bounds_row.addWidget(self.rad_scale_manual)
        layout.addLayout(bounds_row)

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
        layout.addLayout(spin_row)

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

        layout.addStretch()
        return page

    # ── Signal wiring (single authoritative location) ────────────────────

    def _connect_all_signals(self) -> None:
        """
        Every signal connection lives here.
        Scanning this method is sufficient to understand all interactivity.
        """
        # ── View switcher ────────────────────────────────────────────────
        self.rad_view_3d.toggled.connect(self._slot_view_mode_changed)
        self.rad_view_2d.toggled.connect(self._slot_view_mode_changed)

        # ── Icon toolbar → stack pages ───────────────────────────────────
        self._tab_btn_group.idClicked.connect(self._stack.setCurrentIndex)

        # ── Page 1 — Layers ──────────────────────────────────────────────
        self.combo_raster.currentIndexChanged.connect(self._slot_selected_raster_changed)
        self.btn_render_terrain.clicked.connect(self._slot_render_terrain)
        self.list_layers.itemChanged.connect(self._slot_layer_visibility_changed)
        self.btn_add_vector.clicked.connect(self._slot_add_vector_layer)

        # ── Page 2 — Symbology ───────────────────────────────────────────
        self.combo_render_mode.currentIndexChanged.connect(self._slot_render_mode_changed)
        self.spin_class_count.valueChanged.connect(self._rebuild_classified_ui)
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

    # ── Slots ────────────────────────────────────────────────────────────

    # View switcher

    def _slot_view_mode_changed(self) -> None:
        """Swap QGIS 2D canvas and WebGL viewport."""
        if self.rad_view_3d.isChecked():
            self.canvas_2d.hide()
            self.webview.show()
            self.is_3d_active = True

            # Auto-trigger render if a DEM is already selected but not yet loaded
            # layer = self.combo_raster.currentLayer()
            # if layer and self._loaded_raster_id != layer.id():
            #     self._slot_render_terrain()
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
        Process and send DEM to the WebGL viewport.
        Guard: no-op if the same DEM is already loaded.
        Note: for large DEMs, move engine.prepare_dem() into a ComputeWorker
        to avoid freezing the QGIS main thread.
        """
        if not self.is_3d_active:
            self.rad_view_3d.setChecked(True)
            return
        layer = self.combo_raster.currentLayer()
        if not layer:
            return
        if self._loaded_raster_id == layer.id():
            return

        self._loaded_raster_id = layer.id()
        dem_data = self.engine.prepare_dem(layer)
        self._js({"action": "set_main_raster", "payload": dem_data.to_dict()})
       # Cache elevations for classified mapping
        self.active_dem_min_z = dem_data.z_min
        self.active_dem_max_z = dem_data.z_max

        self.spin_min_z.blockSignals(True)
        self.spin_max_z.blockSignals(True)
        self.spin_min_z.setValue(self.active_dem_min_z)
        self.spin_max_z.setValue(self.active_dem_max_z)
        self.spin_min_z.blockSignals(False)
        self.spin_max_z.blockSignals(False)

        # Trigger colorization update
        if self.combo_render_mode.currentIndex() == 1:
            self._rebuild_classified_ui()
        else:
            self._slot_selected_raster_changed()



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
        Drape a vector layer onto the terrain surface.
        All features are sent in a single JSON payload to avoid N JS round-trips.
        """
        layer = self.combo_raster.currentLayer()
        dem_layer = self.combo_raster.currentLayer()
        vec_layer = self.combo_vector.currentLayer()
        if not vec_layer or not dem_layer or not self.is_3d_active:
            return

        vectors = self.engine.prepare_vector_layer(vec_layer, dem_layer)
        if not vectors:
            self.show_info(tr("No features found in the selected layer."))
            return

        # Single message — one serialized list of all features
        self._js({
            "action": "add_vector_batch",
            "payload": [v.to_dict() for v in vectors]
        })

        # Register in scene list
        self._add_layer_item(
            label=f"💧  {vec_layer.name()}",
            element_id=f"vector_{vec_layer.id()}"
        )

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

    def _slot_render_mode_changed(self, index: int) -> None:
        """Swap active configuration page and guarantee that scale bounds are fully synchronized with WebGL."""
        self.stacked_symbology.setCurrentIndex(index)
        if not self.is_3d_active:
            return
            
        if index == 0:  # Continuous Colormap
            # First, force synchronize the Z-bounds state (Auto vs Custom) to JS to resolve state conflicts
            self._slot_color_scale_mode_changed()
            # Then, apply the colormap with updated bounds
            self._slot_update_colormap()
            
        elif index == 1:  # Classified
            self._rebuild_classified_ui()
            
        elif index == 2:  # Solid Color
            hex_color = self.btn_solid_color.property("color_hex") or "#4a90d9"
            self._js({"action": "set_solid_color", "payload": {"color": hex_color}})

            
    def _rebuild_classified_ui(self) -> None:
        """Regenerate dynamic rows in the scroll panel, dividing the Z span into equal intervals while preserving custom colors."""
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

        # Scientific Viridis anchor stops for default programmatic gradient
        viridis_stops = [
            (0.267, 0.004, 0.329), # Deep purple
            (0.223, 0.302, 0.541), # Marine blue
            (0.125, 0.549, 0.556), # Forest green
            (0.368, 0.784, 0.380), # Lime green
            (0.992, 0.901, 0.141)  # Yellow
        ]
        
        def interpolate_viridis(t: float) -> str:
            t = max(0.0, min(1.0, t))
            idx = t * (len(viridis_stops) - 1)
            lower = int(idx)
            upper = min(lower + 1, len(viridis_stops) - 1)
            frac = idx - lower
            c0, c1 = viridis_stops[lower], viridis_stops[upper]
            r = c0[0] + (c1[0] - c0[0]) * frac
            g = c0[1] + (c1[1] - c0[1]) * frac
            b = c0[2] + (c1[2] - c0[2]) * frac
            return f"#{int(r*255):02x}{int(g*255):02x}{int(b*255):02x}"

        # --- Smart Color Preservation Logic ---
        # Adjust or reuse existing custom colors based on current class count
        if len(self._class_colors) != num_classes:
            old_colors = self._class_colors
            self._class_colors = []
            for i in range(num_classes):
                if i < len(old_colors):
                    # Preserve already hand-picked color
                    self._class_colors.append(old_colors[i])
                else:
                    # Append new default color only for newly added classes
                    t = i / max(1, num_classes - 1)
                    self._class_colors.append(interpolate_viridis(t))
        # If len(self._class_colors) == num_classes, we leave the array fully untouched!

        self._class_bounds = []

        for i in range(num_classes):
            c_min = min_z + i * interval
            c_max = min_z + (i + 1) * interval
            self._class_bounds.append(c_max)

            # Retrieve active color (either preserved or freshly generated)
            active_hex = self._class_colors[i]

            # Row layout assembly
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

        self._slot_update_classified_colors()

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
        self._slot_update_classified_colors()

    def _slot_update_classified_colors(self) -> None:
        """Serialize current bounds and color mapping, then push to the WebGL rendering thread."""
        if not self.is_3d_active or self.combo_render_mode.currentIndex() != 1:
            return
        # Bounds has length N-1 (omitting the absolute top bound, handled by raw elevation check)
        self._js({
            "action": "set_classified_colors",
            "payload": {
                "bounds": self._class_bounds[:-1],
                "colors": self._class_colors
            }
        })


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
        # TODO: integrate with RockMorphExporter and DPI selector dialog
        self.show_info(tr("High-resolution export is not yet implemented."))

    def _slot_export_interactive_html(self) -> None:
        # TODO: serialize scene state + inline Three.js into a standalone HTML file
        self.show_info(tr("Interactive HTML export is not yet implemented."))

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

    # ── Internal helpers ─────────────────────────────────────────────────

    def _js(self, command: dict) -> None:
        """Send a JSON command to the WebGL viewport (no-op if page not ready)."""
        print(f"[Python -> JS Command] Action envoyée : {command.get('action')}")
        self.webview.page().runJavaScript(
            f"processPythonCommand({json.dumps(command)});"
        )

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

    # ── BasePanel required overrides ─────────────────────────────────────

    def _on_compute(self) -> None:
        pass  # Computation is driven by UI events, not a single Compute button.

    def _on_result(self, data: dict) -> None:
        pass  # Results are piped in real-time via _js().

    def cleanup(self) -> None:
        """Restore QGIS 2D canvas and detach the WebGL widget on plugin unload."""
        if self.is_3d_active:
            self.rad_view_2d.setChecked(True)
        if self.webview:
            self.central_layout.removeWidget(self.webview)