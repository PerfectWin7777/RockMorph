"""
tools/terrainderivatives/engine.py — Terrain Derivatives Computational Engine

Orchestrates native GDAL geomorphometry and implements a high-performance,
vectorized pure Python/NumPy Yokoyama Topographic Openness solver.

Authors: RockMorph contributors / Tony winter
"""

import os
import math
import uuid
import tempfile
from typing import List
import numpy as np  # type: ignore
from osgeo import gdal  # type: ignore
import processing  # type: ignore
from qgis.core import QgsRasterLayer, QgsProject, QgsCoordinateTransform  # type: ignore
from qgis.PyQt.QtCore import QCoreApplication  # type: ignore

from ...base.base_engine import BaseEngine
from ...core.raster import RasterReader


def tr(message: str) -> str:
    return QCoreApplication.translate("RockMorph", message)


class TerrainDerivativesEngine(BaseEngine):

    def validate(self, **kwargs) -> bool:
        dem = kwargs.get("dem_layer")
        if dem is None or not dem.isValid():
            return False
        return True

    def compute(self, **kwargs) -> dict:
        """
        Executes the terrain analysis pipeline. Runs GDAL wrappers via Processing
        and executes custom NumPy solvers for visualisations [2].
        """
        dem_layer = kwargs["dem_layer"]
        progress_cb = kwargs.get("progress_callback")

        output_layers = {}
        # Compute dynamic horizontal-to-vertical scale factors for geographic coordinates 
        gdal_scale = self._get_gdal_scale_factor(dem_layer)

        # Helper to dynamically extract layer display name from file path [2]
        def _get_layer_name(path: str, default: str) -> str:
            if path and path != "TEMPORARY_OUTPUT":
                return os.path.splitext(os.path.basename(path))[0]
            return default


        # ── 1. Slope (Pente) ──
        if kwargs.get("out_slope"):
            if progress_cb:
                progress_cb(15, tr("Computing Slope..."))
            path_slope = kwargs.get("path_slope", "TEMPORARY_OUTPUT")
            res = processing.run(
                "gdal:slope",
                {
                    "INPUT": dem_layer,
                    "BAND": 1,
                    "AS_PERCENT": kwargs.get("slope_percent", False),
                    "SCALE": gdal_scale,  # Pass computed geographic scale factor
                    "OUTPUT": path_slope
                }
            )
            # Use dynamic file name as layer display name [2]
            lyr_name = _get_layer_name(path_slope, "Slope")
            output_layers["slope"] = QgsRasterLayer(res["OUTPUT"], lyr_name, "gdal")

        # ── 2. Aspect (Exposition) ──
        if kwargs.get("out_aspect"):
            if progress_cb:
                progress_cb(30, tr("Computing Aspect..."))
            path_aspect = kwargs.get("path_aspect", "TEMPORARY_OUTPUT")
            res = processing.run(
                "gdal:aspect",
                {
                    "INPUT": dem_layer,
                    "BAND": 1,
                    "OUTPUT": path_aspect
                }
            )
            lyr_name = _get_layer_name(path_aspect, "Aspect")
            output_layers["aspect"] = QgsRasterLayer(res["OUTPUT"], lyr_name, "gdal")

        # ── 3. Hillshade (Ombrage) ──
        if kwargs.get("out_hillshade"):
            if progress_cb:
                progress_cb(45, tr("Computing Hillshade (GDAL Native)..."))
            variant = kwargs.get("hill_variant", "standard")
            edges = kwargs.get("hill_edges", "default")
            path_hillshade = kwargs.get("path_hillshade", "TEMPORARY_OUTPUT")
            
            # Resolve unique temporary output if not specified to prevent QGIS file locks 
            if path_hillshade == "TEMPORARY_OUTPUT":
                unique_id = uuid.uuid4().hex[:8]
                path_hillshade = os.path.join(tempfile.gettempdir(), f"hillshade_{unique_id}.tif").replace("\\", "/")
            
            # Determine standard gradient algorithm based on UI selection
            algorithm = 'ZevenbergenThorne' if kwargs.get("hill_zevenbergen", False) else 'Horn'

            # exclusif
            altitude=kwargs.get("hill_altitude", 45.0)
            azimuth=kwargs.get("hill_azimuth", 315.0)
            if variant == "multidirectional":
               azimuth = None
            if variant == "igor":
               altitude = None

            # Setup native GDAL processing options (directly supporting the Igor algorithm) 
            options = gdal.DEMProcessingOptions(
                alg=algorithm,
                computeEdges=True if edges == "compute_edges" else False,
                zFactor=kwargs.get("hill_z_factor", 1.0),
                scale=gdal_scale,  # Pass computed geographic scale factor
                azimuth=azimuth,
                altitude=altitude,
                combined=True if variant == "combined" else False,
                multiDirectional=True if variant == "multidirectional" else False,
                igor=True if variant == "igor" else False # Native Igor Sharygin algorithm 
            )
            
            # Execute native C++ GDAL DEMProcessing directly on the DEM source file path 
            gdal.DEMProcessing(
                path_hillshade,
                dem_layer.source(),  # Source file path
                "hillshade",
                options=options
            )
            
            # Load as QgsRasterLayer with dynamic naming based on the output filename 
            lyr_name = _get_layer_name(path_hillshade, "Hillshade")
            output_layers["hillshade"] = QgsRasterLayer(path_hillshade, lyr_name, "gdal")

        # ── 4. TPI (Topographic Position Index) ──
        if kwargs.get("out_tpi"):
            if progress_cb:
                progress_cb(60, tr("Computing TPI..."))
            # Neighborhood smoothing radius is passed to GDAL
            path_tpi = kwargs.get("path_tpi", "TEMPORARY_OUTPUT")
            res = processing.run(
                "gdal:tpitopographicpositionindex",
                {
                    "INPUT": dem_layer,
                    "BAND": 1,
                    "OUTPUT": path_tpi
                }
            )
            lyr_name = _get_layer_name(path_tpi, "TPI")
            output_layers["tpi"] = QgsRasterLayer(res["OUTPUT"], lyr_name, "gdal")

        # ── 5. TRI (Terrain Ruggedness Index) ──
        if kwargs.get("out_tri"):
            if progress_cb:
                progress_cb(75, tr("Computing TRI..."))
            path_tri = kwargs.get("path_tri", "TEMPORARY_OUTPUT")
            res = processing.run(
                "gdal:triterrainruggednessindex",
                {
                    "INPUT": dem_layer,
                    "BAND": 1,
                    "OUTPUT": path_tri
                }
            )
            lyr_name = _get_layer_name(path_tri, "TRI")
            output_layers["tri"] = QgsRasterLayer(res["OUTPUT"], lyr_name, "gdal")

        # ── 6. Positive Openness (NumPy solver) ──
        if kwargs.get("out_openness_pos"):
            if progress_cb:
                progress_cb(85, tr("Computing Positive Openness (NumPy)..."))
            
            path_pos = kwargs.get("path_openness_pos", "TEMPORARY_OUTPUT")
            if path_pos == "TEMPORARY_OUTPUT":
                unique_id = uuid.uuid4().hex[:8]
                path_pos = os.path.join(tempfile.gettempdir(), f"openness_pos_{unique_id}.tif").replace("\\", "/")

            op_pos_array = self._solve_yokoyama_openness(
                dem_layer=dem_layer,
                radius_px=kwargs.get("pos_radius", 10),
                n_sectors=kwargs.get("pos_sectors", 8),
                invert_dem=False
            )
            self._save_numpy_to_gtiff(op_pos_array, path_pos, dem_layer)
            lyr_name = _get_layer_name(path_pos, "Openness_Positive")
            output_layers["openness_pos"] = QgsRasterLayer(path_pos, lyr_name, "gdal")

        # ── 7. Negative Openness (NumPy solver on inverted DEM) ──
        if kwargs.get("out_openness_neg"):
            if progress_cb:
                progress_cb(92, tr("Computing Negative Openness (NumPy)..."))
            
            path_neg = kwargs.get("path_openness_neg", "TEMPORARY_OUTPUT")
            if path_neg == "TEMPORARY_OUTPUT":
                unique_id = uuid.uuid4().hex[:8]
                path_neg = os.path.join(tempfile.gettempdir(), f"openness_neg_{unique_id}.tif").replace("\\", "/")

            op_neg_array = self._solve_yokoyama_openness(
                dem_layer=dem_layer,
                radius_px=kwargs.get("neg_radius", 10),
                n_sectors=kwargs.get("neg_sectors", 8),
                invert_dem=True
            )
            self._save_numpy_to_gtiff(op_neg_array, path_neg, dem_layer)
            lyr_name = _get_layer_name(path_neg, "Openness_Negative")
            output_layers["openness_neg"] = QgsRasterLayer(path_neg, lyr_name, "gdal")

        
        # ── 8. Sky View Factor (NumPy solver) ──
        if kwargs.get("out_svf"):
            if progress_cb:
                progress_cb(95, tr("Computing Sky View Factor (NumPy)..."))

            path_svf = kwargs.get("path_svf", "TEMPORARY_OUTPUT")
            if path_svf == "TEMPORARY_OUTPUT":
                unique_id = uuid.uuid4().hex[:8]
                path_svf = os.path.join(tempfile.gettempdir(), f"svf_{unique_id}.tif").replace("\\", "/")

            svf_array = self._solve_sky_view_factor(
                dem_layer=dem_layer,
                radius_px=kwargs.get("svf_radius", 10),
                n_sectors=kwargs.get("svf_sectors", 8),
            )
            self._save_numpy_to_gtiff(svf_array, path_svf, dem_layer)
            lyr_name = _get_layer_name(path_svf, "Sky_View_Factor")
            output_layers["svf"] = QgsRasterLayer(path_svf, lyr_name, "gdal")

        # ── 9. Sky Illumination (NumPy solver) ──
        if kwargs.get("out_sky_illumination"):
            if progress_cb:
                progress_cb(97, tr("Computing Sky Illumination (NumPy)..."))

            path_sky = kwargs.get("path_sky_illumination", "TEMPORARY_OUTPUT")
            if path_sky == "TEMPORARY_OUTPUT":
                unique_id = uuid.uuid4().hex[:8]
                path_sky = os.path.join(tempfile.gettempdir(), f"sky_illumination_{unique_id}.tif").replace("\\", "/")

            sky_array = self._solve_sky_illumination(
                dem_layer=dem_layer,
                radius_px=kwargs.get("sky_radius", 10),
                n_sectors=kwargs.get("sky_sectors", 8)
            )
            self._save_numpy_to_gtiff(sky_array, path_sky, dem_layer)
            lyr_name = _get_layer_name(path_sky, "Sky_Illumination")
            output_layers["sky_illumination"] = QgsRasterLayer(path_sky, lyr_name, "gdal")

        # ── 10. Anisotropic Sky View Factor (NumPy solver) ──
        if kwargs.get("out_anisotropic_svf"):
            if progress_cb:
                progress_cb(98, tr("Computing Anisotropic SVF (NumPy)..."))

            path_asvf = kwargs.get("path_anisotropic_svf", "TEMPORARY_OUTPUT")
            if path_asvf == "TEMPORARY_OUTPUT":
                unique_id = uuid.uuid4().hex[:8]
                path_asvf = os.path.join(tempfile.gettempdir(), f"anisotropic_svf_{unique_id}.tif").replace("\\", "/")

            asvf_array = self._solve_anisotropic_sky_view_factor(
                dem_layer=dem_layer,
                radius_px=kwargs.get("asvf_radius", 10),
                n_sectors=kwargs.get("asvf_sectors", 8),
                preferred_azimuth_deg=kwargs.get("preferred_azimuth", 315.0),
                anisotropy_level=kwargs.get("anisotropy", 0.5)
            )
            self._save_numpy_to_gtiff(asvf_array, path_asvf, dem_layer)
            lyr_name = _get_layer_name(path_asvf, "Anisotropic_Sky_View_Factor")
            output_layers["anisotropic_svf"] = QgsRasterLayer(path_asvf, lyr_name, "gdal")

        # ── 11. Local Dominance (NumPy solver) ──
        if kwargs.get("out_local_dominance"):
            if progress_cb:
                progress_cb(99, tr("Computing Local Dominance (NumPy)..."))

            path_ld = kwargs.get("path_local_dominance", "TEMPORARY_OUTPUT")
            if path_ld == "TEMPORARY_OUTPUT":
                unique_id = uuid.uuid4().hex[:8]
                path_ld = os.path.join(tempfile.gettempdir(), f"local_dominance_{unique_id}.tif").replace("\\", "/")

            ld_array = self._solve_local_dominance(
                dem_layer=dem_layer,
                min_radius_px=kwargs.get("local_dom_min_radius", 2),
                max_radius_px=kwargs.get("local_dom_max_radius", 15),
                n_sectors=kwargs.get("local_dom_sectors", 8),
                observer_height=kwargs.get("observer_height", 1.7)
            )
            self._save_numpy_to_gtiff(ld_array, path_ld, dem_layer)
            lyr_name = _get_layer_name(path_ld, "Local_Dominance")
            output_layers["local_dominance"] = QgsRasterLayer(path_ld, lyr_name, "gdal")
        
        if progress_cb:
            progress_cb(100, tr("Done."))

        return output_layers

    
    # ------------------------------------------------------------------
    # Vectorized Horizon and Relief Visualisation Solvers 
    # ------------------------------------------------------------------

    def _compute_horizon_angles(
        self,
        dem: np.ndarray,
        radius_px: int,
        n_sectors: int,
        pixel_size: float
    ) -> List[np.ndarray]:
        """
        Computes the maximum elevation angle of the horizon (in radians)
        for each sampled radial direction using vectorized array shifting .

        This core geometric solver is shared by both Topographic Openness
        and Sky View Factor (SVF) calculations to prevent code duplication .

        Args:
            dem (np.ndarray): 2D elevation grid array.
            radius_px (int): Search radius in pixels.
            n_sectors (int): Number of radial directions (4 or 8).
            pixel_size (float): Horizontal spacing of the grid in meters.

        Returns:
            List[np.ndarray]: List of 2D arrays, each containing the maximum
                              horizon angle (radians) for a given azimuth direction.
        """
        # Directional coordinate offsets in strict clockwise order (clockwise starting from North)
        directions = [
            (-1, 0),   # 1. North (0°)
            (-1, 1),   # 2. Northeast (45°)
            (0, 1),    # 3. East (90°)
            (1, 1),    # 4. Southeast (135°)
            (1, 0),    # 5. South (180°)
            (1, -1),   # 6. Southwest (225°)
            (0, -1),   # 7. West (270°)
            (-1, -1)   # 8. Northwest (315°)
        ]
        if n_sectors == 4:
            # Clockwise orthogonal directions: N, E, S, W 
            directions = [(-1, 0), (0, 1), (1, 0), (0, -1)]

        horizon_angles = []
        for dr, dc in directions:
            # Distance scale adjustment: diagonal steps are wider by sqrt(2)
            dir_factor = math.sqrt(dr**2 + dc**2)
            max_beta = np.full_like(dem, -np.inf, dtype=np.float32)

            for r in range(1, radius_px + 1):
                # Shift the elevation matrix along the current radial vector
                shifted_dem = self._shift_array(dem, dr * r, dc * r, fill_value=np.nan)
                
                # Georeferenced horizontal distance to the target pixel 
                dist_m = r * pixel_size * dir_factor
                
                # Elevation angle: beta = arctan(delta_Z / delta_L)
                beta = np.arctan((shifted_dem - dem) / dist_m)
                
                # Keep the maximum elevation angle found, ignoring NaNs safely 
                max_beta = np.fmax(max_beta, beta)

            # Pixels whose radial path falls entirely outside the grid are set to 0.0 (flat horizon) 
            max_beta = np.where(np.isneginf(max_beta), 0.0, max_beta)
            horizon_angles.append(max_beta)

        return horizon_angles
    
    # ------------------------------------------------------------------
    # Vectorized Yokoyama Topographic Openness Solver (Yokoyama et al. 2002)
    # ------------------------------------------------------------------

    def _solve_yokoyama_openness(
        self,
        dem_layer: QgsRasterLayer,
        radius_px: int,
        n_sectors: int,
        invert_dem: bool = False
    ) -> np.ndarray:
        """
        Calculates Yokoyama Topographic Openness (Yokoyama et al. 2002)
        using directional line-of-sight horizon angles .

        Args:
            dem_layer (QgsRasterLayer): Source elevation raster layer.
            radius_px (int): Search radius in pixels.
            n_sectors (int): Number of radial directions (4 or 8).
            invert_dem (bool): If True, computes Negative Openness (valleys) instead.

        Returns:
            np.ndarray: 2D array of Topographic Openness values in degrees.
        """
        reader = RasterReader(dem_layer)
        dem = reader.array.copy().astype(np.float32)
       
        # Mask NoData values
        if reader.nodata is not None:
            dem[dem == reader.nodata] = np.nan
            
        if invert_dem:
            dem = -dem  # Invert relief to trace incised structures 
        
         # Fix: Calculate pixel size in meters adapted to the CRS (degrees -> meters)
        pixel_size_m = self._get_pixel_size_meters(dem_layer, reader)

        # Extract maximum horizon angles across all sectors
        horizon_angles = self._compute_horizon_angles(
            dem, radius_px, n_sectors, pixel_size_m
        )

        # Average openness: Mean of (90 degrees - max_beta) across all azimuths 
        openness_sum = np.zeros_like(dem, dtype=np.float32)
        for max_beta in horizon_angles:
            openness_dir_deg = 90.0 - np.degrees(max_beta)
            # Fill remaining boundary NaNs with neutral 90.0
            openness_dir_deg = np.where(np.isnan(openness_dir_deg), 90.0, openness_dir_deg)
            openness_sum += openness_dir_deg

        final_openness = openness_sum / len(horizon_angles)
        
        # Restore original NoData mask
        if reader.nodata is not None:
            final_openness[np.isnan(dem)] = np.nan
            
        return final_openness

    def _solve_sky_view_factor(
        self,
        dem_layer: QgsRasterLayer,
        radius_px: int,
        n_sectors: int
    ) -> np.ndarray:
        """
        Calculates the Sky View Factor (Zaksek et al. 2011; Kokalj et al. 2011)
        representing the fraction of the visible hemisphere of the sky.
        Sky View Factor (Zakšek et al., 2011) :
            SVF = 1 - (1/N) * Σ sin(γ_i)

        Args:
            dem_layer (QgsRasterLayer): Source elevation raster layer.
            radius_px (int): Search radius in pixels.
            n_sectors (int): Number of radial directions (4 or 8).

        Returns:
            np.ndarray: 2D array of SVF values scaled between 0.0 (obstructed) and 1.0 (fully open).
        """
        reader = RasterReader(dem_layer)
        dem = reader.array.copy().astype(np.float32)
        
        if reader.nodata is not None:
            dem[dem == reader.nodata] = np.nan
        
         # Fix: Calculate pixel size in meters adapted to the CRS (degrees -> meters)
        pixel_size_m = self._get_pixel_size_meters(dem_layer, reader)
        # Extract maximum horizon angles
        horizon_angles = self._compute_horizon_angles(
            dem, radius_px, n_sectors, pixel_size_m
        )

        # SVF = 1 - (1/N) * sum(sin(gamma_i))
        sin_sum = np.zeros_like(dem, dtype=np.float32)
        for max_beta in horizon_angles:
            # Negative elevation angles (horizon below horizontal) do not obstruct the sky -> clip to 0.0
            gamma = np.clip(max_beta, 0.0, np.pi / 2.0)
            sin_sum += np.sin(gamma)

        svf = 1.0 - (sin_sum / len(horizon_angles))
        svf = np.clip(svf, 0.0, 1.0) # Tight bounding

        # Restore NoData mask
        if reader.nodata is not None:
            svf[np.isnan(dem)] = np.nan
            
        return svf
    

    def _shift_array(self, arr: np.ndarray, dr: int, dc: int, fill_value: float = np.nan) -> np.ndarray:
        """
        Shifts a 2D numpy array by (dr, dc) and pads the empty boundaries with fill_value [2].
        Does not wrap around boundaries.
        """
        result = np.full_like(arr, fill_value)
        rows, cols = arr.shape
        
        # Calculate source and target slices
        if dr >= 0:
            src_row_start, src_row_end = 0, rows - dr
            tgt_row_start, d_row_end = dr, rows
        else:
            src_row_start, src_row_end = -dr, rows
            tgt_row_start, d_row_end = 0, rows + dr
            
        if dc >= 0:
            src_col_start, src_col_end = 0, cols - dc
            tgt_col_start, d_col_end = dc, cols
        else:
            src_col_start, src_col_end = -dc, cols
            tgt_col_start, d_col_end = 0, cols + dc
            
        # Perform slice copy if valid
        if src_row_end > src_row_start and src_col_end > src_col_start:
            result[tgt_row_start:d_row_end, tgt_col_start:d_col_end] = \
                arr[src_row_start:src_row_end, src_col_start:src_col_end]
                
        return result
    


    # ------------------------------------------------------------------
    # Advanced Diffuse Sky & Local Dominance NumPy Solvers [2]
    # ------------------------------------------------------------------

    def _solve_sky_illumination(
        self,
        dem_layer: QgsRasterLayer,
        radius_px: int,
        n_sectors: int
    ) -> np.ndarray:
        """
        Calculates Sky Illumination under the Standard Overcast Sky (SOC) model
        originally defined by the CIE (Commission Internationale de l'Éclairage).

        THEORETICAL CONTEXT
        ===================
        Standard hillshading uses a single directional light vector, which often
        creates harsh shadows and completely black voids. Sky Illumination
        replaces this with diffuse hemispherical lighting . 
        
        Under the CIE SOC model, sky luminance decreases from the zenith to the 
        horizon following the function: L(theta) = L_zenith * (1 + 2*cos(theta)) / 3.
        Since theta (zenith angle) is the complement of gamma (horizon elevation), 
        the directional luminance weight becomes: (1 + 2*sin(gamma)) / 3 .
        This is combined with the directional sky visibility (1 - sin(gamma)) to
        integrate the total incoming diffuse light on each cell.

        MATHEMATICAL FORMULA
        ====================
        For each cell, the accumulated diffuse illumination is computed as:

            SOC = (1/N) * sum_{i=1}^{N} [ (1 - sin(gamma_i)) * (1 + 2*sin(gamma_i)) / 3 ]

        Where:
            gamma_i = maximum elevation angle of the horizon in sector i (radians).
            N       = number of compass sectors (4 or 8).

        ACADEMIC REFERENCE
        ==================
        CIE (1990). Spatial distribution of daylight - overcast sky and clear sky.
        CIE Standard S003, Vienna.
        Kokalj, Z., & Somrak, M. (2019). Why Not Use a Hap Hazardly Selected 
        Visualization? Remote Sensing, 11(11), 1168.

        Args:
            dem_layer (QgsRasterLayer): Source elevation raster layer.
            radius_px (int): Search radius in pixels.
            n_sectors (int): Number of radial directions (4 or 8).

        Returns:
            np.ndarray: 2D array of Sky Illumination values scaled between 0.0 and 1.0.
        """
        # Read DEM array and handle NoData masking
        reader = RasterReader(dem_layer)
        dem = reader.array.copy().astype(np.float32)
        
        if reader.nodata is not None:
            dem[dem == reader.nodata] = np.nan

        # Calculate horizontally scaled pixel size in meters (degrees -> meters conversion supported) [2]
        pixel_size_m = self._get_pixel_size_meters(dem_layer, reader)

        # Extract maximum horizon elevation angles across all azimuthal directions
        horizon_angles = self._compute_horizon_angles(
            dem, radius_px, n_sectors, pixel_size_m
        )

        illumination_sum = np.zeros_like(dem, dtype=np.float32)
        for max_beta in horizon_angles:
            # Clip elevation angles: flat terrain = 0.0, deep pit = pi/2
            gamma = np.clip(max_beta, 0.0, np.pi / 2.0)
            
            # CIE SOC Overcast Sky diffuse intensity equation [1.3.1]
            # Visibility term: (1 - sin(gamma)) 
            # Overcast Luminance term: (1 + 2*sin(gamma)) / 3.0
            intensity = (1.0 - np.sin(gamma)) * (1.0 + 2.0 * np.sin(gamma)) / 3.0
            
            # Accumulate values, safely skipping boundaries
            illumination_sum += np.nan_to_num(intensity, nan=0.0)

        sky_illumination = illumination_sum / len(horizon_angles)
        sky_illumination = np.clip(sky_illumination, 0.0, 1.0)

        # Restore original NoData values
        if reader.nodata is not None:
            sky_illumination[np.isnan(dem)] = np.nan
            
        return sky_illumination

    def _solve_anisotropic_sky_view_factor(
        self,
        dem_layer: QgsRasterLayer,
        radius_px: int,
        n_sectors: int,
        preferred_azimuth_deg: float,
        anisotropy_level: float
    ) -> np.ndarray:
        """
        Calculates the Anisotropic (Directional) Sky View Factor (aSVF)
        by weighting horizon angles relative to a preferred structural trend.

        THEORETICAL CONTEXT
        ===================
        Standard Sky View Factor assumes a uniformly bright sky dome (isotropic). 
        Anisotropic SVF introduces a directional light bias . By weighting the 
        directional sky visibility by a cosine function perpendicular to a preferred 
        azimuth, it simulates directional shadows. This reveals subtle linear 
        morphologies (such as fault scarps, dikes, and fracture networks) oriented 
        parallel to the structural trend with extreme clarity .

        MATHEMATICAL FORMULA
        ====================
        For each directional sector i, a weight is calculated as:

            weight_i = 1.0 + A * cos(phi_i - phi_preferred - pi/2)

        Where:
            A             = anisotropy level (0.0 to 1.0).
            phi_i         = azimuth angle of the current sector i (radians).
            phi_preferred = preferred structural azimuth defined by user (radians) .
            pi/2 offset   = shifts the maximum weight perpendicular to the preferred 
                            trend to maximize topographic shadow contrast.

        The anisotropic SVF is then integrated as:

            aSVF = 1.0 - ( sum_{i=1}^{N} sin(gamma_i) * weight_i ) / sum_{i=1}^{N} weight_i

        ACADEMIC REFERENCE
        ==================
        Zaksek, K., Ostir, K., & Kokalj, Z. (2011). Sky-View Factor as a Relief 
        Visualization Technique. Remote Sensing, 3(2), 398-415. doi:10.3390/rs3020398
        Kokalj, Z., & Somrak, M. (2019). Why Not Use a Hap Hazardly Selected 
        Visualization? Remote Sensing, 11(11), 1168.

        Args:
            dem_layer (QgsRasterLayer): Source elevation raster layer.
            radius_px (int): Search radius in pixels.
            n_sectors (int): Number of radial directions (4 or 8).
            preferred_azimuth_deg (float): Preferred tectonic/structural trend in degrees (0-360).
            anisotropy_level (float): Blending factor from 0.0 (isotropic) to 1.0 (anisotropic).

        Returns:
            np.ndarray: 2D array of anisotropic SVF values scaled between 0.0 and 1.0.
        """
        # Read DEM array and handle NoData masking
        reader = RasterReader(dem_layer)
        dem = reader.array.copy().astype(np.float32)
        
        if reader.nodata is not None:
            dem[dem == reader.nodata] = np.nan

        # Calculate horizontally scaled pixel size in meters
        pixel_size_m = self._get_pixel_size_meters(dem_layer, reader)

        # Extract standard isotropic horizon angles
        horizon_angles = self._compute_horizon_angles(
            dem, radius_px, n_sectors, pixel_size_m
        )

        # Predefined direction matrices in strict clockwise order to match horizon_angles [2]
        directions = [
            (-1, 0),   # 1. North (0°)
            (-1, 1),   # 2. Northeast (45°)
            (0, 1),    # 3. East (90°)
            (1, 1),    # 4. Southeast (135°)
            (1, 0),    # 5. South (180°)
            (1, -1),   # 6. Southwest (225°)
            (0, -1),   # 7. West (270°)
            (-1, -1)   # 8. Northwest (315°)
        ]
        if n_sectors == 4:
            # N, E, S, W
            directions = [(-1, 0), (0, 1), (1, 0), (0, -1)]

        preferred_azimuth_rad = math.radians(preferred_azimuth_deg)
        weighted_sin_sum = np.zeros_like(dem, dtype=np.float32)
        weight_total = 0.0

        for i, max_beta in enumerate(horizon_angles):
            dr, dc = directions[i]
            
            # Calculate exact azimuth angle of this sector (clockwise from North) [2]
            azimuth_rad = math.atan2(dc, -dr)
            if azimuth_rad < 0:
                azimuth_rad += 2.0 * math.pi

            # Cosine weight shifted by pi/2 to highlight perpendicular structural lineaments [2]
            weight = np.cos(azimuth_rad - preferred_azimuth_rad - (math.pi / 2.0))
            weight = 1.0 + anisotropy_level * weight

            gamma = np.clip(max_beta, 0.0, np.pi / 2.0)
            
            # Accumulate weighted sinus factors, skipping boundaries safely
            weighted_sin_sum += np.nan_to_num(np.sin(gamma) * weight, nan=0.0)
            weight_total += weight

        anisotropic_svf = 1.0 - (weighted_sin_sum / weight_total)
        anisotropic_svf = np.clip(anisotropic_svf, 0.0, 1.0)

        # Restore original NoData values
        if reader.nodata is not None:
            anisotropic_svf[np.isnan(dem)] = np.nan
            
        return anisotropic_svf



    def _solve_local_dominance(
        self,
        dem_layer: QgsRasterLayer,
        min_radius_px: int,
        max_radius_px: int,
        n_sectors: int,
        observer_height: float = 1.7
    ) -> np.ndarray:
        """
        Calculates Local Dominance (LD) from high-resolution DEMs using the 
        exact mathematical algorithm defined by Ralf Hesse (2016).

        THEORETICAL CONTEXT
        ===================
        Local Dominance measures how much an observer standing at a given cell 
        dominates their surrounding landscape within a radial search window [R_min, R_max]. 
        Unlike Sky View Factor (SVF), which can look flat on steep terrain, 
        Local Dominance preserves macro-topographical volume by calculating 
        the average look-down angle from the observer's eyes to all adjacent pixels.

        High values (crest lines, ridges) correspond to positive look-down angles 
        where the observer looks downward to see the neighbors .
        Low or negative values (valleys, incisions, sinkholes) represent negative 
        angles where the observer must look upward to see the surrounding terrain .
        This produces a dramatic, plastic pseudo-3D visualization.

        MATHEMATICAL FORMULA
        ====================
        For each cell (x0, y0) with elevation Z0 and observer height H_obs: 

            LD = (1/N) * sum_{i=1}^{N} [ (1 / (R_max - R_min + 1)) * sum_{r=R_min}^{R_max} alpha_{i,r} ]

        Where:
            alpha_{i,r} = arctan( ( (Z0 + H_obs) - Z_{i,r} ) / d_r )
            d_r         = r * pixel_size_meters * dir_factor
            dir_factor  = sqrt(dr^2 + dc^2) (1.0 for orthogonal, sqrt(2) for diagonal)
            N           = number of compass sectors (4 or 8)

        ACADEMIC REFERENCE
        ==================
        Hesse, R. (2016). Local dominance - a novel visualization of high-resolution 
        elevation data. Cartography and Geographic Information Science, 43(3), 205-217.
        doi:10.1080/15230406.2015.1059157

        Args:
            dem_layer (QgsRasterLayer): Source elevation raster layer.
            min_radius_px (int): Minimum search boundary in pixels (inner radius) .
            max_radius_px (int): Maximum search boundary in pixels (outer radius) .
            n_sectors (int): Number of radial directions (4 or 8) to sample .
            observer_height (float): Standing height of the virtual observer (default: 1.7 m).

        Returns:
            np.ndarray: 2D array of Local Dominance values in degrees (0-90° typical).
        """
        # ── 1. Read DEM array and handle NoData masking ──
        reader = RasterReader(dem_layer)
        dem = reader.array.copy().astype(np.float32)
        
        if reader.nodata is not None:
            dem[dem == reader.nodata] = np.nan

        # ── 2. Get dynamic georeferenced metric pixel size (degrees -> meters conversion supported) [2]
        pixel_size_m = self._get_pixel_size_meters(dem_layer, reader)

        # ── 3. Define the 8 compass directions in a strict clockwise order ──
        # This prevents azimuthal rotation misalignment during directional loops [2]
        directions = [
            (-1, 0),   # 1. North (0°)
            (-1, 1),   # 2. Northeast (45°)
            (0, 1),    # 3. East (90°)
            (1, 1),    # 4. Southeast (135°)
            (1, 0),    # 5. South (180°)
            (1, -1),   # 6. Southwest (225°)
            (0, -1),   # 7. West (270°)
            (-1, -1)   # 8. Northwest (315°)
        ]
        if n_sectors == 4:
            # Symmetrical orthogonal directions: N, E, S, W
            directions = [(-1, 0), (0, 1), (1, 0), (0, -1)]

        # ── 4. Initialize the dominance accumulator array ──
        dominance_sum = np.zeros_like(dem, dtype=np.float32)

        # ── 5. Compute average look-down angles along each azimuth ──
        for dr, dc in directions:
            # Diagonal steps have wider cell centers by sqrt(2) [2]
            dir_factor = math.sqrt(dr**2 + dc**2)
            direction_angles_sum = np.zeros_like(dem, dtype=np.float32)
            step_count = 0

            # Sum sight angles over the radial search interval [R_min, R_max] [1.2.1]
            for r in range(min_radius_px, max_radius_px + 1):
                # Shift elevation matrices along current azimuth (with edge padding to prevent wrap-around) [2]
                shifted_dem = self._shift_array(dem, dr * r, dc * r, fill_value=np.nan)
                
                # Physical horizontal distance in meters
                dist_m = r * pixel_size_m * dir_factor

                # Sight angle formula: arctan( (Z_observer - Z_neighbor) / L_distance ) [1.2.1]
                # Negative angles (looking up) are preserved to correctly darken depressions and valleys [2]
                angle = np.arctan(((dem + observer_height) - shifted_dem) / dist_m)
                
                # Accumulate angles, treating NaNs at boundaries as 0.0 contribution vectorially
                direction_angles_sum += np.nan_to_num(angle, nan=0.0)
                step_count += 1

            # Accumulate average sight angle of this sector
            dominance_sum += (direction_angles_sum / step_count)

        # ── 6. Average the angles over all N sectors and convert from radians to degrees ──
        local_dominance_rad = dominance_sum / len(directions)
        local_dominance_deg = np.degrees(local_dominance_rad)

        # ── 7. Re-apply the original DEM NoData mask ──
        if reader.nodata is not None:
            local_dominance_deg[np.isnan(dem)] = np.nan
            
        return local_dominance_deg
    

    #============================================================================
    #  HELPER
    #============================================================================

    def _get_pixel_size_meters(self, dem_layer: QgsRasterLayer, reader: RasterReader) -> float:
        """
        Returns the pixel size in meters, dynamically converting geographic degrees
        to meters at the center latitude.
        """
        crs = dem_layer.crs()
        px = abs(reader.pixel_size_x)
        if not crs.isGeographic():
            return px

        # Geographic CRS — convert degrees to meters at center latitude 
        extent = dem_layer.extent()
        center_lat = (extent.yMinimum() + extent.yMaximum()) / 2.0
        meters_per_degree = 111320.0 * math.cos(math.radians(center_lat))
        return px * meters_per_degree

    def _get_gdal_scale_factor(self, dem_layer: QgsRasterLayer) -> float:
        """
        Returns the vertical-to-horizontal conversion scale factor for GDAL tools .
        """
        crs = dem_layer.crs()
        if not crs.isGeographic():
            return 1.0

        extent = dem_layer.extent()
        center_lat = (extent.yMinimum() + extent.yMaximum()) / 2.0
        # Horizontal resolution is degrees, vertical is meters 
        return 111320.0 * math.cos(math.radians(center_lat))
    
    # ------------------------------------------------------------------
    # Native QGIS GeoTIFF Writer (Via OSGeo GDAL API)
    # ------------------------------------------------------------------

    def _save_numpy_to_gtiff(self, array: np.ndarray, output_path: str, reference_layer: QgsRasterLayer):
        """
        Saves a numpy array back into a geo-referenced GeoTIFF raster using
        the projection and geotransform of the source DEM [2].
        """
        driver = gdal.GetDriverByName("GTiff")
        rows, cols = array.shape
        
        # Create output dataset
        ds = driver.Create(output_path, cols, rows, 1, gdal.GDT_Float32)

        # Safety: If writing fails (e.g., protected folder or persistent file lock)
        # if ds is None:
        #     raise RuntimeError(
        #         tr(f"GDAL could not create output file: {output_path}. "
        #            "The file may be locked by QGIS or the directory is read-only.")
        #     )
        
        
        # Copy exact spatial geotransform parameters
        reader = RasterReader(reference_layer)
        ds.SetGeoTransform(reader.geo_transform)
        
        # Copy WKT projection metadata
        projection_wkt = reference_layer.crs().toWkt()
        ds.SetProjection(projection_wkt)
        
        # Write band array
        band = ds.GetRasterBand(1)
        # Convert NaN values back to a standard -9999.0 NoData value
        clean_array = np.where(np.isnan(array), -9999.0, array)
        band.WriteArray(clean_array)
        band.SetNoDataValue(-9999.0)
        
        # Flush to disk
        band.FlushCache()
        ds = None # Close and release file handles