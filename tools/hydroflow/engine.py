"""
tools/hydroflow/engine.py — HydroFlow Computational Engine

Calculates flow routing and extracts topologically ordered stream networks 
(Strahler, Horton, Shreve) using a hybrid GRASS and Python Graph Solver.

Additions ported from GSV-Studio (core/indices/routing.py, pure NumPy):
  * stream burning of a mapped river network before filling and routing;
  * HAND, height above the nearest drainage (Rennó et al., 2008);
  * dispersive accumulation MFD (Freeman, 1991) and D∞ (Tarboton, 1997);
  * reach steepness k_sn = S · A^θ on every segment of the ordered network
    (area from a NumPy D8 accumulation of the same conditioned DEM).

Authors: RockMorph contributors / Tony winter
"""

import os
import math
import tempfile
import uuid
from collections import deque

import numpy as np  # type: ignore
from typing import Dict, Any, List, Tuple

from qgis.core import (  # type: ignore
    QgsRasterLayer, QgsVectorLayer, QgsProject, QgsCoordinateReferenceSystem,
    QgsCoordinateTransformContext,
    QgsCoordinateTransform, QgsFeature, QgsGeometry,
    QgsPointXY, QgsWkbTypes, QgsField, QgsFields,
    QgsDistanceArea, QgsProcessingFeedback,
    QgsVectorFileWriter
)
from qgis.PyQt.QtCore import QCoreApplication, QVariant  # type: ignore
import processing  # type: ignore

