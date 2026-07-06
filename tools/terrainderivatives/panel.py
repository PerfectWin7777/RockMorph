"""
tools/terrainderivatives/panel.py — Terrain Derivatives UI Panel

Provides a highly dynamic, collapsible options checklist to compute basic morphometry,
hillshading, surface texture, and python-native openness/SVF visualisations [2].

Authors: RockMorph contributors / Tony winter
"""

from qgis.PyQt.QtWidgets import (  # type: ignore
    QWidget, QVBoxLayout, QHBoxLayout, QFormLayout,
    QLabel, QSpinBox, QDoubleSpinBox, QCheckBox, QPushButton,
     QComboBox, QRadioButton, QButtonGroup, QScrollArea,
    QSlider
)
from qgis.PyQt.QtCore import Qt, QCoreApplication # type: ignore
from qgis.gui import QgsMapLayerComboBox, QgsCollapsibleGroupBox  # type: ignore
from qgis.core import QgsMapLayerProxyModel, QgsProject, QgsRasterLayer   # type: ignore

from .engine import TerrainDerivativesEngine
from ...base.base_panel import BasePanel, ComputeWorker
from ...widgets.output_selector import OutputSelectorWidget


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
            input_group = QgsCollapsibleGroupBox(tr("Input Terrain"))
            input_layout = QFormLayout(input_group)
            self.dem_combo = QgsMapLayerComboBox()
            self.dem_combo.setFilters(QgsMapLayerProxyModel.RasterLayer)
            input_layout.addRow(tr("Input DEM:"), self.dem_combo)
            root.addWidget(input_group)

            # ── GroupBox 1: Primary Morphometry ──
            morph_group = QgsCollapsibleGroupBox(tr("Primary Morphometry"))
            morph_layout = QVBoxLayout(morph_group)
            morph_layout.setSpacing(8)

            # A. Slope with Degree/Percent selector [2]
            self.out_slope = OutputSelectorWidget(
                tr("Slope Gradient (S)"), "slope.tif", "GeoTIFF (*.tif)", is_checked=False
            )
            self.out_slope.setToolTip(tr(
            "<b>Slope Gradient (S):</b><br>"
            "Calculates the local rate of elevation change (degrees or percent).<br>"
            "Essential for hillslope stability and landslide susceptibility mapping."
        ))
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
            self.out_aspect.setToolTip(tr(
            "<b>Aspect Orientation:</b><br>"
            "Measures the downslope direction of the terrain relative to North (0-360°).<br>"
            "Controls micro-climatic solar insolation and vegetation asymmetry."
            ))
            morph_layout.addWidget(self.out_aspect)
            root.addWidget(morph_group)

            # ── GroupBox 2: Shading & Visualization ──
            shading_group = QgsCollapsibleGroupBox(tr("Shading & Visualisation"))
            shading_layout = QVBoxLayout(shading_group)
            shading_layout.setSpacing(8)

            # C. Hillshade with azimuth, altitude, scale & multi-directional selectors 
            self.out_hillshade = OutputSelectorWidget(
                tr("Topographic Hillshade"), "hillshade.tif", "GeoTIFF (*.tif)", is_checked=False
            )
            self.out_hillshade.setToolTip(tr(
            "<b>Topographic Hillshade:</b><br>"
            "Simulates the illumination of a light source over the relief .<br>"
            "Offers advanced physical algorithms like Igor Sharygin, Combined, and Multidirectional."
            ))

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
            var_group = QgsCollapsibleGroupBox(tr("Shading Variant"))
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
            edge_group = QgsCollapsibleGroupBox(tr("Boundary Treatment"))
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
            texture_group = QgsCollapsibleGroupBox(tr("Surface Texture & Position"))
            texture_layout = QVBoxLayout(texture_group)
            texture_layout.setSpacing(8)

            # D. TPI with neighborhood window size
            self.out_tpi = OutputSelectorWidget(
                tr("Topographic Position Index (TPI)"), "tpi.tif", "GeoTIFF (*.tif)", is_checked=False
            )
            self.out_tpi.setToolTip(tr(
            "<b>Topographic Position Index (TPI):</b><br>"
            "Measures the elevation difference between a pixel and its local average: Z - Z_mean.<br>"
            "Used to classify discrete landforms like ridge tops, valleys, and flat plains."
            ))
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
            self.out_tri.setToolTip(tr(
            "<b>Terrain Ruggedness Index (TRI):</b><br>"
            "Computes the mean elevation difference between a pixel and its 8 neighbors (Riley et al., 1999).<br>"
            "Acts as a direct proxy for surface roughness and bedrock mechanical resistance."
            ))
            texture_layout.addWidget(self.out_tri)
            root.addWidget(texture_group)

            # ── GroupBox 4: Relief Visualization (Pure Python/NumPy) ──
            rvt_group = QgsCollapsibleGroupBox(tr("Relief Visualisation"))
            rvt_layout = QVBoxLayout(rvt_group)
            rvt_layout.setSpacing(10)

            # A. Positive Openness (Crests & Ridges) [1.2.1]
            self.out_openness_pos = OutputSelectorWidget(
                tr("Positive Openness (Crests / Ridges)"), "openness_pos.tif", "GeoTIFF (*.tif)", is_checked=False
            )
            self.out_openness_pos.setToolTip(tr(
                "<b>Positive Openness (Yokoyama et al., 2002):</b><br>"
                "Measures sky visibility above the terrain. Highlights convex landforms "
                "like crests, ridge lines, and fault scarps."
            ))
            self.pos_opts = QWidget()
            pos_layout = QFormLayout(self.pos_opts)
            pos_layout.setContentsMargins(0, 4, 0, 0)
            self.spin_pos_radius = QSpinBox()
            self.spin_pos_radius.setToolTip(tr(
                "Search radius in pixels. Larger values highlight broader, macro-topographic "
                "landforms. Smaller values emphasize local micro-relief details."
            ))
            self.spin_pos_radius.setRange(1, 100)
            self.spin_pos_radius.setValue(10)
            self.spin_pos_radius.setSuffix(tr(" pixels"))
            pos_layout.addRow(tr("Search radius:"), self.spin_pos_radius)
            self.spin_pos_sectors = QSpinBox()
            self.spin_pos_sectors.setToolTip(tr(
                "Number of radial directions to sample (typically 8 or 16). Higher values "
                "increase processing time but improve angular precision."
            ))
            self.spin_pos_sectors.setRange(4, 32)
            self.spin_pos_sectors.setValue(8)
            pos_layout.addRow(tr("Number of sectors:"), self.spin_pos_sectors)
            self.out_openness_pos.addSettingsWidget(self.pos_opts)
            rvt_layout.addWidget(self.out_openness_pos)

            # B. Negative Openness (Valleys & Incisions) [1.2.1]
            self.out_openness_neg = OutputSelectorWidget(
                tr("Negative Openness (Incisions / Valleys)"), "openness_neg.tif", "GeoTIFF (*.tif)", is_checked=False
            )
            self.out_openness_neg.setToolTip(tr(
                "<b>Negative Openness (Yokoyama et al., 2002):</b><br>"
                "Measures enclavement of the relief by inverting the DEM. Highlights concave "
                "features like valleys, river incisions, and structural joint networks."
            ))
            self.neg_opts = QWidget()
            neg_layout = QFormLayout(self.neg_opts)
            neg_layout.setContentsMargins(0, 4, 0, 0)
            self.spin_neg_radius = QSpinBox()
            self.spin_neg_radius.setRange(1, 100)
            self.spin_neg_radius.setValue(10)
            self.spin_neg_radius.setSuffix(tr(" pixels"))
            neg_layout.addRow(tr("Search radius:"), self.spin_neg_radius)
            self.spin_neg_sectors = QSpinBox()
            self.spin_neg_sectors.setRange(4, 32)
            self.spin_neg_sectors.setValue(8)
            neg_layout.addRow(tr("Number of sectors:"), self.spin_neg_sectors)
            self.out_openness_neg.addSettingsWidget(self.neg_opts)
            rvt_layout.addWidget(self.out_openness_neg)

            # C. Isotropic Sky View Factor (SVF) [1.2.1]
            self.out_svf = OutputSelectorWidget(
                tr("Sky View Factor (SVF)"), "svf.tif", "GeoTIFF (*.tif)", is_checked=False
            )
            self.out_svf.setToolTip(tr(
                "<b>Isotropic Sky View Factor (SVF) (Kokalj et al., 2011):</b><br>"
                "Calculates the visible hemisphere portion of the sky. Provides diffuse, "
                "multidirectional illumination ideal for structural lineaments and fault scarp mapping."
            ))
            self.svf_opts = QWidget()
            svf_opts_layout = QFormLayout(self.svf_opts)
            svf_opts_layout.setContentsMargins(0, 4, 0, 0)
            self.spin_svf_radius = QSpinBox()
            self.spin_svf_radius.setRange(1, 100)
            self.spin_svf_radius.setValue(10)
            self.spin_svf_radius.setSuffix(tr(" pixels"))
            svf_opts_layout.addRow(tr("Search radius:"), self.spin_svf_radius)
            self.spin_svf_sectors = QSpinBox()
            self.spin_svf_sectors.setRange(4, 32)
            self.spin_svf_sectors.setValue(8)
            svf_opts_layout.addRow(tr("Number of sectors:"), self.spin_svf_sectors)
            self.out_svf.addSettingsWidget(self.svf_opts)
            rvt_layout.addWidget(self.out_svf)

            # D. Sky Illumination (SOC model)
            self.out_sky_illumination = OutputSelectorWidget(
                tr("Sky Illumination (Diffuse Shading)"), "sky_illumination.tif", "GeoTIFF (*.tif)", is_checked=False
            )
            self.out_sky_illumination.setToolTip(tr(
                "<b>Sky Illumination (CIE SOC Model):</b><br>"
                "Simulates diffuse lighting under overcast sky conditions. Re-illuminates "
                "narrow, deep valleys and reduces high-contrast directional shadows."
            ))
            self.sky_opts = QWidget()
            sky_opts_layout = QFormLayout(self.sky_opts)
            sky_opts_layout.setContentsMargins(0, 4, 0, 0)
            self.spin_sky_radius = QSpinBox()
            self.spin_sky_radius.setRange(1, 100)
            self.spin_sky_radius.setValue(10)
            self.spin_sky_radius.setSuffix(tr(" pixels"))
            sky_opts_layout.addRow(tr("Search radius:"), self.spin_sky_radius)
            self.spin_sky_sectors = QSpinBox()
            self.spin_sky_sectors.setRange(4, 32)
            self.spin_sky_sectors.setValue(8)
            sky_opts_layout.addRow(tr("Number of sectors:"), self.spin_sky_sectors)
            self.out_sky_illumination.addSettingsWidget(self.sky_opts)
            rvt_layout.addWidget(self.out_sky_illumination)

            # E. Anisotropic Sky View Factor [1.1.1]
            self.out_anisotropic_svf = OutputSelectorWidget(
                tr("Anisotropic Sky-View Factor (Directional SVF)"), "anisotropic_svf.tif", "GeoTIFF (*.tif)", is_checked=False
            )
            self.out_anisotropic_svf.setToolTip(tr(
                "<b>Anisotropic Sky View Factor (Zaksek et al., 2011):</b><br>"
                "Applies a directional sky brightness weight perpendicular to the preferred azimuth. "
                "Maximizes shadow contrast for linear faults and joints running parallel to the trend."
            ))
            self.asvf_opts = QWidget()
            asvf_opts_layout = QFormLayout(self.asvf_opts)
            asvf_opts_layout.setContentsMargins(0, 4, 0, 0)
            asvf_opts_layout.setSpacing(4)
            self.spin_asvf_radius = QSpinBox()
            self.spin_asvf_radius.setRange(1, 100)
            self.spin_asvf_radius.setValue(10)
            self.spin_asvf_radius.setSuffix(tr(" pixels"))
            asvf_opts_layout.addRow(tr("Search radius:"), self.spin_asvf_radius)
            self.spin_asvf_sectors = QSpinBox()
            self.spin_asvf_sectors.setRange(4, 32)
            self.spin_asvf_sectors.setValue(8)
            asvf_opts_layout.addRow(tr("Number of sectors:"), self.spin_asvf_sectors)
            self.spin_asvf_azimuth = QSpinBox()
            self.spin_asvf_azimuth.setToolTip(tr(
                "Azimuth direction of the preferred structural trend. The weighting "
                "function is shifted perpendicularly to maximize contrast on parallel features."
            ))
            self.spin_asvf_azimuth.setRange(0, 360)
            self.spin_asvf_azimuth.setValue(315)
            asvf_opts_layout.addRow(tr("Preferred Azimuth (°):"), self.spin_asvf_azimuth)
            self.slider_asvf_anisotropy = QSlider(Qt.Horizontal)
            self.slider_asvf_anisotropy.setToolTip(tr(
                "Anisotropy level (0.0 to 1.0). Controls the intensity of directional weighting "
                "(0.0 is isotropic SVF, 1.0 is maximum directional bias)."
            ))
            self.slider_asvf_anisotropy.setRange(0, 100)
            self.slider_asvf_anisotropy.setValue(50)
            self.lbl_asvf_anisotropy = QLabel("0.50")
            self.slider_asvf_anisotropy.valueChanged.connect(lambda v: self.lbl_asvf_anisotropy.setText(f"{v/100.0:.2f}"))
            row_anisotropy = QHBoxLayout()
            row_anisotropy.addWidget(self.slider_asvf_anisotropy)
            row_anisotropy.addWidget(self.lbl_asvf_anisotropy)
            asvf_opts_layout.addRow(tr("Anisotropy Level:"), row_anisotropy)
            self.out_anisotropic_svf.addSettingsWidget(self.asvf_opts)
            rvt_layout.addWidget(self.out_anisotropic_svf)

            # F. Local Dominance [1.2.1]
            self.out_local_dominance = OutputSelectorWidget(
                tr("Local Dominance (Hesse 2016)"), "local_dominance.tif", "GeoTIFF (*.tif)", is_checked=False
            )
            self.out_local_dominance.setToolTip(tr(
                "<b>Local Dominance (Hesse, 2016):</b><br>"
                "Computes average look-down angles from a virtual observer standing height. "
                "Highlights local micro-relief details while preserving macro-topographical volume."
            ))
            self.ld_opts = QWidget()
            ld_opts_layout = QFormLayout(self.ld_opts)
            ld_opts_layout.setContentsMargins(0, 4, 0, 0)
            self.spin_ld_min_radius = QSpinBox()
            self.spin_ld_min_radius.setToolTip(tr(
                "Minimum search distance in pixels. Filters out high-frequency pixel noise "
                "from the observer's immediate surroundings."
            ))
            self.spin_ld_min_radius.setRange(1, 50)
            self.spin_ld_min_radius.setValue(2)
            ld_opts_layout.addRow(tr("Min Search Radius (px):"), self.spin_ld_min_radius)
            self.spin_ld_max_radius = QSpinBox()
            self.spin_ld_max_radius.setToolTip(tr(
                "Maximum search distance in pixels. Controls the outer spatial scale of the "
                "dominance calculations."
            ))
            self.spin_ld_max_radius.setRange(2, 100)
            self.spin_ld_max_radius.setValue(15)
            ld_opts_layout.addRow(tr("Max Search Radius (px):"), self.spin_ld_max_radius)
            self.spin_ld_sectors = QSpinBox()
            self.spin_ld_sectors.setRange(4, 32)
            self.spin_ld_sectors.setValue(8)
            ld_opts_layout.addRow(tr("Number of sectors:"), self.spin_ld_sectors)
            self.spin_ld_obs_height = QDoubleSpinBox()
            self.spin_ld_obs_height.setToolTip(tr(
                "Virtual observer standing height in meters. Prevents local terrain blocks "
                "from completely obscuring minor elevation changes."
            ))
            self.spin_ld_obs_height.setRange(0.1, 5.0)
            self.spin_ld_obs_height.setValue(1.7)
            self.spin_ld_obs_height.setDecimals(1)
            ld_opts_layout.addRow(tr("Observer Height (m):"), self.spin_ld_obs_height)
            self.out_local_dominance.addSettingsWidget(self.ld_opts)
            rvt_layout.addWidget(self.out_local_dominance)

            root.addWidget(rvt_group)

            self.spin_neg_sectors.setToolTip(self.spin_pos_sectors.toolTip())
            self.spin_svf_sectors.setToolTip(self.spin_pos_sectors.toolTip())
            self.spin_sky_sectors.setToolTip(self.spin_pos_sectors.toolTip())
            self.spin_asvf_sectors.setToolTip(self.spin_pos_sectors.toolTip())
            self.spin_ld_sectors.setToolTip(self.spin_pos_sectors.toolTip())

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
        return [
            self.out_slope, self.out_aspect, self.out_hillshade, self.out_tpi, self.out_tri,
            self.out_openness_pos, self.out_openness_neg, self.out_svf, 
            self.out_sky_illumination, self.out_anisotropic_svf, self.out_local_dominance
        ]

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
            "out_svf": self.out_svf.isChecked(),
            "out_sky_illumination": self.out_sky_illumination.isChecked(),
            "out_anisotropic_svf": self.out_anisotropic_svf.isChecked(),
            "out_local_dominance": self.out_local_dominance.isChecked(),

            # Target output file paths
            "path_slope": self.out_slope.filePath(),
            "path_aspect": self.out_aspect.filePath(),
            "path_hillshade": self.out_hillshade.filePath(),
            "path_tpi": self.out_tpi.filePath(),
            "path_tri": self.out_tri.filePath(),
            "path_openness_pos": self.out_openness_pos.filePath(),
            "path_openness_neg": self.out_openness_neg.filePath(),
            "path_svf": self.out_svf.filePath(),
            "path_sky_illumination": self.out_sky_illumination.filePath(),
            "path_anisotropic_svf": self.out_anisotropic_svf.filePath(),
            "path_local_dominance": self.out_local_dominance.filePath(),

            # Configurations
            "slope_percent": self.rad_slope_pct.isChecked(),
            "hill_variant": hill_variant,
            "hill_edges": hill_edges,
            "hill_azimuth": self.spin_hill_azimuth.value(),
            "hill_altitude": self.spin_hill_altitude.value(),
            "hill_z_factor": self.spin_hill_z.value(),
            "tpi_radius": self.spin_tpi_radius.value(),

            # Decoupled directional variables [2]
            "pos_radius": self.spin_pos_radius.value(),
            "pos_sectors": self.spin_pos_sectors.value(),
            
            "neg_radius": self.spin_neg_radius.value(),
            "neg_sectors": self.spin_neg_sectors.value(),
            
            "svf_radius": self.spin_svf_radius.value(),
            "svf_sectors": self.spin_svf_sectors.value(),
            
            "sky_radius": self.spin_sky_radius.value(),
            "sky_sectors": self.spin_sky_sectors.value(),

            "asvf_radius": self.spin_asvf_radius.value(),
            "asvf_sectors": self.spin_asvf_sectors.value(),
            "preferred_azimuth": self.spin_asvf_azimuth.value(),
            "anisotropy": self.slider_asvf_anisotropy.value() / 100.0,

            "local_dom_min_radius": self.spin_ld_min_radius.value(),
            "local_dom_max_radius": self.spin_ld_max_radius.value(),
            "local_dom_sectors": self.spin_ld_sectors.value(),
            "observer_height": self.spin_ld_obs_height.value()
        }

        self.btn_compute.setEnabled(False)
        self.set_loading_state(True, tr("Computing terrain derivatives asynchronously..."), total=100)
        

        # ── Pre-emptive Layer Unloading (Releases file locks on custom physical paths) ── [2]
        # project = QgsProject.instance()
        # for selector in self._get_all_selectors():
        #     if selector.isChecked():
        #         path = selector.filePath()
        #         if path and path != "TEMPORARY_OUTPUT":
        #             # Scan active layers and remove matches to release GDAL file handles [2]
        #             layers_to_remove = []
        #             for layer in project.mapLayers().values():
        #                 if layer.source() == path:
        #                     layers_to_remove.append(layer.id())
                    
        #             for lyr_id in layers_to_remove:
        #                 project.removeMapLayer(lyr_id)

                        
        # Dispatch background calculation thread
        self._worker = ComputeWorker(self.engine, params)
        self._worker.progress.connect(self.update_progress)
        self._worker.finished.connect(self._on_compute_finished)
        self._worker.error.connect(self._on_compute_error)
        self._worker.start()

    def _on_compute_finished(self, result: dict) -> None:
        self.btn_compute.setEnabled(True)
        self.set_loading_state(False)

        # Load all successfully generated layers onto the map canvas 
        project = QgsProject.instance()
        for key, (path, name) in result.items():
            layer = QgsRasterLayer(path, name, "gdal")
            if layer.isValid():
                project.addMapLayer(layer)

        self.show_info(tr("Terrain analysis complete. Layers added successfully."))

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
        """Enforces mutual exclusion between compute_edges and no_edges."""
        if checked:
            self.chk_hill_compute_edges.blockSignals(True)
            self.chk_hill_compute_edges.setChecked(False)
            self.chk_hill_compute_edges.blockSignals(False)

    # ── BasePanel required overrides ────────────────────────────────────

    def _on_result(self, data: dict) -> None:
        pass