# tools/fluvial/panel.py

"""
tools/fluvial/panel.py

FluvialPanel — UI panel for the Fluvial Geomorphology tool.

Layout
------
  Input group     : DEM + basin + stream + FAC (optional)
  Parameters      : theta_ref slider + A0 + n_points + snap + sl_window + n_knick
  Compute button  + progress bar
  View toggle     : [Longitudinal] [Chi-plot]
  Layer checkboxes: context-sensitive per view
  Basin tree      : label / SLk max / chi max / ksn mean / ksn max / N knickpoints
  WebEngineView   : fluvial.html
  Export group    : PNG / JPG / SVG / PDF / CSV / JSON

Authors: RockMorph contributors / Tony
"""

import json
import re
import copy
import math
import numpy as np # type: ignore

from PyQt5.QtWidgets import (  # type: ignore
    QVBoxLayout, QHBoxLayout, QFormLayout,
    QPushButton, QSpinBox, QDoubleSpinBox,
    QComboBox, QGroupBox, QLabel,
    QTreeWidget, QTreeWidgetItem,
    QSizePolicy, QAbstractItemView,
    QButtonGroup, QRadioButton,
    QMenu, QApplication, QFileDialog,
    QCheckBox, QSlider, QWidget,
)
from PyQt5.QtCore import Qt, QCoreApplication  # type: ignore
from PyQt5.QtGui import QColor  # type: ignore
from qgis.gui import QgsMapLayerComboBox, QgsRubberBand  # type: ignore
from qgis.core import (  # type: ignore
    QgsMapLayerProxyModel, QgsWkbTypes,
    QgsCoordinateTransform, QgsProject,
    QgsVectorLayer, QgsFeature, QgsGeometry,
    QgsPointXY, QgsField, QgsFields,
    QgsWkbTypes as WkbTypes,
)
from PyQt5.QtCore import QVariant  # type: ignore

from ...base.base_panel import BasePanel, ComputeWorker
from ...core.exporter import RockMorphExporter
from .engine import FluvialEngine


def tr(message):
    return QCoreApplication.translate("RockMorph", message)


def _natural_sort_key(s: str):
    return [
        int(part) if part.isdigit() else part.lower()
        for part in re.split(r'(\d+)', s)
    ]


# ---------------------------------------------------------------------------
# FluvialPanel
# ---------------------------------------------------------------------------

