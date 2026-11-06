# core/sampling.py

"""


Spatial sampling utilities for RockMorph.
Handles line discretization, perpendicular transversals,
and swath statistics computation.
No UI logic — pure computation.
"""

import numpy as np # type: ignore
from qgis.core import ( # type: ignore
    QgsLineString,
    QgsPointXY,
    QgsDistanceArea,
    QgsProject,
    QgsCoordinateReferenceSystem,
    QgsCoordinateTransform,
    QgsVectorLayer,
)
from qgis.PyQt.QtCore import QCoreApplication # type: ignore
from .raster import RasterReader


def tr(message):
    return QCoreApplication.translate("RockMorph", message)


class SwathSampler:
    """
    Extracts swath profile statistics from a DEM along a line.

    Workflow
    --------
    1. Discretize line into N equally-spaced points
    2. At each point, generate a perpendicular transversal
    3. Sample DEM values along each transversal
    4. Compute statistics per station (min/max/mean/Q1/Q3)
    5. Compute secondary profiles (local relief, hypsometry)

    Parameters
    ----------
    reader      : RasterReader  — DEM reader
    line_layer  : QgsVectorLayer — single line feature
    n_stations  : int           — number of sample points along line
    width_m     : float         — half-width of swath band in metres
    n_transversal : int         — number of sample points per transversal
    compute_q   : bool          — compute Q1/Q3 percentiles
    compute_relief : bool       — compute local relief profile
    compute_hyps   : bool       — compute transversal hypsometry
    """

    def __init__(
        self,
        reader:           RasterReader,
        line_layer:       QgsVectorLayer,
        n_stations:       int   = 200,
        width_m:          float = 1000.0,
        n_transversal:    int   = 50,
        compute_q:        bool  = False,
        compute_relief:   bool  = True,
        compute_hyps:     bool  = True,
        progress_callback: callable = None,
        feature_id:       int   = None,
    ):
        self.reader        = reader
        self.line_layer    = line_layer
        self.n_stations    = n_stations
        self.width_m       = width_m
        self.n_transversal = n_transversal
        self.compute_q     = compute_q
        self.compute_relief = compute_relief
        self.compute_hyps  = compute_hyps
        self.progress_callback = progress_callback
        # Feature of line_layer to profile (None = first feature). The panel
        # passes the feature chosen by the user, by default the selected one.
        self.feature_id    = feature_id
        self.warnings: list = []
        self._band_left: list = []    # transversal end points, DEM CRS
        self._band_right: list = []


        # Use DEM CRS for distance measurement
    # because line coords are reprojected to DEM CRS before sampling
        self._da = QgsDistanceArea()
        self._da.setSourceCrs(
            reader.crs,   # ← DEM CRS, pas line_layer.crs()
            QgsProject.instance().transformContext()
        )
        self._da.setEllipsoid('WGS84')
    # ------------------------------------------------------------------
    # Public API
    # ------------------------------------------------------------------

    def sample(self) -> dict:
        """
        Run the full swath sampling pipeline.

        Returns
        -------
        dict with keys:
            distances  : list[float]  — distance from start (metres)
            mean       : list[float]
            min        : list[float]
            max        : list[float]
            q1         : list[float] | None
            q3         : list[float] | None
            relief     : list[float] | None
            hyps       : list[float] | None
            total_length_m : float
            width_m    : float
            n_stations : int
        """
        # 1. Extract line geometry
        line_coords = self._extract_line()
        if len(line_coords) < 2:
            raise ValueError(tr("Line layer must contain at least one feature."))

        # 2. Discretize into stations
        stations, distances = self._discretize(line_coords)

        # 3. Sample DEM at each station
        results = self._sample_stations(stations)

        # Station positions and the outline of the sampled band, in the DEM
        # CRS: the panel places its hover marker and draws the band from
        # them, so the map shows exactly where each value was measured (the
        # former marker interpolated metres in CRS units — degrees for a
        # geographic DEM — and ignored a reversed profile).
        return {
            "distances":      [round(d, 2) for d in distances],
            "mean":           self._clean(results["mean"]),
            "min":            self._clean(results["min"]),
            "max":            self._clean(results["max"]),
            "q1":             self._clean(results["q1"]) if self.compute_q else None,
            "q3":             self._clean(results["q3"]) if self.compute_q else None,
            "relief":         self._clean(results["relief"]) if self.compute_relief else None,
            "hyps":           self._clean(results["hyps"]) if self.compute_hyps else None,
            "total_length_m": round(distances[-1], 2) if distances else 0.0,
            "width_m":        self.width_m,
            "n_stations":     self.n_stations,
            "station_x":      [round(float(p[0]), 7) for p in stations],
            "station_y":      [round(float(p[1]), 7) for p in stations],
            "band":           [[round(float(x), 7), round(float(y), 7)]
                               for x, y in self._band_left + self._band_right[::-1]],
            "station_crs":    self.reader.crs.toWkt(),
            "feature_id":     self.feature_id,
            "warnings":       self.warnings,
        }

    # ------------------------------------------------------------------
    # Step 1 — Extract line coordinates
    # ------------------------------------------------------------------

    def _extract_line(self) -> list:
        """
        (x, y) coordinates of the chosen feature (``feature_id``, else the
        first feature), reprojected to the DEM CRS.

        A multi-part line cannot be one swath: its parts were chained into a
        single path with straight jumps between them. The LONGEST part is
        used and a warning says so.
        """
        transform = None
        if self.line_layer.crs() != self.reader.crs:
            transform = QgsCoordinateTransform(
                self.line_layer.crs(),
                self.reader.crs,
                QgsProject.instance()
            )

        feature = None
        if self.feature_id is not None:
            feature = self.line_layer.getFeature(int(self.feature_id))
            if not feature.isValid():
                feature = None
                self.warnings.append(tr("The chosen line no longer exists; the first line was used."))
        if feature is None:
            feature = next((f for f in self.line_layer.getFeatures()
                            if f.geometry() is not None and not f.geometry().isEmpty()), None)
        if feature is None:
            return []
        self.feature_id = feature.id()

        parts = []
        for part in feature.geometry().constParts():
            points = []
            for vertex in part.vertices():
                pt = QgsPointXY(vertex.x(), vertex.y())
                if transform:
                    pt = transform.transform(pt)
                points.append((pt.x(), pt.y()))
            if len(points) >= 2:
                parts.append(points)
        if not parts:
            return []
        if len(parts) > 1:
            self.warnings.append(tr("The line has {0} parts; the longest one was used.").format(len(parts)))

        def part_length(points):
            return sum(self._da.measureLine(QgsPointXY(*a), QgsPointXY(*b))
                       for a, b in zip(points[:-1], points[1:]))

        return max(parts, key=part_length)

    # ------------------------------------------------------------------
    # Step 2 — Discretize line into equally spaced stations
    # ------------------------------------------------------------------

    def _discretize(self, coords: list) -> tuple:
        """
        Place N equally-spaced stations along the line.
        Returns (stations, cumulative_distances_m).
        Uses QgsDistanceArea for correct metric distances.
        """
        # Build cumulative distance array along vertices
        cum_dist = [0.0]
        for i in range(1, len(coords)):
            p1 = QgsPointXY(*coords[i - 1])
            p2 = QgsPointXY(*coords[i])
            d  = self._da.measureLine(p1, p2)
            # Guard against NaN distance
            if np.isnan(d) or d < 0:
                d = 0.0
            cum_dist.append(cum_dist[-1] + d)


        total = cum_dist[-1]
        if total < 1e-10:
            raise ValueError(tr("Line has zero length — check CRS."))
        target_dists = np.linspace(0, total, self.n_stations)

        stations  = []
        distances = []

        for td in target_dists:
            # Find segment containing td
            for i in range(1, len(cum_dist)):
                if cum_dist[i] >= td or i == len(cum_dist) - 1:
                    seg_len = cum_dist[i] - cum_dist[i - 1]
                    if seg_len < 1e-10:
                        pt = coords[i]
                    else:
                        t = (td - cum_dist[i - 1]) / seg_len
                        x = coords[i-1][0] + t * (coords[i][0] - coords[i-1][0])
                        y = coords[i-1][1] + t * (coords[i][1] - coords[i-1][1])
                        # Guard against NaN interpolation
                        if np.isnan(x) or np.isnan(y):
                            pt = coords[i]
                        else:
                            pt = (x, y)
                    stations.append(pt)
                    distances.append(td)
                    break

        return stations, distances

    # ------------------------------------------------------------------
    # Step 3 — Sample DEM at each station
    # ------------------------------------------------------------------

    def _sample_stations(self, stations: list) -> dict:
        """
        For each station, generate perpendicular transversal,
        sample DEM, compute statistics.
        """
        means   = []
        mins    = []
        maxs    = []
        q1s     = []
        q3s     = []
        reliefs = []
        hypss   = []

        n = len(stations)

        for i, pt in enumerate(stations):
            # Direction vector from neighbors
            if i == 0:
                dx = stations[1][0] - stations[0][0]
                dy = stations[1][1] - stations[0][1]
            elif i == n - 1:
                dx = stations[-1][0] - stations[-2][0]
                dy = stations[-1][1] - stations[-2][1]
            else:
                dx = stations[i+1][0] - stations[i-1][0]
                dy = stations[i+1][1] - stations[i-1][1]

            # Perpendicular direction
            perp = self._perpendicular(pt, dx, dy)
            self._band_left.append(perp[0])
            self._band_right.append(perp[-1])

            # Sample DEM along transversal
            values = self.reader.sample_points(perp)
            values = values[~np.isnan(values)]

            if len(values) == 0:
                means.append(np.nan)
                mins.append(np.nan)
                maxs.append(np.nan)
                q1s.append(np.nan)
                q3s.append(np.nan)
                reliefs.append(np.nan)
                hypss.append(np.nan)
                continue

            vmin = float(np.min(values))
            vmax = float(np.max(values))
            vmean = float(np.mean(values))

            means.append(vmean)
            mins.append(vmin)
            maxs.append(vmax)

            if self.compute_q:
                q1s.append(float(np.percentile(values, 25)))
                q3s.append(float(np.percentile(values, 75)))

            if self.compute_relief:
                reliefs.append(vmax - vmin)

            if self.compute_hyps:
                denom = vmax - vmin
                hypss.append(
                    float((vmean - vmin) / denom) if denom > 1e-6 else 0.5
                )
            
            # Progress callback every 10% of stations
            if self.progress_callback and i % (max(1, n // 10)) == 0:
                percent = 30 + int((i / n) * 60) # 30% to 90% range
                self.progress_callback(percent, tr(f"Sampling station {i}/{n}..."))


        return {
            "mean":   means,
            "min":    mins,
            "max":    maxs,
            "q1":     q1s    if self.compute_q      else None,
            "q3":     q3s    if self.compute_q      else None,
            "relief": reliefs if self.compute_relief else None,
            "hyps":   hypss   if self.compute_hyps  else None,
        }

    # ------------------------------------------------------------------
    # Geometry helpers
    # ------------------------------------------------------------------

    def _perpendicular(self, center: tuple, dx: float, dy: float) -> list:
        """
        Generate perpendicular transversal points centered on `center`.
        Handles both geographic and projected CRS.

        Parameters
        ----------
        center : (x, y) in DEM CRS
        dx, dy : direction vector of line at this station
        """

        # Guard against NaN or zero direction vector
        if np.isnan(dx) or np.isnan(dy):
            return [center] * self.n_transversal


        norm = np.hypot(dx, dy)
        if norm < 1e-10:
            return [center] * self.n_transversal

        # Perpendicular unit vector
        px = -dy / norm
        py =  dx / norm

        # Guard against NaN center coordinates
        if np.isnan(center[0]) or np.isnan(center[1]):
            return [center] * self.n_transversal

        # Convert width_m to map units
        if self.reader.is_geographic:
            # Approximate degrees from metres at this latitude
            lat_rad = np.radians(center[1])
            width_x = self.width_m / (111320.0 * np.cos(lat_rad))
            width_y = self.width_m / 111320.0
            half_x  = width_x * px
            half_y  = width_y * py
        else:
            # Projected CRS — map units are metres
            half_x = self.width_m * px
            half_y = self.width_m * py

        # Guard against NaN result
        if np.isnan(half_x) or np.isnan(half_y):
            return [center] * self.n_transversal


        # Generate points from -width to +width
        t_values = np.linspace(-1.0, 1.0, self.n_transversal)
        points = [
            (center[0] + t * half_x, center[1] + t * half_y)
            for t in t_values
        ]
        return points

    # ------------------------------------------------------------------
    # Utility
    # ------------------------------------------------------------------

    @staticmethod
    def _clean(values: list) -> list:
        """Replace nan with None for JSON serialization."""
        if values is None:
            return None
        return [
            None if (v is None or (isinstance(v, float) and np.isnan(v)))
            else round(float(v), 3)
            for v in values
        ]