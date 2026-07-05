"""
tools/terrainderivatives/panel.py — Terrain Derivatives UI Panel

Provides a highly dynamic, collapsible options checklist to compute basic morphometry,
hillshading, surface texture, and python-native openness/SVF visualisations [2].

Authors: RockMorph contributors / Tony winter
"""

from qgis.PyQt.QtWidgets import (  # type: ignore
    QWidget, QVBoxLayout, QHBoxLayout, QFormLayout,
    QLabel, QSpinBox, QDoubleSpinBox, QCheckBox, QPushButton,
    QGroupBox, QComboBox, QRadioButton, QButtonGroup, QScrollArea
)
from qgis.PyQt.QtCore import Qt, QCoreApplication  # type: ignore
from qgis.gui import QgsMapLayerComboBox  # type: ignore
from qgis.core import QgsMapLayerProxyModel, QgsProject  # type: ignore

from ...base.base_panel import BasePanel, ComputeWorker
from ...widgets.output_selector import OutputSelectorWidget
from .engine import TerrainDerivativesEngine

def tr(message: str) -> str:
    return QCoreApplication.translate("RockMorph", message)


class TerrainDerivativesPanel(BasePanel):

    def _html_file(self) -> str:
        return "empty.html"

    def _load_html(self) -> None:
        pass

    def _build_ui(self) -> None:
        try:
            self.engine = TerrainDerivativesEngine()

            # Build directly inside the inherited self._inner widget 
            root = QVBoxLayout(self._inner)
            root.setContentsMargins(8, 8, 8, 8)
            root.setSpacing(10)

            # ── GroupBox: Input Elevation ──
            input_group = QGroupBox(tr("Input Terrain"))
            input_layout = QFormLayout(input_group)
            self.dem_combo = QgsMapLayerComboBox()
            self.dem_combo.setFilters(QgsMapLayerProxyModel.RasterLayer)
            input_layout.addRow(tr("Input DEM:"), self.dem_combo)
            root.addWidget(input_group)

            # ── GroupBox 1: Primary Morphometry ──
            morph_group = QGroupBox(tr("1. Primary Morphometry"))
            morph_layout = QVBoxLayout(morph_group)
            morph_layout.setSpacing(8)

            # A. Slope with Degree/Percent selector [2]
            self.out_slope = OutputSelectorWidget(
                tr("Slope Gradient (S)"), "slope.tif", "GeoTIFF (*.tif)", is_checked=True
            )
            self.slope_opts = QWidget()
            slope_opts_layout = QHBoxLayout(self.slope_opts)
            slope_opts_layout.setContentsMargins(0, 4, 0, 0)
            self.rad_slope_deg = QRadioButton(tr("Degrees (°)"))
            self.rad_slope_deg.setChecked(True)
            self.rad_slope_pct = QRadioButton(tr("Percent (%)"))
            slope_opts_layout.addWidget(QLabel(tr("Slope units:")))
            slope_opts_layout.addWidget(self.rad_slope_deg)
            slope_opts_layout.addWidget(self.rad_slope_pct)
            slope_opts_layout.addStretch()
            self.out_slope.addSettingsWidget(self.slope_opts)
            morph_layout.addWidget(self.out_slope)

            # B. Aspect
            self.out_aspect = OutputSelectorWidget(
                tr("Aspect Orientation (A)"), "aspect.tif", "GeoTIFF (*.tif)", is_checked=False
            )
            morph_layout.addWidget(self.out_aspect)
            root.addWidget(morph_group)

            # ── GroupBox 2: Shading & Visualization ──
            shading_group = QGroupBox(tr("2. Shading & Visualisation"))
            shading_layout = QVBoxLayout(shading_group)
            shading_layout.setSpacing(8)

            # C. Hillshade with azimuth, altitude, scale & multi-directional selectors 
            self.out_hillshade = OutputSelectorWidget(
                tr("Topographic Hillshade"), "hillshade.tif", "GeoTIFF (*.tif)", is_checked=False
            )

            self.hill_opts = QWidget()
            hill_opts_layout = QVBoxLayout(self.hill_opts)
            hill_opts_layout.setContentsMargins(0, 4, 0, 0)
            hill_opts_layout.setSpacing(6)

            # Standard Light Source angles
            self.spin_hill_azimuth = QSpinBox()
            self.spin_hill_azimuth.setRange(0, 360)
            self.spin_hill_azimuth.setValue(315)
            hill_opts_layout.addWidget(QLabel(tr("Azimuth (Light angle):")))
            hill_opts_layout.addWidget(self.spin_hill_azimuth)

            self.spin_hill_altitude = QSpinBox()
            self.spin_hill_altitude.setRange(0, 90)
            self.spin_hill_altitude.setValue(45)
            hill_opts_layout.addWidget(QLabel(tr("Altitude (Sun height):")))
            hill_opts_layout.addWidget(self.spin_hill_altitude)

            self.spin_hill_z = QDoubleSpinBox()
            self.spin_hill_z.setRange(0.1, 100.0)
            self.spin_hill_z.setValue(1.0)
            self.spin_hill_z.setDecimals(1)
            hill_opts_layout.addWidget(QLabel(tr("Z-Factor (Exaggeration scale):")))
            hill_opts_layout.addWidget(self.spin_hill_z)

            # Shading Variants (Mutually exclusive Radio Buttons) [1, 3]
            var_group = QGroupBox(tr("Shading Variant"))
            var_layout = QVBoxLayout(var_group)
            var_layout.setSpacing(4)

            self.rad_var_standard = QRadioButton(tr("Standard (Regular)"))
            self.rad_var_combined = QRadioButton(tr("Combined (Slope + Aspect)"))
            self.rad_var_multi = QRadioButton(tr("Multidirectional (4-Axis)"))
            self.rad_var_igor = QRadioButton(tr("Igor Sharygin (Non-linear)"))

            self.rad_var_standard.setChecked(True)

            self.variant_btn_group = QButtonGroup(self)
            self.variant_btn_group.addButton(self.rad_var_standard)
            self.variant_btn_group.addButton(self.rad_var_combined)
            self.variant_btn_group.addButton(self.rad_var_multi)
            self.variant_btn_group.addButton(self.rad_var_igor)

            var_layout.addWidget(self.rad_var_standard)
            var_layout.addWidget(self.rad_var_combined)
            var_layout.addWidget(self.rad_var_multi)
            var_layout.addWidget(self.rad_var_igor)
            hill_opts_layout.addWidget(var_group)

            # Boundary Treatment Options (Mutually exclusive checkboxes) [3]
            edge_group = QGroupBox(tr("Boundary Treatment"))
            edge_layout = QVBoxLayout(edge_group)
            edge_layout.setSpacing(4)

            self.chk_hill_compute_edges = QCheckBox(tr("Force edge interpolation (compute_edges)"))
            self.chk_hill_compute_edges.setChecked(True)
            self.chk_hill_no_edges = QCheckBox(tr("Ignore boundary interpolation (no_edges)"))

            edge_layout.addWidget(self.chk_hill_compute_edges)
            edge_layout.addWidget(self.chk_hill_no_edges)
            hill_opts_layout.addWidget(edge_group)

            # Attach settings widget — will now stack vertically below the file selector [2]
            self.out_hillshade.addSettingsWidget(self.hill_opts)
            shading_layout.addWidget(self.out_hillshade)
            root.addWidget(shading_group)

            # ── GroupBox 3: Texture & Roughness ──
            texture_group = QGroupBox(tr("3. Surface Texture & Position"))
            texture_layout = QVBoxLayout(texture_group)
            texture_layout.setSpacing(8)

            # D. TPI with neighborhood window size
            self.out_tpi = OutputSelectorWidget(
                tr("Topographic Position Index (TPI)"), "tpi.tif", "GeoTIFF (*.tif)", is_checked=False
            )
            self.tpi_opts = QWidget()
            tpi_opts_layout = QHBoxLayout(self.tpi_opts)
            tpi_opts_layout.setContentsMargins(0, 4, 0, 0)
            self.spin_tpi_radius = QSpinBox()
            self.spin_tpi_radius.setRange(1, 50)
            self.spin_tpi_radius.setValue(3)
            self.spin_tpi_radius.setSuffix(tr(" pixels"))
            tpi_opts_layout.addWidget(QLabel(tr("Smoothing radius:")))
            tpi_opts_layout.addWidget(self.spin_tpi_radius)
            tpi_opts_layout.addStretch()
            self.out_tpi.addSettingsWidget(self.tpi_opts)
            texture_layout.addWidget(self.out_tpi)

            # E. TRI
            self.out_tri = OutputSelectorWidget(
                tr("Terrain Ruggedness Index (TRI)"), "tri.tif", "GeoTIFF (*.tif)", is_checked=False
            )
            texture_layout.addWidget(self.out_tri)
            root.addWidget(texture_group)

            # ── GroupBox 4: Relief Visualization (Pure Python/RVT) ──
            rvt_group = QGroupBox(tr("4. Relief Visualisation (Pure Python/NumPy)"))
            rvt_layout = QVBoxLayout(rvt_group)
            rvt_layout.setSpacing(8)

            # F. Positive Openness with search radius and sectors
            self.out_openness_pos = OutputSelectorWidget(
                tr("Positive Openness (Crests / Ridges)"), "openness_pos.tif", "GeoTIFF (*.tif)", is_checked=False
            )
            self.open_pos_opts = QWidget()
            open_pos_opts_layout = QVBoxLayout(self.open_pos_opts)
            open_pos_opts_layout.setContentsMargins(0, 4, 0, 0)
            open_pos_opts_layout.setSpacing(4)
            self.spin_open_radius = QSpinBox()
            self.spin_open_radius.setRange(1, 100)
            self.spin_open_radius.setValue(10)
            open_pos_opts_layout.addWidget(QLabel(tr("Search radius:")))
            open_pos_opts_layout.addWidget(self.spin_open_radius)
            self.spin_open_sectors = QSpinBox()
            self.spin_open_sectors.setRange(4, 32)
            self.spin_open_sectors.setValue(8)
            open_pos_opts_layout.addWidget(QLabel(tr("Number of sectors:")))
            open_pos_opts_layout.addWidget(self.spin_open_sectors)
            self.out_openness_pos.addSettingsWidget(self.open_pos_opts)
            rvt_layout.addWidget(self.out_openness_pos)

            # G. Negative Openness
            self.out_openness_neg = OutputSelectorWidget(
                tr("Negative Openness (Incisions / Valleys)"), "openness_neg.tif", "GeoTIFF (*.tif)", is_checked=False
            )
            rvt_layout.addWidget(self.out_openness_neg)
            root.addWidget(rvt_group)

            # ── Global Checklist Controls ──
            global_ctrl_layout = QHBoxLayout()
            self.btn_select_all = QPushButton(tr("Select All"))
            self.btn_select_all.clicked.connect(self._on_select_all)
            self.btn_deselect_all = QPushButton(tr("Deselect All"))
            self.btn_deselect_all.clicked.connect(self._on_deselect_all)
            global_ctrl_layout.addWidget(self.btn_select_all)
            global_ctrl_layout.addWidget(self.btn_deselect_all)
            root.addLayout(global_ctrl_layout)

            # ── Compute Button ──
            self.btn_compute = QPushButton(tr("⚙  Compute All Checked Derivatives"))
            self.btn_compute.setFixedHeight(36)
            self.btn_compute.setStyleSheet("""
                QPushButton { background-color: #2d6a9f; color: white; font-weight: bold; border-radius: 4px; }
                QPushButton:hover { background-color: #3a7fc1; }
                QPushButton:disabled { background-color: #aaa; }
            """)
            self.btn_compute.clicked.connect(self._on_compute)
            root.addWidget(self.btn_compute)

            root.addWidget(self._progress_container)


            # Wire dynamic GDAL Hillshade exclusions based on the active variant 
            self.rad_var_standard.toggled.connect(self._on_variant_changed)
            self.rad_var_combined.toggled.connect(self._on_variant_changed)
            self.rad_var_multi.toggled.connect(self._on_variant_changed)
            self.rad_var_igor.toggled.connect(self._on_variant_changed)

            # Wire mutual exclusions for border treatment [3]
            self.chk_hill_compute_edges.toggled.connect(self._on_compute_edges_toggled)
            self.chk_hill_no_edges.toggled.connect(self._on_no_edges_toggled)

        except :
            import traceback; traceback.print_exc()

    # ── Helpers ──

    def _get_all_selectors(self) -> list[OutputSelectorWidget]:
        return [self.out_slope, self.out_aspect, self.out_hillshade,
                self.out_tpi, self.out_tri, self.out_openness_pos, self.out_openness_neg]

    def _on_select_all(self):
        for selector in self._get_all_selectors():
            selector.setChecked(True)

    def _on_deselect_all(self):
        for selector in self._get_all_selectors():
            selector.setChecked(False)

    def _on_compute(self) -> None:
        dem_layer = self.dem_combo.currentLayer()
        if not dem_layer or not dem_layer.isValid():
            self.show_error(tr("Please load and select a valid input DEM layer."))
            return

        selectors = self._get_all_selectors()
        if not any(s.isChecked() for s in selectors):
            self.show_error(tr("Please select at least one terrain derivative to compute."))
            return

        # Package parameters to pass to processing thread
        # Determine the selected hillshade variant as a clean parameter string [1, 3]
        hill_variant = "standard"
        if self.rad_var_combined.isChecked():
            hill_variant = "combined"
        elif self.rad_var_multi.isChecked():
            hill_variant = "multidirectional"
        elif self.rad_var_igor.isChecked():
            hill_variant = "igor"

        # Determine the boundary treatment mode 
        hill_edges = "default"
        if self.chk_hill_compute_edges.isChecked():
            hill_edges = "compute_edges"
        elif self.chk_hill_no_edges.isChecked():
            hill_edges = "no_edges"

        # Package parameters to pass to the asynchronous processing thread [2]
        params = {
            "dem_layer": dem_layer,
            
            # Active outputs flags
            "out_slope": self.out_slope.isChecked(),
            "out_aspect": self.out_aspect.isChecked(),
            "out_hillshade": self.out_hillshade.isChecked(),
            "out_tpi": self.out_tpi.isChecked(),
            "out_tri": self.out_tri.isChecked(),
            "out_openness_pos": self.out_openness_pos.isChecked(),
            "out_openness_neg": self.out_openness_neg.isChecked(),

            # Target output file paths (Memory or physical disk paths) [2]
            "path_slope": self.out_slope.filePath(),
            "path_aspect": self.out_aspect.filePath(),
            "path_hillshade": self.out_hillshade.filePath(),
            "path_tpi": self.out_tpi.filePath(),
            "path_tri": self.out_tri.filePath(),
            "path_openness_pos": self.out_openness_pos.filePath(),
            "path_openness_neg": self.out_openness_neg.filePath(),

            # Configurations
            "slope_percent": self.rad_slope_pct.isChecked(),
            "hill_azimuth": self.spin_hill_azimuth.value(),
            "hill_altitude": self.spin_hill_altitude.value(),
            "hill_variant": hill_variant,          # Condenses mutually exclusive variants 
            "hill_edges": hill_edges,              # Condenses mutually exclusive edges 
            "hill_z_factor": self.spin_hill_z.value(),
            "tpi_radius": self.spin_tpi_radius.value(),
            "openness_radius": self.spin_open_radius.value(),
            "openness_sectors": self.spin_open_sectors.value()
        }

        self.btn_compute.setEnabled(False)
        self.set_loading_state(True, tr("Computing terrain derivatives asynchronously..."), total=100)

        # Dispatch background calculation thread
        self._worker = ComputeWorker(self.engine, params)
        self._worker.progress.connect(self.update_progress)
        self._worker.finished.connect(self._on_compute_finished)
        self._worker.error.connect(self._on_compute_error)
        self._worker.start()

    def _on_compute_finished(self, result: dict) -> None:
        self.btn_compute.setEnabled(True)
        self.set_loading_state(False)

        # Load all successfully generated layers onto the map canvas [2]
        project = QgsProject.instance()
        for key, layer in result.items():
            if layer and layer.isValid():
                project.addMapLayer(layer)

        self.show_info(tr("Terrain analysis complete. Layers added successfully [2]."))

    def _on_compute_error(self, error_msg: str) -> None:
        self.btn_compute.setEnabled(True)
        self.set_loading_state(False)
        self.show_error(tr(f"Terrain analysis failed: {error_msg}"))
    


    # ── Hillshade Exclusivity Slots ──
    def _on_variant_changed(self):
        """Enforces mutual exclusivity rules for the illumination angles based on the active variant [1, 3]."""
        if self.rad_var_multi.isChecked():
            # Multidirectional ignores Azimuth entirely [1, 3]
            self.spin_hill_azimuth.setEnabled(False)
            self.spin_hill_altitude.setEnabled(True)
        elif self.rad_var_igor.isChecked():
            # Igor Sharygin ignores Altitude entirely [1, 3]
            self.spin_hill_azimuth.setEnabled(True)
            self.spin_hill_altitude.setEnabled(False)
        else:
            # Standard & Combined allow both parameters [1, 3]
            self.spin_hill_azimuth.setEnabled(True)
            self.spin_hill_altitude.setEnabled(True)

    def _on_compute_edges_toggled(self, checked: bool):
        """Enforces mutual exclusion between compute_edges and no_edges [3]."""
        if checked:
            self.chk_hill_no_edges.blockSignals(True)
            self.chk_hill_no_edges.setChecked(False)
            self.chk_hill_no_edges.blockSignals(False)

    def _on_no_edges_toggled(self, checked: bool):
        """Enforces mutual exclusion between compute_edges and no_edges [3]."""
        if checked:
            self.chk_hill_compute_edges.blockSignals(True)
            self.chk_hill_compute_edges.setChecked(False)
            self.chk_hill_compute_edges.blockSignals(False)


    # ── BasePanel required overrides ────────────────────────────────────

    def _on_result(self, data: dict) -> None:
        pass