# tools/digitizer/engine.py

"""
tools/digitizer/engine.py

DigitizerEngine — orchestrates the semi-automatic geological map digitization.

PIPELINE
--------
1. Intersect user Polygon (study area) with Raster (geological map).
2. Extract RGB bands as a NumPy array.
3. Run NumPy K-Means clustering to group similar colors.
4. Apply morphological smoothing to remove scanned map artifacts.
5. Polygonize the resulting categorical raster into QgsGeometries.
6. Clip the polygons strictly to the study area boundary.
7. Return structured data to the UI.
"""

import numpy as np # type: ignore
from osgeo import gdal, ogr # type: ignore
from typing import Optional

from qgis.core import ( # type: ignore
    QgsRasterLayer, QgsVectorLayer, QgsGeometry, 
    QgsCoordinateTransform, QgsProject
)
from PyQt5.QtCore import QCoreApplication # type: ignore

from ...base.base_engine import BaseEngine
from ...core.raster import RasterReader
from ...core.digitizer import (
    kmeans_image_segmentation, 
    clean_segmentation_noise, 
    # watershed_image_segmentation,
    preprocess_geological_map,
    # find_seeds,
    # region_growing,
    # merge_small_regions,
    # get_region_colors,
    clean_segmentation_noise,
    rgb_to_hsv_array
)


def tr(message: str) -> str:
    return QCoreApplication.translate("RockMorph", message)

 
def _hsv_centroids_to_rgb(centroids_hsv: np.ndarray) -> np.ndarray:
    """
    Convert K centroid vectors from HSV uint8 back to RGB uint8 for UI display.
    H in [0,179], S,V in [0,255].
    """
    k = centroids_hsv.shape[0]
    rgb_out = np.zeros((k, 3), dtype=np.uint8)
 
    for i in range(k):
        h = centroids_hsv[i, 0] / 179.0 * 360.0
        s = centroids_hsv[i, 1] / 255.0
        v = centroids_hsv[i, 2] / 255.0
 
        c = v * s
        x = c * (1.0 - abs((h / 60.0) % 2.0 - 1.0))
        m = v - c
 
        if   h < 60:  r, g, b = c, x, 0
        elif h < 120: r, g, b = x, c, 0
        elif h < 180: r, g, b = 0, c, x
        elif h < 240: r, g, b = 0, x, c
        elif h < 300: r, g, b = x, 0, c
        else:         r, g, b = c, 0, x
 
        rgb_out[i] = [
            int((r + m) * 255),
            int((g + m) * 255),
            int((b + m) * 255),
        ]
 
    return rgb_out


