"""
tools/hydroflow/panel.py — HydroFlow UI Panel

Provides the interface to run depression filling, extract stream networks, 
and load topologically styled vector stream layers directly into QGIS [1.13.2].

Authors: RockMorph contributors / Tony winter
"""

from qgis.PyQt.QtWidgets import (  # type: ignore
    QWidget, QVBoxLayout, QHBoxLayout, QFormLayout,
    QLabel, QSpinBox, QCheckBox, QPushButton, QGroupBox
)
from qgis.PyQt.QtCore import Qt, QCoreApplication  # type: ignore
from qgis.PyQt.QtGui import QColor  # type: ignore

from qgis.gui import QgsMapLayerComboBox  # type: ignore
from qgis.core import (  # type: ignore
    QgsMapLayerProxyModel, QgsProject, QgsWkbTypes,
    QgsCategorizedSymbolRenderer, QgsRendererCategory, QgsSymbol,
    QgsVectorLayer
)

from ...base.base_panel import BasePanel, ComputeWorker
from ...widgets.output_selector import OutputSelectorWidget
from .engine import HydroFlowEngine


def tr(message: str) -> str:
    return QCoreApplication.translate("RockMorph", message)


class HydroFlowPanel(BasePanel):
    """
    UI Panel to configure, execute, and load topologically styled stream networks.
    """

    # ── BasePanel required hook ──────────────────────────────────────────

    def _html_file(self) -> str:
        # Since this tool is fully GIS-native, we do not require a WebEngine HTML
        # page. We return a blank HTML template or fallback to satisfy the abstract class [1.13.2].
        return "empty.html"

    # ── Top-level UI assembly ────────────────────────────────────────────

    def _build_ui(self) -> None:
        self.engine = HydroFlowEngine()

        root = QVBoxLayout(self._inner)
        root.setContentsMargins(8, 8, 8, 8)
        root.setSpacing(10)

        # ── GroupBox: Input Data ──
        input_group = QGroupBox(tr("Input Data"))
        input_layout = QFormLayout(input_group)

        self.dem_combo = QgsMapLayerComboBox()
        self.dem_combo.setFilters(QgsMapLayerProxyModel.RasterLayer)
        input_layout.addRow(tr("Input DEM:"), self.dem_combo)
        root.addWidget(input_group)

        # ── GroupBox: Settings ──
        param_group = QGroupBox(tr("Extraction Settings"))
        param_layout = QFormLayout(param_group)

        self.chk_fill_depressions = QCheckBox(tr("Enable Sink Filling (Wang & Liu)"))
        self.chk_fill_depressions.setChecked(True)
        param_layout.addRow(tr("Conditioning:"), self.chk_fill_depressions)

        self.spin_threshold = QSpinBox()
        self.spin_threshold.setRange(100, 1000000)
        self.spin_threshold.setValue(1000)
        self.spin_threshold.setSingleStep(500)
        self.spin_threshold.setSuffix(tr(" cells"))
        param_layout.addRow(tr("Initiation threshold:"), self.spin_threshold)
        root.addWidget(param_group)

        # ── GroupBox: Outputs Selection ──
        outputs_group = QGroupBox(tr("Desired Outputs"))
        outputs_layout = QVBoxLayout(outputs_group)
        outputs_layout.setSpacing(6)

        self.chk_out_streams = OutputSelectorWidget(
            tr("Ordered Vector Stream Network (Strahler/Horton/Shreve)"),
            "ordered_streams.gpkg",
            "GeoPackage (*.gpkg);;Shapefile (*.shp)",
            is_checked=True
        )
        self.chk_out_fdr = OutputSelectorWidget(
            tr("Flow Direction Raster (FDR)"),
            "flow_direction.tif",
            "GeoTIFF (*.tif)",
            is_checked=False
        )
        self.chk_out_fac = OutputSelectorWidget(
            tr("Flow Accumulation Raster (FAC)"),
            "flow_accumulation.tif",
            "GeoTIFF (*.tif)",
            is_checked=False
        )
        self.chk_out_basins = OutputSelectorWidget(
            tr("Delineated Basins Vector (Polygons)"),
            "delineated_basins.gpkg",
            "GeoPackage (*.gpkg);;Shapefile (*.shp)",
            is_checked=False
        )

        outputs_layout.addWidget(self.chk_out_streams)
        outputs_layout.addWidget(self.chk_out_fdr)
        outputs_layout.addWidget(self.chk_out_fac)
        outputs_layout.addWidget(self.chk_out_basins)
        root.addWidget(outputs_group)

        # ── GroupBox: Statistics ──
        self.stats_group = QGroupBox(tr("Extraction Statistics"))
        self.stats_group.setVisible(False)
        stats_layout = QFormLayout(self.stats_group)

        self.lbl_max_strahler = QLabel("—")
        self.lbl_max_shreve = QLabel("—")
        self.lbl_max_horton = QLabel("—")
        self.lbl_total_segments = QLabel("—")

        stats_layout.addRow(tr("Max Strahler Order:"), self.lbl_max_strahler)
        stats_layout.addRow(tr("Max Shreve Magnitude:"), self.lbl_max_shreve)
        stats_layout.addRow(tr("Max Horton Order:"), self.lbl_max_horton)
        stats_layout.addRow(tr("Total Stream Segments:"), self.lbl_total_segments)
        root.addWidget(self.stats_group)

        # ── Compute Button ──
        self.btn_compute = QPushButton(tr("⚙  Generate Desired Outputs"))
        self.btn_compute.setFixedHeight(36)
        self.btn_compute.setStyleSheet("""
            QPushButton { background-color: #2e7d32; color: white; font-weight: bold; border-radius: 4px; }
            QPushButton:hover { background-color: #1b5e20; }
            QPushButton:disabled { background-color: #81c784; }
        """)
        self.btn_compute.clicked.connect(self._on_compute)
        root.addWidget(self.btn_compute)

        root.addWidget(self._progress_container)
        root.addStretch()

    def _on_compute(self) -> None:
        dem_layer = self.dem_combo.currentLayer()
        if not dem_layer or not dem_layer.isValid():
            self.show_error(tr("Please select a valid DEM layer."))
            return

        # Vérifier qu'au moins une sortie est sélectionnée
        if not (self.chk_out_streams.isChecked() or self.chk_out_fdr.isChecked() or 
                self.chk_out_fac.isChecked() or self.chk_out_basins.isChecked()):
            self.show_error(tr("Please select at least one output layer from the checklist."))
            return

        params = {
            "dem_layer": dem_layer,
            "threshold": self.spin_threshold.value(),
            "fill_depressions": self.chk_fill_depressions.isChecked(),
            "out_streams": self.chk_out_streams.isChecked(),
            "out_fdr": self.chk_out_fdr.isChecked(),
            "out_fac": self.chk_out_fac.isChecked(),
            "out_basins": self.chk_out_basins.isChecked(),
            
            "streams_path": self.chk_out_streams.filePath(),
            "fdr_path": self.chk_out_fdr.filePath(),
            "fac_path": self.chk_out_fac.filePath(),
            "basins_path": self.chk_out_basins.filePath()
        }

        self.btn_compute.setEnabled(False)
        self.set_loading_state(True, tr("Extracting and solving hydrology network..."), total=100)

        self._worker = ComputeWorker(self.engine, params)
        self._worker.progress.connect(self.update_progress)
        self._worker.finished.connect(self._on_compute_finished)
        self._worker.error.connect(self._on_compute_error)
        self._worker.start()

    def _on_compute_finished(self, result: dict) -> None:
        self.btn_compute.setEnabled(True)
        self.set_loading_state(False)

        project = QgsProject.instance()

        # 1. Charger FDR si demandé
        fdr_layer = result.get("fdr_layer")
        if fdr_layer and fdr_layer.isValid():
            project.addMapLayer(fdr_layer)

        # 2. Charger FAC si demandé
        fac_layer = result.get("fac_layer")
        if fac_layer and fac_layer.isValid():
            project.addMapLayer(fac_layer)

        # 3. Charger les Bassins versants si demandés
        basins_layer = result.get("basins_layer")
        if basins_layer and basins_layer.isValid():
            project.addMapLayer(basins_layer)

        # 4. Charger et styliser le réseau si demandé
        stream_layer = result.get("stream_layer")
        if stream_layer and stream_layer.isValid():
            strahlers = [feat["strahler"] for feat in stream_layer.getFeatures()]
            shreves = [feat["shreve"] for feat in stream_layer.getFeatures()]
            hortons = [feat["horton"] for feat in stream_layer.getFeatures()]

            max_s = max(strahlers) if strahlers else 0
            max_sh = max(shreves) if shreves else 0
            max_h = max(hortons) if hortons else 0

            self.lbl_max_strahler.setText(str(max_s))
            self.lbl_max_shreve.setText(str(max_sh))
            self.lbl_max_horton.setText(str(max_h))
            self.lbl_total_segments.setText(str(stream_layer.featureCount()))
            self.stats_group.setVisible(True)

            self._apply_strahler_symbology(stream_layer, max_s)
            project.addMapLayer(stream_layer)

        self.show_info(tr("Extraction complete."))

    def _on_compute_error(self, error_msg: str) -> None:
        self.btn_compute.setEnabled(True)
        self.set_loading_state(False)
        self.show_error(tr(f"Extraction failed: {error_msg}"))

    # ------------------------------------------------------------------
    # QGIS Categorized Renderer (Strahler Color and Thickness ramp)
    # ------------------------------------------------------------------

    def _apply_strahler_symbology(self, layer: QgsVectorLayer, max_strahler: int) -> None:
        """
        Dynamically builds QGIS line symbols and maps thicker, darker lines to higher
        Strahler stream orders using a clean, publication-ready categorized renderer [1.13.2].
        """
        # Blue gradient styling
        colors = {
            1: "#90caf9",  # Order 1: Very thin, light blue
            2: "#64b5f6",  # Order 2: Thin, medium light blue
            3: "#42a5f5",  # Order 3: Medium, blue
            4: "#2196f3",  # Order 4: Medium-thick, royal blue
            5: "#1e88e5",  # Order 5: Thick, dark blue
            6: "#1565c0",  # Order 6: Very thick, dark blue
            7: "#0d47a1"   # Order 7+: Thickest, deep navy blue
        }

        # Width scale in millimeters
        widths = {
            1: 0.25,
            2: 0.45,
            3: 0.70,
            4: 1.00,
            5: 1.40,
            6: 1.90,
            7: 2.50
        }

        categories = []

        # Map up to 9 Strahler orders gracefully
        for val in range(1, max_strahler + 1):
            color_hex = colors.get(val, "#0a2558")
            width = widths.get(val, 3.20)

            # Build a default line symbol
            symbol = QgsSymbol.defaultSymbol(QgsWkbTypes.LineGeometry)
            if symbol:
                symbol.setColor(QColor(color_hex))
                symbol.symbolLayer(0).setWidth(width)
                
                # Create a specific category for each Strahler order
                category = QgsRendererCategory(
                    val,
                    symbol,
                    tr(f"Strahler Order {val}"),
                    True  # Enabled
                )
                categories.append(category)

        # Set categorized renderer
        renderer = QgsCategorizedSymbolRenderer("strahler", categories)
        layer.setRenderer(renderer)
        layer.triggerRepaint()

    # ── BasePanel required overrides ─────────────────────────────────────

    def _on_result(self, data: dict) -> None:
        pass