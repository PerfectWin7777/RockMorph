# tools/ncp/panel.py

"""
tools/ncp/panel.py

NCPPanel — UI panel for the Normalized Channel Profile tool.

Layout
------
  Input group     : DEM + basin layer + stream network + label field
  Parameters      : n_points + snap_dist
  Compute button  + progress bar
  View toggle     : [Profiles] [Binary plot]
  Binary axes     : X-axis combo + Y-axis combo  (visible in binary mode)
  Basin tree      : label / MaxC / dL / Concavity%
  WebEngineView   : ncp.html
  Export group    : PNG / JPG / SVG / PDF / CSV / JSON

# ─────────────────────────────────────────────────────────────────
# TODO — Mode B : user provides (basin, pre-extracted river) pairs
#                 directly — skip MainRiverExtractor entirely.
#                 Add a radio button "Mode A / Mode B" at top of panel.
# ─────────────────────────────────────────────────────────────────

Authors: RockMorph contributors / Tony
"""

import json
import re

from PyQt5.QtWidgets import (  # type: ignore
    QVBoxLayout, QHBoxLayout, QFormLayout,
    QPushButton, QSpinBox, QDoubleSpinBox,
    QComboBox, QGroupBox, QLabel,
    QTreeWidget, QTreeWidgetItem,
    QSizePolicy, QAbstractItemView,
    QProgressBar, QButtonGroup, QRadioButton,
    QMenu, QApplication, QFileDialog, QWidget,
    QCheckBox
)
from PyQt5.QtCore import Qt, QCoreApplication, QThread, pyqtSignal  # type: ignore
from PyQt5.QtGui import QColor  # type: ignore
from qgis.gui import QgsMapLayerComboBox  # type: ignore
from qgis.core import (  # type: ignore
    QgsMapLayerProxyModel, QgsWkbTypes,
    QgsCoordinateTransform, QgsProject,
)

from ...base.base_panel import BasePanel
from ...core.exporter import RockMorphExporter
from .engine import NCPEngine

import re


def _natural_sort_key(s: str):
    return [
        int(part) if part.isdigit() else part.lower()
        for part in re.split(r'(\d+)', s)
    ]


def tr(message):
    return QCoreApplication.translate("RockMorph", message)


# ---------------------------------------------------------------------------
# Axis options for binary plot
# ---------------------------------------------------------------------------

AXIS_OPTIONS = [
    ("MaxC",        "maxC"),
    ("dL",          "dL"),
    ("Concavity %", "concavity"),
    ("Length (km)", "length_km"),
]


# ---------------------------------------------------------------------------
# Background worker
# ---------------------------------------------------------------------------

class _ComputeWorker(QThread):
    finished = pyqtSignal(dict)
    error    = pyqtSignal(str)

    def __init__(self, engine, params):
        super().__init__()
        self._engine = engine
        self._params = params

    def run(self):
        try:
            result = self._engine.compute(**self._params)
            self.finished.emit(result)
        except Exception as e:
            import traceback
            traceback.print_exc()
            self.error.emit(str(e))


# ---------------------------------------------------------------------------
# Panel
# ---------------------------------------------------------------------------

