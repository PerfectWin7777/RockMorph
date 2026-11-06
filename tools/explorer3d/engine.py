# tools/explorer3d/engine.py

"""
Computation engine for the 3D Explorer.
Handles fast NumPy-based raster subsampling, coordinate projection,
and vertical Z-projection of 2D vectors onto 3D terrain grids.

Authors: RockMorph contributors
"""

import math
import warnings

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
    

    def prepare_dem(self, dem_layer: QgsRasterLayer, max_resolution: int = 400,
                    texture_resolution: int = 1024) -> ThreeDRaster:
        """
        Prepare a DEM for the WebGL viewer.

        Two grids are produced from the same raster:

        * the MESH grid (at most ``max_resolution`` cells per side), which
          sets the number of triangles and keeps the scene interactive;
        * the HEIGHT TEXTURE (at most ``texture_resolution`` per side), read
          by the fragment shader for per-pixel normals, cast shadows and
          ambient occlusion. Relief detail therefore no longer depends on the
          mesh density.

        Both grids are reduced by BLOCK MEANS (NaN-aware) rather than by
        keeping one pixel out of N: plain striding aliases fine relief into
        false ridges and moiré (audit M11).

        Coordinates are cell centres, in metres, centred on the centre of the
        full raster extent: the same origin as ``prepare_vector_layer``, so
        draped vectors line up. Geographic rasters are converted with the
        metres-per-degree of the central latitude.
        """
        reader = RasterReader(dem_layer)
        raw = np.asarray(reader.array, dtype=np.float64)
        rows, cols = raw.shape
        gt = reader.geo_transform
        x_min, y_max = gt[0], gt[3]
        px, py = reader.pixel_size_x, reader.pixel_size_y

        mesh, step_x, step_y = _block_mean(raw, max_resolution)
        texture, _, _ = _block_mean(raw, texture_resolution)

        # Metres per CRS unit along x and y.
        if reader.is_geographic:
            centre_lat = y_max - rows * py / 2.0
            unit_x = 111320.0 * math.cos(math.radians(centre_lat))
            unit_y = 111320.0
        else:
            unit_x = unit_y = 1.0

        # Block centres, relative to the centre of the full extent.
        n_rows, n_cols = mesh.shape
        x_centre = cols * px / 2.0
        y_centre = rows * py / 2.0
        x_coords = [((i + 0.5) * step_x * px - x_centre) * unit_x for i in range(n_cols)]
        y_coords = [(y_centre - (j + 0.5) * step_y * py) * unit_y for j in range(n_rows)]

        valid = mesh[np.isfinite(mesh)]
        z_min = float(valid.min()) if valid.size else 0.0
        z_max = float(valid.max()) if valid.size else 0.0

        return ThreeDRaster(
            element_id=f"dem_{dem_layer.id()}",
            element_type="raster",
            width=n_cols,
            height=n_rows,
            x_coords=x_coords,
            y_coords=y_coords,
            z_values=_json_grid(mesh, 2),
            z_min=z_min,
            z_max=z_max,
            nodata_value=reader.nodata_value,
            label=dem_layer.name(),
            texture_width=int(texture.shape[1]),
            texture_height=int(texture.shape[0]),
            # Decimetre precision is far below DEM accuracy and halves the
            # size of the JSON sent to the viewer.
            texture_values=_json_grid(texture, 1),
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
        
        # Same origin and units as prepare_dem: metres from the centre of
        # the raster extent (geographic rasters use the central latitude).
        x_center = (x_min + x_max) / 2.0
        y_center = (y_min + y_max) / 2.0
        if is_geographic:
            meters_per_degree_lat = 111320.0
            meters_per_degree_lon = 111320.0 * math.cos(math.radians(y_center))

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
                        x_local = (pt.x() - x_center) * meters_per_degree_lon
                        y_local = (pt.y() - y_center) * meters_per_degree_lat
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


def _block_mean(array: np.ndarray, max_size: int):
    """
    Reduce ``array`` so that no side exceeds ``max_size``, averaging square
    blocks of cells and ignoring NaN (a block that is entirely NaN stays
    NaN). Returns (reduced array, block width, block height) in cells.
    """
    rows, cols = array.shape
    step = max(1, int(math.ceil(max(rows, cols) / float(max_size))))
    if step == 1:
        return array.copy(), 1, 1
    pad_r = (-rows) % step
    pad_c = (-cols) % step
    padded = np.pad(array, ((0, pad_r), (0, pad_c)), constant_values=np.nan)
    blocks = padded.reshape(padded.shape[0] // step, step, padded.shape[1] // step, step)
    with np.errstate(invalid="ignore"), warnings.catch_warnings():
        warnings.simplefilter("ignore", category=RuntimeWarning)  # all-NaN blocks
        reduced = np.nanmean(blocks, axis=(1, 3))
    return reduced, step, step


def _json_grid(array: np.ndarray, decimals: int) -> list:
    """Nested lists with NaN as None (strict JSON), rounded to ``decimals``."""
    rounded = np.round(array, decimals)
    return [[None if not np.isfinite(v) else float(v) for v in row] for row in rounded]
