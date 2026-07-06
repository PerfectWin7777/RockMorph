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
                progress_cb(45, tr("Computing Hillshade..."))
            variant = kwargs.get("hill_variant", "standard")
            edges = kwargs.get("hill_edges", "default")
            path_hillshade = kwargs.get("path_hillshade", "TEMPORARY_OUTPUT")
            
            # Map parameters based on our UI selections and actual GDAL specifications [1, 3]
            res = processing.run(
                "gdal:hillshade",
                {
                    "INPUT": dem_layer,
                    "BAND": 1,
                    "AZIMUTH": kwargs.get("hill_azimuth", 315),
                    "ALTITUDE": kwargs.get("hill_altitude", 45),
                    "COMBINED": True if variant == "combined" else False,
                    "MULTIDIRECTIONAL": True if variant == "multidirectional" else False,
                    "EXTRA": "-igor" if variant == "igor" else "", 
                    "ZEVENBERGEN": kwargs.get("hill_zevenbergen", False),
                    "COMPUTE_EDGES": True if edges == "compute_edges" else False,
                    "Z_FACTOR": kwargs.get("hill_z_factor", 1.0),
                    "SCALE": gdal_scale,  # Pass computed geographic scale factor
                    "OUTPUT": path_hillshade
                }
            )
            lyr_name = _get_layer_name(path_hillshade, "Hillshade")
            output_layers["hillshade"] = QgsRasterLayer(res["OUTPUT"], lyr_name, "gdal")

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
                # Create a temporary file path
                unique_id = uuid.uuid4().hex[:8] 
                path_pos = os.path.join(tempfile.gettempdir(), f"openness_pos_{unique_id}.tif").replace("\\", "/")

            op_pos_array = self._solve_yokoyama_openness(
                dem_layer=dem_layer,
                radius_px=kwargs.get("openness_radius", 10),
                n_sectors=kwargs.get("openness_sectors", 8),
                invert_dem=False
            )
            self._save_numpy_to_gtiff(op_pos_array, path_pos, dem_layer)
            lyr_name = _get_layer_name(path_pos, "Openness_Positive")
            output_layers["openness_pos"] = QgsRasterLayer(path_pos, lyr_name, "gdal")

        # ── 7. Negative Openness (NumPy solver on inverted DEM) ──
        if kwargs.get("out_openness_neg"):
            if progress_cb:
                progress_cb(95, tr("Computing Negative Openness (NumPy)..."))
            
            path_neg = kwargs.get("path_openness_neg", "TEMPORARY_OUTPUT")
            if path_neg == "TEMPORARY_OUTPUT":
                unique_id = uuid.uuid4().hex[:8]
                path_neg = os.path.join(tempfile.gettempdir(), f"openness_neg_{unique_id}.tif").replace("\\", "/")

            op_neg_array = self._solve_yokoyama_openness(
                dem_layer=dem_layer,
                radius_px=kwargs.get("openness_radius", 10),
                n_sectors=kwargs.get("openness_sectors", 8),
                invert_dem=True
            )
            self._save_numpy_to_gtiff(op_neg_array, path_neg, dem_layer)
            lyr_name = _get_layer_name(path_neg, "Openness_Negative")
            output_layers["openness_neg"] = QgsRasterLayer(path_neg, lyr_name, "gdal")


        if progress_cb:
            progress_cb(100, tr("Done."))
        

        # ── 8. Sky View Factor (NumPy solver) ──
        if kwargs.get("out_svf"):
            if progress_cb:
                progress_cb(98, tr("Computing Sky View Factor (NumPy)..."))

            path_svf = kwargs.get("path_svf", "TEMPORARY_OUTPUT")
            if path_svf == "TEMPORARY_OUTPUT":
                unique_id = uuid.uuid4().hex[:8]
                path_svf = os.path.join(tempfile.gettempdir(), f"svf_{unique_id}.tif").replace("\\", "/")

            svf_array = self._solve_sky_view_factor(
                dem_layer=dem_layer,
                radius_px=kwargs.get("openness_radius", 10),
                n_sectors=kwargs.get("openness_sectors", 8),
            )
            self._save_numpy_to_gtiff(svf_array, path_svf, dem_layer)
            lyr_name = _get_layer_name(path_svf, "Sky_View_Factor")
            output_layers["svf"] = QgsRasterLayer(path_svf, lyr_name, "gdal")


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
        # Directional coordinate offsets (clockwise starting from North)
        directions = [
            (-1, 0),  (1, 0),  (0, -1), (0, 1),   # Orthogonal (N, S, W, E)
            (-1, -1), (-1, 1), (1, -1), (1, 1)    # Diagonal (NW, NE, SW, SE)
        ]
        if n_sectors == 4:
            directions = directions[:4]

        horizon_angles = []
        for dr, dc in directions:
            # Distance scale adjustment: diagonal steps are wider by sqrt(2)
            dir_factor = math.sqrt(dr**2 + dc**2)
            max_beta = np.full_like(dem, -np.inf, dtype=np.float32)

            for r in range(1, radius_px + 1):
                # Shift the elevation matrix along the current radial vector
                shifted_dem = self._shift_array(dem, dr * r, dc * r, fill_value=np.nan)
                
                # Georeferenced horizontal distance to the target pixel [2]
                dist_m = r * pixel_size * dir_factor
                
                # Elevation angle: beta = arctan(delta_Z / delta_L)
                beta = np.arctan((shifted_dem - dem) / dist_m)
                
                # Keep the maximum elevation angle found, ignoring NaNs safely [2]
                max_beta = np.fmax(max_beta, beta)

            # Pixels whose radial path falls entirely outside the grid are set to 0.0 (flat horizon) [2]
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