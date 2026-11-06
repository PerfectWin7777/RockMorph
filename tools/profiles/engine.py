"""
tools/profiles/engine.py

Computation side of the Topographic Profiles tool.

Two kinds of profiles:

* "Along the lines": one profile per line feature (or per line drawn on the
  map), from its first to its last vertex.
* "Serial cross-sections": profiles PERPENDICULAR to a baseline (valley
  axis, fault trace, river), centred on it, every ``spacing_m`` metres. The
  sections are numbered from the start of the baseline and coloured in that
  order in the chart, which shows how the valley or the relief changes along
  the baseline.

All geometry work happens in a metric WORK CRS (the DEM CRS when projected
in metres, otherwise the UTM zone of the lines, as in the morphometry tool):
distances along the profiles are therefore true metres even with a
geographic DEM. Elevations are read by bilinear interpolation on the DEM in
its own CRS (points are transformed, the DEM is never resampled), at a step
equal to the DEM cell size unless the user sets one.

Optional "crossings": where a profile crosses the features of a line or
polygon layer (faults, geological contacts, rivers...), the distance along
the profile and the feature label are reported and drawn on the chart.

The engine runs in RockMorph's ComputeWorker, which hands it private copies
of the layers (thread safety) and stops it at its next progress report when
the user cancels.

Authors: RockMorph contributors / Tony
"""

from __future__ import annotations

import math
from dataclasses import dataclass, field
from typing import Dict, List, Optional, Sequence, Tuple

import numpy as np  # type: ignore
from qgis.PyQt.QtCore import QCoreApplication  # type: ignore
from qgis.core import (  # type: ignore
    QgsCoordinateReferenceSystem, QgsCoordinateTransform, QgsFeatureRequest,
    QgsGeometry, QgsPointXY, QgsProject, QgsWkbTypes,
)

from ...base.base_engine import BaseEngine
from ...core.basin_context_builder import choose_work_crs
from ...core.indices.profiles import bilinear_sample
from ...core.raster import RasterReader


def tr(message: str) -> str:
    return QCoreApplication.translate("RockMorph", message)


MODE_LINES = "lines"
MODE_SECTIONS = "sections"
MAX_POINTS_PER_PROFILE = 20000      # keeps the chart responsive
SURFACE_GRID = 140                  # cells per side of the 3D terrain surface


@dataclass
class ProfileLine:
    """One profile to sample, in the work CRS."""

    pid: int
    label: str
    geometry: QgsGeometry
    kind: str = MODE_LINES
    along_m: Optional[float] = None       # position on the baseline (sections)
    baseline: str = ""


# ---------------------------------------------------------------------------
# Geometry helpers (work CRS, metres)
# ---------------------------------------------------------------------------

def single_lines(geometry: QgsGeometry) -> List[QgsGeometry]:
    """Parts of a (multi)line geometry as separate line geometries."""
    if geometry is None or geometry.isEmpty():
        return []
    if geometry.isMultipart():
        return [QgsGeometry.fromPolylineXY(part) for part in geometry.asMultiPolyline() if len(part) >= 2]
    line = geometry.asPolyline()
    return [QgsGeometry.fromPolylineXY(line)] if len(line) >= 2 else []


