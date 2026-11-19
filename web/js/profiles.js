/**
 * web/js/profiles.js
 *
 * Client side of the Topographic Profiles viewer (web/profiles.html).
 *
 * Colours: up to 8 profiles drawn "along lines" use the categorical palette
 * in a fixed order; serial cross-sections use an ordinal blue ramp from the
 * first (light) to the last section (dark), because their ORDER along the
 * baseline is what the reader compares.
 *
 * Vertical exaggeration: x is in km and y in m, so a true-scale chart needs
 * 1 m on y drawn as long as 1 m on x: yaxis.scaleanchor = "x" with
 * scaleratio = exaggeration / 1000. Exaggeration 0 = "fit to the panel".
 *
 * Authors: RockMorph contributors / Tony
 */

"use strict";

var STATE = { payload: null, exaggeration: 0, stacked: false, cursorProfile: null };

var SERIES = ["#2a78d6", "#eb6834", "#1baf7a", "#eda100", "#e87ba4", "#008300", "#6250d6", "#e34948"];
var RAMP = ["#cde2fb", "#9ec5f4", "#6da7ec", "#3987e5", "#2a78d6", "#256abf", "#1c5cab", "#184f95", "#104281", "#0d366b"];
var CURSOR = "#d03b3b";

function colorOf(index) {
    var profiles = STATE.payload.profiles;
    if (STATE.payload.mode === "sections" || profiles.length > SERIES.length) {
        var t = profiles.length > 1 ? index / (profiles.length - 1) : 1;
        return RAMP[Math.min(RAMP.length - 1, 2 + Math.round(t * (RAMP.length - 3)))];
    }
    return SERIES[index % SERIES.length];
}

function km(values) { return values.map(function (d) { return d === null ? null : d / 1000; }); }
function fmt(v, digits) { return v === null || v === undefined ? "—" : Number(v).toFixed(digits === undefined ? 0 : digits); }

/**
 * Stacked view: each section is drawn relative to its own lowest point and
 * shifted by 1.15 × the largest relief, so sections can never overlap (an
 * offset of absolute elevations failed where the terrain rises along the
 * baseline faster than the offset). Hover labels keep the real elevation.
 */
function stackOffset() {
    var relief = 0;
    STATE.payload.profiles.forEach(function (p) { relief = Math.max(relief, p.stats.relief_m || 0); });
    return Math.max(1, relief * 1.15);
}

function shifted(profile, index) {
    if (!STATE.stacked) return profile.z;
    var base = profile.stats.z_min || 0;
    var offset = index * stackOffset();
    return profile.z.map(function (z) { return z === null ? null : z - base + offset; });
}

// ---------------------------------------------------------------------------
// Entry points (Python → JS)
// ---------------------------------------------------------------------------

function renderProfiles(payload) {
    STATE.payload = payload;
    STATE.cursorProfile = null;
    render2D();
    if (activeTab() === "three") render3D();
    renderTable();
}

function setOptions(options) {
    if (options.exaggeration !== undefined) STATE.exaggeration = options.exaggeration;
    if (options.stacked !== undefined) STATE.stacked = options.stacked;
    if (!STATE.payload) return;
    render2D();
    if (activeTab() === "three") render3D();
}

/** Cursor requested by the MAP (mouse near a profile line). */
function showCursor(index, distance) {
    if (!STATE.payload) return;
    var p = STATE.payload.profiles[index];
    if (!p) return;
    var i = nearestIndex(p.distance, distance);
    var z = shifted(p, index)[i];
    var gd = document.getElementById("plot-main");
    var cursorTrace = gd.data.length - 1;
    Plotly.restyle(gd, { x: [[p.distance[i] / 1000]], y: [[z]], visible: true,
                         "marker.color": CURSOR }, [cursorTrace]);
    Plotly.relayout(gd, { shapes: crossingShapes().concat([{
        type: "line", xref: "x", yref: "paper", x0: p.distance[i] / 1000, x1: p.distance[i] / 1000,
        y0: 0, y1: 1, line: { color: CURSOR, width: 1, dash: "dot" } }]) });
}

function hideCursor() {
    var gd = document.getElementById("plot-main");
    if (!gd.data) return;
    Plotly.restyle(gd, { visible: false }, [gd.data.length - 1]);
    Plotly.relayout(gd, { shapes: crossingShapes() });
}

