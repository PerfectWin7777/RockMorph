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
    QSlider, QLineEdit
)
from qgis.PyQt.QtGui import QColor  # type: ignore
from qgis.PyQt.QtCore import Qt, QCoreApplication # type: ignore
from qgis.gui import QgsMapLayerComboBox, QgsCollapsibleGroupBox  # type: ignore
from qgis.core import QgsMapLayerProxyModel, QgsProject, QgsRasterLayer, QgsPalettedRasterRenderer  # type: ignore

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

            # ── GroupBox 5: Land-surface parameters (core/terrain_metrics) ──
            root.addWidget(self._build_surface_parameters_group())

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

    def _build_surface_parameters_group(self) -> QgsCollapsibleGroupBox:
        """Curvatures, insolation, Swiss shading, DEV, valley bottoms and landform classes.

        All are computed in NumPy by core/terrain_metrics.py; defaults are the
        GSV-Studio constants.
        """
        group = QgsCollapsibleGroupBox(tr("Curvature, Insolation & Landforms"))
        layout = QVBoxLayout(group)
        layout.setSpacing(10)

        # Curvatures (Florinsky, 2016)
        self.out_curvature = OutputSelectorWidget(
            tr("Curvatures (Florinsky)"), "curvature.tif", "GeoTIFF (*.tif)", is_checked=False)
        self.out_curvature.setToolTip(tr(
            "<b>Surface curvatures (Evans–Young 3 × 3, Florinsky 2016), in 1/m:</b><br>"
            "Positive = convex (crests, spurs), negative = concave (valleys, hollows).<br>"
            "One raster per selected kind; a file name gets the suffix _profile, _plan…"))
        curv_opts = QWidget()
        curv_layout = QVBoxLayout(curv_opts)
        curv_layout.setContentsMargins(0, 4, 0, 0)
        curv_layout.setSpacing(2)
        self.curvature_checks = {}
        for kind, label, tip, checked in (
                ("profile", tr("Profile (kv) – flow acceleration"), tr("Curvature along the slope line."), True),
                ("plan", tr("Plan (kh) – flow convergence"), tr("Horizontal curvature: < 0 converging flow (hollows)."), True),
                ("mean", tr("Mean (H)"), tr("Average of the two principal curvatures."), False),
                ("gaussian", tr("Gaussian (K)"), tr("Product of the principal curvatures: > 0 dome or basin, < 0 saddle."), False),
                ("minimal", tr("Minimal (kmin)"), tr("Smaller principal curvature: valley lines."), False),
                ("maximal", tr("Maximal (kmax)"), tr("Larger principal curvature: ridge lines."), False)):
            box = QCheckBox(label)
            box.setToolTip(tip)
            box.setChecked(checked)
            curv_layout.addWidget(box)
            self.curvature_checks[kind] = box
        self.out_curvature.addSettingsWidget(curv_opts)
        layout.addWidget(self.out_curvature)

        # Relative insolation (Kumar et al., 1997)
        self.out_insolation = OutputSelectorWidget(
            tr("Relative Insolation (yearly)"), "insolation.tif", "GeoTIFF (*.tif)", is_checked=False)
        self.out_insolation.setToolTip(tr(
            "<b>Relative yearly insolation (Kumar et al., 1997):</b><br>"
            "Direct sun received over the solstices and equinoxes at the DEM latitude, "
            "divided by what flat ground receives: &gt; 1 sunny slopes, &lt; 1 shaded slopes.<br>"
            "Explains vegetation, weathering and periglacial asymmetries between opposite slopes. "
            "Cast shadows of distant relief are not traced."))
        layout.addWidget(self.out_insolation)

        # Swiss hillshade (Jenny, 2001)
        self.out_swiss = OutputSelectorWidget(
            tr("Swiss Hillshade (Imhof style)"), "swiss_hillshade.tif", "GeoTIFF (*.tif)", is_checked=False)
        self.out_swiss.setToolTip(tr(
            "<b>Swiss relief shading (Jenny, 2001):</b><br>"
            "North-west light on a generalised DEM for the large forms, multidirectional "
            "shading for the detail, plains kept light. Values 0–1."))
        swiss_opts = QWidget()
        swiss_layout = QFormLayout(swiss_opts)
        swiss_layout.setContentsMargins(0, 4, 0, 0)
        self.spin_swiss_generalize = QSpinBox()
        self.spin_swiss_generalize.setRange(20, 5000)
        self.spin_swiss_generalize.setValue(300)
        self.spin_swiss_generalize.setSuffix(" m")
        self.spin_swiss_generalize.setToolTip(tr("Radius over which the large forms are generalised."))
        swiss_layout.addRow(tr("Generalisation:"), self.spin_swiss_generalize)
        self.spin_swiss_z = QDoubleSpinBox()
        self.spin_swiss_z.setRange(0.1, 20.0)
        self.spin_swiss_z.setValue(1.0)
        self.spin_swiss_z.setDecimals(1)
        swiss_layout.addRow(tr("Z-Factor:"), self.spin_swiss_z)
        self.out_swiss.addSettingsWidget(swiss_opts)
        layout.addWidget(self.out_swiss)

        # Multi-scale DEV (Lindsay et al., 2015)
        self.out_dev = OutputSelectorWidget(
            tr("Multi-scale Topographic Position (DEV)"), "dev_multiscale.tif", "GeoTIFF (*.tif)", is_checked=False)
        self.out_dev.setToolTip(tr(
            "<b>Multi-scale deviation from mean elevation (Lindsay et al., 2015):</b><br>"
            "(z − mean) / standard deviation, kept at the scale where it is strongest.<br>"
            "&gt; 0 crests and highs, &lt; 0 valleys and hollows, whatever their size."))
        dev_opts = QWidget()
        dev_layout = QFormLayout(dev_opts)
        dev_layout.setContentsMargins(0, 4, 0, 0)
        self.edit_dev_radii = QLineEdit("3, 9, 27")
        self.edit_dev_radii.setToolTip(tr("Window radii in cells, separated by commas."))
        dev_layout.addRow(tr("Radii (cells):"), self.edit_dev_radii)
        self.out_dev.addSettingsWidget(dev_opts)
        layout.addWidget(self.out_dev)

        # Valley bottoms (simplified MRVBF)
        self.out_mrvbf = OutputSelectorWidget(
            tr("Valley Bottoms (MRVBF, simplified)"), "valley_bottoms.tif", "GeoTIFF (*.tif)", is_checked=False)
        self.out_mrvbf.setToolTip(tr(
            "<b>Multi-resolution valley bottom flatness (Gallant & Dowling, 2003), simplified:</b><br>"
            "0 on slopes and summits; the value grows with the width of the flat, low valley floor. "
            "Maps alluvial fills, terraces and depositional areas."))
        mrvbf_opts = QWidget()
        mrvbf_layout = QFormLayout(mrvbf_opts)
        mrvbf_layout.setContentsMargins(0, 4, 0, 0)
        self.spin_mrvbf_slope = QDoubleSpinBox()
        self.spin_mrvbf_slope.setRange(1.0, 45.0)
        self.spin_mrvbf_slope.setValue(16.0)
        self.spin_mrvbf_slope.setSuffix(" °")
        self.spin_mrvbf_slope.setToolTip(tr("Flatness threshold at the finest scale; halved at each coarser scale."))
        mrvbf_layout.addRow(tr("Initial slope threshold:"), self.spin_mrvbf_slope)
        self.out_mrvbf.addSettingsWidget(mrvbf_opts)
        layout.addWidget(self.out_mrvbf)

        # Iwahashi & Pike landforms
        self.out_landforms = OutputSelectorWidget(
            tr("Landform Classes (Iwahashi & Pike)"), "landforms.tif", "GeoTIFF (*.tif)", is_checked=False)
        self.out_landforms.setToolTip(tr(
            "<b>Terrain classes of Iwahashi & Pike (2007), first level:</b><br>"
            "Slope steep or gentle, surface convex or concave, texture fine or coarse, "
            "each compared to its mean over the DEM: 8 classes, styled with a legend."))
        land_opts = QWidget()
        land_layout = QFormLayout(land_opts)
        land_layout.setContentsMargins(0, 4, 0, 0)
        self.spin_landform_window = QSpinBox()
        self.spin_landform_window.setRange(30, 10000)
        self.spin_landform_window.setValue(300)
        self.spin_landform_window.setSuffix(" m")
        self.spin_landform_window.setToolTip(tr("Window over which convexity and texture are measured."))
        land_layout.addRow(tr("Window:"), self.spin_landform_window)
        self.out_landforms.addSettingsWidget(land_opts)
        layout.addWidget(self.out_landforms)

        # Geomorphons (Jasiewicz & Stepinski, 2013)
        self.out_geomorphons = OutputSelectorWidget(
            tr("Geomorphons (10 landform elements)"), "geomorphons.tif", "GeoTIFF (*.tif)", is_checked=False)
        self.out_geomorphons.setToolTip(tr(
            "<b>Geomorphons (Jasiewicz & Stepinski, 2013):</b><br>"
            "Flat, peak, ridge, shoulder, spur, slope, hollow, footslope, valley, pit — "
            "from the line-of-sight pattern in the 8 directions. Styled with a legend."))
        geo_opts = QWidget()
        geo_layout = QFormLayout(geo_opts)
        geo_layout.setContentsMargins(0, 4, 0, 0)
        self.spin_geomorphon_lookup = QSpinBox()
        self.spin_geomorphon_lookup.setRange(10, 20000)
        self.spin_geomorphon_lookup.setValue(1500)
        self.spin_geomorphon_lookup.setSuffix(" m")
        self.spin_geomorphon_lookup.setToolTip(tr("Search distance: larger values describe larger landforms."))
        geo_layout.addRow(tr("Search distance:"), self.spin_geomorphon_lookup)
        self.spin_geomorphon_flat = QDoubleSpinBox()
        self.spin_geomorphon_flat.setRange(0.0, 10.0)
        self.spin_geomorphon_flat.setValue(1.0)
        self.spin_geomorphon_flat.setSuffix(" °")
        self.spin_geomorphon_flat.setToolTip(tr("Elevation angle below which a direction counts as flat."))
        geo_layout.addRow(tr("Flatness threshold:"), self.spin_geomorphon_flat)
        self.out_geomorphons.addSettingsWidget(geo_opts)
        layout.addWidget(self.out_geomorphons)
        return group

    def _surface_parameter_params(self) -> dict:
        """Parameters of the land-surface group for the engine."""
        radii = []
        for token in self.edit_dev_radii.text().replace(";", ",").split(","):
            token = token.strip()
            if token.isdigit() and int(token) > 0:
                radii.append(int(token))
        params = {
            "curvature_kinds": [kind for kind, box in self.curvature_checks.items() if box.isChecked()],
            "swiss_generalize_m": float(self.spin_swiss_generalize.value()),
            "swiss_z_factor": self.spin_swiss_z.value(),
            "dev_radii": tuple(radii) or (3, 9, 27),
            "mrvbf_slope": self.spin_mrvbf_slope.value(),
            "landform_window_m": float(self.spin_landform_window.value()),
            "geomorphon_lookup_m": float(self.spin_geomorphon_lookup.value()),
            "geomorphon_flat_deg": self.spin_geomorphon_flat.value(),
        }
        for key in ("curvature", "insolation", "swiss", "dev", "mrvbf", "landforms", "geomorphons"):
            selector = getattr(self, f"out_{key}")
            params[f"out_{key}"] = selector.isChecked()
            params[f"path_{key}"] = selector.filePath()
        return params

    @staticmethod
    def _style_classes(layer: QgsRasterLayer, legend: dict) -> None:
        """Paletted renderer with one colour and label per class value."""
        colors = ["#9e9e9e", "#7b1f1f", "#d1442b", "#f08d49", "#f6c85f", "#c9d17e",
                  "#7fb685", "#3f8f9e", "#2c5d9e", "#1d2f6f"]
        classes = [QgsPalettedRasterRenderer.Class(value, QColor(colors[(int(value) - 1) % len(colors)]), label)
                   for value, label in sorted(legend.items())]
        layer.setRenderer(QgsPalettedRasterRenderer(layer.dataProvider(), 1, classes))
        layer.triggerRepaint()

    # ── Helpers ──

    def _get_all_selectors(self) -> list[OutputSelectorWidget]:
        return [
            self.out_slope, self.out_aspect, self.out_hillshade, self.out_tpi, self.out_tri,
            self.out_openness_pos, self.out_openness_neg, self.out_svf, 
            self.out_sky_illumination, self.out_anisotropic_svf, self.out_local_dominance,
            self.out_curvature, self.out_insolation, self.out_swiss, self.out_dev,
            self.out_mrvbf, self.out_landforms, self.out_geomorphons,
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
        params.update(self._surface_parameter_params())

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
                legend = self.engine.class_legends.get(key)
                if legend:
                    self._style_classes(layer, {value: tr(label) for value, label in legend.items()})
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