def cross_sections(baseline: QgsGeometry, label: str, spacing_m: float, length_m: float,
                   first_id: int, max_sections: int) -> List[ProfileLine]:
    """
    Sections perpendicular to ``baseline`` every ``spacing_m``, centred on
    it, ``length_m`` long, drawn from the LEFT to the RIGHT of the baseline
    direction (the usual "looking downstream / along the axis" convention).

    The local direction is measured over ±spacing/4 (at least ±1 m) so that
    a section is perpendicular to the trend of the baseline, not to a
    single digitising segment.
    """
    total = baseline.length()
    if total <= 0 or spacing_m <= 0:
        return []
    count = int(total // spacing_m) + 1
    if count > max_sections:
        count = max_sections
        spacing_m = total / max(1, count - 1)
    half = length_m / 2.0
    window = max(1.0, spacing_m / 4.0)
    sections = []
    for k in range(count):
        d = min(k * spacing_m, total)
        a = baseline.interpolate(max(0.0, d - window)).asPoint()
        b = baseline.interpolate(min(total, d + window)).asPoint()
        c = baseline.interpolate(d).asPoint()
        dx, dy = b.x() - a.x(), b.y() - a.y()
        norm = math.hypot(dx, dy)
        if norm < 1e-9:
            continue
        # Unit normal pointing to the left of the baseline direction.
        nx, ny = -dy / norm, dx / norm
        start = QgsPointXY(c.x() + nx * half, c.y() + ny * half)
        end = QgsPointXY(c.x() - nx * half, c.y() - ny * half)
        sections.append(ProfileLine(
            pid=first_id + len(sections),
            label=f"{label} · S{k + 1} ({d / 1000.0:.2f} km)",
            geometry=QgsGeometry.fromPolylineXY([start, end]),
            kind=MODE_SECTIONS, along_m=d, baseline=label))
    return sections


def sample_positions(length_m: float, step_m: float) -> np.ndarray:
    """Distances 0, step, 2·step, …, length (the end is always included)."""
    if length_m <= 0:
        return np.zeros(1)
    step = max(step_m, length_m / MAX_POINTS_PER_PROFILE)
    positions = np.arange(0.0, length_m, step)
    return np.append(positions, length_m)


def profile_statistics(distance: np.ndarray, z: np.ndarray) -> Dict[str, Optional[float]]:
    """
    Length (map and along the ground), elevation range, slopes and the
    cumulative ascent/descent walking from the start to the end.
    Slopes are computed between consecutive VALID samples.
    """
    valid = np.isfinite(z)
    stats: Dict[str, Optional[float]] = {
        "length_m": float(distance[-1]) if distance.size else 0.0,
        "length_3d_m": None, "z_min": None, "z_max": None, "relief_m": None,
        "z_start": None, "z_end": None, "mean_slope_deg": None, "max_slope_deg": None,
        "ascent_m": None, "descent_m": None, "valid_share": float(valid.mean()) if z.size else 0.0,
    }
    if valid.sum() < 2:
        return stats
    d, h = distance[valid], z[valid]
    dd, dz = np.diff(d), np.diff(h)
    keep = dd > 0
    dd, dz = dd[keep], dz[keep]
    slopes = np.degrees(np.arctan(np.abs(dz) / dd)) if dd.size else np.array([])
    stats.update({
        "length_3d_m": float(np.sum(np.hypot(dd, dz))),
        "z_min": float(h.min()), "z_max": float(h.max()), "relief_m": float(h.max() - h.min()),
        "z_start": float(h[0]), "z_end": float(h[-1]),
        "mean_slope_deg": float(slopes.mean()) if slopes.size else None,
        "max_slope_deg": float(slopes.max()) if slopes.size else None,
        "ascent_m": float(dz[dz > 0].sum()), "descent_m": float(-dz[dz < 0].sum()),
    })
    return stats


def crossings_along(line: QgsGeometry, features: Sequence[Tuple[str, QgsGeometry]]) -> List[dict]:
    """
    Points where ``line`` crosses each feature (polygon boundaries for
    polygons), as {distance_m, label}, sorted along the line.
    """
    found = []
    box = line.boundingBox()
    for label, geometry in features:
        if not geometry.boundingBox().intersects(box):
            continue
        target = geometry
        if QgsWkbTypes.geometryType(geometry.wkbType()) == QgsWkbTypes.PolygonGeometry:
            target = QgsGeometry(geometry.constGet().boundary())
        hit = line.intersection(target)
        if hit.isEmpty():
            continue
        for vertex in hit.vertices():
            point = QgsGeometry.fromPointXY(QgsPointXY(vertex.x(), vertex.y()))
            found.append({"distance_m": float(line.lineLocatePoint(point)), "label": label})
    found.sort(key=lambda c: c["distance_m"])
    # A boundary shared by two polygons is met twice at the same place:
    # keep one mark per position (within 1 m) and join the labels.
    merged: List[dict] = []
    for crossing in found:
        if merged and crossing["distance_m"] - merged[-1]["distance_m"] < 1.0:
            if crossing["label"] not in merged[-1]["label"].split(" / "):
                merged[-1]["label"] += " / " + crossing["label"]
            continue
        merged.append(dict(crossing))
    return merged


def _json_list(values: np.ndarray, decimals: int) -> list:
    return [None if not np.isfinite(v) else round(float(v), decimals) for v in values]


# ---------------------------------------------------------------------------
# Engine
# ---------------------------------------------------------------------------

class ProfilesEngine(BaseEngine):
    """
    compute() parameters
    --------------------
    dem_layer           : QgsRasterLayer
    line_layer          : QgsVectorLayer | None   — lines to profile / baselines
    drawn_wkt           : list[str]               — lines drawn on the map
    drawn_crs           : str                     — CRS (WKT) of the drawn lines
    selected_only       : bool
    label_field         : str
    mode                : "lines" | "sections"
    step_m              : float  (0 = DEM cell size)
    spacing_m, section_length_m, max_sections : cross-section geometry
    crossing_layer      : QgsVectorLayer | None
    crossing_label_field: str
    surface             : bool  — also sample a terrain grid for the 3D view
    """

    def compute(self, **kwargs) -> dict:
        progress = kwargs.get("progress_callback") or (lambda value, message="": None)
        dem_layer = kwargs["dem_layer"]
        line_layer = kwargs.get("line_layer")
        mode = kwargs.get("mode", MODE_LINES)
        warnings: List[str] = []

        progress(5, tr("Reading the DEM…"))
        reader = RasterReader(dem_layer)
        dem_crs = dem_layer.crs()
        cell = reader.pixel_size_x

        # ── Input lines → work CRS ─────────────────────────────────────
        progress(15, tr("Preparing the profile lines…"))
        sources: List[Tuple[str, QgsGeometry, QgsCoordinateReferenceSystem]] = []
        if line_layer is not None:
            request = QgsFeatureRequest()
            features = (line_layer.getSelectedFeatures(request) if kwargs.get("selected_only")
                        else line_layer.getFeatures(request))
            label_field = kwargs.get("label_field") or ""
            for feature in features:
                label = str(feature[label_field]) if label_field and feature[label_field] not in (None, "") \
                    else f"{line_layer.name()} #{feature.id()}"
                sources.append((label, QgsGeometry(feature.geometry()), line_layer.crs()))
        drawn_crs = QgsCoordinateReferenceSystem.fromWkt(kwargs.get("drawn_crs", "")) \
            if kwargs.get("drawn_crs") else dem_crs
        for number, wkt in enumerate(kwargs.get("drawn_wkt") or [], start=1):
            sources.append((tr("Drawn profile {0}").format(number), QgsGeometry.fromWkt(wkt), drawn_crs))
        if not sources:
            raise ValueError(tr("No line to profile."))

        to_wgs84 = QgsCoordinateTransform(sources[0][2], QgsCoordinateReferenceSystem("EPSG:4326"),
                                          QgsProject.instance())
        centre = to_wgs84.transform(sources[0][1].boundingBox().center())
        work_crs = choose_work_crs(dem_crs, (centre.x(), centre.y()))

        lines: List[ProfileLine] = []
        for label, geometry, crs in sources:
            geometry = QgsGeometry(geometry)
            if crs != work_crs:
                geometry.transform(QgsCoordinateTransform(crs, work_crs, QgsProject.instance()))
            for part_number, part in enumerate(single_lines(geometry), start=1):
                part_label = label if len(single_lines(geometry)) == 1 else f"{label} ({part_number})"
                if mode == MODE_SECTIONS:
                    lines += cross_sections(part, part_label, float(kwargs.get("spacing_m", 500.0)),
                                            float(kwargs.get("section_length_m", 2000.0)), len(lines),
                                            int(kwargs.get("max_sections", 100)))
                else:
                    lines.append(ProfileLine(len(lines), part_label, part))
        if not lines:
            raise ValueError(tr("The lines are empty or too short to be profiled."))

        # ── Crossing features → work CRS ───────────────────────────────
        crossing_features: List[Tuple[str, QgsGeometry]] = []
        crossing_layer = kwargs.get("crossing_layer")
        if crossing_layer is not None:
            field_name = kwargs.get("crossing_label_field") or ""
            to_work = QgsCoordinateTransform(crossing_layer.crs(), work_crs, QgsProject.instance())
            for feature in crossing_layer.getFeatures():
                geometry = QgsGeometry(feature.geometry())
                if geometry.isEmpty():
                    continue
                geometry.transform(to_work)
                label = str(feature[field_name]) if field_name and feature[field_name] not in (None, "") \
                    else crossing_layer.name()
                crossing_features.append((label, geometry))

        # ── Sampling ───────────────────────────────────────────────────
        to_dem = None if work_crs == dem_crs else \
            QgsCoordinateTransform(work_crs, dem_crs, QgsProject.instance())
        step = float(kwargs.get("step_m") or 0.0)
        if step <= 0:
            # DEM cell size in metres (approximate for geographic DEMs).
            step = cell * (111320.0 if dem_crs.isGeographic() else 1.0)
        profiles = []
        for number, line in enumerate(lines):
            progress(20 + 70 * number / len(lines),
                     tr("Profile {0}/{1}").format(number + 1, len(lines)))
            length = line.geometry.length()
            positions = sample_positions(length, step)
            points = [line.geometry.interpolate(float(d)).asPoint() for d in positions]
            xs = np.array([p.x() for p in points])
            ys = np.array([p.y() for p in points])
            if to_dem is not None:
                dem_points = [to_dem.transform(p) for p in points]
                dx = np.array([p.x() for p in dem_points])
                dy = np.array([p.y() for p in dem_points])
            else:
                dx, dy = xs, ys
            z = bilinear_sample(reader.array, reader.geo_transform, dx, dy)
            stats = profile_statistics(positions, z)
            if stats["valid_share"] < 1.0:
                warnings.append(tr("{0}: {1:.0f} % of the profile lies outside the DEM or on "
                                   "NoData.").format(line.label, 100 * (1 - stats["valid_share"])))
            profiles.append({
                "id": line.pid, "label": line.label, "kind": line.kind,
                "along_m": line.along_m, "baseline": line.baseline,
                "distance": _json_list(positions, 1), "z": _json_list(z, 2),
                "x": _json_list(xs, 2), "y": _json_list(ys, 2),
                "stats": stats,
                "crossings": crossings_along(line.geometry, crossing_features),
                "wkt": line.geometry.asWkt(2),
            })

        surface = None
        if kwargs.get("surface", True):
            progress(92, tr("Sampling the terrain for the 3D view…"))
            surface = self._terrain_surface(profiles, reader, to_dem)

        progress(100, tr("Done"))
        return {"profiles": profiles, "surface": surface, "warnings": warnings,
                "work_crs": work_crs.toWkt(), "work_crs_name": work_crs.authid() or work_crs.description(),
                "step_m": step, "mode": mode}

    @staticmethod
    def _terrain_surface(profiles: list, reader: RasterReader, to_dem) -> Optional[dict]:
        """
        Regular grid of elevations around the profiles (bounding box + 15 %)
        for the 3D view, in work-CRS coordinates.
        """
        xs = [v for p in profiles for v in p["x"] if v is not None]
        ys = [v for p in profiles for v in p["y"] if v is not None]
        if not xs:
            return None
        xmin, xmax, ymin, ymax = min(xs), max(xs), min(ys), max(ys)
        # Square area centred on the profiles, 30 % wider than their longest
        # extent: a north-south line otherwise got a narrow strip of terrain
        # with no context on either side.
        half = 0.65 * max(xmax - xmin, ymax - ymin, 1.0)
        cx, cy = (xmin + xmax) / 2.0, (ymin + ymax) / 2.0
        gx = np.linspace(cx - half, cx + half, SURFACE_GRID)
        gy = np.linspace(cy - half, cy + half, SURFACE_GRID)
        mx, my = np.meshgrid(gx, gy)
        if to_dem is not None:
            pts = [to_dem.transform(QgsPointXY(float(a), float(b))) for a, b in zip(mx.ravel(), my.ravel())]
            sx = np.array([p.x() for p in pts]).reshape(mx.shape)
            sy = np.array([p.y() for p in pts]).reshape(my.shape)
        else:
            sx, sy = mx, my
        z = bilinear_sample(reader.array, reader.geo_transform, sx.ravel(), sy.ravel()).reshape(mx.shape)
        return {"x": _json_list(gx, 1), "y": _json_list(gy, 1),
                "z": [_json_list(row, 1) for row in z]}