function nearestIndex(values, target) {
    var lo = 0, hi = values.length - 1;
    while (hi - lo > 1) {
        var mid = (lo + hi) >> 1;
        if (values[mid] < target) lo = mid; else hi = mid;
    }
    return Math.abs(values[lo] - target) <= Math.abs(values[hi] - target) ? lo : hi;
}

// ---------------------------------------------------------------------------
// 2D chart
// ---------------------------------------------------------------------------

function crossingShapes() {
    var shapes = [];
    // Stacked sections: crossings are drawn as marks ON each section
    // (see crossingMarks) — full-height lines would cross every section.
    if (!STATE.payload || STATE.stacked) return shapes;
    STATE.payload.profiles.forEach(function (p, index) {
        (p.crossings || []).forEach(function (c) {
            shapes.push({ type: "line", xref: "x", yref: "paper", x0: c.distance_m / 1000, x1: c.distance_m / 1000,
                          y0: 0, y1: 1, line: { color: colorOf(index), width: 1, dash: "dash" }, opacity: 0.7 });
        });
    });
    return shapes;
}

function crossingAnnotations() {
    var notes = [];
    // Labels only when few profiles are shown; beyond that they overlap and
    // the names stay available in the hover of the crossing marks.
    if (STATE.stacked || STATE.payload.profiles.length > 3) return notes;
    STATE.payload.profiles.forEach(function (p, index) {
        (p.crossings || []).forEach(function (c) {
            notes.push({ x: c.distance_m / 1000, y: 1, yref: "paper", text: c.label, showarrow: false,
                         textangle: -90, xanchor: "right", yanchor: "top", font: { size: 9, color: "#52514e" } });
        });
    });
    return notes.length <= 40 ? notes : [];   // too many labels would hide the profiles
}

/** "S1", "S2"… at the end of each stacked section. */
function sectionLabels() {
    if (!STATE.stacked) return [];
    return STATE.payload.profiles.map(function (p, index) {
        var zs = shifted(p, index), last = zs.length - 1;
        while (last > 0 && zs[last] === null) last--;
        var short = (p.label.match(/S\d+/) || [String(index + 1)])[0];
        return { x: p.distance[last] / 1000, y: zs[last], text: short, showarrow: false, xanchor: "left",
                 xshift: 4, font: { size: 9, color: colorOf(index) } };
    });
}

function render2D() {
    var gd = document.getElementById("plot-main");
    var profiles = STATE.payload.profiles;
    if (!profiles.length) { Plotly.purge(gd); gd.innerHTML = '<div class="empty">No profile.</div>'; return; }
    var traces = profiles.map(function (p, index) {
        return {
            type: "scatter", mode: "lines", name: p.label, x: km(p.distance), y: shifted(p, index),
            line: { color: colorOf(index), width: 1.8 }, connectgaps: false,
            customdata: p.z,
            hovertemplate: "<b>" + p.label.replace(/</g, "&lt;") + "</b><br>%{x:.3f} km · %{customdata:.0f} m<extra></extra>",
        };
    });
    // Crossing marks on the curves (stacked view, or many profiles).
    if (STATE.stacked || profiles.length > 3) {
        profiles.forEach(function (p, index) {
            var crossings = p.crossings || [];
            if (!crossings.length) return;
            var zs = shifted(p, index);
            traces.push({
                type: "scatter", mode: "markers", showlegend: false,
                x: crossings.map(function (c) { return c.distance_m / 1000; }),
                y: crossings.map(function (c) { return zs[nearestIndex(p.distance, c.distance_m)]; }),
                text: crossings.map(function (c) { return c.label; }),
                marker: { symbol: "line-ns-open", size: 12, color: "#52514e", line: { width: 2 } },
                hovertemplate: "%{text}<br>%{x:.2f} km<extra>crossing</extra>",
            });
        });
    }
    // Last trace = cursor driven by the map (hidden until used).
    traces.push({ type: "scatter", mode: "markers", x: [0], y: [0], visible: false, showlegend: false,
                  hoverinfo: "skip", marker: { size: 10, color: CURSOR, line: { color: "#fff", width: 2 } } });
    var ve = STATE.exaggeration;
    var layout = {
        margin: { l: 56, r: STATE.stacked ? 30 : 12, t: 10, b: 40 }, paper_bgcolor: "#fcfcfb", plot_bgcolor: "#fcfcfb",
        font: { family: "Segoe UI, system-ui, Arial, sans-serif", size: 11, color: "#52514e" },
        xaxis: { title: "Distance (km)", gridcolor: "#e6e5e0", zeroline: false },
        yaxis: { title: STATE.stacked ? "Height above each section's low point (m, stacked)" : "Elevation (m)", gridcolor: "#e6e5e0",
                 zeroline: false },
        showlegend: profiles.length <= 12, legend: { orientation: "h", y: -0.18, font: { size: 10 } },
        hovermode: "closest", shapes: crossingShapes(), annotations: crossingAnnotations().concat(sectionLabels()),
    };
    if (ve > 0) { layout.yaxis.scaleanchor = "x"; layout.yaxis.scaleratio = ve / 1000; }
    Plotly.react(gd, traces, layout, { displaylogo: false, responsive: true,
                                       modeBarButtonsToRemove: ["lasso2d", "select2d", "toImage"] });
    gd.removeAllListeners && gd.removeAllListeners("plotly_hover");
    gd.on("plotly_hover", function (event) {
        var point = event.points[0];
        if (point.curveNumber >= profiles.length) return;
        var p = profiles[point.curveNumber];
        onBridgeReady(function (bridge) { bridge.on_profile_hover(point.curveNumber, p.distance[point.pointIndex]); });
    });
    gd.on("plotly_unhover", function () { onBridgeReady(function (bridge) { bridge.on_plot_leave(); }); });
}

