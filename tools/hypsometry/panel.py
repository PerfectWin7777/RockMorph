# tools/hypsometry/panel.py

"""
tools/hypsometry/panel.py

HypsometryPanel — UI panel for the Hypsometric Curve tool.

Layout
------
  Input group      : DEM + basin layer + label field
  Grouping group   : strategy + max curves + bins
  List widget      : one row per basin, sortable, selectable
  WebEngineView    : hypsometry.html
  Export group     : PNG / JPG / SVG / CSV

Authors: RockMorph contributors / Tony
"""

import json
from PyQt5.QtWidgets import (  # type: ignore
    QVBoxLayout, QHBoxLayout, QFormLayout,
    QPushButton, QSpinBox, QComboBox,
    QCheckBox, QGroupBox, QLabel,
    QTreeWidget, QTreeWidgetItem,
    QSizePolicy, QAbstractItemView,
    QProgressBar, QLineEdit,QDoubleSpinBox,
    QStackedWidget,QColorDialog,
    QMenu,QApplication,QFileDialog
)
from PyQt5.QtWebEngineWidgets import QWebEngineView  # type: ignore
from PyQt5.QtCore import Qt, QCoreApplication, QThread, pyqtSignal  # type: ignore
from PyQt5.QtGui import QColor  # type: ignore
from qgis.gui import QgsMapLayerComboBox  # type: ignore
from qgis.core import QgsMapLayerProxyModel, QgsWkbTypes  # type: ignore
from qgis.core import QgsCoordinateTransform, QgsProject  # type: ignore


from ...base.base_panel import BasePanel,ComputeWorker
from ...core.exporter import RockMorphExporter
from .engine import HypsometryEngine
from .grouper import group_results, move_to_ungrouped, _group_stats




def tr(message):
    return QCoreApplication.translate("RockMorph", message)


"""
# ─────────────────────────────────────────────────────────────────
# TODO — high priority
# ─────────────────────────────────────────────────────────────────

# TODO(hypsometry/highlight): QgsRubberBand on active basin polygon
#       Show a blue outline on the canvas when a basin is selected
#       in the tree — similar to SwathPanel rubber band.
#       Cleanup in panel.cleanup() like swath does.
#       Trigger: _on_selection_changed() and _show_active_group()

# TODO(hypsometry/progress): Per-basin progress bar
#       _ComputeWorker currently gives no feedback during long runs.
#       Solution: make HypsometryEngine.compute() accept a
#       progress_callback(current, total) parameter.
#       Worker emits progress(int) signal, panel updates QProgressBar.
#       Switch progress_bar from indeterminate (setRange(0,0))
#       to determinate (setRange(0, total)).

# ─────────────────────────────────────────────────────────────────
# TODO — medium priority
# ─────────────────────────────────────────────────────────────────

# TODO(hypsometry/mode-b): Multiple DEM/basin pairs (Mode B)
#       Add ModeBInputWidget — a QTableWidget where user adds
#       (dem_layer, basin_layer, label) rows one by one.
#       "Compute all" button runs engine on each pair.
#       Results fed into same self._results pipeline.
#       Grouping, navigation, export all unchanged.
#       Switch between Mode A / Mode B via a radio button at top of panel.

# ─────────────────────────────────────────────────────────────────
# TODO — low priority / future
# ─────────────────────────────────────────────────────────────────

# TODO(hypsometry/pdf-report): Export group as PDF report
#       One page per group: curves + stats table + HI interpretation.
#       Use QPrinter or reportlab.

# TODO(hypsometry/i18n): Run pylupdate5 after all new tr() calls
#       New strings added: zoom, select, copy, export curve, etc.
"""


import re

def _natural_sort_key(s: str):
    """
    Sort key for natural ordering.
    '2' < '10' < '11' instead of '10' < '11' < '2'.
    """
    return [
        int(part) if part.isdigit() else part.lower()
        for part in re.split(r'(\d+)', s)
    ]



