# tools/rose/engine.py

from qgis.PyQt.QtCore import QCoreApplication # type: ignore
from qgis.core import QgsWkbTypes,QgsGeometry  # type: ignore
import numpy as np # type: ignore
from ...base.base_engine import BaseEngine
import math

def tr(message):
    return QCoreApplication.translate("RockMorph", message)


class RoseEngine(BaseEngine):
    """
    Computes directional statistics from a line vector layer.
    Returns pure data dict — no UI logic.
    """

    def validate(self, **kwargs) -> bool:
        layer = kwargs.get("layer")
        if layer is None:
            return False
        if not QgsWkbTypes.isMultiType(layer.wkbType()) and \
           layer.geometryType() != QgsWkbTypes.LineGeometry:
            return False
        return True

    def compute(self, **kwargs) -> dict:
        """
        Parameters
        ----------
        layer       : QgsVectorLayer  — input line layer
        n_sectors   : int             — number of sectors (8-72)
        mode        : str             — 'count' | 'length' | 'frequency'
        half_rose   : bool            — True = 0-180°, False = 0-360°
        color       : str             — hex color for petals
        opacity     : float           — 0.0 to 1.0
        show_grid   : bool            — show radial grid
        title       : str             — plot title
        """
        layer     = kwargs["layer"]
        n_sectors = kwargs.get("n_sectors", 36)
        mode      = kwargs.get("mode", "length")
        half_rose = kwargs.get("half_rose", False)
        axial_sym     = kwargs.get("axial_symmetry", True) # Default to True for Geology
        densify_dist  = kwargs.get("densify_dist", 0.0)    # Distance in meters
        min_length    = kwargs.get("min_length", 0.0)      # Filter small segments
        color     = kwargs.get("color", "#4a9eff")
        opacity   = kwargs.get("opacity", 0.85)
        show_grid = kwargs.get("show_grid", True)
        show_labels = kwargs.get("show_labels", False)
        title     = kwargs.get("title", "Rose Diagram")
        min_rectitude = kwargs.get("min_rectitude", 0.0)

        # 1. Extraction with Densification and Filtering
        azimuths, lengths = self._extract_segments(layer, densify_dist, min_length, min_rectitude)

        if len(azimuths) == 0:
            raise ValueError(tr("No valid segments found in layer."))

        # Fold to 0-180° for half rose
        # 2. Axial Symmetry (The "Butterfly" effect)
        # We add 180° to every measurement to treat lines as axes, not vectors
        if axial_sym and not half_rose:
            sym_azimuths = (azimuths + 180.0) % 360.0
            azimuths = np.concatenate([azimuths, sym_azimuths])
            lengths = np.concatenate([lengths, lengths])

        # 3. Binning
        if half_rose:
            azimuths = azimuths % 180.0
            max_angle = 180.0
        else:
            max_angle = 360.0

        sector_width = max_angle / n_sectors
        bins = np.arange(0, max_angle + sector_width, sector_width)
        
        values = self._bin_values(azimuths, lengths, bins, mode)
        bin_centers = bins[:-1] + sector_width / 2.0

        # 4. Statistics
        stats = self._compute_stats(azimuths, lengths, bin_centers, values)

        return {
            "azimuths":     bin_centers.tolist(),
            "values":       values.tolist(),
            "sector_width": [sector_width] * len(values),
            "color":        color,
            "opacity":      opacity,
            "show_grid":    show_grid,
            "show_labels":  show_labels,
            "title":        title,
            "stats":        stats,
            "mode":         mode, 
        }

    # ------------------------------------------------------------------
    # Private helpers
    # ------------------------------------------------------------------

    def _extract_segments(self, layer, densify_dist: float, min_length: float, min_rectitude: float) -> tuple:
        """
        Iterates over all features and extracts segment azimuths and lengths.
        Filters by rectitude at feature level, then extracts
        individual segments vertex-by-vertex.
        Returns two numpy arrays: azimuths (degrees), lengths (map units).
        """
        azimuths = []
        lengths  = []

        for feature in layer.getFeatures():
            geom = feature.geometry()
            if geom is None or geom.isEmpty():
                continue

            # Rectitude filter at feature level
            if min_rectitude > 0.0:
                if self._calculate_rectitude(geom ) < min_rectitude:
                    continue

                # Process each part of the geometry
            for part in geom.constParts():
                nodes = list(part.vertices())
                if len(nodes) < 2:
                    continue
                for i in range(len(nodes) - 1):
                    p1, p2 = nodes[i], nodes[i+1]
                    dx, dy = p2.x() - p1.x(), p2.y() - p1.y()
                    seg_len = math.sqrt(dx**2 + dy**2)

                    if seg_len < min_length: continue

                    az = math.degrees(math.atan2(dx, dy)) % 360.0

                    # DENSIFICATION LOGIC
                    # If line is 1000m and step is 100m, we create 10 virtual segments
                    if densify_dist > 0 and seg_len > densify_dist:
                        num_sub = int(seg_len / densify_dist)
                        sub_len = seg_len / num_sub
                        for _ in range(num_sub):
                            azimuths.append(az)
                            lengths.append(sub_len)
                    else:
                        azimuths.append(az)
                        lengths.append(seg_len)

        return np.array(azimuths), np.array(lengths)

    def _calculate_rectitude(self, geom: QgsGeometry) -> float:
        """
        Ratio straight_length / real_length.
        1.0 = perfectly straight. 0.0 = very sinuous.
        """
        total_len = geom.length()
        if total_len == 0: return 0.0
        # Distance between first and last point
        pts = list(geom.vertices())
        straight_len = math.sqrt((pts[-1].x()-pts[0].x())**2 + (pts[-1].y()-pts[0].y())**2)
        return straight_len / total_len

    def _bin_values(self, azimuths: np.ndarray, lengths: np.ndarray, bins: np.ndarray, mode: str) -> np.ndarray:
        """
        Bins azimuths into sectors according to the selected mode.
        Ultra-fast vectorized binning. 
        Zero loops on data points.
        """
        # 1. Determine which bin index each azimuth belongs to
        # np.digitize returns an array of indices (e.g., [0, 5, 22, 0...])
        bin_indices = np.digitize(azimuths, bins) - 1

        # 2. Handle the edge case where azimuth is exactly the max bin value
        # (e.g., exactly 360.0° must go into the last bin, not create a new one)
        num_bins = len(bins) - 1
        bin_indices = np.clip(bin_indices, 0, num_bins - 1)

        # 3. Aggregate values based on the selected mode
        if mode == "count":
            # np.bincount is a NumPy ninja trick: it counts occurrences of each index
            return np.bincount(bin_indices, minlength=num_bins).astype(float)
        
        else:
            # For "length" and "frequency", we sum the 'lengths' array 
            # grouped by the 'bin_indices' labels.
            weighted_sums = np.bincount(bin_indices, weights=lengths, minlength=num_bins)
            
            if mode == "frequency":
                total_len = weighted_sums.sum()
                return (weighted_sums / total_len * 100.0) if total_len > 0 else weighted_sums
            
            return weighted_sums
        

    def _compute_stats(self, azimuths, lengths, bin_centers, values) -> dict:
        """
        Computes circular statistics on the distribution.
        Includes dominant, secondary direction, anisotropy, entropy.
        """
        values = np.array(values)
        
        # Dominant azimuth
        dominant_idx = int(np.argmax(values))
        dominant = float(bin_centers[dominant_idx])

        # Secondary — mask dominant neighborhood
        masked = values.copy()
        for offset in [-1, 0, 1]:
            idx = (dominant_idx + offset) % len(masked)
            masked[idx] = 0
        secondary = float(bin_centers[int(np.argmax(masked))])

        # Circular mean (length-weighted)
        rad = np.radians(azimuths)
        sin_mean = np.average(np.sin(rad), weights=lengths)
        cos_mean = np.average(np.cos(rad), weights=lengths)
        mean_az  = float(np.degrees(np.arctan2(sin_mean, cos_mean)) % 360.0)

        # Concentration R (0 = isotropic, 1 = perfectly aligned)
        R = float(np.hypot(sin_mean, cos_mean))

        # Anisotropy ( max bin / total)
        total = float(np.sum(values))
        anisotropy = float(np.max(values) / total) if total > 0 else 0.0

        # Directional entropy
        probs = values / total if total > 0 else values
        probs = probs[probs > 0]
        entropy = float(-np.sum(probs * np.log2(probs)))

        return {
            "dominant":   round(dominant, 1),
            "secondary":  round(secondary, 1),
            "mean":       round(mean_az, 1),
            "r":          round(R, 4),
            "anisotropy": round(anisotropy, 3),
            "entropy":    round(entropy, 4),
            "count":      int(len(azimuths)),
            "total_km":   round(float(np.sum(lengths)) / 1000, 2),
        }