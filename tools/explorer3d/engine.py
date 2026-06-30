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
            nodata_value=reader.nodata_value
        )
    
    
    def prepare_vector_layer(
        self,
        vector_layer: QgsVectorLayer,
        dem_layer: QgsRasterLayer,
        extrude_depth: float = 0.0
    ) -> List[ThreeDVector]:
        """
        Processes vector features (e.g., rivers, faults), reprojects them to the DEM CRS,
        and samples the DEM to interpolate vertical Z coordinates for every vertex.
        """
        reader = RasterReader(dem_layer)
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

            # Handle each part of the geometry
            for part in geom.constParts():
                vertices_3d = []
                for vertex in part.vertices():
                    pt = QgsPointXY(vertex.x(), vertex.y())
                    if transform:
                        pt = transform.transform(pt)

                    # Sample DEM elevation at current (X, Y) point
                    z_val = reader.sample_at(pt.x(), pt.y())
                    if np.isnan(z_val):
                        z_val = 0.0  # Default fallback for out-of-bounds nodes

                    vertices_3d.append([pt.x(), pt.y(), float(z_val)])

                if len(vertices_3d) < 2:
                    continue

                features.append(ThreeDVector(
                    element_id=f"vector_{vector_layer.id()}_{feature.id()}",
                    element_type="vector",
                    geom_type="line",
                    vertices=vertices_3d,
                    color="#3498db" if "river" in vector_layer.name().lower() else "#e74c3c",
                    label=str(feature.id()),
                    extrude_depth=extrude_depth
                ))

        return features