from ...base.base_engine import BaseEngine
from ...core.processing_ids import FILL_SINKS_IDS, first_available, grass_id
from ...core.raster import RasterReader, rasterize_geometries, write_geotiff
from ...core.indices.flow import d8_receivers, flow_accumulation, priority_flood_fill
from ...core.indices.routing import (
    burn_streams, dinf_accumulation, height_above_drainage, mfd_accumulation, reach_ksn,
)


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

        # Optional NumPy outputs (core/indices/routing.py)
        burn_layer = kwargs.get("burn_layer")
        burn_depth = float(kwargs.get("burn_depth_m", 10.0))
        out_hand = kwargs.get("out_hand", False)
        out_mfd = kwargs.get("out_mfd", False)
        out_dinf = kwargs.get("out_dinf", False)
        network_ksn = kwargs.get("network_ksn", False) and out_streams
        ksn_theta = float(kwargs.get("ksn_theta", 0.45))

        output_layers = {}

        # ── Step 0: Stream burning of a mapped network ──
        routing_dem = dem_layer
        burn_mask = None
        if burn_layer is not None and burn_layer.isValid() and burn_layer.featureCount() > 0:
            if progress_callback:
                progress_callback(5, tr("Burning the mapped river network into the DEM..."))
            routing_dem, burn_mask = self._burn_network(dem_layer, burn_layer, burn_depth)

        # ── Step 1: Pre-conditioning (Pit filling) ──
        conditioned_dem = routing_dem
        if fill_depressions:
            if progress_callback:
                progress_callback(10, tr("Conditioning DEM (filling depressions)..."))
            conditioned_dem = self._fill_sinks(routing_dem)

        # ── Step 1b: NumPy fields (HAND, MFD, D∞, area for k_sn) ──
        area_info = None
        if out_hand or out_mfd or out_dinf or network_ksn:
            area_info = self._numpy_fields(
                dem_layer, burn_mask, burn_depth, threshold_cells,
                out_hand, out_mfd, out_dinf, kwargs, output_layers, progress_callback)

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
                ordered_vector = self._solve_topology(
                    raw_stream_vector, conditioned_dem, streams_path,
                    area_info=area_info if network_ksn else None, ksn_theta=ksn_theta)
                output_layers["stream_layer"] = ordered_vector

        if progress_callback:
            progress_callback(100, tr("All desired outputs generated [2]."))

        return output_layers

    # ------------------------------------------------------------------
    # Stream burning and NumPy hydrological fields
    # ------------------------------------------------------------------

    @staticmethod
    def _dem_array(dem_layer: QgsRasterLayer):
        """(reader, float64 elevations with NaN for NoData, dx, dy in metres)."""
        reader = RasterReader(dem_layer)
        z = reader.array.astype(np.float64, copy=True)
        if reader.nodata is not None:
            z[z == reader.nodata] = np.nan
        px, py = abs(reader.pixel_size_x), abs(reader.pixel_size_y)
        if dem_layer.crs().isGeographic():
            extent = dem_layer.extent()
            lat = math.radians((extent.yMinimum() + extent.yMaximum()) / 2.0)
            px, py = px * 111320.0 * math.cos(lat), py * 110574.0
        return reader, z, px, py

    def _burn_network(self, dem_layer: QgsRasterLayer, burn_layer: QgsVectorLayer, depth_m: float):
        """
        Lowers the cells crossed by ``burn_layer`` by ``depth_m`` (stream
        burning, Hutchinson 1989 / Saunders 1999) and writes the burned DEM
        to a temporary GeoTIFF.

        Returns (burned QgsRasterLayer, boolean mask of the burned cells).
        """
        reader, z, _dx, _dy = self._dem_array(dem_layer)
        transform = QgsCoordinateTransform(burn_layer.crs(), dem_layer.crs(), QgsCoordinateTransformContext())
        wkbs = []
        for feature in burn_layer.getFeatures():
            geometry = QgsGeometry(feature.geometry())
            if geometry.isEmpty():
                continue
            if burn_layer.crs() != dem_layer.crs():
                geometry.transform(transform)
            wkbs.append(geometry.asWkb())
        mask = rasterize_geometries(wkbs, z.shape, reader.geo_transform, dem_layer.crs().toWkt())
        mask &= np.isfinite(z)
        burned = burn_streams(z, mask, depth_m)
        path = os.path.join(tempfile.gettempdir(), f"burned_dem_{uuid.uuid4().hex[:8]}.tif")
        write_geotiff(burned, path, reader.geo_transform, dem_layer.crs().toWkt())
        return QgsRasterLayer(path, "dem_burned", "gdal"), mask

    def _numpy_fields(self, dem_layer, burn_mask, burn_depth, threshold_cells,
                      out_hand, out_mfd, out_dinf, kwargs, output_layers, progress_callback):
        """
        Priority-Flood + ε fill, D8 routing and the requested NumPy fields.

        The DEM is burned (if a network was given) and filled in NumPy so
        that HAND, MFD, D∞ and the reach areas all come from one consistent
        conditioned surface. HAND is measured on the original elevations.
        Writes the rasters into ``output_layers`` and returns the area grid
        used for the reach k_sn: {"acc_m2", "reader"}.
        """
        def report(value, text):
            if progress_callback:
                progress_callback(value, text)

        reader, z, dx, dy = self._dem_array(dem_layer)
        route = burn_streams(z, burn_mask, burn_depth) if burn_mask is not None else z
        valid = np.isfinite(route)
        report(12, tr("Filling depressions (Priority-Flood, NumPy)..."))
        filled = priority_flood_fill(route, valid)
        report(16, tr("D8 routing and accumulation (NumPy)..."))
        receiver = d8_receivers(filled, valid, math.sqrt(dx * dy))
        acc_cells = flow_accumulation(filled, receiver, valid)
        wkt = dem_layer.crs().toWkt()
        gt = reader.geo_transform

        def out_path(key, stem):
            path = kwargs.get(key, "TEMPORARY_OUTPUT")
            if not path or path == "TEMPORARY_OUTPUT":
                path = os.path.join(tempfile.gettempdir(), f"{stem}_{uuid.uuid4().hex[:8]}.tif")
            return path

        if out_hand:
            report(18, tr("Height above the nearest drainage (HAND)..."))
            streams = acc_cells >= threshold_cells
            if burn_mask is not None:
                streams |= burn_mask
            hand = height_above_drainage(z, receiver, streams & valid)
            path = write_geotiff(hand, out_path("hand_path", "hand"), gt, wkt)
            output_layers["hand_layer"] = QgsRasterLayer(path, "HAND_m", "gdal")
        if out_mfd:
            report(20, tr("Dispersive accumulation MFD (Freeman 1991)..."))
            mfd = mfd_accumulation(filled, dx, dy)
            path = write_geotiff(mfd, out_path("mfd_path", "acc_mfd"), gt, wkt)
            output_layers["mfd_layer"] = QgsRasterLayer(path, "Accumulation_MFD_m2", "gdal")
        if out_dinf:
            report(23, tr("Dispersive accumulation D-infinity (Tarboton 1997)..."))
            dinf = dinf_accumulation(filled, dx, dy)
            path = write_geotiff(dinf, out_path("dinf_path", "acc_dinf"), gt, wkt)
            output_layers["dinf_layer"] = QgsRasterLayer(path, "Accumulation_Dinf_m2", "gdal")
        return {"acc_m2": acc_cells * dx * dy, "reader": reader}

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

    def _solve_topology(self, stream_layer: QgsVectorLayer, dem_layer: QgsRasterLayer, streams_path:str,
                        area_info: dict = None, ksn_theta: float = 0.45) -> QgsVectorLayer:
        """
        Builds a Directed Acyclic Graph (DAG) from vector segments and calculates
        Strahler stream order, Horton order, and Shreve magnitude in Python [1.13.2].

        With ``area_info`` ({"acc_m2", "reader"} from ``_numpy_fields``) every
        segment also gets its drainage area and its steepness
        k_sn = S · A^θ (``ksn_theta``), the area being read at 90 % of the
        segment length downstream (largest value of the 3 × 3 cells, so a
        line drawn half a cell off the channel still finds it, while the
        confluence cell below is avoided).
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

            area_m2, ksn = None, None
            if area_info is not None:
                area_m2 = self._reach_area(QgsGeometry.fromPolylineXY(vertices), area_info)
                if area_m2 is not None and known:
                    ksn = reach_ksn(z_start, z_end, length_m, area_m2, ksn_theta)

            edges.append({
                "area_m2": area_m2,
                "ksn": ksn,
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
        if area_info is not None:
            output_fields.append(QgsField("area_km2", QVariant.Double))
            output_fields.append(QgsField("ksn", QVariant.Double))

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
            attributes = [
                edge_idx,
                edge["strahler"],
                edge["shreve"],
                edge["horton"],
                round(edge["length_m"], 2),
                round(edge["slope_mean"], 6)
            ]
            if area_info is not None:
                attributes.append(round(edge["area_m2"] / 1e6, 4) if edge["area_m2"] else None)
                attributes.append(round(edge["ksn"], 2) if edge["ksn"] is not None else None)
            feat.setAttributes(attributes)
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

    @staticmethod
    def _reach_area(oriented: QgsGeometry, area_info: dict):
        """Drainage area (m²) near the downstream end of an oriented reach."""
        reader = area_info["reader"]
        acc = area_info["acc_m2"]
        point = oriented.interpolate(0.9 * oriented.length()).asPoint()
        col, row = reader.world_to_pixel(point.x(), point.y())
        if col < 0:
            return None
        window = acc[max(0, row - 1):row + 2, max(0, col - 1):col + 2]
        finite = window[np.isfinite(window)]
        return float(finite.max()) if finite.size else None
