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

from qgis.core import QgsRasterLayer, QgsVectorLayer, QgsPointXY, QgsCoordinateTransform, QgsProject  # type: ignore


class Explorer3DEngine(BaseEngine):
    """
    Engine to prepare geospatial layers for the 3D WebGL viewer.
    """

    def __init__(self):
        super().__init__()

    def compute(self, **kwargs) -> dict:
        """Required override of BaseEngine abstract method."""
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
        z_values = np.flipud(subsampled).tolist()
        y_coords = y_coords[::-1]

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
        extrude_depth: float = 0.0
    ) -> List[ThreeDVector]:
        """
        Processes vector features (rivers, faults, boundaries), reprojects them,
        and projects them to the local coordinate system of the 3D terrain.
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
            # Match the exact latitude-dependent metric calculation from prepare_dem
            raw_y_coords = [y_max - j * pixel_size_y for j in range(rows)]
            center_lat = (raw_y_coords[0] + raw_y_coords[-1]) / 2.0 if len(raw_y_coords) > 1 else raw_y_coords[0]
            meters_per_degree_lat = 111320.0
            meters_per_degree_lon = 111320.0 * math.cos(math.radians(center_lat))
        else:
            x_center = (x_min + x_max) / 2.0
            y_center = (y_min + y_max) / 2.0

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

            # Identify structural features: Point (0), Line (1), Polygon (2)
            geom_type_id = geom.type() 
            
            # Extract boundaries from geometries
            for part in geom.constParts():
                vertices_3d = []
                for vertex in part.vertices():
                    pt = QgsPointXY(vertex.x(), vertex.y())
                    if transform:
                        pt = transform.transform(pt)

                    # Sample DEM elevation
                    z_val = reader.sample_at(pt.x(), pt.y())
                    if np.isnan(z_val):
                        z_val = 0.0

                    # Convert coordinates to the identical metric/offset space of the DEM
                    if is_geographic:
                        x_local = (pt.x() - x_min) * meters_per_degree_lon
                        y_local = (pt.y() - y_min) * meters_per_degree_lat
                    else:
                        x_local = pt.x() - x_center
                        y_local = pt.y() - y_center

                    vertices_3d.append([x_local, y_local, float(z_val)])

                if not vertices_3d:
                    continue

                # Deduplicate identical adjacent vertices
                cleaned_vertices = []
                for v in vertices_3d:
                    if not cleaned_vertices or v != cleaned_vertices[-1]:
                        cleaned_vertices.append(v)

                if len(cleaned_vertices) < 1:
                    continue

                features.append(ThreeDVector(
                    element_id=f"vector_{vector_layer.id()}_{feature.id()}",
                    element_type="vector",
                    geom_type="line" if geom_type_id in (1, 2) else "point",
                    vertices=cleaned_vertices,
                    color="#3498db" if "river" in vector_layer.name().lower() else "#e74c3c",
                    label=str(feature.id()),
                    extrude_depth=extrude_depth
                ))

        return features