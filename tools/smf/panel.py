"""
tools/smf/panel.py

SMFPanel — Production-ready, highly documented UI panel for the 
Mountain Front Sinuosity (Smf) Tool in RockMorph.

This panel provides:
- Clean and compact layout focusing on raw geomorphic inputs, parameters,
  and tabular results.
- Semiautomatic/manual scarp processing or fully automated extraction using 
  the high-precision Mountain-Flank Skeleton Detector (MFSD).
- Interactive results table (QTreeWidget) listing: Unit, Scarp ID, Lf (km), 
  Lr (km), Smf, and Tectonic Activity Class.
- Geodesic highlighting using QgsRubberBand on row selection to visually
  validate computed scarps directly on the QGIS Map Canvas.
- Fully-featured academic data export pipeline targeting:
  * CSV (Tabular data for Excel/R statistical analysis)
  * JSON (Raw data structured for reproducibility)
  * GeoPackage (Spatial GIS layers ready to be added to the QGIS legend)

Heritage:
---------
This class extends RockMorph's BasePanel. It retains the internal webview 
instantiated by the parent class to maintain API inheritance safety, but does 
not add it to the layout, keeping the user interface completely focused and clutter-free.

Authors: RockMorph contributors / Tony
"""

import json
import re

from PyQt5.QtWidgets import (  # type: ignore
    QVBoxLayout, QHBoxLayout, QFormLayout,
    QPushButton, QDoubleSpinBox, QComboBox, 
     QLabel, QTreeWidget, QTreeWidgetItem, 
    QAbstractItemView, QMenu, QApplication, QMessageBox,
    QRadioButton
)
from PyQt5.QtCore import Qt, QCoreApplication  # type: ignore
from PyQt5.QtGui import QColor  # type: ignore

from qgis.gui import QgsMapLayerComboBox, QgsRubberBand, QgsCollapsibleGroupBox   # type: ignore
from qgis.core import (  # type: ignore
    QgsMapLayerProxyModel, QgsCoordinateTransform, QgsProject,
    QgsVectorLayer, QgsFeature, QgsGeometry, QgsPointXY, 
    QgsField, QgsFields, QgsWkbTypes,
    QgsWkbTypes as WkbTypes
)
from PyQt5.QtCore import QVariant  # type: ignore

from ...base.base_panel import BasePanel, ComputeWorker
from ...core.exporter import RockMorphExporter
from .engine import SMFEngine


def tr(message):
    return QCoreApplication.translate("RockMorph", message)


def _natural_sort_key(s: str):
    """Auxiliary natural sorting key for scarp IDs (e.g., E2 before E10)."""
    return [
        int(part) if part.isdigit() else part.lower()
        for part in re.split(r'(\d+)', s)
    ]


