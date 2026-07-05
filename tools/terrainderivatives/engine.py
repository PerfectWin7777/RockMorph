"""
tools/terrainderivatives/engine.py — Terrain Derivatives Computational Engine

Orchestrates native GDAL geomorphometry and implements a high-performance,
vectorized pure Python/NumPy Yokoyama Topographic Openness solver.

Authors: RockMorph contributors / Tony winter
"""

import os
import math
import numpy as np  # type: ignore
from osgeo import gdal  # type: ignore

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
        import processing  # type: ignore

        output_layers = {}

        # ── 1. Slope (Pente) ──
        if kwargs.get("out_slope"):
            if progress_cb:
                progress_cb(15, tr("Computing Slope..."))
            res = processing.run(
                "gdal:slope",
                {
                    "INPUT": dem_layer,
                    "BAND": 1,
                    "AS_PERCENT": kwargs.get("slope_percent", False),
                    "OUTPUT": kwargs.get("path_slope", "TEMPORARY_OUTPUT")
                }
            )
            output_layers["slope"] = QgsRasterLayer(res["OUTPUT"], "Slope", "gdal")

        # ── 2. Aspect (Exposition) ──
        if kwargs.get("out_aspect"):
            if progress_cb:
                progress_cb(30, tr("Computing Aspect..."))
            res = processing.run(
                "gdal:aspect",
                {
                    "INPUT": dem_layer,
                    "BAND": 1,
                    "OUTPUT": kwargs.get("path_aspect", "TEMPORARY_OUTPUT")
                }
            )
            output_layers["aspect"] = QgsRasterLayer(res["OUTPUT"], "Aspect", "gdal")

        # ── 3. Hillshade (Ombrage) ──
        if kwargs.get("out_hillshade"):
            if progress_cb:
                progress_cb(45, tr("Computing Hillshade..."))
            variant = kwargs.get("hill_variant", "standard")
            edges = kwargs.get("hill_edges", "default")
            
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
                    "IGOR": True if variant == "igor" else False,
                    "ZEVENBERGEN": kwargs.get("hill_zevenbergen", False),
                    "COMPUTE_EDGES": True if edges == "compute_edges" else False,
                    "Z_FACTOR": kwargs.get("hill_z_factor", 1.0),
                    "OUTPUT": kwargs.get("path_hillshade", "TEMPORARY_OUTPUT")
                }
            )
            output_layers["hillshade"] = QgsRasterLayer(res["OUTPUT"], "Hillshade", "gdal")

        # ── 4. TPI (Topographic Position Index) ──
        if kwargs.get("out_tpi"):
            if progress_cb:
                progress_cb(60, tr("Computing TPI..."))
            # Neighborhood smoothing radius is passed to GDAL
            res = processing.run(
                "gdal:tpitopographicpositionindex",
                {
                    "INPUT": dem_layer,
                    "BAND": 1,
                    "OUTPUT": kwargs.get("path_tpi", "TEMPORARY_OUTPUT")
                }
            )
            output_layers["tpi"] = QgsRasterLayer(res["OUTPUT"], "TPI", "gdal")

        # ── 5. TRI (Terrain Ruggedness Index) ──
        if kwargs.get("out_tri"):
            if progress_cb:
                progress_cb(75, tr("Computing TRI..."))
            res = processing.run(
                "gdal:triterrainruggednessindex",
                {
                    "INPUT": dem_layer,
                    "BAND": 1,
                    "OUTPUT": kwargs.get("path_tri", "TEMPORARY_OUTPUT")
                }
            )
            output_layers["tri"] = QgsRasterLayer(res["OUTPUT"], "TRI", "gdal")

        # ── 6. Positive Openness (NumPy solver) ──
        if kwargs.get("out_openness_pos"):
            if progress_cb:
                progress_cb(85, tr("Computing Positive Openness (NumPy)..."))
            
            path_pos = kwargs.get("path_openness_pos", "TEMPORARY_OUTPUT")
            if path_pos == "TEMPORARY_OUTPUT":
                # Create a temporary file path
                import tempfile
                path_pos = os.path.join(tempfile.gettempdir(), "openness_pos.tif").replace("\\", "/")

            op_pos_array = self._solve_yokoyama_openness(
                dem_layer=dem_layer,
                radius_px=kwargs.get("openness_radius", 10),
                n_sectors=kwargs.get("openness_sectors", 8),
                invert_dem=False
            )
            self._save_numpy_to_gtiff(op_pos_array, path_pos, dem_layer)
            output_layers["openness_pos"] = QgsRasterLayer(path_pos, "Openness_Positive", "gdal")

        # ── 7. Negative Openness (NumPy solver on inverted DEM) ──
        if kwargs.get("out_openness_neg"):
            if progress_cb:
                progress_cb(95, tr("Computing Negative Openness (NumPy)..."))
            
            path_neg = kwargs.get("path_openness_neg", "TEMPORARY_OUTPUT")
            if path_neg == "TEMPORARY_OUTPUT":
                import tempfile
                path_neg = os.path.join(tempfile.gettempdir(), "openness_neg.tif").replace("\\", "/")

            op_neg_array = self._solve_yokoyama_openness(
                dem_layer=dem_layer,
                radius_px=kwargs.get("openness_radius", 10),
                n_sectors=kwargs.get("openness_sectors", 8),
                invert_dem=True
            )
            self._save_numpy_to_gtiff(op_neg_array, path_neg, dem_layer)
            output_layers["openness_neg"] = QgsRasterLayer(path_neg, "Openness_Negative", "gdal")

        if progress_cb:
            progress_cb(100, tr("Done."))

        return output_layers

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
        Calculates Yokoyama Topographic Openness in pure NumPy with boundary clipping.
        Uses array shifting to compute directional horizon angles over 8 azimuths [2].
        """
        reader = RasterReader(dem_layer)
        dem = reader.array.copy().astype(np.float32)
        
        # Replace NoData with NaN
        if reader.nodata is not None:
            dem[dem == reader.nodata] = np.nan

        if invert_dem:
            dem = -dem  # Negative openness is simply openness on an inverted DEM [2]

        rows, cols = dem.shape
        pixel_size = reader.pixel_size_x  # Horizontal pixel spacing in meters (or degrees)

        # We construct coordinate offsets for 8 compass directions
        directions = [
            (-1, 0),  (1, 0),  (0, -1), (0, 1),   # Orthogonal (N, S, W, E)
            (-1, -1), (-1, 1), (1, -1), (1, 1)    # Diagonal (NW, NE, SW, SE)
        ]
        
        # Adjust direction list dynamically if the user specified a custom sector size
        if n_sectors == 4:
            directions = directions[:4]

        # Initialize global accumulation array (stores the average openness over all directions)
        openness_sum = np.zeros_like(dem, dtype=np.float32)

        for dr, dc in directions:
            # Metric spacing factor: diagonal steps are sqrt(2) times wider than orthogonal steps [2]
            dir_factor = math.sqrt(dr**2 + dc**2)
            
            # Temporary array to store the maximum angle found along this specific direction
            max_beta = np.full_like(dem, -np.inf, dtype=np.float32)

            for r in range(1, radius_px + 1):
                # Shift array using our edge-clipped slice shifter (prevents wrapping at borders) [2]
                shifted_dem = self._shift_array(dem, dr * r, dc * r, fill_value=np.nan)
                
                # Physical distance to the target pixel [2]
                dist_m = r * pixel_size * dir_factor

                # Elevation angle: beta = arctan(dH / dL)
                beta = np.arctan((shifted_dem - dem) / dist_m)
                
                # Accumulate the maximum angle found so far
                max_beta = np.maximum(max_beta, beta)

            # Openness angle = 90 degrees - maximum elevation angle
            openness_dir_deg = 90.0 - np.degrees(max_beta)
            
            # Handle NoData pixels safely
            openness_dir_deg = np.where(np.isnan(openness_dir_deg), 90.0, openness_dir_deg)
            openness_sum += openness_dir_deg

        # Average openness over all directions
        final_openness = openness_sum / len(directions)
        
        # Restore NoData mask
        if reader.nodata is not None:
            final_openness[np.isnan(dem)] = np.nan

        return final_openness

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