class FluvialPanel(BasePanel):
    """UI panel for the Fluvial Geomorphology tool."""

    def __init__(self, iface, parent=None):
        self._engine       = FluvialEngine()
        self._exporter     = RockMorphExporter(iface)
        self._results      = []
        self._worker       = None
        self._active_index = 0
        self._view_mode    = "longitudinal"   # "longitudinal" | "chi"
        self._rubber_bands = []               # QgsRubberBand knickpoint markers
        super().__init__(iface, parent)

    # ------------------------------------------------------------------
    # BasePanel hooks
    # ------------------------------------------------------------------

    def _html_file(self) -> str:
        return "fluvial.html"

    def _build_ui(self):
        root = QVBoxLayout(self._inner)
        root.setContentsMargins(8, 8, 8, 8)
        root.setSpacing(6)

        # ── Input ─────────────────────────────────────────────────────
        input_group  = QGroupBox(tr("Input"))
        input_layout = QFormLayout(input_group)

        self.dem_combo = QgsMapLayerComboBox()
        self.dem_combo.setFilters(QgsMapLayerProxyModel.RasterLayer)
        input_layout.addRow(tr("DEM layer:"), self.dem_combo)

        self.basin_combo = QgsMapLayerComboBox()
        self.basin_combo.setFilters(QgsMapLayerProxyModel.PolygonLayer)
        self.basin_combo.layerChanged.connect(self._on_basin_layer_changed)
        input_layout.addRow(tr("Basin layer:"), self.basin_combo)

        self.stream_combo = QgsMapLayerComboBox()
        self.stream_combo.setFilters(QgsMapLayerProxyModel.LineLayer)
        input_layout.addRow(tr("Stream network:"), self.stream_combo)

        # FAC — optional, with auto label
        fac_row    = QWidget()
        fac_layout = QHBoxLayout(fac_row)
        fac_layout.setContentsMargins(0, 0, 0, 0)
        fac_layout.setSpacing(4)

        self.fac_combo = QgsMapLayerComboBox()
        self.fac_combo.setFilters(QgsMapLayerProxyModel.RasterLayer)
        self.fac_combo.setAllowEmptyLayer(True)
        self.fac_combo.setCurrentIndex(0)  # empty = auto

        self._fac_auto_label = QLabel(tr("auto"))
        self._fac_auto_label.setStyleSheet(
            "color: #2980b9; font-size: 10px; font-style: italic;"
        )
        self.fac_combo.layerChanged.connect(self._on_fac_changed)

        fac_layout.addWidget(self.fac_combo, stretch=1)
        fac_layout.addWidget(self._fac_auto_label)
        input_layout.addRow(tr("FAC raster:"), fac_row)

        self.label_combo = QComboBox()
        self.label_combo.setToolTip(tr("Field used as basin label."))
        input_layout.addRow(tr("Label field:"), self.label_combo)

        self._on_basin_layer_changed(self.basin_combo.currentLayer())
        root.addWidget(input_group)

        # ── Parameters ────────────────────────────────────────────────
        param_group  = QGroupBox(tr("Parameters"))
        param_layout = QFormLayout(param_group)

        # theta_ref slider — live recompute of chi/ksn
        theta_row    = QWidget()
        theta_layout = QHBoxLayout(theta_row)
        theta_layout.setContentsMargins(0, 0, 0, 0)
        theta_layout.setSpacing(6)

        self.theta_slider = QSlider(Qt.Horizontal)
        self.theta_slider.setRange(10, 90)     # 0.10 → 0.90
        self.theta_slider.setValue(45)         # default 0.45
        self.theta_slider.setTickInterval(5)
        self.theta_slider.setToolTip(
            tr("Reference concavity index m/n (θref). "
               "Move to update chi and k_sn instantly.")
        )
        self._theta_label = QLabel("0.45")
        self._theta_label.setFixedWidth(32)
        self._theta_label.setStyleSheet("font-weight: bold; color: #2d6a9f;")
        self.theta_slider.valueChanged.connect(self._on_theta_changed)

        theta_layout.addWidget(self.theta_slider, stretch=1)
        theta_layout.addWidget(self._theta_label)
        param_layout.addRow(tr("θ ref (m/n):"), theta_row)

        self.a0_spin = QDoubleSpinBox()
        self.a0_spin.setRange(0.1, 1e6)
        self.a0_spin.setValue(1.0)
        self.a0_spin.setDecimals(1)
        self.a0_spin.setSuffix(" m²")
        self.a0_spin.setToolTip(tr("Reference drainage area A₀ (standard = 1 m²)."))
        param_layout.addRow(tr("A₀ reference:"), self.a0_spin)

        self.n_points_spin = QSpinBox()
        self.n_points_spin.setRange(50, 1000)
        self.n_points_spin.setValue(200)
        param_layout.addRow(tr("Profile points:"), self.n_points_spin)

        self.snap_spin = QDoubleSpinBox()
        self.snap_spin.setRange(0.1, 100.0)
        self.snap_spin.setValue(2.0)
        self.snap_spin.setDecimals(1)
        self.snap_spin.setSuffix(" m")
        param_layout.addRow(tr("Snap tolerance:"), self.snap_spin)

        self.sl_window_spin = QDoubleSpinBox()
        self.sl_window_spin.setRange(50, 5000)
        self.sl_window_spin.setValue(500.0)
        self.sl_window_spin.setDecimals(0)
        self.sl_window_spin.setSuffix(" m")
        self.sl_window_spin.setToolTip(
            tr("Moving window for SL / SLk computation (metres).")
        )
        param_layout.addRow(tr("SL window:"), self.sl_window_spin)

        self.n_knick_spin = QSpinBox()
        self.n_knick_spin.setRange(0, 5)
        self.n_knick_spin.setValue(3)
        self.n_knick_spin.setToolTip(
            tr("Maximum number of knickpoints to detect per river.")
        )
        param_layout.addRow(tr("Max knickpoints:"), self.n_knick_spin)

        self.smooth_spin = QSpinBox()
        self.smooth_spin.setRange(0, 30)
        self.smooth_spin.setValue(0)
        self.smooth_spin.setSuffix(" pts")
        self.smooth_spin.setToolTip(
            tr("Hanning smoothing window on elevation profile (0 = off).")
        )
        param_layout.addRow(tr("Smoothing:"), self.smooth_spin)

        root.addWidget(param_group)

        # ── Compute ───────────────────────────────────────────────────
        self.compute_btn = QPushButton(tr("Compute all basins"))
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

        # ── View toggle ───────────────────────────────────────────────
        view_group  = QGroupBox(tr("View"))
        view_layout = QHBoxLayout(view_group)

        self.btn_longitudinal = QRadioButton(tr("Longitudinal"))
        self.btn_chi          = QRadioButton(tr("Chi-plot"))
        self.btn_longitudinal.setChecked(True)
        self.btn_longitudinal.toggled.connect(self._on_view_toggled)

        self._view_btn_group = QButtonGroup()
        self._view_btn_group.addButton(self.btn_longitudinal, 0)
        self._view_btn_group.addButton(self.btn_chi,          1)

        view_layout.addWidget(self.btn_longitudinal)
        view_layout.addWidget(self.btn_chi)
        root.addWidget(view_group)

        # ── Layer checkboxes ──────────────────────────────────────────
        layers_group  = QGroupBox(tr("Layers"))
        layers_layout = QVBoxLayout(layers_group)

        # Longitudinal layers
        self._long_group = QGroupBox(tr("Longitudinal view"))
        lg = QVBoxLayout(self._long_group)
        self.chk_equil      = QCheckBox(tr("Equilibrium profile (Hack)"))
        # Radio SL vs SLk
        
        self._sl_btn_group = QButtonGroup()
        self.btn_sl_none = QRadioButton(tr("None"))
        self.btn_sl_raw  = QRadioButton(tr("SL (raw)"))
        self.btn_slk     = QRadioButton(tr("SLk (normalized)"))
        self.btn_sl_raw.setChecked(True)   # defaut

        self._sl_btn_group.addButton(self.btn_sl_none, 0)
        self._sl_btn_group.addButton(self.btn_sl_raw,  1)
        self._sl_btn_group.addButton(self.btn_slk,     2)
        
        lg.addWidget(QLabel(tr("SL index:")))
        # Checkbox invert Y2
        self.chk_sl_invert = QCheckBox(tr("Invert SL axis (Y2)"))
        self.chk_sl_invert.setChecked(False)
        lg.addWidget(self.chk_sl_invert)

        for btn in (self.btn_sl_none, self.btn_sl_raw, self.btn_slk):
            btn.toggled.connect(self._on_layer_toggled)
            lg.addWidget(btn)
    
        self.chk_knick_long = QCheckBox(tr("Knickpoints"))
        self.chk_equil.setChecked(True)
        self.chk_knick_long.setChecked(True)

        for chk in (self.chk_sl_invert, self.chk_equil, 
                   self.chk_knick_long):
            chk.stateChanged.connect(self._on_layer_toggled)
            lg.addWidget(chk)
        layers_layout.addWidget(self._long_group)

        # Chi-plot layers
        self._chi_group = QGroupBox(tr("Chi-plot view"))
        cg = QVBoxLayout(self._chi_group)
        self.chk_ksn_profile = QCheckBox(tr("k_sn profile (continuous)"))
        self.chk_ksn_segs    = QCheckBox(tr("k_sn segments"))
        self.chk_equil_chi   = QCheckBox(tr("Equilibrium line"))
        self.chk_knick_chi   = QCheckBox(tr("Knickpoints"))
        self.chk_ksn_profile.setChecked(True)
        self.chk_ksn_segs.setChecked(True)
        self.chk_equil_chi.setChecked(True)
        self.chk_knick_chi.setChecked(True)
        for chk in (self.chk_ksn_profile, self.chk_ksn_segs,
                    self.chk_equil_chi, self.chk_knick_chi):
            chk.stateChanged.connect(self._on_layer_toggled)
            cg.addWidget(chk)
        layers_layout.addWidget(self._chi_group)

        self._chi_group.setVisible(False)
        root.addWidget(layers_group)

        # ── Basin tree ────────────────────────────────────────────────
        results_group  = QGroupBox(tr("Results"))
        results_layout = QVBoxLayout(results_group)

        self.basin_tree = QTreeWidget()
        self.basin_tree.setHeaderLabels([
            tr("Basin"), tr("SLk max"), tr("χ max"),
            tr("ksn mean"), tr("ksn max"), tr("Knickpts"),
        ])
        self.basin_tree.setFixedHeight(160)
        self.basin_tree.setSelectionMode(QAbstractItemView.ExtendedSelection)
        self.basin_tree.itemSelectionChanged.connect(self._on_selection_changed)
        self.basin_tree.setContextMenuPolicy(Qt.CustomContextMenu)
        self.basin_tree.customContextMenuRequested.connect(
            self._on_tree_context_menu
        )
        results_layout.addWidget(self.basin_tree)

        # Navigation ◄ ►
        nav_layout = QHBoxLayout()
        self.prev_btn = QPushButton("◄")
        self.prev_btn.setFixedWidth(40)
        self.prev_btn.clicked.connect(self._on_prev)
        self.prev_btn.setEnabled(False)

        self.nav_label = QLabel("—")
        self.nav_label.setAlignment(Qt.AlignCenter)
        self.nav_label.setStyleSheet("color: #666; font-size: 11px;")

        self.next_btn = QPushButton("►")
        self.next_btn.setFixedWidth(40)
        self.next_btn.clicked.connect(self._on_next)
        self.next_btn.setEnabled(False)

        nav_layout.addWidget(self.prev_btn)
        nav_layout.addWidget(self.nav_label, stretch=1)
        nav_layout.addWidget(self.next_btn)
        results_layout.addLayout(nav_layout)

        # Stats bar
        stats_layout = QHBoxLayout()
        self.stat_n_label    = QLabel("—")
        self.stat_slk_label  = QLabel("—")
        self.stat_ksn_label  = QLabel("—")
        self.stat_knk_label  = QLabel("—")
        for lbl, widget in [
            ("n:",        self.stat_n_label),
            ("SLk mean:", self.stat_slk_label),
            ("ksn mean:", self.stat_ksn_label),
            ("knick:",    self.stat_knk_label),
        ]:
            stats_layout.addWidget(QLabel(tr(lbl)))
            stats_layout.addWidget(widget)
            stats_layout.addStretch(1)
        results_layout.addLayout(stats_layout)

        root.addWidget(results_group)

        # ── WebEngineView ──────────────────────────────────────────────
        self.webview.setMinimumHeight(420)
        self.webview.setSizePolicy(
            QSizePolicy.Expanding, QSizePolicy.Expanding
        )
        root.addWidget(self.webview)

        # ── Export ────────────────────────────────────────────────────
        export_group  = QGroupBox(tr("Export"))
        export_layout = QHBoxLayout(export_group)
        for fmt in ["PNG", "JPG", "SVG", "PDF", "CSV", "JSON"]:
            btn = QPushButton(fmt)
            btn.setFixedHeight(28)
            btn.clicked.connect(lambda checked, f=fmt: self._on_export(f))
            export_layout.addWidget(btn)
        root.addWidget(export_group)

        # ── Warnings ──────────────────────────────────────────────────
        self.warning_label = QLabel("")
        self.warning_label.setWordWrap(True)
        self.warning_label.setStyleSheet("color: #c0392b; font-size: 10px;")
        self.warning_label.setVisible(False)
        root.addWidget(self.warning_label)

    # ------------------------------------------------------------------
    # FAC combo — show/hide auto label
    # ------------------------------------------------------------------

    def _on_fac_changed(self, layer):
        is_auto = (layer is None or not layer.isValid())
        self._fac_auto_label.setVisible(is_auto)

    # ------------------------------------------------------------------
    # Basin layer changed — repopulate label combo
    # ------------------------------------------------------------------

    def _on_basin_layer_changed(self, layer):
        self.label_combo.clear()
        self.label_combo.addItem(tr("Auto-detect"), None)
        if layer is None:
            return
        for field in layer.fields():
            self.label_combo.addItem(field.name(), field.name())

    # ------------------------------------------------------------------
    # Theta slider — live update if results already computed
    # ------------------------------------------------------------------

    def _on_theta_changed(self, value: int):
        theta = value / 100.0
        self._theta_label.setText(f"{theta:.2f}")
        # If results exist — recompute chi/ksn in place (lightweight)
        # Full recompute is too slow for live update; we recalculate
        # only chi and ksn from the cached profile arrays.
        if self._results:
            self._recompute_chi_ksn(theta)
            self._show_active()

    def _recompute_chi_ksn(self, theta_ref: float):
        """
        Lightweight recompute of chi and ksn from cached arrays.
        Does NOT re-run GRASS or MainRiverExtractor.
        Called when user moves the theta slider.
        """

        a0 = self.a0_spin.value()

        for r in self._results:
            dist  = np.array(r["distances_m"])
            area  = np.array(r["area_m2"])
            slope = np.array(r["slope_local"])

            # Recompute chi
            chi = self._engine._compute_chi(dist, area, theta_ref, a0)
            r["chi"]     = chi.tolist()
            r["chi_max"] = round(float(np.nanmax(chi)), 4)

            # Recompute ksn profile
            ksn_prof, th_local = self._engine._compute_ksn_loglog(
                slope, area, theta_ref
            )
            r["ksn_profile"] = np.nan_to_num(ksn_prof, nan=0.0).tolist()
            r["ksn_mean"]    = round(float(np.nanmean(ksn_prof)), 2)
            r["ksn_max"]     = round(float(np.nanmax(ksn_prof)),  2)
            r["theta_local"] = round(float(np.nanmean(th_local)), 4)
            r["theta_ref"]   = theta_ref

            # Recompute ksn segments
            knickpoints = r.get("knickpoints", [])
            r["ksn_segments"] = self._engine._compute_ksn_segments(
                chi, np.array(r["elevations"]),
                slope, area, theta_ref, knickpoints
            )

        self._refresh_tree()
        self._update_stats()

    # ------------------------------------------------------------------
    # Compute
    # ------------------------------------------------------------------

    def _on_compute(self):
        dem_layer    = self.dem_combo.currentLayer()
        basin_layer  = self.basin_combo.currentLayer()
        stream_layer = self.stream_combo.currentLayer()

        if not self._engine.validate(
            dem_layer    = dem_layer,
            basin_layer  = basin_layer,
            stream_layer = stream_layer,
        ):
            self.show_error(tr(
                "Please select a valid DEM, a polygon basin layer, "
                "and a line stream network."
            ))
            return

        fac_layer = self.fac_combo.currentLayer()

        params = {
            "dem_layer":     dem_layer,
            "basin_layer":   basin_layer,
            "stream_layer":  stream_layer,
            "fac_layer":     fac_layer if (fac_layer and fac_layer.isValid()) else None,
            "label_field":   self.label_combo.currentData(),
            "n_points":      self.n_points_spin.value(),
            "snap_dist_m":   self.snap_spin.value(),
            "theta_ref":     self.theta_slider.value() / 100.0,
            "a0":            self.a0_spin.value(),
            "sl_window_m":   self.sl_window_spin.value(),
            "n_knickpoints": self.n_knick_spin.value(),
            "smooth":        self.smooth_spin.value(),
        }

        self.compute_btn.setEnabled(False)
        self.compute_btn.setText(tr("Computing…"))
        self.set_loading_state(True, tr("Starting computation..."))

        self._worker = ComputeWorker(self._engine, params)
        self._worker.progress.connect(self.update_progress)
        self._worker.finished.connect(self._on_compute_finished)
        self._worker.error.connect(self._on_compute_error)
        self._worker.start()

    def _on_compute_finished(self, result: dict):
        self.compute_btn.setEnabled(True)
        self.compute_btn.setText(tr("Compute all basins"))
        self.set_loading_state(False)

        self._results = result.get("results", [])
        warnings      = result.get("warnings", [])
        skipped       = result.get("skipped",  [])
        fac_auto      = result.get("fac_auto", False)

        if not self._results:
            self.show_error(tr("No valid basins found."))
            return

        # Resolve dist_m in knickpoints (idx → actual distance)
        for r in self._results:
            dist_arr = r["distances_m"]
            for kp in r.get("knickpoints", []):
                idx = kp.get("idx", 0)
                kp["dist_m"] = round(dist_arr[idx], 1) \
                    if idx < len(dist_arr) else 0.0

        if fac_auto:
            warnings.insert(0, tr(
                "Flow accumulation computed automatically via GRASS."
            ))

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
        self._update_stats()
        self._update_nav_buttons()
        self._show_active()

        msg = tr(f"{len(self._results)} basins computed.")
        if skipped:
            msg += tr(f" {len(skipped)} skipped.")
        self.show_info(msg)

    def _on_compute_error(self, message: str):
        self.compute_btn.setEnabled(True)
        self.compute_btn.setText(tr("Compute all basins"))
        self.set_loading_state(False)
        self.show_error(message)

    # ------------------------------------------------------------------
    # View toggle
    # ------------------------------------------------------------------

    def _on_view_toggled(self, checked: bool):
        self._view_mode = (
            "longitudinal" if self.btn_longitudinal.isChecked() else "chi"
        )
        self._long_group.setVisible(self._view_mode == "longitudinal")
        self._chi_group.setVisible(self._view_mode == "chi")
        if self._results:
            self._send_to_plot()

    def _on_layer_toggled(self):
        if self._results:
            self._send_to_plot()

    # ------------------------------------------------------------------
    # Navigation
    # ------------------------------------------------------------------

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
        self.basin_tree.blockSignals(True)
        self.basin_tree.clearSelection()
        item = self.basin_tree.topLevelItem(self._active_index)
        if item:
            item.setSelected(True)
            self.basin_tree.scrollToItem(item)
        self.basin_tree.blockSignals(False)
        self._update_nav_buttons()
        self._send_to_plot(
            single_fid=self._results[self._active_index]["fid"]
        )
        # Draw knickpoints on map canvas
        self._draw_knickpoints(self._results[self._active_index])

    def _update_nav_buttons(self):
        total = len(self._results)
        idx   = self._active_index
        self.prev_btn.setEnabled(idx > 0)
        self.next_btn.setEnabled(idx < total - 1)
        self.nav_label.setText(
            f"{idx + 1} / {total}" if total > 1 else ""
        )

    # ------------------------------------------------------------------
    # Tree
    # ------------------------------------------------------------------

    def _refresh_tree(self):
        self.basin_tree.clear()
        for r in self._results:
            item = QTreeWidgetItem(self.basin_tree)
            item.setText(0, r["label"])
            item.setText(1, f"{r['slk_max']:.3f}")
            item.setText(2, f"{r['chi_max']:.3f}")
            item.setText(3, f"{r['ksn_mean']:.1f}")
            item.setText(4, f"{r['ksn_max']:.1f}")
            item.setText(5, str(len(r.get("knickpoints", []))))
            item.setData(0, Qt.UserRole, r["fid"])
        for col in range(6):
            self.basin_tree.resizeColumnToContents(col)

    def _on_selection_changed(self):
        selected = self.basin_tree.selectedItems()
        if not selected:
            return
        idx = self.basin_tree.indexOfTopLevelItem(selected[0])
        if idx >= 0:
            self._active_index = idx
            self._update_nav_buttons()
        fids = [item.data(0, Qt.UserRole) for item in selected]
        self._send_to_plot(
            single_fid     = fids[0] if len(fids) == 1 else None,
            highlight_fids = fids,
        )
        if len(fids) == 1:
            r = self._find_result(fids[0])
            if r:
                self._draw_knickpoints(r)

    def _on_tree_context_menu(self, pos):
        item = self.basin_tree.itemAt(pos)
        if item is None:
            return
        fid = item.data(0, Qt.UserRole)
        if fid is None:
            return

        menu            = QMenu(self)
        zoom_action     = menu.addAction(tr("Zoom to Basin"))
        select_action   = menu.addAction(tr("Select on Map"))
        menu.addSeparator()
        copy_action     = menu.addAction(tr("Copy Stats to Clipboard"))
        export_action   = menu.addAction(tr("Export This Profile (CSV)…"))
        knick_action    = menu.addAction(tr("Export Knickpoints (CSV)…"))

        chosen = menu.exec_(
            self.basin_tree.viewport().mapToGlobal(pos)
        )
        if chosen == zoom_action:    self._zoom_to_basin(fid)
        elif chosen == select_action:  self._select_on_map(fid)
        elif chosen == copy_action:    self._copy_basin_stats(fid)
        elif chosen == export_action:  self._export_basin_profile(fid)
        elif chosen == knick_action:   self._export_knickpoints(fid)

    # ------------------------------------------------------------------
    # Stats bar
    # ------------------------------------------------------------------

    def _update_stats(self):
        if not self._results:
            return
        n     = len(self._results)
        slks  = [r["slk_max"]  for r in self._results]
        ksns  = [r["ksn_mean"] for r in self._results]
        knks  = [len(r.get("knickpoints", [])) for r in self._results]
        self.stat_n_label.setText(str(n))
        self.stat_slk_label.setText(f"{sum(slks)/n:.3f}")
        self.stat_ksn_label.setText(f"{sum(ksns)/n:.1f}")
        self.stat_knk_label.setText(f"{sum(knks)/n:.1f}")

    # ------------------------------------------------------------------
    # Plot communication
    # ------------------------------------------------------------------

    def _send_to_plot(
        self,
        single_fid:     int  = None,
        highlight_fids: list = None,
    ):
        if not self._results:
            return

        # Both views — show active basin only
        # highlight_fids used for multi-selection emphasis
        if single_fid is not None:
            display = [r for r in self._results if r["fid"] == single_fid]
        else:
            display = [self._results[self._active_index]]

        if self.btn_sl_none.isChecked():
            sl_mode = "none"
        elif self.btn_sl_raw.isChecked():
            sl_mode = "sl"
        else:
            sl_mode = "slk"

        payload = {
            "view_mode":       self._view_mode,
            "results":         display,
            "highlight_fids":  highlight_fids or [],

            # Layer toggles — longitudinal
            "show_equil":      self.chk_equil.isChecked(),
            "sl_mode":   sl_mode,
            "invert_sl": self.chk_sl_invert.isChecked(),
            "show_knick_long": self.chk_knick_long.isChecked(),

            # Layer toggles — chi
            "show_ksn_profile": self.chk_ksn_profile.isChecked(),
            "show_ksn_segs":    self.chk_ksn_segs.isChecked(),
            "show_equil_chi":   self.chk_equil_chi.isChecked(),
            "show_knick_chi":   self.chk_knick_chi.isChecked(),
        }

        self._last_data = payload
        js = f"updatePlot({json.dumps(json.dumps(payload))})"
        self.webview.page().runJavaScript(js)

    # ------------------------------------------------------------------
    # Knickpoint rubber bands on map canvas
    # ------------------------------------------------------------------

    def _clear_rubber_bands(self):
        for rb in self._rubber_bands:
            rb.reset()
        self._rubber_bands.clear()

    def _draw_knickpoints(self, result: dict):
        """Draw knickpoint markers on the QGIS map canvas."""
        self._clear_rubber_bands()

        knickpoints = result.get("knickpoints", [])
        if not knickpoints:
            return

        basin_layer = self.basin_combo.currentLayer()
        if basin_layer is None:
            return

        # Get the feature geometry to locate knickpoints on the river
        # We use dist_m to interpolate the position on the river geometry
        river_geom = self._get_river_geom(result["fid"])
        if river_geom is None:
            return

        canvas     = self.iface.mapCanvas()
        canvas_crs = canvas.mapSettings().destinationCrs()
        basin_crs  = basin_layer.crs()

        transform = None
        if basin_crs != canvas_crs:
            transform = QgsCoordinateTransform(
                basin_crs, canvas_crs, QgsProject.instance()
            )

        for kp in knickpoints:
            dist_m = kp.get("dist_m", 0.0)
            # Interpolate point along river at dist_m
            pt = river_geom.interpolate(dist_m)
            if pt.isEmpty():
                continue

            pt_xy = pt.asPoint()
            if transform:
                pt_xy = transform.transform(pt_xy)

            rb = QgsRubberBand(canvas, WkbTypes.PointGeometry)
            rb.setColor(QColor(231, 76, 60))    # red
            rb.setIconSize(10)
            rb.setIcon(QgsRubberBand.ICON_CIRCLE)
            rb.addPoint(pt_xy)
            self._rubber_bands.append(rb)

        canvas.refresh()

    def _get_river_geom(self, fid: int):
        """
        Retrieves the main river geometry for a basin fid
        from the stream layer intersected with the basin.
        Returns None if unavailable.
        """
        # The river geometry is not stored in results (too heavy).
        # We fall back to None — knickpoints appear in the plot only.
        # Future: store geom reference in results dict.
        return None

    # ------------------------------------------------------------------
    # Context menu helpers
    # ------------------------------------------------------------------

    def _zoom_to_basin(self, fid: int):
        layer = self.basin_combo.currentLayer()
        if not layer:
            return
        feature = layer.getFeature(fid)
        if not feature.isValid():
            return
        canvas = self.iface.mapCanvas()
        extent = feature.geometry().boundingBox()
        extent.scale(1.1)
        basin_crs  = layer.crs()
        canvas_crs = canvas.mapSettings().destinationCrs()
        if basin_crs != canvas_crs:
            t = QgsCoordinateTransform(
                basin_crs, canvas_crs, QgsProject.instance()
            )
            extent = t.transformBoundingBox(extent)
        canvas.setExtent(extent)
        canvas.refresh()

    def _select_on_map(self, fid: int):
        layer = self.basin_combo.currentLayer()
        if not layer:
            return
        layer.selectByIds([fid])

    def _copy_basin_stats(self, fid: int):
        r = self._find_result(fid)
        if not r:
            return
        n_knick = len(r.get("knickpoints", []))
        text = (
            f"Basin: {r['label']}\n"
            f"---------------------------\n"
            f"SLk max:    {r['slk_max']:.4f}\n"
            f"χ max:      {r['chi_max']:.4f}\n"
            f"k_sn mean:  {r['ksn_mean']:.2f}\n"
            f"k_sn max:   {r['ksn_max']:.2f}\n"
            f"θ local:    {r['theta_local']:.4f}\n"
            f"θ ref:      {r['theta_ref']:.2f}\n"
            f"Knickpts:   {n_knick}\n"
            f"Length:     {r['length_km']:.3f} km\n"
            f"Points:     {r['n_points']}"
        )
        QApplication.clipboard().setText(text)
        self.show_info(tr(f"Stats for '{r['label']}' copied."))

    def _export_basin_profile(self, fid: int):
        r = self._find_result(fid)
        if not r:
            return
        path, _ = QFileDialog.getSaveFileName(
            self, tr("Export Profile"),
            f"fluvial_{r['label']}.csv",
            "CSV Files (*.csv)"
        )
        if not path:
            return
        rows = [
            {
                "dist_m":  round(d, 1),
                "elev_m":  round(e, 2),
                "area_m2": round(a, 1),
                "slope":   round(s, 6),
                "sl":      round(sl, 2),
                "slk":     round(slk, 4),
                "chi":     round(c, 4),
                "ksn":     round(k, 2),
            }
            for d, e, a, s, sl, slk, c, k in zip(
                r["distances_m"], r["elevations"], r["area_m2"],
                r["slope_local"], r["sl"], r["slk"],
                r["chi"], r["ksn_profile"],
            )
        ]
        headers = ["dist_m", "elev_m", "area_m2", "slope",
                   "sl", "slk", "chi", "ksn"]
        self._exporter.save_csv(rows, headers, path)

    def _export_knickpoints(self, fid: int):
        r = self._find_result(fid)
        if not r:
            return
        knickpoints = r.get("knickpoints", [])
        if not knickpoints:
            self.show_error(tr("No knickpoints for this basin."))
            return
        path, _ = QFileDialog.getSaveFileName(
            self, tr("Export Knickpoints"),
            f"knickpoints_{r['label']}.csv",
            "CSV Files (*.csv)"
        )
        if not path:
            return
        rows = [
            {
                "basin":   r["label"],
                "chi":     kp["chi"],
                "dist_m":  kp["dist_m"],
                "elev_m":  kp["elev_m"],
            }
            for kp in knickpoints
        ]
        self._exporter.save_csv(rows, ["basin", "chi", "dist_m", "elev_m"], path)

    # ------------------------------------------------------------------
    # BasePanel abstract methods
    # ------------------------------------------------------------------

    def _on_result(self, data: dict):
        pass  # panel drives plot directly

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
                parent=self,
            )
            return
        if fmt_lower == "json":
            if self._last_data is None:
                self.show_error(tr("No data — run Compute first."))
                return
            self._exporter.export_json(self._last_data, parent=self)
            return
        ok, path, dpi = self._exporter.prepare_image_export(
            fmt_lower, parent=self
        )
        if not ok:
            return
        self._pending_export_path = path
        self._pending_export_dpi  = dpi
        self.webview.page().runJavaScript(
            f"exportViaSvg('{self.div_id}')"
        )

    def _csv_headers(self) -> list:
        return ["label", "fid", "slk_max", "chi_max",
                "ksn_mean", "ksn_max", "theta_local",
                "theta_ref", "n_knickpoints", "length_km"]

    def _build_csv_rows(self) -> list:
        return [
            {
                "label":         r["label"],
                "fid":           r["fid"],
                "slk_max":       r["slk_max"],
                "chi_max":       r["chi_max"],
                "ksn_mean":      r["ksn_mean"],
                "ksn_max":       r["ksn_max"],
                "theta_local":   r["theta_local"],
                "theta_ref":     r["theta_ref"],
                "n_knickpoints": len(r.get("knickpoints", [])),
                "length_km":     r["length_km"],
            }
            for r in self._results
        ]

    # ------------------------------------------------------------------
    # Helpers
    # ------------------------------------------------------------------

    def _find_result(self, fid: int) -> dict | None:
        for r in self._results:
            if r["fid"] == fid:
                return r
        return None

    def cleanup(self):
        """Called on plugin unload."""
        self._clear_rubber_bands()