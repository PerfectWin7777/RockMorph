# tools/rose/engine.py

from qgis.PyQt.QtCore import QCoreApplication # type: ignore
from qgis.core import QgsWkbTypes # type: ignore
import numpy as np # type: ignore
from ...base.base_engine import BaseEngine


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
        color     = kwargs.get("color", "#4a9eff")
        opacity   = kwargs.get("opacity", 0.85)
        show_grid = kwargs.get("show_grid", True)
        show_labels = kwargs.get("show_labels", False)
        title     = kwargs.get("title", "Rose Diagram")
        min_rectitude = kwargs.get("min_rectitude", 0.0)

        azimuths, lengths = self._extract_segments(layer, min_rectitude)

        if len(azimuths) == 0:
            raise ValueError(tr("No valid segments found in layer."))

        # Fold to 0-180° for half rose
        if half_rose:
            azimuths = azimuths % 180.0

        sector_width = 180.0 / n_sectors if half_rose else 360.0 / n_sectors
        bins = np.arange(0, (180.0 if half_rose else 360.0) + sector_width, sector_width)

        values = self._bin_values(azimuths, lengths, bins, mode)
        bin_centers = bins[:-1] + sector_width / 2.0

        stats = self._compute_stats(azimuths, lengths, bin_centers, values)

        return {
            "azimuths":     bin_centers.tolist(),
            "values":       values.tolist(),
            "sector_width": sector_width,
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

    def _extract_segments(self, layer, min_rectitude: float = 0.0) -> tuple:
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

            for part in geom.constParts():
                vertices = list(part.vertices())
                if len(vertices) < 2:
                    continue

                # Rectitude filter at feature level
                if min_rectitude > 0.0:
                    coords = [(v.x(), v.y()) for v in vertices]
                    if self._rectitude(coords) < min_rectitude:
                        continue

                # Vertex-by-vertex segmentation
                for i in range(len(vertices) - 1):
                    x1, y1 = vertices[i].x(),   vertices[i].y()
                    x2, y2 = vertices[i+1].x(), vertices[i+1].y()
                    dx = x2 - x1
                    dy = y2 - y1
                    length = np.hypot(dx, dy)
                    if length < 1e-10:
                        continue
                    azimuth = np.degrees(np.arctan2(dx, dy)) % 360.0
                    azimuths.append(azimuth)
                    lengths.append(length)

        return np.array(azimuths), np.array(lengths)

    def _rectitude(self, coords: list) -> float:
        """
        Ratio straight_length / real_length.
        1.0 = perfectly straight. 0.0 = very sinuous.
        """
        total = 0.0
        for i in range(len(coords) - 1):
            dx = coords[i+1][0] - coords[i][0]
            dy = coords[i+1][1] - coords[i][1]
            total += np.hypot(dx, dy)
        if total == 0:
            return 0.0
        dx = coords[-1][0] - coords[0][0]
        dy = coords[-1][1] - coords[0][1]
        straight = np.hypot(dx, dy)
        return straight / total

    def _bin_values(self, azimuths, lengths, bins, mode) -> np.ndarray:
        """
        Bins azimuths into sectors according to the selected mode.
        """
        n = len(bins) - 1
        values = np.zeros(n)

        for i in range(n):
            mask = (azimuths >= bins[i]) & (azimuths < bins[i + 1])
            if not mask.any():
                continue

            if mode == "count":
                values[i] = mask.sum()
            elif mode == "length":
                values[i] = lengths[mask].sum()
            elif mode == "frequency":
                values[i] = lengths[mask].sum() / lengths.sum() * 100.0

        return values

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

        # Anisotropy (Sovereign method — max bin / total)
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