// ---------------------------------------------------------------------------
// 3D view
// ---------------------------------------------------------------------------

function render3D() {
    var gd = document.getElementById("plot-3d");
    var payload = STATE.payload;
    if (!payload || !payload.profiles.length) return;
    var s = payload.surface;
    var x0 = Infinity, y0 = Infinity;
    payload.profiles.forEach(function (p) {
        p.x.forEach(function (v) { if (v !== null && v < x0) x0 = v; });
        p.y.forEach(function (v) { if (v !== null && v < y0) y0 = v; });
    });
    if (s) { x0 = Math.min(x0, s.x[0]); y0 = Math.min(y0, s.y[0]); }
    var toKmX = function (v) { return v === null ? null : (v - x0) / 1000; };
    var toKmY = function (v) { return v === null ? null : (v - y0) / 1000; };
    var traces = [];
    var zmin = Infinity, zmax = -Infinity;
    if (s) {
        s.z.forEach(function (row) { row.forEach(function (v) { if (v !== null) { zmin = Math.min(zmin, v); zmax = Math.max(zmax, v); } }); });
        traces.push({
            // Muted hypsometric tints so the coloured profile lines stand out.
            type: "surface", x: s.x.map(toKmX), y: s.y.map(toKmY), z: s.z, showscale: false,
            colorscale: [[0, "#6f8a5e"], [0.35, "#a7a77a"], [0.7, "#c9b48f"], [1, "#efebe4"]],
            opacity: 0.92, hoverinfo: "skip", contours: { z: { show: false } },
            lighting: { ambient: 0.55, diffuse: 0.75, roughness: 0.9, specular: 0.05 },
            lightposition: { x: -10000, y: 10000, z: 20000 },
        });
    }
    payload.profiles.forEach(function (p, index) {
        var lift = (zmax - zmin) * 0.004;      // drawn just above the surface
        traces.push({
            type: "scatter3d", mode: "lines", name: p.label, x: p.x.map(toKmX), y: p.y.map(toKmY),
            z: p.z.map(function (v) { return v === null ? null : v + lift; }),
            line: { color: payload.profiles.length === 1 ? "#d03b3b" : colorOf(index), width: 6 },
            customdata: p.distance,
            hovertemplate: "<b>" + p.label.replace(/</g, "&lt;") + "</b><br>%{customdata:.0f} m · %{z:.0f} m<extra></extra>",
        });
    });
    var spanX = s ? (s.x[s.x.length - 1] - s.x[0]) / 1000 : 1, spanY = s ? (s.y[s.y.length - 1] - s.y[0]) / 1000 : 1;
    var span = Math.max(spanX, spanY, 1e-3);
    var ve = STATE.exaggeration > 0 ? STATE.exaggeration : 2;
    var zSpan = isFinite(zmax) ? (zmax - zmin) / 1000 * ve : span * 0.3;
    var layout = {
        margin: { l: 0, r: 0, t: 0, b: 0 }, paper_bgcolor: "#fcfcfb", showlegend: false,
        scene: {
            aspectmode: "manual", aspectratio: { x: spanX / span, y: spanY / span, z: Math.max(zSpan / span, 0.05) },
            xaxis: { title: "East (km)", backgroundcolor: "#fcfcfb", gridcolor: "#e6e5e0" },
            yaxis: { title: "North (km)", backgroundcolor: "#fcfcfb", gridcolor: "#e6e5e0" },
            zaxis: { title: "Elevation (m)", backgroundcolor: "#fcfcfb", gridcolor: "#e6e5e0" },
            camera: { eye: { x: -1.3, y: -1.5, z: 0.9 } },
        },
    };
    Plotly.react(gd, traces, layout, { displaylogo: false, responsive: true });
    gd.removeAllListeners && gd.removeAllListeners("plotly_hover");
    gd.on("plotly_hover", function (event) {
        var point = event.points[0];
        var offset = s ? 1 : 0;
        var index = point.curveNumber - offset;
        if (index < 0) return;
        onBridgeReady(function (bridge) { bridge.on_profile_hover(index, payload.profiles[index].distance[point.pointNumber]); });
    });
    gd.on("plotly_unhover", function () { onBridgeReady(function (bridge) { bridge.on_plot_leave(); }); });
}