class SMFPanel(BasePanel):
    """
    Highly documented, production-grade QGIS Sidebar Panel 
    for Mountain Front Sinuosity (Smf) Analysis.
    """

    def __init__(self, iface, parent=None):
        self._engine = SMFEngine()
        self._exporter = RockMorphExporter(iface)
        self._results = []
        self._worker = None
        self._active_index = 0
        self._rubber_bands = []
        super().__init__(iface, parent)

    # ------------------------------------------------------------------
    # BasePanel Hooks
    # ------------------------------------------------------------------

    def _html_file(self) -> str:
        """
        Required by parent class. We return a dummy HTML reference.
        The QWebEngineView remains unparented and hidden.
        """
        return "smf.html"

    def _build_ui(self):
        """
        Constructs the Qt user interface layout.
        Provides a highly structured, compact, and responsive side panel.
        """
        root = QVBoxLayout(self._inner)
        root.setContentsMargins(8, 8, 8, 8)
        root.setSpacing(6)

       # ── 1. Input Layer Group ──────────────────────────────────────
        input_group = QgsCollapsibleGroupBox(tr("Input Data"))
        input_layout = QFormLayout(input_group)

        self.dem_combo = QgsMapLayerComboBox()
        self.dem_combo.setFilters(QgsMapLayerProxyModel.RasterLayer)
        input_layout.addRow(tr("DEM layer:"), self.dem_combo)

        # Extraction Mode Selection (Radios)
        self.rad_auto = QRadioButton(tr("Automatic detection (MFSD)"))
        self.rad_auto.setToolTip(tr(
            "<b>Automatic detection (MFSD):</b><br>"
            "Runs RockMorph's high-precision Mountain-Flank Skeleton Detector.<br>"
            "This algorithm automatically extracts continuous, 1-pixel-thick scarp fronts "
            "directly from the MNT using regional relief and slope thresholds.<br>"
            "Recommended for objective, regional tectono-morphological analysis."
        ))

        self.rad_manual = QRadioButton(tr("Provide my own scarp layer"))
        self.rad_manual.setToolTip(tr(
            "<b>Provide my own scarp layer:</b><br>"
            "Allows you to select a pre-digitized or mapped vector line layer of mountain fronts.<br>"
            "RockMorph will bypass automatic detection and compute geodesic sinuosity (Smf) "
            "and relief metrics directly on your custom lines.<br>"
            "Perfect for validating hand-mapped structural faults."
        ))

        self.rad_auto.setChecked(True)  # Default mode

        input_layout.addRow(tr("Extraction Mode:"), self.rad_auto)
        input_layout.addRow("", self.rad_manual)

        self.scarp_combo = QgsMapLayerComboBox()
        self.scarp_combo.setFilters(QgsMapLayerProxyModel.LineLayer)
        self.scarp_combo.setAllowEmptyLayer(True)
        self.scarp_combo.layerChanged.connect(self._on_scarp_layer_changed)
        self.scarp_combo.setToolTip(tr(
            "<b>Scarp Layer:</b><br>"
            "Select the vector line layer representing digitized mountain front scarps.<br>"
            "<i>(This input is only active when manual mode is selected).</i>"
        ))
        input_layout.addRow(tr("Scarp layer:"), self.scarp_combo)

        self.unit_combo = QComboBox()
        self.unit_combo.setToolTip(tr(
            "<b>Unit Field:</b><br>"
            "Optional attribute field specifying geomorphological or tectonic units.<br>"
            "If selected, results will be grouped and summarized by these regional units.<br>"
            "<i>(This input is only active when manual mode is selected).</i>"
        ))
        input_layout.addRow(tr("Unit field:"), self.unit_combo)

        # Connect signals for dynamic UI toggling
        self.rad_auto.toggled.connect(self._on_mode_changed)
        self._on_mode_changed()  # Initialize state on load

        self._on_scarp_layer_changed(self.scarp_combo.currentLayer())
        root.addWidget(input_group)

        # ── 2. Parameter Tuning ───────────────────────────────────────
        param_group = QgsCollapsibleGroupBox(tr("Analysis Parameters"))
        param_layout = QFormLayout(param_group)

        self.baseline_combo = QComboBox()
        self.baseline_combo.addItem(tr("Direct Endpoints (Standard)"), "endpoints")
        self.baseline_combo.addItem(tr("PCA Axis (Structural Trend)"), "pca")
        self.baseline_combo.setToolTip(tr(
            "<b>Baseline Calculation Method (Lr):</b><br>"
            "• <b>Direct Endpoints:</b> Straight distance between the two endpoints of the scarp line.<br>"
            "• <b>PCA Axis:</b> Orthogonal linear regression representing the main structural fault strike."
        ))
        param_layout.addRow(tr("Baseline method (Lr):"), self.baseline_combo)

        # A. Classification Thresholds
        self.min_slope_spin = QDoubleSpinBox()
        self.min_slope_spin.setRange(1.0, 90.0)
        self.min_slope_spin.setValue(15.0)
        self.min_slope_spin.setSuffix(" %")
        self.min_slope_spin.setToolTip(tr(
            "<b>Minimum Slope Threshold (%):</b><br>"
            "Minimum terrain slope required to classify a pixel as part of the mountain massif. "
            "Lower values capture gentler foothills; higher values restrict detection to steep, active scarps."
        ))
        param_layout.addRow(tr("Min slope threshold:"), self.min_slope_spin)

        self.min_relief_spin = QDoubleSpinBox()
        self.min_relief_spin.setRange(10.0, 2000.0)
        self.min_relief_spin.setValue(100.0)
        self.min_relief_spin.setSuffix(" m")
        self.min_relief_spin.setToolTip(tr(
            "<b>Minimum Local Relief (m):</b><br>"
            "Minimum elevation difference (max minus min) required within the search window "
            "to classify a pixel as mountain-flank. Filters out small, isolated topographic features."
        ))
        param_layout.addRow(tr("Min relief threshold:"), self.min_relief_spin)

        self.relief_win_spin = QDoubleSpinBox()
        self.relief_win_spin.setRange(100.0, 10000.0)
        self.relief_win_spin.setValue(1500.0)
        self.relief_win_spin.setSuffix(" m")
        self.relief_win_spin.setToolTip(tr(
            "<b>Relief Window Size (m):</b><br>"
            "The search window radius used to compute local relief. Large windows generalize "
            "major tectonic steps; small windows capture localized slopes."
        ))
        param_layout.addRow(tr("Relief window size:"), self.relief_win_spin)

        # B. Morphological & Filtering Parameters
        self.min_area_spin = QDoubleSpinBox()
        self.min_area_spin.setRange(0.01, 200.0)
        self.min_area_spin.setValue(10.0)
        self.min_area_spin.setSuffix(" km²")
        self.min_area_spin.setToolTip(tr(
            "<b>Minimum Mountain Area (km²):</b><br>"
            "Minimum surface area of a classified mountain massif. "
            "Increasing this value eliminates small isolated hills and ridges, preserving only large regional massifs."
        ))
        param_layout.addRow(tr("Min massif area:"), self.min_area_spin)

        self.min_len_spin = QDoubleSpinBox()
        self.min_len_spin.setRange(100.0, 50000.0)
        self.min_len_spin.setValue(4000.0)
        self.min_len_spin.setSuffix(" m")
        self.min_len_spin.setToolTip(tr(
            "<b>Minimum Front Length (m):</b><br>"
            "Minimum length required to retain a traced front. Shorter segments below this "
            "threshold are discarded to ensure only main structural features are analyzed."
        ))
        param_layout.addRow(tr("Min front length:"), self.min_len_spin)

        # C. Fragment Merging Parameters
        self.max_gap_spin = QDoubleSpinBox()
        self.max_gap_spin.setRange(10.0, 10000.0)
        self.max_gap_spin.setValue(1500.0)
        self.max_gap_spin.setSuffix(" m")
        self.max_gap_spin.setToolTip(tr(
            "<b>Maximum Gap to Bridge (m):</b><br>"
            "Maximum distance allowed to automatically connect adjacent scarp fragments across valleys or erosion channels. "
            "Increase this to bridge wide river valleys."
        ))
        param_layout.addRow(tr("Max gap to bridge:"), self.max_gap_spin)

        self.max_angle_spin = QDoubleSpinBox()
        self.max_angle_spin.setRange(1.0, 90.0)
        self.max_angle_spin.setValue(45.0)
        self.max_angle_spin.setSuffix(" °")
        self.max_angle_spin.setToolTip(tr(
            "<b>Maximum Merging Angle (deg):</b><br>"
            "Maximum angular deviation allowed between two fragments to merge them. "
            "Lower values require straight alignment; higher values allow tracing sinuous fronts."
        ))
        param_layout.addRow(tr("Max merge angle:"), self.max_angle_spin)

        root.addWidget(param_group)

        # ── 3. Computation Controls ───────────────────────────────────
        self.compute_btn = QPushButton(tr("Compute S_mf"))
        self.compute_btn.setFixedHeight(36)
        self.compute_btn.setStyleSheet("""
            QPushButton {
                background-color: #2d6a9f; color: white;
                border-radius: 4px; font-weight: bold;
            }
            QPushButton:hover   { background-color: #3a7fc1; }
            QPushButton:pressed { background-color: #1f4f7a; }
            QPushButton:disabled{ background-color: #aaa; }
        """)
        self.compute_btn.clicked.connect(self._on_compute)
        root.addWidget(self.compute_btn)
        root.addWidget(self._progress_container)

        # ── 4. Results Tree Widget ────────────────────────────────────
        results_group = QgsCollapsibleGroupBox(tr("Results Table"))
        results_layout = QVBoxLayout(results_group)

        self.tree_widget = QTreeWidget()
        self.tree_widget.setHeaderLabels([
            tr("Unit"), tr("Scarp ID"), tr("Lf (km)"), tr("Lr (km)"), tr("S_mf"), tr("Tectonic Class")
        ])
        self.tree_widget.setFixedHeight(180)  # Extended height to replace web view space
        self.tree_widget.setSortingEnabled(True)
        self.tree_widget.setSelectionMode(QAbstractItemView.SingleSelection)
        self.tree_widget.itemSelectionChanged.connect(self._on_selection_changed)
        self.tree_widget.setContextMenuPolicy(Qt.CustomContextMenu)
        self.tree_widget.customContextMenuRequested.connect(self._on_tree_context_menu)
        results_layout.addWidget(self.tree_widget)

        # Navigation Controls ◄ ►
        nav_layout = QHBoxLayout()
        self.prev_btn = QPushButton("◄")
        self.prev_btn.setFixedWidth(40)
        self.prev_btn.clicked.connect(self._on_prev)
        self.prev_btn.setEnabled(False)

        self.nav_label = QLabel("—")
        self.nav_label.setAlignment(Qt.AlignCenter)

        self.next_btn = QPushButton("►")
        self.next_btn.setFixedWidth(40)
        self.next_btn.clicked.connect(self._on_next)
        self.next_btn.setEnabled(False)

        nav_layout.addWidget(self.prev_btn)
        nav_layout.addWidget(self.nav_label, stretch=1)
        nav_layout.addWidget(self.next_btn)
        results_layout.addLayout(nav_layout)

        root.addWidget(results_group)

        # ── 5. Data Export Group ──────────────────────────────────────
        export_group = QgsCollapsibleGroupBox(tr("Academic Data Export"))
        export_layout = QHBoxLayout(export_group)

        self.btn_csv = QPushButton(tr("Export to CSV"))
        self.btn_csv.setToolTip(tr("Export tabular geomorphic metrics to CSV for statistical packages (Excel/R)."))
        self.btn_csv.clicked.connect(lambda: self._on_export("csv"))
        export_layout.addWidget(self.btn_csv)

        self.btn_json = QPushButton(tr("Export to JSON"))
        self.btn_json.setToolTip(tr("Export raw results as a structured JSON file for maximum reproducibility."))
        self.btn_json.clicked.connect(lambda: self._on_export("json"))
        export_layout.addWidget(self.btn_json)

        self.btn_gpkg = QPushButton(tr("Export to GeoPackage"))
        self.btn_gpkg.setToolTip(tr("Export mountain front lines to a spatial GeoPackage layer and reload it in QGIS."))
        self.btn_gpkg.clicked.connect(self._on_export_geopackage)
        export_layout.addWidget(self.btn_gpkg)

        root.addWidget(export_group)

        # Warning Bar
        self.warning_label = QLabel("")
        self.warning_label.setWordWrap(True)
        self.warning_label.setStyleSheet("color: #c0392b; font-size: 10px;")
        self.warning_label.setVisible(False)
        root.addWidget(self.warning_label)

    # ------------------------------------------------------------------
    # Dynamic Form Handlers
    # ------------------------------------------------------------------

    def _on_scarp_layer_changed(self, layer):
        self.unit_combo.clear()
        self.unit_combo.addItem(tr("Auto / None (Global)"), None)
        if layer is None or not layer.isValid():
            return
        for field in layer.fields():
            self.unit_combo.addItem(field.name(), field.name())
    
    def _on_mode_changed(self):
        """Dynamically enables or disables manual vector inputs based on the selected mode."""
        is_manual = self.rad_manual.isChecked()
        self.scarp_combo.setEnabled(is_manual)
        # self.unit_combo.setEnabled(is_manual)

        # Clear the QGIS selection and baseline if turning manual mode off
        if not is_manual:
            self.scarp_combo.setCurrentIndex(0)  # Reset combobox to empty

    # ------------------------------------------------------------------
    # Computation Pipeline
    # ------------------------------------------------------------------

    def _on_compute(self):
        dem_layer = self.dem_combo.currentLayer()
        scarp_layer = self.scarp_combo.currentLayer()

        if not self._engine.validate(dem_layer=dem_layer, scarp_layer=scarp_layer):
            self.show_error(tr("Please select a valid DEM raster layer."))
            return

        # Retrieve scarp layer only if manual mode is explicitly chosen
        use_manual = self.rad_manual.isChecked()
        scarp_layer = self.scarp_combo.currentLayer() if use_manual else None

        params = {
            "dem_layer": dem_layer,
            "scarp_layer": scarp_layer if (scarp_layer and scarp_layer.isValid()) else None,
            "unit_field": self.unit_combo.currentData(), # Completely neutral
            "baseline_method": self.baseline_combo.currentData(),
            "min_slope_pct": self.min_slope_spin.value(),
            "min_relief_m": self.min_relief_spin.value(),
            "relief_window_m": self.relief_win_spin.value(),
            "min_front_area_km2": self.min_area_spin.value(),
            "min_front_length_m": self.min_len_spin.value(),
            "max_gap_m": self.max_gap_spin.value(),
            "max_merge_angle_deg": self.max_angle_spin.value(),
        }

        self.compute_btn.setEnabled(False)
        self.compute_btn.setText(tr("Computing S_mf…"))
        self.set_loading_state(True, tr("Starting Smf computation..."))

        self._worker = ComputeWorker(self._engine, params)
        self._worker.progress.connect(self.update_progress)
        self._worker.finished.connect(self._on_compute_finished)
        self._worker.error.connect(self._on_compute_error)
        self._worker.start()

    def _on_compute_finished(self, result: dict):
        self.compute_btn.setEnabled(True)
        self.compute_btn.setText(tr("Compute S_mf"))
        self.set_loading_state(False)

        self._results = result.get("results", [])
        warnings = result.get("warnings", [])
        skipped = result.get("skipped", [])

        if not self._results:
            self.show_error(tr("No valid mountain front segments found. Adjust your parameters."))
            return

        if warnings:
            self.warning_label.setText("\n".join(warnings[:5]))
            self.warning_label.setVisible(True)
        else:
            self.warning_label.setVisible(False)

        self._results = sorted(
            self._results,
            key=lambda r: _natural_sort_key(r["label"])
        )

        self._active_index = 0
        self._clear_rubber_bands()
        self._refresh_tree()
        self._update_nav_buttons()
        self._show_active()

        msg = tr(f"{len(self._results)} mountain front segments processed.")
        if skipped:
            msg += tr(f" {len(skipped)} skipped.")
        self.show_info(msg)

    def _on_compute_error(self, message: str):
        self.compute_btn.setEnabled(True)
        self.compute_btn.setText(tr("Compute S_mf"))
        self.set_loading_state(False)
        self.show_error(message)

    # ------------------------------------------------------------------
    # Results Navigation & Selection
    # ------------------------------------------------------------------

    def _refresh_tree(self):
        self.tree_widget.clear()
        for r in self._results:
            item = QTreeWidgetItem(self.tree_widget)
            item.setText(0, str(r["unit"]))
            item.setText(1, str(r["label"]))
            item.setText(2, f"{r['lf_km']:.2f}")
            item.setText(3, f"{r['lr_km']:.2f}")
            item.setText(4, f"{r['smf']:.2f}")
            
            cls_text = f"Class {r['class']}"
            if r['class'] == 1:
                cls_text += " (Active)"
            elif r['class'] == 2:
                cls_text += " (Moderate)"
            else:
                cls_text += " (Inactive)"
            item.setText(5, cls_text)
            item.setData(0, Qt.UserRole, r["fid"])

        for col in range(6):
            self.tree_widget.resizeColumnToContents(col)

    def _on_selection_changed(self):
        selected = self.tree_widget.selectedItems()
        if not selected:
            return
        idx = self.tree_widget.indexOfTopLevelItem(selected[0])
        if idx >= 0:
            self._active_index = idx
            self._update_nav_buttons()
            self._show_active()

    def _on_prev(self):
        if self._active_index > 0:
            self._active_index -= 1
            self._show_active()

    def _on_next(self):
        if self._active_index < len(self._results) - 1:
            self._active_index += 1
            self._show_active()

    def _show_active(self):
        if not self._results:
            return
        self.tree_widget.blockSignals(True)
        self.tree_widget.clearSelection()
        item = self.tree_widget.topLevelItem(self._active_index)
        if item:
            item.setSelected(True)
            self.tree_widget.scrollToItem(item)
        self.tree_widget.blockSignals(False)
        self._update_nav_buttons()

        active_res = self._results[self._active_index]
        self._draw_active_scarp(active_res)

    def _update_nav_buttons(self):
        total = len(self._results)
        idx = self._active_index
        self.prev_btn.setEnabled(idx > 0)
        self.next_btn.setEnabled(idx < total - 1)
        self.nav_label.setText(f"{idx + 1} / {total}" if total > 1 else "")

    # ------------------------------------------------------------------
    # Map Canvas Interactivity (RubberBands)
    # ------------------------------------------------------------------

    def _clear_rubber_bands(self):
        for rb in self._rubber_bands:
            rb.reset()
        self._rubber_bands.clear()

    def _draw_active_scarp(self, result: dict):
        self._clear_rubber_bands()
        
        geom = result.get("geom")
        if not geom or geom.isEmpty():
            return

        canvas = self.iface.mapCanvas()
        canvas_crs = canvas.mapSettings().destinationCrs()
        
        dem_layer = self.dem_combo.currentLayer()
        if not dem_layer:
            return
        layer_crs = dem_layer.crs()

        transform = None
        if layer_crs != canvas_crs:
            transform = QgsCoordinateTransform(
                layer_crs, canvas_crs, QgsProject.instance()
            )

        geom_disp = QgsGeometry(geom)
        if transform:
            geom_disp.transform(transform)

        color = QColor(231, 76, 60) if result['class'] == 1 else (
            QColor(230, 126, 34) if result['class'] == 2 else QColor(46, 204, 113)
        )

        rb = QgsRubberBand(canvas, WkbTypes.LineGeometry)
        rb.setColor(color)
        rb.setWidth(4)
        rb.setToGeometry(geom_disp, None)
        self._rubber_bands.append(rb)

    # ------------------------------------------------------------------
    # BasePanel Required Abstract Methods
    # ------------------------------------------------------------------

    def _on_result(self, data: dict):
        """
        Required abstract method from BasePanel.
        We provide an empty implementation as we only output tabular data.
        """
        pass

    # ------------------------------------------------------------------
    # Context Menu & Exports
    # ------------------------------------------------------------------

    def _on_tree_context_menu(self, pos):
        item = self.tree_widget.itemAt(pos)
        if item is None:
            return
        fid = item.data(0, Qt.UserRole)
        if fid is None:
            return

        menu = QMenu(self)
        zoom_action = menu.addAction(tr("Zoom to Front"))
        copy_action = menu.addAction(tr("Copy Metrics to Clipboard"))

        chosen = menu.exec_(self.tree_widget.viewport().mapToGlobal(pos))
        if chosen == zoom_action:
            self._zoom_to_front(fid)
        elif chosen == copy_action:
            self._copy_stats(fid)

    def _zoom_to_front(self, fid: int):
        active_res = next((item for item in self._results if item["fid"] == fid), None)
        if not active_res or "geom" not in active_res:
            return
        canvas = self.iface.mapCanvas()
        extent = active_res["geom"].boundingBox()
        extent.scale(1.4)
        canvas.setExtent(extent)
        canvas.refresh()

    def _copy_stats(self, fid: int):
        r = next((item for item in self._results if item["fid"] == fid), None)
        if not r:
            return
        text = (
            f"Unit: {r['unit']}\n"
            f"Front: {r['label']}\n"
            f"Lf: {r['lf_km']} km\n"
            f"Lr: {r['lr_km']} km\n"
            f"Smf: {r['smf']}\n"
            f"Class: {r['class']}"
        )
        QApplication.clipboard().setText(text)
        self.show_info(tr(f"Metrics for '{r['label']}' copied to clipboard."))

    def _on_export(self, fmt: str):
        """Processes CSV and JSON exports for our calculated metrics."""
        fmt_lower = fmt.lower()
        if fmt_lower == "csv":
            if not self._results:
                self.show_error(tr("No data available to export. Run Compute first."))
                return
            self._exporter.export_csv(
                self._build_csv_rows(),
                self._csv_headers(),
                parent=self,
            )
        elif fmt_lower == "json":
            if not self._results:
                self.show_error(tr("No data available to export. Run Compute first."))
                return
            # Package structural results for clean JSON output without binary geometries
            clean_results = []
            for r in self._results:
                r_copy = r.copy()
                if "geom" in r_copy:
                    del r_copy["geom"]
                clean_results.append(r_copy)
            
            self._exporter.export_json({"results": clean_results}, parent=self)

    def _csv_headers(self) -> list:
        return ["unit", "label", "lf_km", "lr_km", "smf", "class"]

    def _build_csv_rows(self) -> list:
        return [
            {
                "unit": r["unit"],
                "label": r["label"],
                "lf_km": r["lf_km"],
                "lr_km": r["lr_km"],
                "smf": r["smf"],
                "class": r["class"]
            }
            for r in self._results
        ]

    def _on_export_geopackage(self):
        if not self._results:
            self.show_error(tr("No data available to export. Run Compute first."))
            return

        path = self._exporter._ask_path("gpkg", self)
        if not path:
            return

        dem_layer = self.dem_combo.currentLayer()
        crs = dem_layer.crs() if dem_layer else QgsProject.instance().crs()

        fields = QgsFields()
        fields.append(QgsField("unit", QVariant.String))
        fields.append(QgsField("label", QVariant.String))
        fields.append(QgsField("lf_km", QVariant.Double))
        fields.append(QgsField("lr_km", QVariant.Double))
        fields.append(QgsField("smf", QVariant.Double))
        fields.append(QgsField("class", QVariant.Int))

        features = []
        for r in self._results:
            feat = QgsFeature(fields)
            if "geom" in r:
                feat.setGeometry(r["geom"])
            feat.setAttributes([r["unit"], r["label"], r["lf_km"], r["lr_km"], r["smf"], r["class"]])
            features.append(feat)

        layers_dict = {
            "smf_mountain_fronts": {
                "fields": fields,
                "features": features,
                "crs": crs,
                "geom_type": QgsWkbTypes.LineString
            }
        }

        success = self._exporter.save_geopackage(path, layers_dict)
        if success:
            res = QMessageBox.question(
                self, tr("Export Successful"),
                tr("Would you like to add the exported SMF layer to your map legend?"),
                QMessageBox.Yes | QMessageBox.No
            )
            if res == QMessageBox.Yes:
                uri = f"{path}|layername=smf_mountain_fronts"
                sub_layer = QgsVectorLayer(uri, "RM_SMF_Fronts", "ogr")
                if sub_layer.isValid():
                    QgsProject.instance().addMapLayer(sub_layer)

    def cleanup(self):
        """Properly releases all map canvas indicators on unload."""
        self._clear_rubber_bands()