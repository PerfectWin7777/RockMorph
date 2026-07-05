# tools/explorer3d/engine.py

"""
Computation engine for the 3D Explorer.
Handles fast NumPy-based raster subsampling, coordinate projection,
and vertical Z-projection of 2D vectors onto 3D terrain grids.

Authors: RockMorph contributors
"""

import math
import numpy as np  # type: ignore
from typing import List, Tuple, Optional

from ...base.base_engine import BaseEngine
from ...core.raster import RasterReader
from .models import ThreeDRaster, ThreeDVector

from qgis.core import (QgsRasterLayer,  # type: ignore
    QgsVectorLayer, QgsPointXY, QgsCoordinateTransform, QgsProject)  

from qgis.PyQt.QtCore import  QCoreApplication # type: ignore


def tr(message: str) -> str:
    return QCoreApplication.translate("RockMorph", message)


class Explorer3DEngine(BaseEngine):
    """
    Engine to prepare geospatial layers for the 3D WebGL viewer.
    """

    def __init__(self):
        super().__init__()

    def compute(self, **kwargs) -> dict:
        """
        Runs in the background thread.
        Prepares either the raster DEM or a draped vector layer based on the task_type parameter.
        """
        task_type = kwargs.get("task_type", "prepare_dem")
        progress_callback = kwargs.get("progress_callback")
        
        # ── TASK 1: DEM RASTER PREPARATION ─────────────────────────────────
        if task_type == "prepare_dem":
            dem_layer = kwargs.get("dem_layer")
            if not dem_layer:
                raise ValueError("No raster DEM layer provided to the compute engine.")
                
            if progress_callback:
                progress_callback(15, tr("Reading geospatial coordinates..."))
                
            dem_data = self.prepare_dem(dem_layer)
            
            if progress_callback:
                progress_callback(90, tr("Assembling 3D mesh matrix..."))
                
            return {"task_type": "dem", "dem_data": dem_data}
            
        # ── TASK 2: VECTOR LAYER DRAPING ───────────────────────────────────
        elif task_type == "prepare_vectors":
            vector_layer = kwargs.get("vector_layer")
            dem_layer = kwargs.get("dem_layer")
            extrude_depth = kwargs.get("extrude_depth", 0.0)
            
            # Extract decoupled styling fields instead of the old single attribute field
            color_attribute = kwargs.get("color_attribute", "")
            size_attribute = kwargs.get("size_attribute", "")
            style_params = kwargs.get("style_params") # Carry style parameters back to UI
            
            if not vector_layer or not dem_layer:
                raise ValueError("Both vector and raster layers are required for vector draping.")
                
            if progress_callback:
                progress_callback(20, tr("Iterating features and projecting onto elevation grid..."))
                
            # Pass both attributes to the updated geometry pipeline
            vectors = self.prepare_vector_layer(
                vector_layer=vector_layer,
                dem_layer=dem_layer,
                extrude_depth=extrude_depth,
                color_attribute=color_attribute,
                size_attribute=size_attribute
            )
            
            return {
                "task_type": "vectors", 
                "vectors": vectors, 
                "style_params": style_params
            }
            
        return {}
    

    def prepare_dem(self, dem_layer: QgsRasterLayer, max_resolution: int = 400) -> ThreeDRaster:
        """
        Reads, processes, and subsamples a DEM to ensure optimal WebGL performance.
        Converts geographic coordinates (degrees) to metric space (meters) to prevent scaling issues.
        """
        reader = RasterReader(dem_layer)
        raw_array = reader.array.copy()  # Shape: (rows, cols)
        rows, cols = raw_array.shape

        # Determine the downsampling strides
        stride_y = max(1, int(math.ceil(rows / max_resolution)))
        stride_x = max(1, int(math.ceil(cols / max_resolution)))

        # Downsample the matrix
        subsampled = raw_array[::stride_y, ::stride_x]
        sub_rows, sub_cols = subsampled.shape

        # Retrieve geographic dimensions in original units (could be degrees or meters)
        gt = reader.geo_transform
        x_min = gt[0]
        y_max = gt[3]

        pixel_size_x = reader.pixel_size_x * stride_x
        pixel_size_y = reader.pixel_size_y * stride_y

        # Generate base coordinates relative to top-left
        raw_x_coords = [x_min + i * pixel_size_x for i in range(sub_cols)]
        raw_y_coords = [y_max - j * pixel_size_y for j in range(sub_rows)]

        # --- GEOGRAPHIC TO METRIC CONVERSION ROADMAP ---
        # If the CRS is geographic, we approximate degrees to meters at the center latitude [1.13.2]
        if reader.is_geographic:
            center_lat = (raw_y_coords[0] + raw_y_coords[-1]) / 2.0 if len(raw_y_coords) > 1 else raw_y_coords[0]
            meters_per_degree_lat = 111320.0
            meters_per_degree_lon = 111320.0 * math.cos(math.radians(center_lat))
            
            # Generate coordinates in meters starting from 0 [1.13.2]
            x_coords = [i * pixel_size_x * meters_per_degree_lon for i in range(sub_cols)]
            y_coords = [j * pixel_size_y * meters_per_degree_lat for j in range(sub_rows)]
        else:
            # If already projected, center coordinates around 0 to avoid large coordinate jitters
            x_center = (raw_x_coords[0] + raw_x_coords[-1]) / 2.0
            y_center = (raw_y_coords[0] + raw_y_coords[-1]) / 2.0
            x_coords = [x - x_center for x in raw_x_coords]
            y_coords = [y - y_center for y in raw_y_coords]

        # Ensure vertical inversion aligns correctly with standard bottom-left WebGL systems
        # z_values = np.flipud(subsampled).tolist()
        z_values = subsampled.tolist()
        # y_coords = y_coords[::-1]

        # Compute valid elevation stats, filtering out NaNs
        valid_values = subsampled[~np.isnan(subsampled)]
        if valid_values.size > 0:
            z_min = float(np.min(valid_values))
            z_max = float(np.max(valid_values))
        else:
            z_min, z_max = 0.0, 0.0

        return ThreeDRaster(
            element_id=f"dem_{dem_layer.id()}",
            element_type="raster",
            width=sub_cols,
            height=sub_rows,
            x_coords=x_coords,
            y_coords=y_coords,
            z_values=z_values,
            z_min=z_min,
            z_max=z_max,
            nodata_value=reader.nodata_value,
            label=dem_layer.name()  # Propagate the clean QGIS layer name
        )
    

    def prepare_vector_layer(
        self,
        vector_layer: QgsVectorLayer,
        dem_layer: QgsRasterLayer,
        extrude_depth: float = 0.0,
        color_attribute: str = "",
        size_attribute: str = ""
    ) -> List[ThreeDVector]:
        """
        Processes vector features, projects them with clamping, removes duplicate vertices,
        and retrieves independent attribute values for both color and size styling [1.13.2].
        """
        reader = RasterReader(dem_layer)
        gt = reader.geo_transform
        x_min = gt[0]
        y_max = gt[3]
        rows, cols = reader.shape
        pixel_size_x = reader.pixel_size_x
        pixel_size_y = reader.pixel_size_y
        
        x_max = x_min + cols * pixel_size_x
        y_min = y_max - rows * pixel_size_y
        
        is_geographic = reader.is_geographic
        
        if is_geographic:
            raw_y_coords = [y_max - j * pixel_size_y for j in range(rows)]
            center_lat = (raw_y_coords[0] + raw_y_coords[-1]) / 2.0 if len(raw_y_coords) > 1 else raw_y_coords[0]
            meters_per_degree_lat = 111320.0
            meters_per_degree_lon = 111320.0 * math.cos(math.radians(center_lat))
        else:
            x_center = (x_min + x_max) / 2.0
            y_center = (y_min + y_max) / 2.0

        # Retrieve distinct field indices for color and size variables
        color_attr_idx = vector_layer.fields().indexOf(color_attribute) if color_attribute else -1
        size_attr_idx = vector_layer.fields().indexOf(size_attribute) if size_attribute else -1

        features = []

        # Coordinate transformation setup
        transform = None
        if vector_layer.crs() != dem_layer.crs():
            transform = QgsCoordinateTransform(
                vector_layer.crs(),
                dem_layer.crs(),
                QgsProject.instance()
            )

        for feature in vector_layer.getFeatures():
            geom = feature.geometry()
            if geom is None or geom.isEmpty():
                continue

            # Extract distinct color attribute value
            color_val = 0.0
            if color_attr_idx != -1:
                val = feature.attribute(color_attr_idx)
                try:
                    color_val = float(val) if val is not None else 0.0
                except (ValueError, TypeError):
                    color_val = 0.0

            # Extract distinct size attribute value
            size_val = 0.0
            if size_attr_idx != -1:
                val = feature.attribute(size_attr_idx)
                try:
                    size_val = float(val) if val is not None else 0.0
                except (ValueError, TypeError):
                    size_val = 0.0

            # Pack both variables for WebGL decoding
            attr_values = [color_val, size_val]

            for part in geom.constParts():
                vertices_3d = []
                for vertex in part.vertices():
                    # 1. Start with the raw vertex in the Vector layer's CRS
                    pt = QgsPointXY(vertex.x(), vertex.y())

                    # 2. Reproject to the DEM's CRS first
                    if transform:
                        try:
                            pt = transform.transform(pt)
                        except Exception:
                            # Skip corrupted or non-transformable coordinates safely
                            continue

                    # 3. Apply the half-pixel clamping now that pt is in DEM CRS
                    half_pixel_x = pixel_size_x * 0.5
                    half_pixel_y = pixel_size_y * 0.5

                    clamped_x = max(x_min + half_pixel_x, min(x_max - half_pixel_x, pt.x()))
                    clamped_y = max(y_min + half_pixel_y, min(y_max - half_pixel_y, pt.y()))
                    
                    pt = QgsPointXY(clamped_x, clamped_y)

                    # 4. Sample elevation safely
                    z_val = reader.sample_at(pt.x(), pt.y())
                    if np.isnan(z_val):
                        z_val = 0.0

                    if is_geographic:
                        x_local = (pt.x() - x_min) * meters_per_degree_lon
                        y_local = (pt.y() - y_min) * meters_per_degree_lat
                    else:
                        x_local = pt.x() - x_center
                        y_local = pt.y() - y_center

                    vertices_3d.append([x_local, y_local, float(z_val)])

                if not vertices_3d:
                    continue

                # Clean duplicate consecutive vertices to avoid rendering artifacts in WebGL
                cleaned_vertices = []
                for v in vertices_3d:
                    if not cleaned_vertices or v != cleaned_vertices[-1]:
                        cleaned_vertices.append(v)

                if len(cleaned_vertices) < 1:
                    continue

                features.append(ThreeDVector(
                    element_id=f"vector_{vector_layer.id()}_{feature.id()}",
                    element_type="vector",
                    geom_type="line",
                    vertices=cleaned_vertices,
                    color="#ffffff",
                    label=str(feature.id()),
                    extrude_depth=extrude_depth,
                    attribute_values=attr_values
                ))

        return features