// ---------------------------------------------------------------------------
// Table
// ---------------------------------------------------------------------------

function renderTable() {
    var rows = STATE.payload.profiles.map(function (p, index) {
        var s = p.stats;
        return "<tr><td><span class='swatch' style='background:" + colorOf(index) + "'></span>" +
            p.label.replace(/</g, "&lt;") + "</td><td>" + fmt(s.length_m / 1000, 2) + "</td><td>" + fmt(s.length_3d_m / 1000, 2) +
            "</td><td>" + fmt(s.z_min) + "</td><td>" + fmt(s.z_max) + "</td><td>" + fmt(s.relief_m) +
            "</td><td>" + fmt(s.mean_slope_deg, 1) + "</td><td>" + fmt(s.max_slope_deg, 1) +
            "</td><td>" + fmt(s.ascent_m) + "</td><td>" + fmt(s.descent_m) + "</td><td>" + (p.crossings || []).length + "</td></tr>";
    }).join("");
    document.getElementById("table-wrap").innerHTML =
        "<table><thead><tr><th>Profile</th><th>Length (km)</th><th>Ground length (km)</th><th>Min (m)</th><th>Max (m)</th>" +
        "<th>Relief (m)</th><th>Mean slope (°)</th><th>Max slope (°)</th><th>Ascent (m)</th><th>Descent (m)</th>" +
        "<th>Crossings</th></tr></thead><tbody>" + rows + "</tbody></table>";
}

// ---------------------------------------------------------------------------
// Tabs and export
// ---------------------------------------------------------------------------

function activeTab() {
    var b = document.querySelector("nav button.active");
    return b ? b.getAttribute("data-tab") : "profiles";
}

document.getElementById("tabs").addEventListener("click", function (event) {
    var button = event.target.closest("button");
    if (!button) return;
    document.querySelectorAll("nav button").forEach(function (b) { b.classList.toggle("active", b === button); });
    document.querySelectorAll("main section").forEach(function (s) {
        s.classList.toggle("active", s.id === button.getAttribute("data-tab"));
    });
    if (STATE.payload && button.getAttribute("data-tab") === "three") render3D();
    window.dispatchEvent(new Event("resize"));
});

/** Export the visible chart (2D or 3D) through the shared bridge. */
function exportActiveChart() {
    var id = activeTab() === "three" ? "plot-3d" : "plot-main";
    var gd = document.getElementById(id);
    var format = id === "plot-3d" ? "png" : "svg";    // WebGL 3D scenes cannot be exported as SVG
    Plotly.toImage(gd, { format: format, width: gd.offsetWidth * (format === "png" ? 3 : 1),
                         height: gd.offsetHeight * (format === "png" ? 3 : 1) })
        .then(function (url) { onBridgeReady(function (bridge) { bridge.receive_export(url); }); });
}