class DigitizerEngine(BaseEngine):
    """
    Engine for extracting geological features from RGB maps using color clustering.
    """
    
    def validate(self, **kwargs) -> bool:
        raster = kwargs.get("raster_layer")
        poly = kwargs.get("polygon_layer")
        
        if not isinstance(raster, QgsRasterLayer) or not raster.isValid():
            return False
        # layer polygon_layer is optional — if user doesn't provide one, we process the whole raster extent
        if poly is not None:
            if not isinstance(poly, QgsVectorLayer) or not poly.isValid():
                return False
            # accept line and polygone
            if poly.geometryType() not in [1, 2]: 
                return False
                
        return True

    def computeSSSS(self, **kwargs) -> dict:
        """
        Executes the digitization pipeline.
        """
        raster_layer = kwargs["raster_layer"]
        poly_layer   = kwargs["polygon_layer"]
        n_clusters   = kwargs.get("n_clusters", 8)
        smooth_size  = kwargs.get("smooth_size", 5)
        sieve_size   = kwargs.get("sieve_threshold", 5000)
        max_iter      = kwargs.get("max_iter",      30)
        subsample_pct = kwargs.get("subsample_pct", 30)
        progress_cb  = kwargs.get("progress_callback")

        def _progress(pct: int, msg: str):
            if progress_cb: progress_cb(pct, msg)

        # ── Step 1: Get Bounding Box of the Polygon ─────────────────────
        _progress(10, tr("Extracting study area..."))
        
        # Reproject polygon to raster CRS to ensure exact clipping
        if poly_layer is not None:
            mask_geom = self._get_mask_geometry(poly_layer, raster_layer.crs())
            if not mask_geom:
                raise ValueError(tr("The boundary layer has no valid geometry."))
            bbox = mask_geom.boundingBox()
        else:
            # Full extent mode
            bbox = raster_layer.extent()
            mask_geom = QgsGeometry.fromRect(bbox)
        
        # ── Step 2: Read Raster inside Bounding Box ─────────────────────
        _progress(20, tr("Reading raster pixels..."))
        
        # Open raster with GDAL
        ds = gdal.Open(raster_layer.source())
        if not ds:
            raise ValueError(tr("Failed to open raster layer with GDAL."))
            
        # PRO FIX: Get GeoTransform directly from QGIS layer instead of raw GDAL dataset
        # because the user might have forced a custom CRS/Extent in QGIS!
        extent = raster_layer.extent()
        width = raster_layer.width()
        height = raster_layer.height()

        pixel_width = extent.width() / width
        pixel_height = extent.height() / height
        
        # Standard GDAL geotransform format from QGIS context
        gt = (extent.xMinimum(), pixel_width, 0.0, extent.yMaximum(), 0.0, -pixel_height)
        inv_gt = gdal.InvGeoTransform(gt)
        
        # Convert bounding box coordinates to pixel offsets
        # Use min/max properly to avoid negative sizes if Y axis is inverted
        px_x1, px_y1 = gdal.ApplyGeoTransform(inv_gt, bbox.xMinimum(), bbox.yMinimum())
        px_x2, px_y2 = gdal.ApplyGeoTransform(inv_gt, bbox.xMaximum(), bbox.yMaximum())
        
        px_min_x = min(px_x1, px_x2)
        px_max_x = max(px_x1, px_x2)
        px_min_y = min(px_y1, px_y2)
        px_max_y = max(px_y1, px_y2)
        
        x_off = int(px_min_x)
        y_off = int(px_min_y)
        x_size = int(px_max_x - px_min_x)
        y_size = int(px_max_y - px_min_y)
        
        # Security bounds to avoid reading outside the raster
        x_off = max(0, x_off)
        y_off = max(0, y_off)
        
        # Adjust size if it goes beyond raster dimensions
        x_size = min(x_size, ds.RasterXSize - x_off)
        y_size = min(y_size, ds.RasterYSize - y_off)

        if x_size <= 0 or y_size <= 0:
            raise ValueError(tr("The polygon is outside the raster extent."))

        # PRO FIX: Handle Color Table (Paletted rasters) commonly used in geological maps
        band1 = ds.GetRasterBand(1)
        color_table = band1.GetColorTable()
        
        img_rgb = np.zeros((y_size, x_size, 3), dtype=np.uint8)
        
        if color_table is not None:
            # It's an indexed color map (8-bit paletted) -> Convert to RGB
            index_array = band1.ReadAsArray(x_off, y_off, x_size, y_size)
            ct_count = color_table.GetCount()
            lut = np.zeros((256, 3), dtype=np.uint8)
            for i in range(ct_count):
                entry = color_table.GetColorEntry(i)
                lut[i] = [entry[0], entry[1], entry[2]]
            
            # Fast mapping of indices to RGB colors using NumPy
            img_rgb = lut[index_array]
        else:
            # Read normal RGB or Grayscale
            bands_to_read = min(3, ds.RasterCount)
            for i in range(bands_to_read):
                band = ds.GetRasterBand(i + 1)
                img_rgb[:, :, i] = band.ReadAsArray(x_off, y_off, x_size, y_size)
                
            # Handle grayscale maps by duplicating the single channel
            if bands_to_read == 1:
                img_rgb[:, :, 1] = img_rgb[:, :, 0]
                img_rgb[:, :, 2] = img_rgb[:, :, 0]

        # ── Step 3: K-Means Segmentation ──────────────────────────────
        # algorithm = kwargs.get("algorithm", "watershed") # On met watershed par défaut pour le test
        # if algorithm == "kmeans":
        #     _progress(40, tr(f"Running Pure K-Means (k={n_clusters})..."))
        #     labels, centroids = kmeans_image_segmentation(
        #         img_rgb,
        #         k=n_clusters,
        #         max_iter=max_iter,
        #         subsample_pct=subsample_pct,
        #     )
        # else:
        #     _progress(40, tr(f"Running Watershed+Colors (k={n_clusters})..."))
        #     labels, centroids = watershed_image_segmentation(
        #         img_rgb,
        #         k=n_clusters,
        #         line_threshold=15.0, # Ajustable plus tard dans l'UI si besoin
        #         closing_size=3
        #     )

        _progress(30, tr("Pre-filtering text and grid lines..."))
        img_rgb_clean = preprocess_geological_map(img_rgb, filter_size=7)
 
        _progress(35, tr("Converting to HSV colorspace..."))
        img_hsv = rgb_to_hsv_array(img_rgb_clean)
 
        _progress(40, tr(f"Running K-Means in HSV space (k={n_clusters})..."))
        labels, centroids_hsv = kmeans_image_segmentation(
            img_hsv,
            k=n_clusters,
            max_iter=max_iter,
            subsample_pct=subsample_pct,
        )
 
        # Convert HSV centroids back to RGB for UI display
        centroids = _hsv_centroids_to_rgb(centroids_hsv)
        

        # ── Step 4: Smoothing (Morphological filter) ──────────────────
        _progress(70, tr("Applying morphological cleaning..."))
        labels = clean_segmentation_noise(labels, smooth_size=smooth_size)

        # ── Step 5: Polygonization (GDAL) & Clipping ──────────────────
        _progress(85, tr("Vectorizing geological units..."))
        
        # Create a new local geotransform for the clipped window
        new_gt = list(gt)
        new_gt[0] = gt[0] + x_off * gt[1]
        new_gt[3] = gt[3] + y_off * gt[5]
        
        polygons = self._polygonize_labels(labels, tuple(new_gt), mask_geom, sieve_size)

        # Format colors to Hex strings for UI and Map Layer styling
        hex_colors =["#{:02x}{:02x}{:02x}".format(c[0], c[1], c[2]) for c in centroids]

        _progress(100, tr("Digitization complete."))

        return {
            "polygons": polygons,
            "colors": hex_colors,
            "crs_wkt": raster_layer.crs().toWkt(),
            "n_clusters": n_clusters
        }



    def _get_mask_geometry(self, vector_layer: QgsVectorLayer, target_crs) -> Optional[QgsGeometry]:
        """
        Extracts the first feature from the polygon or line layer and reprojects it if necessary.
        If it's a line, dynamically closes it to form a Polygon.
        """
        for feat in vector_layer.getFeatures():
            geom = feat.geometry()
            
            # Reprojection if necessary
            if vector_layer.crs() != target_crs:
                xform = QgsCoordinateTransform(vector_layer.crs(), target_crs, QgsProject.instance())
                geom.transform(xform)
                
            # If it's a Line (GeometryType == 1), convert to Polygon
            if vector_layer.geometryType() == 1:
                if geom.isMultipart():
                    line_pts = geom.asMultiPolyline()[0]
                else:
                    line_pts = geom.asPolyline()
                    
                # Close the line to make a valid polygon ring
                if line_pts and line_pts[0] != line_pts[-1]:
                    line_pts.append(line_pts[0])
                    
                geom = QgsGeometry.fromPolygonXY([line_pts])
                
            return geom
            
        return None

    def _polygonize_labels(self, label_array: np.ndarray, geo_transform: tuple, mask_geom: QgsGeometry, sieve_threshold: int) -> list[dict]:
        """
        Converts the NumPy array of labels into QgsGeometry polygons.
        Intersects the resulting polygons with the exact user mask boundary
        to ensure no pixels spill over the study area polygon.
        """
        rows, cols = label_array.shape
        driver = gdal.GetDriverByName("MEM")
        ds = driver.Create("", cols, rows, 1, gdal.GDT_Int32)
        ds.SetGeoTransform(geo_transform)

        band = ds.GetRasterBand(1)
        band.WriteArray(label_array)

        # ✨ LE COUP DE GÉNIE EST ICI ✨
        # On demande à GDAL d'absorber tous les micro-polygones (texte, grilles, bruit)
        if sieve_threshold > 0:
            # Paramètres : srcBand, maskBand, dstBand, sizeThreshold, connectedness (8 = pixels diagonaux inclus)
            gdal.SieveFilter(band, None, band, sieve_threshold, 8)

        # Create memory layer for ogr
        drv = ogr.GetDriverByName("Memory")
        vds = drv.CreateDataSource("out")
        layer = vds.CreateLayer("polygonized", geom_type=ogr.wkbMultiPolygon)
        field_def = ogr.FieldDefn("cluster_id", ogr.OFTInteger)
        layer.CreateField(field_def)

        # Execute GDAL Polygonize
        gdal.Polygonize(band, None, layer, 0,[], callback=None)

        results =[]
        
        for feat in layer:
            cluster_id = feat.GetField("cluster_id")
            geom_ogr = feat.GetGeometryRef()
            if geom_ogr is None:
                continue
                
            # Convert OGR geometry to WKT, then to QgsGeometry
            wkt = geom_ogr.ExportToWkt()
            geom_qgis = QgsGeometry.fromWkt(wkt)
            
            # Clip strictly to the user's study area polygon
            if geom_qgis.intersects(mask_geom):
                clipped_geom = geom_qgis.intersection(mask_geom)
                
                # Check if intersection is valid and not just a touching line/point
                if not clipped_geom.isEmpty() and clipped_geom.type() == 2: # 2 = Polygon
                    results.append({
                        "cluster_id": cluster_id,
                        "geometry": clipped_geom
                    })
                    
        # Clean up GDAL memory
        band = None
        ds = None
        vds = None
        
        return results



    def compute(self, **kwargs) -> dict:
        raster_layer    = kwargs["raster_layer"]
        poly_layer      = kwargs["polygon_layer"]
        color_tolerance = kwargs.get("color_tolerance", 15.0) # NEW! Replaces n_clusters
        sieve_threshold = kwargs.get("sieve_threshold", 5000)
        progress_cb     = kwargs.get("progress_callback")
 
        def _p(pct, msg):
            if progress_cb: progress_cb(pct, msg)
 
        # ── 1. Study area ─────────────────────────────────────────────
        _p(5, tr("Extracting study area..."))
        if poly_layer is not None:
            mask_geom = self._get_mask_geometry(poly_layer, raster_layer.crs())
            if not mask_geom:
                raise ValueError(tr("Boundary layer has no valid geometry."))
            bbox = mask_geom.boundingBox()
        else:
            bbox = raster_layer.extent()
            mask_geom = QgsGeometry.fromRect(bbox)
 
        # ── 2. Read raster ────────────────────────────────────────────
        _p(10, tr("Reading raster pixels..."))
        ds = gdal.Open(raster_layer.source())
        if not ds:
            raise ValueError(tr("Cannot open raster with GDAL."))
 
        extent = raster_layer.extent()
        pw = extent.width()  / raster_layer.width()
        ph = extent.height() / raster_layer.height()
        gt     = (extent.xMinimum(), pw, 0.0, extent.yMaximum(), 0.0, -ph)
        inv_gt = gdal.InvGeoTransform(gt)
 
        x1, y1 = gdal.ApplyGeoTransform(inv_gt, bbox.xMinimum(), bbox.yMinimum())
        x2, y2 = gdal.ApplyGeoTransform(inv_gt, bbox.xMaximum(), bbox.yMaximum())
        x_off  = max(0, int(min(x1, x2)))
        y_off  = max(0, int(min(y1, y2)))
        x_size = min(int(abs(x2 - x1)), ds.RasterXSize - x_off)
        y_size = min(int(abs(y2 - y1)), ds.RasterYSize - y_off)
 
        if x_size <= 0 or y_size <= 0:
            raise ValueError(tr("Polygon is outside raster extent."))
 
        band1 = ds.GetRasterBand(1)
        ct    = band1.GetColorTable()
        img_rgb = np.zeros((y_size, x_size, 3), dtype=np.uint8)
 
        if ct is not None:
            idx = band1.ReadAsArray(x_off, y_off, x_size, y_size)
            lut = np.array([[ct.GetColorEntry(i)[j] for j in range(3)]
                            for i in range(ct.GetCount())], dtype=np.uint8)
            img_rgb = lut[idx]
        else:
            nb = min(3, ds.RasterCount)
            for i in range(nb):
                img_rgb[:, :, i] = ds.GetRasterBand(i+1).ReadAsArray(
                    x_off, y_off, x_size, y_size)
            if nb == 1:
                img_rgb[:, :, 1] = img_rgb[:, :, 2] = img_rgb[:, :, 0]
 
        # ── 3. Run Advanced Auto-Pipeline ─────────────────────────────
        _p(30, tr("Executing Object-Based Image Analysis (Superpixels)..."))
        from ...core.digitizer import run_obia_digitization
        
        # Le seuil standard pour Ward Linkage se situe souvent entre 50 et 200.
        label_map, cluster_colors_rgb = run_obia_digitization(img_rgb, tolerance=color_tolerance)
 
        # ── 3. Polygonize ─────────────────────────────────────────────
        _p(85, tr("Vectorising geological units..."))
        new_gt = list(gt)
        new_gt[0] = gt[0] + x_off * gt[1]
        new_gt[3] = gt[3] + y_off * gt[5]
        
        polygons = self._polygonize_labels(label_map, tuple(new_gt), mask_geom, sieve_threshold)
 
        # ── 5. Colors for UI ─────────────────────────────────────────
        hex_colors = {
            i: "#{:02x}{:02x}{:02x}".format(*rgb)
            for i, rgb in enumerate(cluster_colors_rgb)
        }
 
        # Attach hex color to each polygon
        for poly in polygons:
            cid = poly["cluster_id"]
            poly["hex_color"] = hex_colors.get(cid, "#888888")
 
        _p(100, tr(f"Digitization complete. Discovered {len(hex_colors)} formations."))
 
        return {
            "polygons":    polygons,
            "colors_dict": hex_colors,
            "colors":      list(hex_colors.values()), 
            "crs_wkt":     raster_layer.crs().toWkt(),
            "n_clusters":  len(hex_colors),
        }