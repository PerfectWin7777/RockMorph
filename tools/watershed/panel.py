# tools/watershed/panel.py

"""
tools/watershed/panel.py

WatershedPanel — UI panel for the Watershed Delineation & Subdivision tool.

Layout
------
  Input group      : fdir raster + facc raster + outlet point (optional)
  Encoding group   : auto-detect combo (ESRI / GRASS / SAGA)
  Mode group       : radio N sub-basins  ↔  radio Min area
  Parameters       : N spin  |  min area spin  |  snap radius  |  polygon method
  Compute button   + progress bar
  Results table    : rank / area km² / n pixels / outlet XY
  Map output group : layer name + color ramp + add-to-map button
  Export group     : Shapefile / GeoPackage / CSV

No WebEngineView — results are spatial (polygons on the QGIS canvas).
Stats are displayed in a QTreeWidget; no Plotly needed at this stage.

Authors: RockMorph contributors / Tony
"""

import os
import traceback

import numpy as np                              # type: ignore

from PyQt5.QtWidgets import (                   # type: ignore
    QVBoxLayout, QHBoxLayout, QFormLayout,
    QPushButton, QSpinBox, QDoubleSpinBox,
    QComboBox, QGroupBox, QLabel,
    QTreeWidget, QTreeWidgetItem,
    QSizePolicy, QAbstractItemView,
    QButtonGroup, QRadioButton,
    QFileDialog, QLineEdit, QCheckBox,
    QWidget
)
from PyQt5.QtCore import Qt, QCoreApplication  # type: ignore
from PyQt5.QtGui import QColor                 # type: ignore

from qgis.gui import QgsMapLayerComboBox        # type: ignore
from qgis.core import (                         # type: ignore
    QgsMapLayerProxyModel,
    QgsWkbTypes,
    QgsVectorLayer,
    QgsFeature,
    QgsGeometry,
    QgsField,
    QgsFields,
    QgsProject,
    QgsPointXY,
    QgsMarkerSymbol,
    QgsCategorizedSymbolRenderer,
    QgsRendererCategory,
    QgsFillSymbol,
)
from PyQt5.QtCore import QVariant              # type: ignore

from ...base.base_panel import BasePanel, ComputeWorker
from ...core.exporter import RockMorphExporter
from .engine import WatershedEngine


def tr(message: str) -> str:
    return QCoreApplication.translate("RockMorph", message)


# ---------------------------------------------------------------------------
# Color palette for sub-basins (up to 10 ranks)
# ---------------------------------------------------------------------------

_SUBBASIN_COLORS: list[str] = [
    "#2980b9",   # rank 1 — blue
    "#27ae60",   # rank 2 — green
    "#e67e22",   # rank 3 — orange
    "#8e44ad",   # rank 4 — purple
    "#c0392b",   # rank 5 — red
    "#16a085",   # rank 6 — teal
    "#f39c12",   # rank 7 — amber
    "#2c3e50",   # rank 8 — dark blue
    "#d35400",   # rank 9 — burnt orange
    "#7f8c8d",   # rank 10+ — grey
]


def _rank_color(rank: int) -> str:
    """Returns a hex color string for a given sub-basin rank (1-indexed)."""
    idx = min(rank - 1, len(_SUBBASIN_COLORS) - 1)
    return _SUBBASIN_COLORS[idx]


# ===========================================================================
# WatershedPanel
# ===========================================================================

