"""
tools/hydroflow/engine.py — HydroFlow Computational Engine

Calculates flow routing and extracts topologically ordered stream networks 
(Strahler, Horton, Shreve) using a hybrid GRASS and Python Graph Solver.

Authors: RockMorph contributors / Tony winter
"""

import os
import math
from collections import deque

import numpy as np  # type: ignore
from typing import Dict, Any, List, Tuple

from qgis.core import (  # type: ignore
    QgsRasterLayer, QgsVectorLayer, QgsProject,
    QgsCoordinateTransform, QgsFeature, QgsGeometry,
    QgsPointXY, QgsWkbTypes, QgsField, QgsFields,
    QgsDistanceArea, QgsProcessingFeedback,
    QgsVectorFileWriter
)
from qgis.PyQt.QtCore import QCoreApplication, QVariant  # type: ignore
import processing  # type: ignore

from ...base.base_engine import BaseEngine
from ...core.processing_ids import FILL_SINKS_IDS, first_available, grass_id
from ...core.raster import RasterReader


def tr(message: str) -> str:
    return QCoreApplication.translate("RockMorph", message)


class HydroFlowEngine(BaseEngine):
    """
    Engine to extract drainage networks from DEMs, compute hydrological routing,
    and resolve stream orders (Strahler, Horton, Shreve) topologically in Python.
    """

    def __init__(self):
        super().__init__()

    def validate(self, **kwargs) -> bool:
        dem = kwargs.get("dem_layer")
        if dem is None or not dem.isValid():
            return False
        return True

    def compute(self, **kwargs) -> dict:
        """
        Main execution thread.
        Prepares the conditioned DEM, runs GRASS extraction, and solves topology.
        """
        dem_layer = kwargs["dem_layer"]
        threshold_cells = kwargs.get("threshold", 1000)
        fill_depressions = kwargs.get("fill_depressions", True)
        
        # Desired Outputs Flags [2]
        out_streams = kwargs.get("out_streams", True)
        out_fdr = kwargs.get("out_fdr", False)
        out_fac = kwargs.get("out_fac", False)
        out_basins = kwargs.get("out_basins", False)

        # Output target paths (Memory 'TEMPORARY_OUTPUT' or physical disk path) [2]
        streams_path = kwargs.get("streams_path", "TEMPORARY_OUTPUT")
        fdr_path = kwargs.get("fdr_path", "TEMPORARY_OUTPUT")
        fac_path = kwargs.get("fac_path", "TEMPORARY_OUTPUT")
        basins_path = kwargs.get("basins_path", "TEMPORARY_OUTPUT")

        progress_callback = kwargs.get("progress_callback")
        

        # ── Step 1: Pre-conditioning (Pit filling) ──
        conditioned_dem = dem_layer
        if fill_depressions:
            if progress_callback:
                progress_callback(10, tr("Conditioning DEM (filling depressions)..."))
            conditioned_dem = self._fill_sinks(dem_layer)

        output_layers = {}

        # ── Step 2: Run GRASS r.watershed for FDR/FAC/Basins if requested ──
        if out_fdr or out_fac or out_basins:
            if progress_callback:
                progress_callback(25, tr("Running flow routing (GRASS r.watershed)..."))
            
            # extent = conditioned_dem.extent()
            # region_param = f"{extent.xMinimum()},{extent.xMaximum()},{extent.yMinimum()},{extent.yMaximum()}"

            watershed_results = processing.run(
                grass_id("r.watershed"),
                {
                    "elevation": conditioned_dem,
                    "threshold": threshold_cells,
                    # -a: positive accumulation even where flow leaves the
                    # region (otherwise negative); output = cell counts.
                    "-a": True,
                    "accumulation": fac_path if out_fac and fac_path != "TEMPORARY_OUTPUT" else ("TEMPORARY_OUTPUT" if out_fac else None),
                    "drainage": fdr_path if out_fdr and fdr_path != "TEMPORARY_OUTPUT" else ("TEMPORARY_OUTPUT" if out_fdr else None),
                    "basin": "TEMPORARY_OUTPUT" if out_basins else None, # Always temporary because it is polygonized immediately afterwards
                    "GRASS_REGION_PARAMETER": conditioned_dem,
                    "GRASS_REGION_CELLSIZE_PARAMETER": 0
                }
            )

            # Retrieve and load FAC
            if out_fac:
                fac_res = fac_path if fac_path != "TEMPORARY_OUTPUT" else watershed_results.get("accumulation")
                if fac_res:
                    output_layers["fac_layer"] = QgsRasterLayer(fac_res, "Flow_Accumulation_FAC", "gdal")

            # Retrieve and load FDR
            if out_fdr:
                fdr_res = fdr_path if fdr_path != "TEMPORARY_OUTPUT" else watershed_results.get("drainage")
                if fdr_res:
                    output_layers["fdr_layer"] = QgsRasterLayer(fdr_res, "Flow_Direction_FDR", "gdal")

            # Retrieve and polygonize Basins [2]
            if out_basins:
                basins_res = watershed_results.get("basin")
                if basins_res:
                    if progress_callback:
                        progress_callback(50, tr("Polygonizing watersheds..."))
                    poly_res = processing.run(
                        "gdal:polygonize",
                        {
                            "INPUT": basins_res,
                            "BAND": 1,
                            "FIELD": "basin_id",
                            "OUTPUT": basins_path if basins_path != "TEMPORARY_OUTPUT" else "TEMPORARY_OUTPUT"
                        }
                    )
                    poly_final = poly_res.get("OUTPUT")
                    if poly_final:
                        output_layers["basins_layer"] = QgsVectorLayer(poly_final, "Delineated_Basins", "ogr")
                        

        # ── Step 3: Stream network extraction & Topological Ordering ──
        if out_streams:
            if progress_callback:
                progress_callback(70, tr("Extracting stream network..."))
            raw_stream_vector = self._run_stream_extraction(conditioned_dem, threshold_cells)

            if raw_stream_vector and raw_stream_vector.isValid():
                if progress_callback:
                    progress_callback(85, tr("Solving network topology..."))
                ordered_vector = self._solve_topology(raw_stream_vector, conditioned_dem, streams_path)
                output_layers["stream_layer"] = ordered_vector

        if progress_callback:
            progress_callback(100, tr("All desired outputs generated [2]."))

        return output_layers

    # ------------------------------------------------------------------
    # DEM Conditioning (Wang & Liu Fill Depressions with fallback)
    # ------------------------------------------------------------------

    def _fill_sinks(self, dem_layer: QgsRasterLayer) -> QgsRasterLayer:
        """
        Applies pit-filling to guarantee continuous flow paths.
        Uses native QGIS Fill Depressions or falls back to GRASS.
        """
        # Wang & Liu (2006) sink filling. The former id "native:filldepressions"
        # does not exist, so the code always fell through to GRASS (audit M5).
        fill_id = first_available(FILL_SINKS_IDS)
        if fill_id is not None:
            try:
                result = processing.run(fill_id, {
                    "INPUT": dem_layer,
                    "BAND": 1,
                    # Minimum slope (degrees) imposed across filled flats so
                    # that every cell keeps a downslope neighbour.
                    "MIN_SLOPE": 0.01,
                    "OUTPUT_FILLED_DEM": "TEMPORARY_OUTPUT",
                })
                out_path = result.get("OUTPUT_FILLED_DEM")
                if out_path:
                    return QgsRasterLayer(out_path, "dem_filled", "gdal")
            except Exception as error:  # fall back to GRASS below
                print(f"[HydroFlow] {fill_id} failed, trying GRASS r.fill.dir: {error}")

        # Fallback to GRASS r.fill.dir
        try:
            result = processing.run(
                grass_id("r.fill.dir"),
                {
                    "input": dem_layer,
                    "output": "TEMPORARY_OUTPUT",
                    "direction": "TEMPORARY_OUTPUT_DIR"
                }
            )
            out_path = result.get("output")
            if out_path:
                return QgsRasterLayer(out_path, "dem_filled", "gdal")
        except Exception as e:
            print(f"[HydroFlow] DEM conditioning failed, using raw DEM: {e}")
            
        return dem_layer

    # ------------------------------------------------------------------
    # GRASS Stream Extraction Interface
    # ------------------------------------------------------------------

    def _run_stream_extraction(self, dem_layer: QgsRasterLayer, threshold: int) -> QgsVectorLayer:
        """
        Calls GRASS r.stream.extract to perform core channel extraction.
        """
        extent = dem_layer.extent()
        region_param = f"{extent.xMinimum()},{extent.xMaximum()},{extent.yMinimum()},{extent.yMaximum()}"

        try:
            result = processing.run(
                grass_id("r.stream.extract"),
                {
                    "elevation": dem_layer,
                    "threshold": threshold,
                    "stream_vector": "TEMPORARY_OUTPUT",
                    "GRASS_REGION_PARAMETER": region_param,
                    "GRASS_REGION_CELLSIZE_PARAMETER": 0
                }
            )
            vect_path = result.get("stream_vector")
            if vect_path:
                return QgsVectorLayer(vect_path, "raw_streams", "ogr")
        except Exception as e:
            raise RuntimeError(f"GRASS r.stream.extract failed: {e}")

        return None

    # ------------------------------------------------------------------
    # Pure Python Topological Stream Order Solver
    # ------------------------------------------------------------------

    def _solve_topology(self, stream_layer: QgsVectorLayer, dem_layer: QgsRasterLayer, streams_path:str) -> QgsVectorLayer:
        """
        Builds a Directed Acyclic Graph (DAG) from vector segments and calculates
        Strahler stream order, Horton order, and Shreve magnitude in Python [1.13.2].
        """
        reader = RasterReader(dem_layer)
        
        # Setup metric distance calculator
        da = QgsDistanceArea()
        da.setSourceCrs(stream_layer.crs(), QgsProject.instance().transformContext())
        da.setEllipsoid('WGS84')

        # ── Step A: Build Nodes & Edges registries ──
        # Nodes: segment ends rounded to ~1 mm, i.e. 3 decimals in metres but
        # 8 in degrees (3 decimals of a degree is ~100 m and merged distinct
        # confluences in geographic CRS, audit M13).
        decimals = 8 if stream_layer.crs().isGeographic() else 3
        nodes_map: Dict[Tuple[float, float], int] = {}
        node_counter = 0

        def get_node_id(pt: QgsPointXY) -> int:
            nonlocal node_counter
            key = (round(pt.x(), decimals), round(pt.y(), decimals))
            if key not in nodes_map:
                nodes_map[key] = node_counter
                node_counter += 1
            return nodes_map[key]

        edges = [] # list of dicts representing segments

        for feat in stream_layer.getFeatures():
            geom = feat.geometry()
            if geom is None or geom.isEmpty():
                continue

            vertices = [QgsPointXY(v) for v in geom.vertices()]
            if len(vertices) < 2:
                continue

            pt_start = vertices[0]
            pt_end = vertices[-1]

            # Direct segment downstream (from high elevation to low elevation) [1.13.2]
            z_start = reader.sample_at(pt_start.x(), pt_start.y())
            z_end = reader.sample_at(pt_end.x(), pt_end.y())
            
            # Keep the digitised direction when an end has no elevation
            # (comparing with a -9999 placeholder could flip a segment).
            known = not (np.isnan(z_start) or np.isnan(z_end))
            if known and z_start < z_end:
                # Reverse points to ensure downstream orientation
                vertices = vertices[::-1]
                pt_start, pt_end = pt_end, pt_start
                z_start, z_end = z_end, z_start

            u_id = get_node_id(pt_start) # Upstream node
            v_id = get_node_id(pt_end)   # Downstream node

            # Length ALONG the segment (the straight start-end chord
            # underestimated sinuous reaches and overestimated their slope).
            length_m = da.measureLength(geom)
            drop = abs(z_start - z_end) if known else 0.0
            slope_mean = drop / length_m if length_m > 0 else 0.0

            edges.append({
                "feat_id": feat.id(),
                "u": u_id,
                "v": v_id,
                "geom": geom,
                "length_m": length_m,
                "slope_mean": slope_mean,
                "strahler": None,
                "shreve": None,
                "horton": None
            })

        # ── Step B: Resolve Connectivity ──
        # Map node -> incoming edges / outgoing edges
        incoming: Dict[int, List[int]] = {i: [] for i in range(node_counter)}
        outgoing: Dict[int, List[int]] = {i: [] for i in range(node_counter)}

        for edge_idx, edge in enumerate(edges):
            incoming[edge["v"]].append(edge_idx)
            outgoing[edge["u"]].append(edge_idx)

        # ── Step C: Compute Strahler Order & Shreve Magnitude ──
        # Kahn's topological order over the edges: an edge is processed once
        # all edges flowing into its upstream node are done. Each edge is
        # visited once (O(E)); the former loop rescanned every edge at each
        # pass (O(E²), audit M13).
        waiting = [len(incoming[edge["u"]]) for edge in edges]
        ready = deque(i for i, count in enumerate(waiting) if count == 0)
        while ready:
            edge_idx = ready.popleft()
            edge = edges[edge_idx]
            upstream_edges = incoming[edge["u"]]
            if not upstream_edges:
                edge["strahler"] = 1
                edge["shreve"] = 1
            else:
                upstream_strahlers = [edges[up]["strahler"] for up in upstream_edges]
                max_s = max(upstream_strahlers)
                # Two or more tributaries of the highest order raise it by one.
                edge["strahler"] = max_s + 1 if upstream_strahlers.count(max_s) >= 2 else max_s
                edge["shreve"] = sum(edges[up]["shreve"] for up in upstream_edges)
            for down_idx in outgoing[edge["v"]]:
                waiting[down_idx] -= 1
                if waiting[down_idx] == 0:
                    ready.append(down_idx)

        # Edges never reached belong to cycles (digitising or orientation
        # errors): they get order 1 so the layer stays complete.
        for edge in edges:
            if edge["strahler"] is None:
                edge["strahler"] = 1
                edge["shreve"] = 1

        # ── Step D: Compute Horton Order (Branch extension) ──
        # Horton ordering gives the trunk stream's order to its whole length
        # up to its main source: from every outlet, walk upstream along the
        # highest-order (then longest) branch. Iterative, so very long trunks
        # cannot exceed Python's recursion limit.
        for edge in edges:
            edge["horton"] = edge["strahler"]

        outlets = [n_id for n_id, out_list in outgoing.items() if not out_list]
        for outlet_node in outlets:
            for root_edge_idx in incoming[outlet_node]:
                current = root_edge_idx
                visited = {current}
                while True:
                    candidates = incoming[edges[current]["u"]]
                    if not candidates:
                        break
                    best = max(candidates, key=lambda idx: (edges[idx]["strahler"], edges[idx]["length_m"]))
                    if best in visited:
                        break
                    edges[best]["horton"] = edges[current]["horton"]
                    visited.add(best)
                    current = best

        # ── Step E: Assemble output QgsVectorLayer ──
        output_fields = QgsFields()
        output_fields.append(QgsField("segment_id", QVariant.Int))
        output_fields.append(QgsField("strahler", QVariant.Int))
        output_fields.append(QgsField("shreve", QVariant.Int))
        output_fields.append(QgsField("horton", QVariant.Int))
        output_fields.append(QgsField("length_m", QVariant.Double))
        output_fields.append(QgsField("slope_mean", QVariant.Double))

        ordered_layer = QgsVectorLayer(
            f"LineString?crs={stream_layer.crs().authid()}",
            "ordered_streams",
            "memory"
        )
        ordered_layer.startEditing()
        ordered_layer.dataProvider().addAttributes(output_fields)
        ordered_layer.updateFields()

        features_to_add = []
        for edge_idx, edge in enumerate(edges):
            feat = QgsFeature(ordered_layer.fields())
            feat.setGeometry(edge["geom"])
            feat.setAttributes([
                edge_idx,
                edge["strahler"],
                edge["shreve"],
                edge["horton"],
                round(edge["length_m"], 2),
                round(edge["slope_mean"], 6)
            ])
            features_to_add.append(feat)

        ordered_layer.dataProvider().addFeatures(features_to_add)
        ordered_layer.commitChanges()

        if streams_path != "TEMPORARY_OUTPUT":
            options = QgsVectorFileWriter.SaveVectorOptions()
            options.driverName = "GPKG" if streams_path.endswith(".gpkg") else "ESRI Shapefile"
            
            QgsVectorFileWriter.writeAsVectorFormatV3(
                ordered_layer,
                streams_path,
                QgsProject.instance().transformContext(),
                options
            )
            # Reload the layer from its final storage location to display it
            ordered_layer = QgsVectorLayer(streams_path, "ordered_streams", "ogr")


        return ordered_layer