class NCPPanel(BasePanel):
    """UI panel for the Normalized Channel Profile tool."""

    def __init__(self, iface, parent=None):
        self._engine       = NCPEngine()
        self._exporter     = RockMorphExporter(iface)
        self._results      = []       # flat list from engine
        self._worker       = None
        self._active_index = 0
        self._view_mode    = "profiles"   # "profiles" | "binary"
        super().__init__(iface, parent)

    # ------------------------------------------------------------------
    # BasePanel hooks
    # ------------------------------------------------------------------

    def _html_file(self) -> str:
        return "ncp.html"

    def _build_ui(self):
        root = QVBoxLayout(self._inner)
        root.setContentsMargins(8, 8, 8, 8)
        root.setSpacing(6)

        # ── Input ─────────────────────────────────────────────────
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

        self.label_combo = QComboBox()
        self.label_combo.setToolTip(tr(
            "Field used as basin label.\n"
            "Auto-detects: nom, name, id, bv, basin."
        ))
        input_layout.addRow(tr("Label field:"), self.label_combo)

        self._on_basin_layer_changed(self.basin_combo.currentLayer())

        root.addWidget(input_group)

        # ── Parameters ────────────────────────────────────────────
        param_group  = QGroupBox(tr("Parameters"))
        param_layout = QFormLayout(param_group)

        self.n_points_spin = QSpinBox()
        self.n_points_spin.setRange(50, 1000)
        self.n_points_spin.setValue(200)
        self.n_points_spin.setToolTip(tr("Sample points along each river."))
        param_layout.addRow(tr("Profile points:"), self.n_points_spin)

        self.snap_spin = QDoubleSpinBox()
        self.snap_spin.setRange(0.1, 100.0)
        self.snap_spin.setValue(2.0)
        self.snap_spin.setDecimals(1)
        self.snap_spin.setSuffix(" m")
        self.snap_spin.setToolTip(tr(
            "Snapping tolerance for stream connectivity.\n"
            "Increase if your network has gaps between segments."
        ))
        param_layout.addRow(tr("Snap tolerance:"), self.snap_spin)

        self.show_arrows_check = QCheckBox(tr("Show MaxC / dL annotations"))
        self.show_arrows_check.setChecked(True)
        self.show_arrows_check.stateChanged.connect(
            lambda: self._show_active() if self._results else None
        )

        self.show_info_box_check = QCheckBox(tr("Show top-right info box"))
        self.show_info_box_check.setChecked(True)
        self.show_info_box_check.stateChanged.connect(
            lambda: self._show_active() if self._results else None
        )

        param_layout.addRow("", self.show_arrows_check)
        param_layout.addRow("", self.show_info_box_check)

        root.addWidget(param_group)

        # ── Compute button + progress ──────────────────────────────
        self.compute_btn = QPushButton(tr("Compute all basins"))
        self.compute_btn.setFixedHeight(36)
        self.compute_btn.setStyleSheet("""
            QPushButton {
                background-color: #2d6a9f; color: white;
                border-radius: 4px; font-weight: bold;
            }
            QPushButton:hover    { background-color: #3a7fc1; }
            QPushButton:pressed  { background-color: #1f4f7a; }
            QPushButton:disabled { background-color: #aaa; }
        """)
        self.compute_btn.clicked.connect(self._on_compute)
        root.addWidget(self.compute_btn)

        self.progress_bar = QProgressBar()
        self.progress_bar.setRange(0, 0)
        self.progress_bar.setFixedHeight(10)
        self.progress_bar.setVisible(False)
        root.addWidget(self.progress_bar)

        # ── View toggle ───────────────────────────────────────────
        view_group  = QGroupBox(tr("View"))
        view_layout = QHBoxLayout(view_group)

        self.btn_profiles = QRadioButton(tr("Normalized profiles"))
        self.btn_binary   = QRadioButton(tr("Binary plot"))
        self.btn_profiles.setChecked(True)
        self.btn_profiles.toggled.connect(self._on_view_toggled)

        self._view_btn_group = QButtonGroup()
        self._view_btn_group.addButton(self.btn_profiles, 0)
        self._view_btn_group.addButton(self.btn_binary,   1)

        view_layout.addWidget(self.btn_profiles)
        view_layout.addWidget(self.btn_binary)
        root.addWidget(view_group)

        # ── Binary axes (visible only in binary mode) ─────────────
        self.axes_group  = QGroupBox(tr("Binary plot axes"))
        axes_layout      = QFormLayout(self.axes_group)

        self.x_axis_combo = QComboBox()
        self.y_axis_combo = QComboBox()
        for label, key in AXIS_OPTIONS:
            self.x_axis_combo.addItem(tr(label), key)
            self.y_axis_combo.addItem(tr(label), key)
        # Defaults: X = MaxC, Y = dL  (matches published diagrams)
        self.x_axis_combo.setCurrentIndex(0)   # MaxC
        self.y_axis_combo.setCurrentIndex(1)   # dL
        self.x_axis_combo.currentIndexChanged.connect(self._on_axes_changed)
        self.y_axis_combo.currentIndexChanged.connect(self._on_axes_changed)

        axes_layout.addRow(tr("X axis:"), self.x_axis_combo)
        axes_layout.addRow(tr("Y axis:"), self.y_axis_combo)
        self.axes_group.setVisible(False)
        root.addWidget(self.axes_group)

        # ── Basin tree ────────────────────────────────────────────
        results_group  = QGroupBox(tr("Results"))
        results_layout = QVBoxLayout(results_group)

        self.basin_tree = QTreeWidget()
        self.basin_tree.setHeaderLabels([
            tr("Basin"), tr("MaxC"), tr("dL"), tr("Concavity %"), tr("Length km")
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
        self.stat_n_label      = QLabel("—")
        self.stat_maxc_label   = QLabel("—")
        self.stat_dl_label     = QLabel("—")
        self.stat_concav_label = QLabel("—")
        for lbl, widget in [
            ("n:", self.stat_n_label),
            ("MaxC mean:", self.stat_maxc_label),
            ("dL mean:", self.stat_dl_label),
            ("Concavity mean:", self.stat_concav_label),
        ]:
            stats_layout.addWidget(QLabel(lbl))
            stats_layout.addWidget(widget)
        results_layout.addLayout(stats_layout)

        root.addWidget(results_group)

        # ── WebEngineView ──────────────────────────────────────────
        self.webview.setMinimumHeight(400)
        self.webview.setSizePolicy(
            QSizePolicy.Expanding, QSizePolicy.Expanding
        )
        root.addWidget(self.webview)

        # ── Export ────────────────────────────────────────────────
        export_group  = QGroupBox(tr("Export"))
        export_layout = QHBoxLayout(export_group)
        for fmt in ["PNG", "JPG", "SVG", "PDF", "CSV", "JSON"]:
            btn = QPushButton(fmt)
            btn.setFixedHeight(28)
            btn.clicked.connect(lambda checked, f=fmt: self._on_export(f))
            export_layout.addWidget(btn)
        root.addWidget(export_group)

        # ── Warnings ──────────────────────────────────────────────
        self.warning_label = QLabel("")
        self.warning_label.setWordWrap(True)
        self.warning_label.setStyleSheet(
            "color: #c0392b; font-size: 10px;"
        )
        self.warning_label.setVisible(False)
        root.addWidget(self.warning_label)
    
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
        # Sync tree selection
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

    def _update_nav_buttons(self):
        total = len(self._results)
        idx   = self._active_index
        self.prev_btn.setEnabled(idx > 0)
        self.next_btn.setEnabled(idx < total - 1)
        self.nav_label.setText(
            f"{idx + 1} / {total}" if total > 1 else ""
        )

    # ------------------------------------------------------------------
    # Layer changed
    # ------------------------------------------------------------------

    def _on_basin_layer_changed(self, layer):
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

        params = {
            "dem_layer":    dem_layer,
            "basin_layer":  basin_layer,
            "stream_layer": stream_layer,
            "label_field":  self.label_combo.currentData(),
            "n_points":     self.n_points_spin.value(),
            "snap_dist_m":  self.snap_spin.value(),
        }

        self.compute_btn.setEnabled(False)
        self.compute_btn.setText(tr("Computing…"))
        self.progress_bar.setVisible(True)

        self._worker = _ComputeWorker(self._engine, params)
        self._worker.finished.connect(self._on_compute_finished)
        self._worker.error.connect(self._on_compute_error)
        self._worker.start()

    def _on_compute_finished(self, result: dict):
        self.compute_btn.setEnabled(True)
        self.compute_btn.setText(tr("Compute all basins"))
        self.progress_bar.setVisible(False)

        self._results = result.get("results", [])
        warnings      = result.get("warnings", [])
        skipped       = result.get("skipped",  [])

        if not self._results:
            self.show_error(tr("No valid basins found."))
            return

        if warnings:
            self.warning_label.setText("\n".join(warnings[:5]))
            self.warning_label.setVisible(True)
        else:
            self.warning_label.setVisible(False)

        # Sort naturally by label
        self._results = sorted(
            self._results,
            key=lambda r: _natural_sort_key(r["label"])
        )

        # Add length_km convenience field
        for r in self._results:
            r["length_km"] = round(r["length_m"] / 1000, 3)

        self._active_index = 0
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
        self.progress_bar.setVisible(False)
        self.show_error(message)

    # ------------------------------------------------------------------
    # View toggle
    # ------------------------------------------------------------------

    def _on_view_toggled(self, checked: bool):
        """Switch between profiles and binary plot."""
        self._view_mode = "profiles" if self.btn_profiles.isChecked() else "binary"
        self.axes_group.setVisible(self._view_mode == "binary")
        if self._results:
            self._send_to_plot()

    def _on_axes_changed(self):
        """Re-render binary plot with new axis selection."""
        if self._results and self._view_mode == "binary":
            self._send_to_plot()

    # ------------------------------------------------------------------
    # Tree
    # ------------------------------------------------------------------

    def _refresh_tree(self):
        self.basin_tree.clear()
        for r in self._results:
            item = QTreeWidgetItem(self.basin_tree)
            item.setText(0, r["label"])
            item.setText(1, f"{r['maxC']:.4f}")
            item.setText(2, f"{r['dL']:.4f}")
            item.setText(3, f"{r['concavity']:.2f}")
            item.setText(4, f"{r['length_km']:.2f}")
            item.setData(0, Qt.UserRole, r["fid"])
        self.basin_tree.resizeColumnToContents(0)
        self.basin_tree.resizeColumnToContents(1)
        self.basin_tree.resizeColumnToContents(2)
        self.basin_tree.resizeColumnToContents(3)

    def _on_selection_changed(self):
        """Highlight selected basins in the current view."""
        selected = self.basin_tree.selectedItems()
        if not selected:
            return
        # Update active index from tree click
        idx = self.basin_tree.indexOfTopLevelItem(selected[0])
        if idx >= 0:
            self._active_index = idx
            self._update_nav_buttons()
        fids = [item.data(0, Qt.UserRole) for item in selected]
        self._send_to_plot(single_fid=fids[0] if len(fids) == 1 else None,
                           highlight_fids=fids)

    def _get_selected_fids(self) -> list:
        fids = []
        for item in self.basin_tree.selectedItems():
            fid = item.data(0, Qt.UserRole)
            if fid is not None:
                fids.append(fid)
        return fids

    def _on_tree_context_menu(self, pos):
        item = self.basin_tree.itemAt(pos)
        if item is None:
            return
        fid = item.data(0, Qt.UserRole)
        if fid is None:
            return

        menu = QMenu(self)
        zoom_action        = menu.addAction(tr("Zoom to Basin"))
        select_action      = menu.addAction(tr("Select on Map"))
        menu.addSeparator()
        copy_action        = menu.addAction(tr("Copy Stats to Clipboard"))
        export_curve_action = menu.addAction(tr("Export This Profile (CSV)…"))

        chosen = menu.exec_(
            self.basin_tree.viewport().mapToGlobal(pos)
        )

        if chosen == zoom_action:
            self._zoom_to_basin(fid)
        elif chosen == select_action:
            self._select_on_map(fid)
        elif chosen == copy_action:
            self._copy_basin_stats(fid)
        elif chosen == export_curve_action:
            self._export_basin_profile(fid)

    # ------------------------------------------------------------------
    # Context menu actions
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
        canvas.flashFeatureIds(layer, [fid])

    def _select_on_map(self, fid: int):
        layer = self.basin_combo.currentLayer()
        if not layer:
            return
        layer.selectByIds([fid])
        self.iface.mapCanvas().flashFeatureIds(layer, [fid])

    def _copy_basin_stats(self, fid: int):
        result = self._find_result(fid)
        if not result:
            return
        text = (
            f"Basin: {result['label']}\n"
            f"---------------------------\n"
            f"MaxC:       {result['maxC']:.4f}\n"
            f"dL:         {result['dL']:.4f}\n"
            f"Concavity:  {result['concavity']:.2f} %\n"
            f"Length:     {result['length_km']:.3f} km\n"
            f"Points:     {result['n_points']}"
        )
        QApplication.clipboard().setText(text)
        self.show_info(tr(f"Stats for '{result['label']}' copied."))

    def _export_basin_profile(self, fid: int):
        result = self._find_result(fid)
        if not result:
            return
        path, _ = QFileDialog.getSaveFileName(
            self,
            tr("Export Profile"),
            f"ncp_{result['label']}.csv",
            "CSV Files (*.csv)"
        )
        if not path:
            return
        rows = [
            {"dist_norm": round(x, 6), "elev_norm": round(y, 6)}
            for x, y in zip(result["x"], result["y"])
        ]
        self._exporter.save_csv(rows, ["dist_norm", "elev_norm"], path)

    # ------------------------------------------------------------------
    # Stats bar
    # ------------------------------------------------------------------

    def _update_stats(self):
        if not self._results:
            return
        n     = len(self._results)
        maxcs = [r["maxC"]      for r in self._results]
        dls   = [r["dL"]        for r in self._results]
        cons  = [r["concavity"] for r in self._results]
        self.stat_n_label.setText(str(n))
        self.stat_maxc_label.setText(f"{sum(maxcs)/n:.4f}")
        self.stat_dl_label.setText(f"{sum(dls)/n:.4f}")
        self.stat_concav_label.setText(f"{sum(cons)/n:.2f} %")

    # ------------------------------------------------------------------
    # Plot communication
    # ------------------------------------------------------------------

    def _send_to_plot(self, highlight_fids: list = None, single_fid: int = None):
        """Build payload and call JS updatePlot()."""
        if not self._results:
            return

        n         = len(self._results)
        mean_maxC = sum(r["maxC"] for r in self._results) / n
        mean_dL   = sum(r["dL"]   for r in self._results) / n

        # In profile view — send only the active single basin
        if self._view_mode == "profiles" and single_fid is not None:
            display_results = [r for r in self._results if r["fid"] == single_fid]
        elif self._view_mode == "profiles":
            # First call after compute — show first basin
            display_results = [self._results[self._active_index]]
        else:
            # Binary plot — always all results
            display_results = self._results

        payload = {
            "view_mode":      self._view_mode,
            "results":        display_results,
            "all_results":    self._results,   # binary always needs all
            "highlight_fids": highlight_fids or [],
            "mean_maxC":      round(mean_maxC, 4),
            "mean_dL":        round(mean_dL,   4),
            "show_arrows":   self.show_arrows_check.isChecked(),
            "show_info_box": self.show_info_box_check.isChecked(),
            "x_axis":         self.x_axis_combo.currentData(),
            "y_axis":         self.y_axis_combo.currentData(),
            "x_axis_label":   self.x_axis_combo.currentText(),
            "y_axis_label":   self.y_axis_combo.currentText(),
        }

        self._last_data = payload
        js = f"updatePlot({json.dumps(json.dumps(payload))})"
        self.webview.page().runJavaScript(js)

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
                parent=self
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
        return ["label", "fid", "maxC", "dL",
                "concavity", "length_km", "n_points"]

    def _build_csv_rows(self) -> list:
        return [
            {
                "label":      r["label"],
                "fid":        r["fid"],
                "maxC":       r["maxC"],
                "dL":         r["dL"],
                "concavity":  r["concavity"],
                "length_km":  r["length_km"],
                "n_points":   r["n_points"],
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