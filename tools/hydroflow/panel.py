"""
tools/hydroflow/panel.py — HydroFlow UI Panel

Provides the interface to run depression filling, extract stream networks, 
and load topologically styled vector stream layers directly into QGIS [1.13.2].

Authors: RockMorph contributors / Tony winter
"""

from qgis.PyQt.QtWidgets import (  # type: ignore
    QWidget, QVBoxLayout, QHBoxLayout, QFormLayout,
    QLabel, QSpinBox, QDoubleSpinBox, QCheckBox, QPushButton
)
from qgis.PyQt.QtCore import Qt, QCoreApplication  # type: ignore
from qgis.PyQt.QtGui import QColor  # type: ignore

from qgis.gui import QgsMapLayerComboBox, QgsCollapsibleGroupBox  # type: ignore
from qgis.gui import QgsMapLayerComboBox   # type: ignore
from qgis.core import (  # type: ignore
    QgsMapLayerProxyModel, QgsProject, QgsWkbTypes,
    QgsCategorizedSymbolRenderer, QgsRendererCategory, QgsSymbol,
    QgsVectorLayer, QgsGraduatedSymbolRenderer, QgsRendererRange, QgsRasterLayer,
    QgsSingleBandPseudoColorRenderer, QgsColorRampShader, QgsRasterShader,
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
        input_group = QgsCollapsibleGroupBox(tr("Input Data"))
        input_layout = QFormLayout(input_group)

        self.dem_combo = QgsMapLayerComboBox()
        self.dem_combo.setFilters(QgsMapLayerProxyModel.RasterLayer)
        input_layout.addRow(tr("Input DEM:"), self.dem_combo)
        root.addWidget(input_group)

        # ── GroupBox: Settings ──
        param_group = QgsCollapsibleGroupBox(tr("Extraction Settings"))
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

        # Stream burning of a mapped network (optional)
        self.burn_combo = QgsMapLayerComboBox()
        self.burn_combo.setFilters(QgsMapLayerProxyModel.LineLayer)
        self.burn_combo.setAllowEmptyLayer(True)
        self.burn_combo.setLayer(None)
        self.burn_combo.setToolTip(tr(
            "<b>Stream burning (optional):</b><br>"
            "Mapped rivers (e.g. HydroRIVERS, national hydrography) are lowered into the DEM "
            "before filling, so that the extracted network follows them instead of DEM "
            "artefacts (bridges, dams, flat floodplains)."))
        param_layout.addRow(tr("Burn rivers:"), self.burn_combo)
        self.spin_burn_depth = QDoubleSpinBox()
        self.spin_burn_depth.setRange(0.5, 200.0)
        self.spin_burn_depth.setValue(10.0)
        self.spin_burn_depth.setSuffix(" m")
        self.spin_burn_depth.setToolTip(tr("Depth by which the mapped river cells are lowered."))
        param_layout.addRow(tr("Burn depth:"), self.spin_burn_depth)
        root.addWidget(param_group)

        # ── GroupBox: Outputs Selection ──
        outputs_group = QgsCollapsibleGroupBox(tr("Desired Outputs"))
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

        # Reach k_sn on the ordered network
        ksn_row = QWidget()
        ksn_layout = QHBoxLayout(ksn_row)
        ksn_layout.setContentsMargins(0, 4, 0, 0)
        self.chk_network_ksn = QCheckBox(tr("Add reach k_sn and colour by it, θ ="))
        self.chk_network_ksn.setToolTip(tr(
            "<b>Steepness of the whole network:</b><br>"
            "k_sn = S · A^θ for every segment (S: mean slope, A: drainage area near its "
            "downstream end), stored in the 'ksn' and 'area_km2' fields; the network is "
            "coloured from the 5th to the 95th percentile. Use the Fluvial Toolbox for the "
            "χ analysis of a single channel."))
        self.spin_ksn_theta = QDoubleSpinBox()
        self.spin_ksn_theta.setRange(0.1, 0.9)
        self.spin_ksn_theta.setSingleStep(0.05)
        self.spin_ksn_theta.setValue(0.45)
        ksn_layout.addWidget(self.chk_network_ksn)
        ksn_layout.addWidget(self.spin_ksn_theta)
        ksn_layout.addStretch()
        self.chk_out_streams.addSettingsWidget(ksn_row)

        self.chk_out_hand = OutputSelectorWidget(
            tr("Height Above Nearest Drainage (HAND)"), "hand.tif", "GeoTIFF (*.tif)", is_checked=False)
        self.chk_out_hand.setToolTip(tr(
            "<b>HAND (Rennó et al., 2008):</b><br>"
            "Height of every cell above the stream cell its flow path reaches first "
            "(streams: cells above the initiation threshold, plus burned rivers).<br>"
            "Low values map floodplains, terraces close to the channel and flood-prone ground."))
        self.chk_out_mfd = OutputSelectorWidget(
            tr("Accumulation MFD (Freeman, multiple directions)"), "accumulation_mfd.tif", "GeoTIFF (*.tif)",
            is_checked=False)
        self.chk_out_mfd.setToolTip(tr(
            "<b>Multiple flow direction accumulation (Freeman, 1991), in m²:</b><br>"
            "Water shared between all lower neighbours in proportion to slope^1.1: realistic "
            "diffuse flow on hillslopes, for wetness indices."))
        self.chk_out_dinf = OutputSelectorWidget(
            tr("Accumulation D∞ (Tarboton)"), "accumulation_dinf.tif", "GeoTIFF (*.tif)", is_checked=False)
        self.chk_out_dinf.setToolTip(tr(
            "<b>D-infinity accumulation (Tarboton, 1997), in m²:</b><br>"
            "Continuous steepest direction on triangular facets, water split between the two "
            "neighbours that frame it: less dispersion than MFD, no D8 striping."))

        outputs_layout.addWidget(self.chk_out_streams)
        outputs_layout.addWidget(self.chk_out_hand)
        outputs_layout.addWidget(self.chk_out_mfd)
        outputs_layout.addWidget(self.chk_out_dinf)
        outputs_layout.addWidget(self.chk_out_fdr)
        outputs_layout.addWidget(self.chk_out_fac)
        outputs_layout.addWidget(self.chk_out_basins)
        root.addWidget(outputs_group)

        # ── GroupBox: Statistics ──
        self.stats_group = QgsCollapsibleGroupBox(tr("Extraction Statistics"))
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
        self.lbl_mean_ksn = QLabel("—")
        stats_layout.addRow(tr("Mean reach k_sn:"), self.lbl_mean_ksn)
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
                self.chk_out_fac.isChecked() or self.chk_out_basins.isChecked() or
                self.chk_out_hand.isChecked() or self.chk_out_mfd.isChecked() or
                self.chk_out_dinf.isChecked()):
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
            "basins_path": self.chk_out_basins.filePath(),

            "burn_layer": self.burn_combo.currentLayer(),
            "burn_depth_m": self.spin_burn_depth.value(),
            "out_hand": self.chk_out_hand.isChecked(),
            "out_mfd": self.chk_out_mfd.isChecked(),
            "out_dinf": self.chk_out_dinf.isChecked(),
            "hand_path": self.chk_out_hand.filePath(),
            "mfd_path": self.chk_out_mfd.filePath(),
            "dinf_path": self.chk_out_dinf.filePath(),
            "network_ksn": self.chk_network_ksn.isChecked(),
            "ksn_theta": self.spin_ksn_theta.value(),
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

        # NumPy rasters: HAND (blue = low ground) and dispersive accumulations
        for key, style in (("hand_layer", "hand"), ("mfd_layer", "acc"), ("dinf_layer", "acc")):
            layer = result.get(key)
            if layer and layer.isValid():
                self._style_raster(layer, style)
                project.addMapLayer(layer)

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

            if stream_layer.fields().indexOf("ksn") >= 0:
                # NULL attributes come back as a null QVariant, not None:
                # keep real numbers only.
                ksn_values = [float(feat["ksn"]) for feat in stream_layer.getFeatures()
                              if isinstance(feat["ksn"], (int, float)) and feat["ksn"] == feat["ksn"]]
                self.lbl_mean_ksn.setText(f"{sum(ksn_values) / len(ksn_values):.1f}" if ksn_values else "—")
                self._apply_ksn_symbology(stream_layer, ksn_values)
            else:
                self.lbl_mean_ksn.setText("—")
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

    def _apply_ksn_symbology(self, layer: QgsVectorLayer, values: list) -> None:
        """
        Graduated renderer on the reach k_sn, five classes between the 5th
        and 95th percentiles (extreme reaches keep the end colours), line
        width growing with the Strahler order is not used so the colour reads
        alone. Blue = gentle, red = steep.
        """
        import numpy as np  # type: ignore
        if not values:
            self._apply_strahler_symbology(layer, 1)
            return
        low, high = np.percentile(values, [5, 95])
        if high <= low:
            high = low + 1.0
        edges = np.linspace(low, high, 6)
        edges[0], edges[-1] = min(values), max(values)
        colors = ["#2c7bb6", "#abd9e9", "#ffffbf", "#fdae61", "#d7191c"]
        ranges = []
        for i in range(5):
            symbol = QgsSymbol.defaultSymbol(QgsWkbTypes.LineGeometry)
            symbol.setColor(QColor(colors[i]))
            symbol.symbolLayer(0).setWidth(0.6 + 0.25 * i)
            label = f"{edges[i]:.0f} – {edges[i + 1]:.0f}"
            ranges.append(QgsRendererRange(float(edges[i]), float(edges[i + 1]), symbol, label))
        layer.setRenderer(QgsGraduatedSymbolRenderer("ksn", ranges))
        layer.triggerRepaint()

    @staticmethod
    def _style_raster(layer: QgsRasterLayer, kind: str) -> None:
        """Pseudo-colour styles: HAND 0–60 m (blue low ground), accumulation log-like ramp."""
        provider = layer.dataProvider()
        stats = provider.bandStatistics(1)
        if kind == "hand":
            stops = [(0, "#08306b"), (2, "#2171b5"), (5, "#6baed6"), (15, "#c6dbef"),
                     (30, "#f7fbff"), (60, "#fff5eb")]
            items = [QgsColorRampShader.ColorRampItem(v, QColor(c), f"{v} m") for v, c in stops]
        else:
            top = max(stats.maximumValue, 1.0)
            cell = max(stats.minimumValue, 1.0)      # one cell's area, in m²
            stops = [cell, cell * 10, cell * 100, cell * 1000, top]
            colors = ["#f7fbff", "#c6dbef", "#6baed6", "#2171b5", "#08306b"]
            items = [QgsColorRampShader.ColorRampItem(v, QColor(c), f"{v:.3g}")
                     for v, c in zip(sorted(set(stops)), colors)]
        ramp = QgsColorRampShader()
        ramp.setColorRampType(QgsColorRampShader.Interpolated)
        ramp.setColorRampItemList(items)
        shader = QgsRasterShader()
        shader.setRasterShaderFunction(ramp)
        layer.setRenderer(QgsSingleBandPseudoColorRenderer(provider, 1, shader))
        layer.triggerRepaint()

    # ── BasePanel required overrides ─────────────────────────────────────

    def _on_result(self, data: dict) -> None:
        pass