# ---------------------------------------------------------------------------
# Panel
# ---------------------------------------------------------------------------

class HypsometryPanel(BasePanel):
    """
    UI panel for the Hypsometric Curve tool.
    """

    def __init__(self, iface, parent=None):
        self._engine         = HypsometryEngine()
        self._exporter       = RockMorphExporter(iface)
        self._results        = []    # flat list from engine
        self._groups         = []    # grouped list from grouper
        self._active_group   = 0     # index into _groups
        self._worker         = None
        self._curve_color    = "#042fed"   # default single curve color
        super().__init__(iface, parent)

    # ------------------------------------------------------------------
    # BasePanel hooks
    # ------------------------------------------------------------------

    def _html_file(self) -> str:
        return "hypsometry.html"

    def _build_ui(self):
        root = QVBoxLayout(self._inner)
        root.setContentsMargins(8, 8, 8, 8)
        root.setSpacing(6)

        # ── Input ─────────────────────────────────────────────
        input_group  = QGroupBox(tr("Input"))
        input_layout = QFormLayout(input_group)

        self.dem_combo = QgsMapLayerComboBox()
        self.dem_combo.setFilters(QgsMapLayerProxyModel.RasterLayer)
        input_layout.addRow(tr("DEM layer:"), self.dem_combo)

        self.basin_combo = QgsMapLayerComboBox()
        self.basin_combo.setFilters(QgsMapLayerProxyModel.PolygonLayer)
        self.basin_combo.layerChanged.connect(self._on_basin_layer_changed)
        input_layout.addRow(tr("Basin layer:"), self.basin_combo)

        

        self.label_combo = QComboBox()
        self.label_combo.setToolTip(tr(
            "Field used as curve label.\n"
            "Auto detects: nom, name, id, bv, basin."
        ))
        input_layout.addRow(tr("Label field:"), self.label_combo)
        
        self._on_basin_layer_changed(self.basin_combo.currentLayer())
        
        self.n_points_spin = QSpinBox()
        self.n_points_spin.setRange(50, 1000)
        self.n_points_spin.setValue(200)
        self.n_points_spin.setToolTip(tr("Number of points per curve."))
        input_layout.addRow(tr("Curve points:"), self.n_points_spin)

        self.ref_lines_check = QCheckBox(tr("Show HI reference lines"))
        self.ref_lines_check.setChecked(False)
        self.ref_lines_check.setToolTip(tr(
            "Show horizontal reference lines at HI = 0.4, 0.5, 0.6\n"
            "(old / mature / young relief thresholds)"
        ))
        input_layout.addRow("", self.ref_lines_check)

        root.addWidget(input_group)
        
         # ── Styles ─────────────────────────
        style_group  = QGroupBox(tr("Style"))
        style_layout = QFormLayout(style_group)

        # Line width
        self.line_width_spin = QDoubleSpinBox()
        self.line_width_spin.setRange(0.5, 5.0)
        self.line_width_spin.setSingleStep(0.1)
        self.line_width_spin.setValue(1.0)
        self.line_width_spin.setDecimals(1)
        self.line_width_spin.setToolTip(tr("Curve line width for all basins."))
        style_layout.addRow(tr("Line width:"), self.line_width_spin)

        # Color picker — visible only in ungrouped mode
        self.color_btn = QPushButton()
        # self.color_btn.setFixedSize(32, 24)
        self.color_btn.setToolTip(tr("Curve color (ungrouped mode only)"))
        self._update_color_btn()
        self.color_btn.clicked.connect(self._pick_color)

        # Palette combo — visible only in grouped mode
        self.palette_combo = QComboBox()
        self.palette_combo.addItem(tr("Qualitative"), "qualitative")
        self.palette_combo.addItem(tr("Scientific"),  "scientific")
        self.palette_combo.addItem(tr("Monochrome"),  "monochrome")
        self.palette_combo.setToolTip(tr("Color palette for grouped curves."))

        # Stacked widget — switches between color picker and palette
        self.color_stack = QStackedWidget()
        self.color_stack.addWidget(self.color_btn)    # page 0 — ungrouped
        self.color_stack.addWidget(self.palette_combo) # page 1 — grouped
        self.color_stack.setCurrentIndex(0)
        style_layout.addRow(tr("Color:"), self.color_stack)

        # Grid checkboxes
        grid_row = QHBoxLayout()
        self.grid_x_check = QCheckBox(tr("Grid X"))
        self.grid_x_check.setChecked(True)
        self.grid_y_check = QCheckBox(tr("Grid Y"))
        self.grid_y_check.setChecked(True)
        grid_row.addWidget(self.grid_x_check)
        grid_row.addWidget(self.grid_y_check)
        style_layout.addRow(tr("Grids:"), grid_row)

        # Apply styles button
        self.apply_style_btn = QPushButton(tr("Apply styles"))
        self.apply_style_btn.setFixedHeight(28)
        self.apply_style_btn.setEnabled(False)
        self.apply_style_btn.clicked.connect(self._apply_styles)
        style_layout.addRow("", self.apply_style_btn)

        root.addWidget(style_group)


        # ── Compute button + progress ─────────────────────────
        self.compute_btn = QPushButton(tr("Compute all basins"))
        self.compute_btn.setFixedHeight(36)
        self.compute_btn.setStyleSheet("""
            QPushButton {
                background-color: #2d6a9f; color: white;
                border-radius: 4px; font-weight: bold;
            }
            QPushButton:hover   { background-color: #3a7fc1; }
            QPushButton:pressed { background-color: #1f4f7a; }
            QPushButton:disabled { background-color: #aaa; }
        """)
        self.compute_btn.clicked.connect(self._on_compute)
        root.addWidget(self.compute_btn)
        
        # add a progress bar container
        root.addWidget(self._progress_container) 

        # ── Grouping ──────────────────────────────────────────
        group_group  = QGroupBox(tr("Smart grouping"))
        group_layout = QFormLayout(group_group)

        self.strategy_combo = QComboBox()
        self.strategy_combo.addItems([
            tr("None (one by one)"),
            tr("Group by HI"),
            tr("Group by Area"),
            tr("Group by Relief"),
        ])
        self.strategy_combo.currentIndexChanged.connect(self._on_strategy_changed)
        group_layout.addRow(tr("Strategy:"), self.strategy_combo)

        self.max_spin = QSpinBox()
        self.max_spin.setRange(1, 20)
        self.max_spin.setValue(6)
        self.max_spin.setToolTip(tr("Max curves per group."))
        group_layout.addRow(tr("Max per group:"), self.max_spin)

        self.bins_spin = QSpinBox()
        self.bins_spin.setRange(2, 10)
        self.bins_spin.setValue(4)
        self.bins_spin.setToolTip(tr("Number of bins for HI/Area/Relief grouping."))
        group_layout.addRow(tr("Bins:"), self.bins_spin)

        self.apply_group_btn = QPushButton(tr("Apply grouping"))
        self.apply_group_btn.setFixedHeight(28)
        self.apply_group_btn.clicked.connect(self._apply_grouping)
        self.apply_group_btn.setEnabled(False)
        group_layout.addRow("", self.apply_group_btn)

        root.addWidget(group_group)

        # ── Basin list ────────────────────────────────────────
        list_group  = QGroupBox(tr("Results"))
        list_layout = QVBoxLayout(list_group)

        self.basin_tree = QTreeWidget()
        self.basin_tree.setHeaderLabels([
            tr("Basin"), tr("HI"), tr("Area km²"), tr("Relief m")
        ])
        self.basin_tree.setFixedHeight(160)
        self.basin_tree.setSelectionMode(QAbstractItemView.ExtendedSelection)
        # self.basin_tree.setSortingEnabled(True)
        self.basin_tree.itemSelectionChanged.connect(self._on_selection_changed)
        self.basin_tree.setContextMenuPolicy(Qt.CustomContextMenu)
        self.basin_tree.customContextMenuRequested.connect(self._on_tree_context_menu)
        list_layout.addWidget(self.basin_tree)

        # Navigation buttons
        nav_layout = QHBoxLayout()
        self.prev_btn = QPushButton("◄")
        self.prev_btn.setFixedWidth(40)
        self.prev_btn.clicked.connect(self._on_prev)
        self.prev_btn.setEnabled(False)

        self.group_info_label = QLabel("—")
        self.group_info_label.setAlignment(Qt.AlignCenter)
        self.group_info_label.setStyleSheet("color: #666; font-size: 11px;")

        self.next_btn = QPushButton("►")
        self.next_btn.setFixedWidth(40)
        self.next_btn.clicked.connect(self._on_next)
        self.next_btn.setEnabled(False)

        nav_layout.addWidget(self.prev_btn)
        nav_layout.addWidget(self.group_info_label, stretch=1)
        nav_layout.addWidget(self.next_btn)
        list_layout.addLayout(nav_layout)

        root.addWidget(list_group)

        # ── WebEngineView ─────────────────────────────────────
        self.webview.setMinimumHeight(360)
        self.webview.setSizePolicy(
            QSizePolicy.Expanding, QSizePolicy.Expanding
        )
        root.addWidget(self.webview)

        # ── Export ────────────────────────────────────────────
        export_group  = QGroupBox(tr("Export"))
        export_layout = QHBoxLayout(export_group)
        for fmt in ["PNG", "JPG", "SVG", "PDF", "CSV", "JSON"]:
            btn = QPushButton(fmt)
            btn.setFixedHeight(28)
            btn.clicked.connect(lambda checked, f=fmt: self._on_export(f))
            export_layout.addWidget(btn)
        root.addWidget(export_group)

        # ── Warnings label ────────────────────────────────────
        self.warning_label = QLabel("")
        self.warning_label.setWordWrap(True)
        self.warning_label.setStyleSheet("color: #c0392b; font-size: 10px;")
        self.warning_label.setVisible(False)
        root.addWidget(self.warning_label)
    
   
    def _pick_color(self):
        """Open color dialog for single-curve mode."""
        color = QColorDialog.getColor(
            QColor(self._curve_color), self, tr("Curve color")
        )
        if color.isValid():
            self._curve_color = color.name()
            self._update_color_btn()

    def _update_color_btn(self):
        """Refresh color preview button."""
        self.color_btn.setStyleSheet(
            f"background-color: {self._curve_color}; border: 1px solid #555;"
        )

    def _apply_styles(self):
        """Re-send current group with updated styles — no recompute."""
        if not self._groups:
            return
        group = self._groups[self._active_group]
        self._send_group_to_plot(group)

    # ------------------------------------------------------------------
    # Layer changed — refresh label field combo
    # ------------------------------------------------------------------

    def _on_basin_layer_changed(self, layer):
        """Refresh label field combo when basin layer changes."""
        self.label_combo.clear()
        self.label_combo.addItem(tr("Auto-detect"), None)
        if layer is None:
            return
        for field in layer.fields():
            self.label_combo.addItem(field.name(), field.name())

    # ------------------------------------------------------------------
    # Compute
    # ------------------------------------------------------------------

    def _on_compute(self):
        dem_layer   = self.dem_combo.currentLayer()
        basin_layer = self.basin_combo.currentLayer()

        if not self._engine.validate(
            dem_layer=dem_layer, basin_layer=basin_layer
        ):
            self.show_error(tr(
                "Please select a valid DEM layer and a polygon basin layer."
            ))
            return

        label_field = self.label_combo.currentData()

        params = {
            "dem_layer":   dem_layer,
            "basin_layer": basin_layer,
            "label_field": label_field,
            "n_points":    self.n_points_spin.value(),
        }

        # Disable UI during compute
        self.compute_btn.setEnabled(False)
        self.compute_btn.setText(tr("Computing…"))
        self.apply_group_btn.setEnabled(False)

        self.set_loading_state(True, tr("Sampling DEM data..."))

        # Run in background thread
        self._worker = ComputeWorker(self._engine, params)
        self._worker.progress.connect(self.update_progress)
        self._worker.finished.connect(self._on_compute_finished)
        self._worker.error.connect(self._on_compute_error)
        self._worker.start()

    def _on_compute_finished(self, result: dict):
        """Called when background worker finishes."""
        self.compute_btn.setEnabled(True)
        self.compute_btn.setText(tr("Compute all basins"))
        # hide progress bar and re-enable button
        self.set_loading_state(False)
        self._results = result.get("results", [])
        warnings      = result.get("warnings", [])
        skipped       = result.get("skipped", [])

        if not self._results:
            self.show_error(tr("No valid basins found."))
            return

        # Show warnings
        if warnings:
            self.warning_label.setText("\n".join(warnings[:5]))
            self.warning_label.setVisible(True)
        else:
            self.warning_label.setVisible(False)

        # Apply default grouping
        self._apply_grouping()
        self.apply_group_btn.setEnabled(True)
        self.apply_style_btn.setEnabled(True)

        # Info
        msg = tr(f"{len(self._results)} basins computed.")
        if skipped:
            msg += tr(f" {len(skipped)} skipped.")
        self.show_info(msg)

    def _on_compute_error(self, message: str):
        self.compute_btn.setEnabled(True)
        self.compute_btn.setText(tr("Compute all basins"))
         # hide progress bar and re-enable button
        self.set_loading_state(False)
        self.show_error(message)

    # ------------------------------------------------------------------
    # Grouping
    # ------------------------------------------------------------------

    def _apply_grouping(self):
        """Apply current grouping strategy to _results."""
        if not self._results:
            return

        strategy_map = {0: "none", 1: "hi", 2: "area", 3: "relief"}
        strategy = strategy_map.get(self.strategy_combo.currentIndex(), "none")
        
        # Filter results by label
        self._results = sorted(
            self._results,
            key=lambda r: _natural_sort_key(r["label"])
        )

        self._groups = group_results(
            self._results,
            strategy      = strategy,
            max_per_group = self.max_spin.value(),
            n_bins        = self.bins_spin.value(),
        )

        self._active_group = 0
        self._refresh_tree()
        self._show_active_group()

    def _on_strategy_changed(self, index: int):
        """Switch color widget and Auto-apply when strategy changes if results exist."""
        # index 0 = none → color picker
        # index > 0 = grouped → palette
        self.color_stack.setCurrentIndex(0 if index == 0 else 1)
        if self._results:
            self._apply_grouping()

    # ------------------------------------------------------------------
    # Tree widget
    # ------------------------------------------------------------------

    def _refresh_tree(self):
        """Rebuild tree from current _groups."""
        self.basin_tree.clear()

        if not self._groups:
            return

        strategy = self.strategy_combo.currentIndex()

        if strategy == 0:
            # Ungrouped — flat list
            for result in self._results:
                self._add_tree_item(self.basin_tree.invisibleRootItem(), result)
        else:
            # Grouped — tree with group headers
            for g_idx, group in enumerate(self._groups):
                group_item = QTreeWidgetItem(self.basin_tree)
                group_item.setText(0, group["label"])
                group_item.setData(0, Qt.UserRole, {"type": "group", "index": g_idx})
                group_item.setExpanded(g_idx == self._active_group)
                group_item.setBackground(0, QColor("#e8f0f8"))

                for result in group["members"]:
                    self._add_tree_item(group_item, result)

        self.basin_tree.setColumnWidth(0, 200)  # widest column, let it take remaining space
        self.basin_tree.resizeColumnToContents(1)  # HI
        self.basin_tree.resizeColumnToContents(2)  # Area
        self.basin_tree.resizeColumnToContents(3)  # Relief

    def _add_tree_item(self, parent, result: dict):
        item = QTreeWidgetItem(parent)
        item.setText(0, result["label"])
        item.setText(1, f"{result['hi']:.3f}")
        item.setText(2, f"{result['area_km2']:.3f}")
        item.setText(3, f"{result['relief']:.3f}")
        item.setData(0, Qt.UserRole, {"type": "basin", "fid": result["fid"]})
        return item

    def _on_selection_changed(self):
        """
        On selection change in tree:
        - Single item   → show that basin alone
        - Multiple items → superpose selected curves
        - Group header  → show that whole group
        """
        selected = self.basin_tree.selectedItems()
        if not selected:
            return

        # Collect result dicts from selection
        curves = []
        for item in selected:
            data = item.data(0, Qt.UserRole)
            if data is None:
                continue
            if data["type"] == "basin":
                result = self._find_result_by_fid(data["fid"])
                if result:
                    curves.append(result)
            elif data["type"] == "group":
                group = self._groups[data["index"]]
                curves.extend(group["members"])
                self._active_group = data["index"]
                self._update_nav_buttons()

        if curves:
            self._send_curves_to_plot(curves)

    def _on_tree_context_menu(self, pos):
        """Right-click on a basin item in the tree."""
        item = self.basin_tree.itemAt(pos)
        if item is None:
            return
            
        data = item.data(0, Qt.UserRole)
        if data is None :
            return

        menu = QMenu(self)

       # clic on group header → export group
        if data.get("type") == "group":
            g_idx        = data["index"]
            group        = self._groups[g_idx]
            export_action = menu.addAction(
                tr(f"Export '{group['label']}' to CSV")
            )
            chosen = menu.exec_(
                self.basin_tree.viewport().mapToGlobal(pos)
            )
            if chosen == export_action:
                self._export_group_csv(g_idx)
            return

        if data.get("type") != "basin":
           return 

        fid = data["fid"]
        
        # click on basin item → zoom, select, copy, export curve, move to ungrouped
        # --- NEW ACTIONS ---
        zoom_action = menu.addAction(tr("Zoom to Basin"))
        select_action = menu.addAction(tr("Select on Map"))
        menu.addSeparator()
        # -------------------

        copy_action = menu.addAction(tr("Copy Stats to Clipboard"))
        export_xy_action = menu.addAction(tr("Export This Curve Data (CSV)..."))
        menu.addSeparator()
        
        move_action = menu.addAction(tr("Move to Ungrouped"))
        
        # Map menu to global position and execute
        chosen = menu.exec_(self.basin_tree.viewport().mapToGlobal(pos))
        
        if chosen == zoom_action:
            self._zoom_to_basin(fid)
        elif chosen == select_action:
            self._select_on_map(fid)
        elif chosen == copy_action: 
            self._copy_basin_stats(fid)
        elif chosen == export_xy_action: 
            self._export_basin_xy(fid)
        elif chosen == move_action:
            self._groups = move_to_ungrouped(self._groups, fid)
            self._active_group = min(
                self._active_group, max(0, len(self._groups) - 1)
            )
            self._refresh_tree()
            self._show_active_group()
    

    def _zoom_to_basin(self, fid: int):
        """Zoom the QGIS map canvas to the selected basin feature."""
        layer = self.basin_combo.currentLayer()
        if not layer:
            return

        feature = layer.getFeature(fid)
        if feature.isValid():
            # Get geometry extent and zoom
            canvas = self.iface.mapCanvas()
            extent = feature.geometry().boundingBox()
            
            # Buffer the extent a bit (10%) so it's not touching the edges
            extent.scale(1.1)
           
            basin_crs  = layer.crs()
            canvas_crs = canvas.mapSettings().destinationCrs()
            if basin_crs != canvas_crs:
                transform = QgsCoordinateTransform(
                    basin_crs, canvas_crs, QgsProject.instance()
                )
                extent = transform.transformBoundingBox(extent)
            
            canvas.setExtent(extent)
            canvas.refresh()
            
            # Flash the feature to help the user locate it (Pro touch!)
            canvas.flashFeatureIds(layer, [fid])

    def _select_on_map(self, fid: int):
        """Select the feature on the QGIS layer and optionally flash it."""
        layer = self.basin_combo.currentLayer()
        if not layer:
            return

        # Select only this feature
        layer.selectByIds([fid])
        
        # Flash the feature so the user sees it immediately
        self.iface.mapCanvas().flashFeatureIds(layer, [fid])
    

    def _copy_basin_stats(self, fid: int):
        """Format basin results as text and copy to system clipboard."""
        result = self._find_result_by_fid(fid)
        if not result:
            return

        # Create a professional looking string
        stats_text = (
            f"Basin: {result['label']}\n"
            f"---------------------------\n"
            f"Hypsometric Integral (HI): {result['hi']:.3f}\n"
            f"Drainage Area: {result['area_km2']:.3f} km²\n"
            f"Total Relief: {result['relief']:.2f} m\n"
            f"Min/Max Elev: {result['min_elev']:.1f} / {result['max_elev']:.1f} m\n"
            f"Pixel Count: {result['n_pixels']}"
        )

        # Access system clipboard and set text
        clipboard = QApplication.clipboard()
        clipboard.setText(stats_text)
        
        # Feedback to user
        self.show_info(tr(f"Stats for '{result['label']}' copied to clipboard."))
    

    def _export_basin_xy(self, fid: int):
        """Export the raw (x, y) points of a single basin to a CSV file."""
        result = self._find_result_by_fid(fid)
        if not result:
            return

        # 1. Ask user for file path
        default_name = f"hypsometry_{result['label']}.csv"
        path, _ = QFileDialog.getSaveFileName(
            self, tr("Export Curve Data"), default_name, "CSV Files (*.csv)"
        )
        
        if not path:
            return

        # 2. Prepare data rows
        # We pair each x with its corresponding y
        rows = []
        for xi, yi in zip(result["x"], result["y"]):
            rows.append({
                "area_fraction": round(xi, 6),
                "elev_fraction": round(yi, 6)
            })

        # 3. Use the exporter to save
        headers = ["area_fraction", "elev_fraction"]
        self._exporter.save_csv(rows, headers, path)
    
    def _export_group_csv(self, group_index: int):
        """Export all basins of a specific group to CSV."""
        if group_index >= len(self._groups):
            return

        group   = self._groups[group_index]
        members = group["members"]

        rows = [
            {
                "label":    r["label"],
                "fid":      r["fid"],
                "hi":       r["hi"],
                "area_km2": r["area_km2"],
                "min_elev": r["min_elev"],
                "max_elev": r["max_elev"],
                "relief":   r["relief"],
                "n_pixels": r["n_pixels"],
            }
            for r in members
        ]

        headers = ["label", "fid", "hi", "area_km2",
                "min_elev", "max_elev", "relief", "n_pixels"]

        self._exporter.export_csv(rows, headers, parent=self)

    # ------------------------------------------------------------------
    # Navigation ◄ ►
    # ------------------------------------------------------------------

    def _on_prev(self):
        if self._active_group > 0:
            self._active_group -= 1
            self._show_active_group()

    def _on_next(self):
        if self._active_group < len(self._groups) - 1:
            self._active_group += 1
            self._show_active_group()

    def _show_active_group(self):
        if not self._groups:
            return
        group = self._groups[self._active_group]
        self._send_group_to_plot(group)
        self._update_nav_buttons()

        # Expand active group in tree
        if self.strategy_combo.currentIndex() > 0:
            root = self.basin_tree.invisibleRootItem()
            for i in range(root.childCount()):
                child = root.child(i)
                child.setExpanded(i == self._active_group)

    def _update_nav_buttons(self):
        total = len(self._groups)
        idx   = self._active_group
        self.prev_btn.setEnabled(idx > 0)
        self.next_btn.setEnabled(idx < total - 1)
        self.group_info_label.setText(
            f"{idx + 1} / {total}" if total > 1 else ""
        )

    # ------------------------------------------------------------------
    # Plot communication
    # ------------------------------------------------------------------

    def _send_group_to_plot(self, group: dict):
        """Send a full group dict to hypsometry.html."""
        is_ungrouped = (self.strategy_combo.currentIndex() == 0)

        payload = {
            "label":                group["label"],
            "members":              group["members"],
            "stats":                group.get("stats", {}),
            "show_reference_lines": self.ref_lines_check.isChecked(),
            "style": {                                          
                "line_width":    self.line_width_spin.value(),
                "palette":       None if is_ungrouped
                                else self.palette_combo.currentData(),
                "single_color":  self._curve_color if is_ungrouped else None,
                "show_grid_x":   self.grid_x_check.isChecked(),
                "show_grid_y":   self.grid_y_check.isChecked(),
                    }
        }
        self._last_data = payload
        js = f"updatePlot({json.dumps(json.dumps(payload))})"
        self.webview.page().runJavaScript(js)

    def _send_curves_to_plot(self, curves: list):
        """Send an ad-hoc list of curves (from manual selection)."""
       # manual selection — use current styles but no grouping stats
        payload = {
            "label":                tr("Selection"),
            "members":              curves,
            "stats":                _group_stats(curves),
            "show_reference_lines": self.ref_lines_check.isChecked(),
            "style": {                                         
               "line_width":    self.line_width_spin.value(),
                "palette":       self.palette_combo.currentData(),
                "single_color":  None,
                "show_grid_x":   self.grid_x_check.isChecked(),
                "show_grid_y":   self.grid_y_check.isChecked(),
            }
        }
        self._last_data = payload
        js = f"updatePlot({json.dumps(json.dumps(payload))})"
        self.webview.page().runJavaScript(js)

    # ------------------------------------------------------------------
    # BasePanel abstract methods
    # ------------------------------------------------------------------

    def _on_result(self, data: dict):
        pass   # not used — panel drives plot directly

    # ------------------------------------------------------------------
    # Export
    # ------------------------------------------------------------------

    def _on_export(self, fmt: str):
        fmt_lower = fmt.lower()

        if fmt_lower == "csv":
            if not self._results:
                self.show_error(tr("No data — run Compute first."))
                return
            self._exporter.export_csv(
                self._build_csv_rows(),
                self._csv_headers(),
                parent=self
            )
            return
        
        if fmt_lower == "json":
            if self._last_data is None:
                self.show_error(tr("No data — run Compute first."))
                return
            self._exporter.export_json(
                self._last_data,
                parent=self
            )
            return

        ok, path, dpi = self._exporter.prepare_image_export(
            fmt_lower, parent=self
        )
        if not ok:
            return

        self._pending_export_path = path
        self._pending_export_dpi  = dpi

        # last thing before export: ask JS to export the current plot as SVG data URL, then save it
        div_id = 'plot-main'  # div ID for plotly
        self.webview.page().runJavaScript(f"exportViaSvg('{div_id}')")




    def _csv_headers(self) -> list:
        return ["label", "fid", "hi", "area_km2",
                "min_elev", "max_elev", "relief", "n_pixels"]


    def _build_csv_rows(self) -> list:
        return [
            {
                "label":    r["label"],
                "fid":      r["fid"],
                "hi":       r["hi"],
                "area_km2": r["area_km2"],
                "min_elev": r["min_elev"],
                "max_elev": r["max_elev"],
                "relief":   r["relief"],
                "n_pixels": r["n_pixels"],
            }
            for r in self._results
        ]

    # ------------------------------------------------------------------
    # Helpers
    # ------------------------------------------------------------------

    def _find_result_by_fid(self, fid: int) -> dict | None:
        for r in self._results:
            if r["fid"] == fid:
                return r
        return None