class WatershedPanel(BasePanel):
    """
    UI panel for the Watershed Delineation & Subdivision tool.

    Responsibilities
    ----------------
    - Collects user inputs (layers, encoding, mode, parameters).
    - Launches WatershedEngine via ComputeWorker (background thread).
    - Displays per-sub-basin stats in a QTreeWidget.
    - Adds result polygons to the QGIS canvas as an in-memory vector layer.
    - Exports results to Shapefile, GeoPackage, or CSV.
    """

    def __init__(self, iface, parent=None):
        self._engine   = WatershedEngine()
        self._exporter = RockMorphExporter(iface)
        self._results  = []        # list[dict] — last compute() output
        self._map_layer: QgsVectorLayer | None = None   # current result layer
        self._worker   = None
        super().__init__(iface, parent)

    # ------------------------------------------------------------------
    # BasePanel hooks
    # ------------------------------------------------------------------

    def _html_file(self) -> str:
        # No WebEngineView — return empty string; _load_html is bypassed.
        return ""

    def _load_html(self) -> None:
        # Override to skip HTML loading entirely for this panel.
        pass

    def _build_ui(self) -> None:
        root = QVBoxLayout(self._inner)
        root.setContentsMargins(8, 8, 8, 8)
        root.setSpacing(6)

        # ── Input layers ──────────────────────────────────────────────
        input_group  = QGroupBox(tr("Input Layers"))
        input_layout = QFormLayout(input_group)

        self.fdir_combo = QgsMapLayerComboBox()
        self.fdir_combo.setFilters(QgsMapLayerProxyModel.RasterLayer)
        self.fdir_combo.setToolTip(tr(
            "<b>Flow Direction (D8):</b><br>"
            "Raster encoding which of the 8 neighbours each pixel drains to.<br>"
            "Produced by GRASS r.watershed, SAGA, TauDEM, or ArcGIS Fill+FDir."
        ))
        input_layout.addRow(tr("Flow direction:"), self.fdir_combo)

        self.facc_combo = QgsMapLayerComboBox()
        self.facc_combo.setFilters(QgsMapLayerProxyModel.RasterLayer)
        self.facc_combo.setToolTip(tr(
            "<b>Flow Accumulation:</b><br>"
            "Raster counting how many upstream pixels drain to each cell.<br>"
            "Co-registered with the flow-direction raster (same grid + CRS)."
        ))
        input_layout.addRow(tr("Flow accumulation:"), self.facc_combo)

        # Outlet — optional
        outlet_row    = self._make_optional_row()
        self.outlet_combo = QgsMapLayerComboBox()
        self.outlet_combo.setFilters(QgsMapLayerProxyModel.PointLayer)
        self.outlet_combo.setAllowEmptyLayer(True)
        self.outlet_combo.setCurrentIndex(0)
        self.outlet_combo.setToolTip(tr(
            "<b>Outlet Point (optional):</b><br>"
            "A single point feature defining the basin mouth.<br>"
            "If left empty, the pixel of maximum accumulation is used automatically."
        ))
        self._outlet_auto_label = QLabel(tr("auto"))
        self._outlet_auto_label.setStyleSheet(
            "color: #2980b9; font-size: 12px; font-style: italic;"
        )
        self.outlet_combo.layerChanged.connect(self._on_outlet_changed)
        outlet_row.layout().addWidget(self.outlet_combo, stretch=1)
        outlet_row.layout().addWidget(self._outlet_auto_label)
        input_layout.addRow(tr("Outlet point:"), outlet_row)

        root.addWidget(input_group)

        # ── Encoding ──────────────────────────────────────────────────
        enc_group  = QGroupBox(tr("D8 Encoding"))
        enc_layout = QFormLayout(enc_group)

        self.encoding_combo = QComboBox()
        self.encoding_combo.addItem(tr("Auto-detect"),  "auto")
        self.encoding_combo.addItem(tr("ESRI / ArcGIS (powers of 2)"), "esri")
        self.encoding_combo.addItem(tr("GRASS r.watershed (1–8)"),     "grass")
        self.encoding_combo.addItem(tr("SAGA / TauDEM (0–7)"),         "saga")
        self.encoding_combo.setToolTip(tr(
            "<b>Flow Direction Encoding:</b><br>"
            "Different GIS tools encode the 8 D8 directions differently.<br><br>"
            "• <b>Auto</b>: RockMorph detects the format from the raster values.<br>"
            "• <b>ESRI</b>: 1, 2, 4, 8, 16, 32, 64, 128 (powers of two).<br>"
            "• <b>GRASS</b>: 1 to 8 (clockwise from NE).<br>"
            "• <b>SAGA</b>: 0 to 7 (clockwise from N).<br><br>"
            "Override auto-detect only if you know the exact source."
        ))
        enc_layout.addRow(tr("Encoding:"), self.encoding_combo)
        root.addWidget(enc_group)

        # ── Subdivision mode ──────────────────────────────────────────
        mode_group  = QGroupBox(tr("Subdivision Mode"))
        mode_layout = QVBoxLayout(mode_group)

        self.btn_mode_n    = QRadioButton(tr("By number of sub-basins (N)"))
        self.btn_mode_area = QRadioButton(tr("By minimum area threshold"))
        self.btn_mode_n.setChecked(True)

        self._mode_group = QButtonGroup()
        self._mode_group.addButton(self.btn_mode_n,    0)
        self._mode_group.addButton(self.btn_mode_area, 1)

        self.btn_mode_n.toggled.connect(self._on_mode_changed)

        mode_layout.addWidget(self.btn_mode_n)
        mode_layout.addWidget(self.btn_mode_area)
        root.addWidget(mode_group)

        # ── Parameters ────────────────────────────────────────────────
        param_group  = QGroupBox(tr("Parameters"))
        param_layout = QFormLayout(param_group)

        # N sub-basins
        self.n_spin = QSpinBox()
        self.n_spin.setRange(2, 200)
        self.n_spin.setValue(15)
        self.n_spin.setToolTip(tr(
            "<b>Number of Sub-basins (N):</b><br>"
            "The engine selects the N−1 confluences with the largest upstream "
            "drainage area, producing N sub-basins.<br><br>"
            "The residual (downstream) unit is always included automatically."
        ))
        self._n_label = QLabel(tr("N sub-basins:"))
        param_layout.addRow(self._n_label, self.n_spin)

        # Min area
        self.area_spin = QDoubleSpinBox()
        self.area_spin.setRange(0.01, 100_000.0)
        self.area_spin.setValue(10.0)
        self.area_spin.setDecimals(2)
        self.area_spin.setSuffix(" km²")
        self.area_spin.setToolTip(tr(
            "<b>Minimum Sub-basin Area:</b><br>"
            "Any confluence whose upstream area is smaller than this threshold "
            "is discarded, avoiding hydrologically insignificant units."
        ))
        self._area_label = QLabel(tr("Min area:"))
        self._area_label.setEnabled(False)
        self.area_spin.setEnabled(False)
        param_layout.addRow(self._area_label, self.area_spin)

        # Polygon method
        self.poly_combo = QComboBox()
        self.poly_combo.addItem(tr("Auto (smart dispatch)"), "auto")
        self.poly_combo.addItem(tr("NumPy (fast, simple shapes)"),   "numpy")
        self.poly_combo.addItem(tr("GDAL (robust, complex shapes)"), "gdal")
        self.poly_combo.setToolTip(tr(
            "<b>Polygon Method:</b><br>"
            "Controls how raster masks are converted to vector polygons.<br><br>"
            "• <b>Auto</b>: NumPy for small basins (<20 000 px), GDAL for large ones.<br>"
            "• <b>NumPy</b>: Fast pixel-edge cancellation. Best for simple shapes.<br>"
            "• <b>GDAL</b>: Handles holes and multi-part basins correctly."
        ))
        param_layout.addRow(tr("Polygon method:"), self.poly_combo)


        # NOUVEAU : Option de segmentation du fleuve
        self.chk_segment_main = QCheckBox(tr("Segment main river stems"))
        self.chk_segment_main.setChecked(True) # Option 2 par défaut
        self.chk_segment_main.setToolTip(tr(
            "<b>Segment Main Stems:</b><br>"
            "If checked, main rivers will be split into segments at major confluences.<br>"
            "If unchecked, the main trunk will remain a single continuous sub-basin."
        ))
        param_layout.addRow("", self.chk_segment_main)
        
        self.chk_edge_basins = QCheckBox(tr("Extract edge basins"))
        self.chk_edge_basins.setChecked(True)
        self.chk_edge_basins.setToolTip(tr(
            "<b>Extract Edge Basins:</b><br>"
            "If checked, the algorithm will also extract independent catchments "
            "draining off the map boundary.<br>"
            "Allocates at most 1/3 of the total requested sub-basins to borders."
        ))
        param_layout.addRow("", self.chk_edge_basins)


        root.addWidget(param_group)

        # ── Compute ───────────────────────────────────────────────────
        self.compute_btn = QPushButton(tr("Delineate sub-basins"))
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
        root.addWidget(self._progress_container)

        # ── Results table ─────────────────────────────────────────────
        results_group  = QGroupBox(tr("Sub-basin Results"))
        results_layout = QVBoxLayout(results_group)

        self.result_tree = QTreeWidget()
        self.result_tree.setHeaderLabels([
            tr("Rank"),
            tr("Area (km²)"),
            tr("Pixels"),
            tr("Outlet X"),
            tr("Outlet Y"),
        ])
        self.result_tree.setFixedHeight(180)
        self.result_tree.setSortingEnabled(True)
        self.result_tree.setSelectionMode(QAbstractItemView.SingleSelection)
        self.result_tree.itemSelectionChanged.connect(
            self._on_result_selected
        )
        results_layout.addWidget(self.result_tree)

        # Summary bar
        summary_layout = QHBoxLayout()
        self._lbl_total_area  = QLabel("—")
        self._lbl_n_subbasins = QLabel("—")
        self._lbl_encoding    = QLabel("—")
        for label, widget in [
            (tr("Total area:"),   self._lbl_total_area),
            (tr("Sub-basins:"),   self._lbl_n_subbasins),
            (tr("Encoding:"),     self._lbl_encoding),
        ]:
            summary_layout.addWidget(QLabel(label))
            summary_layout.addWidget(widget)
            summary_layout.addStretch(1)
        results_layout.addLayout(summary_layout)

        root.addWidget(results_group)

        # ── Map output ────────────────────────────────────────────────
        map_group  = QGroupBox(tr("Map Output"))
        map_layout = QFormLayout(map_group)

        self.layer_name_edit = QLineEdit("Watershed_subbasins")
        self.layer_name_edit.setToolTip(tr(
            "Name of the in-memory vector layer added to the QGIS project."
        ))
        map_layout.addRow(tr("Layer name:"), self.layer_name_edit)

        self.chk_zoom = QCheckBox(tr("Zoom to result after compute"))
        self.chk_zoom.setChecked(True)
        map_layout.addRow("", self.chk_zoom)

        add_btn = QPushButton(tr("Add / Refresh layer on map"))
        add_btn.clicked.connect(self._add_layer_to_map)
        map_layout.addRow("", add_btn)

        root.addWidget(map_group)

        # ── Export ────────────────────────────────────────────────────
        export_group  = QGroupBox(tr("Export"))
        export_layout = QHBoxLayout(export_group)

        for fmt in ["Shapefile", "GeoPackage", "CSV"]:
            btn = QPushButton(fmt)
            btn.setFixedHeight(28)
            btn.clicked.connect(
                lambda checked, f=fmt: self._on_export(f)
            )
            export_layout.addWidget(btn)

        root.addWidget(export_group)

        # ── Warning label ─────────────────────────────────────────────
        self.warning_label = QLabel("")
        self.warning_label.setWordWrap(True)
        self.warning_label.setStyleSheet(
            "color: #c0392b; font-size: 10px;"
        )
        self.warning_label.setVisible(False)
        root.addWidget(self.warning_label)

    # ------------------------------------------------------------------
    # UI helpers
    # ------------------------------------------------------------------

    @staticmethod
    def _make_optional_row():
        """Creates a QWidget with a horizontal QHBoxLayout for optional combos."""
        w = QWidget()
        layout = QHBoxLayout(w)
        layout.setContentsMargins(0, 0, 0, 0)
        layout.setSpacing(4)
        return w

    def _on_outlet_changed(self, layer):
        is_auto = (layer is None or not layer.isValid())
        self._outlet_auto_label.setVisible(is_auto)

    def _on_mode_changed(self, checked: bool):
        """Enables / disables parameter widgets based on the active mode."""
        n_mode = self.btn_mode_n.isChecked()
        self._n_label.setEnabled(n_mode)
        self.n_spin.setEnabled(n_mode)
        self._area_label.setEnabled(not n_mode)
        self.area_spin.setEnabled(not n_mode)

    # ------------------------------------------------------------------
    # Compute
    # ------------------------------------------------------------------

    def _on_compute(self):
        fdir_layer = self.fdir_combo.currentLayer()
        facc_layer = self.facc_combo.currentLayer()

        if not self._engine.validate(
            fdir_layer = fdir_layer,
            facc_layer = facc_layer,
        ):
            self.show_error(tr(
                "Please select a valid flow-direction raster "
                "and a flow-accumulation raster."
            ))
            return

        outlet_layer = self.outlet_combo.currentLayer()

        mode = "n" if self.btn_mode_n.isChecked() else "area"

        params = {
            "fdir_layer":        fdir_layer,
            "facc_layer":        facc_layer,
            "outlet_layer":      outlet_layer if (outlet_layer and outlet_layer.isValid()) else None,
            "encoding":          self.encoding_combo.currentData(),
            "mode":              mode,
            "n_subbasins":       self.n_spin.value(),
            "min_area_km2":      self.area_spin.value(),
            "polygon_method":    self.poly_combo.currentData(),
            "segment_main_stem": self.chk_segment_main.isChecked(), 
            "extract_edge_basins": self.chk_edge_basins.isChecked(),
        }

        self.compute_btn.setEnabled(False)
        self.compute_btn.setText(tr("Computing…"))
        self.set_loading_state(True, tr("Starting delineation…"))

        self._worker = ComputeWorker(self._engine, params)
        self._worker.progress.connect(self.update_progress)
        self._worker.finished.connect(self._on_compute_finished)
        self._worker.error.connect(self._on_compute_error)
        self._worker.start()

    def _on_compute_finished(self, result: dict):
        self.compute_btn.setEnabled(True)
        self.compute_btn.setText(tr("Delineate sub-basins"))
        self.set_loading_state(False)

        subbasins = result.get("subbasins", [])
        warnings  = result.get("warnings",  [])
        skipped   = result.get("skipped",   [])
        encoding  = result.get("encoding",  "—")
        parent    = result.get("parent_basin", {})

        # Store for export
        self._results   = subbasins
        self._last_data = result

        # Warnings
        all_warnings = warnings + skipped
        if all_warnings:
            self.warning_label.setText("\n".join(all_warnings[:6]))
            self.warning_label.setVisible(True)
        else:
            self.warning_label.setVisible(False)

        if not subbasins:
            self.show_error(tr("No sub-basins produced. Check warnings."))
            return

        # Populate result table
        self._refresh_tree(subbasins)

        # Summary bar
        total_area = sum(sb["area_km2"] for sb in subbasins)
        self._lbl_total_area.setText(f"{total_area:.2f} km²")
        self._lbl_n_subbasins.setText(str(len(subbasins)))
        self._lbl_encoding.setText(encoding.upper())

        # Add polygons to QGIS map
        self._add_layer_to_map()

        # Zoom to result
        if self.chk_zoom.isChecked() and self._map_layer:
            self.iface.mapCanvas().setExtent(
                self._map_layer.extent()
            )
            self.iface.mapCanvas().refresh()

        msg = tr(
            f"{len(subbasins)} sub-basins delineated "
            f"(encoding: {encoding.upper()})."
        )
        if skipped:
            msg += tr(f" {len(skipped)} vectorisation failure(s).")
        self.show_info(msg)

    def _on_compute_error(self, message: str):
        self.compute_btn.setEnabled(True)
        self.compute_btn.setText(tr("Delineate sub-basins"))
        self.set_loading_state(False)
        self.show_error(message)

    # ------------------------------------------------------------------
    # Result tree
    # ------------------------------------------------------------------

    def _refresh_tree(self, subbasins: list[dict]):
        self.result_tree.clear()
        for sb in subbasins:
            item = QTreeWidgetItem(self.result_tree)
            item.setText(0, str(sb["rank"]))
            item.setText(1, f"{sb['area_km2']:.4f}")
            item.setText(2, f"{sb['n_pixels']:,}")
            ox, oy = sb["outlet_xy"]
            item.setText(3, f"{ox:.2f}")
            item.setText(4, f"{oy:.2f}")
            item.setData(0, Qt.UserRole, sb["rank"])

            # Color swatch matching the map symbology
            color = QColor(_rank_color(sb["rank"]))
            color.setAlpha(180)
            item.setBackground(0, color)

        for col in range(5):
            self.result_tree.resizeColumnToContents(col)

    def _on_result_selected(self):
        """Zooms the canvas to the selected sub-basin extent."""
        selected = self.result_tree.selectedItems()
        if not selected or self._map_layer is None:
            return

        rank = selected[0].data(0, Qt.UserRole)
        if rank is None:
            return

        # Find the feature with matching rank attribute
        for feat in self._map_layer.getFeatures():
            if feat["rank"] == rank:
                extent = feat.geometry().boundingBox()
                extent.scale(1.15)
                self.iface.mapCanvas().setExtent(extent)
                self.iface.mapCanvas().refresh()
                break

    # ------------------------------------------------------------------
    # Map layer management
    # ------------------------------------------------------------------

    def _add_layer_to_map(self):
        """
        Creates (or replaces) an in-memory QgsVectorLayer with sub-basin
        polygons and adds it to the QGIS project.

        Each feature carries:
            rank      (int)   — sub-basin rank (1 = largest)
            area_km2  (float) — area in km²
            n_pixels  (int)   — pixel count
            outlet_x  (float) — outlet X coordinate
            outlet_y  (float) — outlet Y coordinate
        """
        if not self._results:
            self.show_error(tr("No results to display — run Compute first."))
            return

        # Determine CRS from the fdir layer
        fdir_layer = self.fdir_combo.currentLayer()
        crs_wkt    = fdir_layer.crs().toWkt() if fdir_layer else "EPSG:4326"

        layer_name = self.layer_name_edit.text().strip() or "Watershed_subbasins"

        # Remove existing layer with the same name from the project
        existing = QgsProject.instance().mapLayersByName(layer_name)
        for lyr in existing:
            QgsProject.instance().removeMapLayer(lyr.id())

        # Build in-memory polygon layer
        vl = QgsVectorLayer(
            f"Polygon?crs={crs_wkt}",
            layer_name,
            "memory",
        )
        pr = vl.dataProvider()

        # Define fields
        fields = QgsFields()
        fields.append(QgsField("rank",     QVariant.Int))
        fields.append(QgsField("area_km2", QVariant.Double))
        fields.append(QgsField("n_pixels", QVariant.Int))
        fields.append(QgsField("outlet_x", QVariant.Double))
        fields.append(QgsField("outlet_y", QVariant.Double))
        pr.addAttributes(fields)
        vl.updateFields()

        # Add one feature per sub-basin
        features = []
        for sb in self._results:
            geom = sb.get("geometry")
            if geom is None or geom.isEmpty():
                continue

            feat = QgsFeature()
            feat.setGeometry(geom)
            ox, oy = sb["outlet_xy"]
            feat.setAttributes([
                sb["rank"],
                sb["area_km2"],
                sb["n_pixels"],
                ox,
                oy,
            ])
            features.append(feat)

        pr.addFeatures(features)
        vl.updateExtents()

        # Apply categorized symbology by rank
        self._apply_symbology(vl)

        # Register in QGIS project
        QgsProject.instance().addMapLayer(vl)
        self._map_layer = vl

    @staticmethod
    def _apply_symbology(layer: QgsVectorLayer) -> None:
        """
        Applies a categorized fill renderer to the sub-basin layer.
        Each rank gets a distinct semi-transparent color from the palette.
        """
        categories = []
        for feat in layer.getFeatures():
            rank = feat["rank"]
            color_hex = _rank_color(rank)
            color = QColor(color_hex)
            color.setAlpha(160)   # semi-transparent fill

            symbol = QgsFillSymbol.createSimple({
                "color":         color.name(QColor.HexArgb),
                "outline_color": "#333333",
                "outline_width": "0.4",
            })
            cat = QgsRendererCategory(rank, symbol, f"Sub-basin {rank}")
            categories.append(cat)

        renderer = QgsCategorizedSymbolRenderer("rank", categories)
        layer.setRenderer(renderer)
        layer.triggerRepaint()

    # ------------------------------------------------------------------
    # Export
    # ------------------------------------------------------------------

    def _on_export(self, fmt: str):
        if not self._results:
            self.show_error(tr("No data — run Compute first."))
            return

        fmt_lower = fmt.lower()

        if fmt_lower == "csv":
            path, _ = QFileDialog.getSaveFileName(
                self, tr("Export CSV"),
                "watershed_subbasins.csv",
                "CSV Files (*.csv)",
            )
            if not path:
                return
            rows = [
                {
                    "rank":      sb["rank"],
                    "area_km2":  sb["area_km2"],
                    "area_m2":   sb["area_m2"],
                    "n_pixels":  sb["n_pixels"],
                    "outlet_x":  sb["outlet_xy"][0],
                    "outlet_y":  sb["outlet_xy"][1],
                }
                for sb in self._results
            ]
            headers = ["rank", "area_km2", "area_m2",
                       "n_pixels", "outlet_x", "outlet_y"]
            self._exporter.save_csv(rows, headers, path)
            self.show_info(tr(f"CSV exported: {os.path.basename(path)}"))
            return

        # Shapefile / GeoPackage — write via QGIS native writer
        if self._map_layer is None:
            self.show_error(tr(
                "Add the layer to the map first before exporting."
            ))
            return

        if fmt_lower == "shapefile":
            path, _ = QFileDialog.getSaveFileName(
                self, tr("Export Shapefile"),
                "watershed_subbasins.shp",
                "Shapefiles (*.shp)",
            )
            driver = "ESRI Shapefile"
        else:   # GeoPackage
            path, _ = QFileDialog.getSaveFileName(
                self, tr("Export GeoPackage"),
                "watershed_subbasins.gpkg",
                "GeoPackage (*.gpkg)",
            )
            driver = "GPKG"

        if not path:
            return

        from qgis.core import QgsVectorFileWriter  # type: ignore
        error = QgsVectorFileWriter.writeAsVectorFormat(
            self._map_layer,
            path,
            "utf-8",
            self._map_layer.crs(),
            driver,
        )

        if error[0] == QgsVectorFileWriter.NoError:
            self.show_info(
                tr(f"{fmt} exported: {os.path.basename(path)}")
            )
        else:
            self.show_error(tr(f"Export failed: {error[1]}"))

    # ------------------------------------------------------------------
    # BasePanel abstract methods
    # ------------------------------------------------------------------

    def _on_result(self, data: dict) -> None:
        pass   # panel drives everything directly via _on_compute_finished

    def cleanup(self) -> None:
        """Called on plugin unload — nothing to release for this panel."""
        pass