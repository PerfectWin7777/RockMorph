/**
 * bridge.js — shared QWebChannel bridge for all RockMorph tools
 * 
 * Initializes the Qt WebChannel and exposes:
 *   window.bridge  — the Python QObject
 *   onBridgeReady  — callback when bridge is available
 */

window.bridge = null;
var _bridgeCallbacks = [];

/**
 * Register a callback to run once the bridge is ready.
 * Safe to call before QWebChannel is initialized.
 */
function onBridgeReady(callback) {
    if (window.bridge !== null) {
        callback(window.bridge);
    } else {
        _bridgeCallbacks.push(callback);
    }
}

/**
 * Initialize QWebChannel.
 * Called automatically on script load.
 */
function _initBridge() {
    if (typeof QWebChannel === "undefined") {
        console.error("RockMorph: qwebchannel.js not loaded.");
        return;
    }
    new QWebChannel(qt.webChannelTransport, function (channel) {
        window.bridge = channel.objects.bridge;
        _bridgeCallbacks.forEach(function (cb) { cb(window.bridge); });
        _bridgeCallbacks = [];
    });
}

_initBridge();



// ---------------------------------------------------------------------------
// Export scaling utilities — shared by all RockMorph HTML views
// ---------------------------------------------------------------------------


/**
 * Creates a patch object to scale UI elements (fonts, grid lines) 
 * proportional to the export resolution.
 * 
 * @param {number} scaleFactor - Ratio (exportWidth / BASE_RESOLUTION)
 * @param {string[]} axisKeys - Keys for axes (e.g., ['polar'] or ['xaxis', 'yaxis'])
 */
function buildExportStylePatch(scaleFactor, axisKeys) {
    const s = scaleFactor;

    // Core layout scaling (Titles and Legends)
    let patch = {
        'title.font.size': Math.round(16 * s),
        'legend.font.size': Math.round(12 * s),
        'margin.t': Math.round(30 * s),
        'margin.b': Math.round(30 * s),
        'margin.l': Math.round(30 * s),
        'margin.r': Math.round(30 * s)
    };

    axisKeys.forEach(function (key) {
        if (key === 'polar') {
            // Polar Axis Labels (N, S, E, W)
            patch['polar.angularaxis.tickfont.size'] = Math.round(14 * s);
            // Concentric circles labels
            patch['polar.radialaxis.tickfont.size'] = Math.round(12 * s);

            // Grid Lines (The concentric circles and radial spokes)
            // This fixes the "disappearing circles" issue at 300 DPI
            patch['polar.angularaxis.gridwidth'] = Math.max(1, 1.2 * s);
            patch['polar.radialaxis.gridwidth'] = Math.max(1, 1.2 * s);

            // Axis line thickness
            patch['polar.angularaxis.linewidth'] = Math.max(1, 1.5 * s);
            patch['polar.radialaxis.linewidth'] = Math.max(1, 1.5 * s);
        } else {
            // Standard XY Axes (Swath Profiler)
            patch[key + '.title.font.size'] = Math.round(14 * s);
            patch[key + '.tickfont.size'] = Math.round(11 * s);

            // Grid and axis lines
            patch[key + '.gridwidth'] = Math.max(0.5, 0.8 * s);
            patch[key + '.linewidth'] = Math.max(1, 1.5 * s);
        }
    });

    return patch;
}

/**
 * Scales the width of data traces (curves and bar borders).
 */
function buildTraceWidthPatch(traces, scaleFactor) {
    let widths = [];
    for (let i = 0; i < traces.length; i++) {
        let baseWidth = (traces[i].line && traces[i].line.width) ? traces[i].line.width : 1.5;
        widths.push(baseWidth * scaleFactor);
    }
    return { 'line.width': widths };
}

/**
 * Main Export Pipeline: 
 * Temporary UI boost -> Image Capture -> UI Reset.
 */
function exportHighDpi(divId, format, width, height, axisKeys, callback) {
    // We use 1000px as the reference "looks-good" width.
    const BASE_RESOLUTION = 1000;
    const gd = document.getElementById(divId);
    if (!gd || !gd.data) return;

    const scaleFactor = Math.max(1.0, width / BASE_RESOLUTION);

    // 1. Generate Patches
    const exportStylePatch = buildExportStylePatch(scaleFactor, axisKeys);
    const exportTracePatch = buildTraceWidthPatch(gd.data, scaleFactor);

    // 2. Store original state for restoration
    const originalStylePatch = buildExportStylePatch(1.0, axisKeys);
    const originalTracePatch = buildTraceWidthPatch(gd.data, 1.0);

    // 3. Execution chain
    Plotly.relayout(gd, exportStylePatch)
        .then(function () {
            return Plotly.restyle(gd, exportTracePatch);
        })
        .then(function () {
            return Plotly.toImage(gd, {
                format: format,
                width: width,
                height: height
            });
        })
        .then(function (dataUrl) {
            // Revert UI to screen-friendly sizes immediately after capture
            Plotly.relayout(gd, originalStylePatch);
            Plotly.restyle(gd, originalTracePatch);
            return dataUrl;
        })
        .then(function (dataUrl) {
            callback(dataUrl);
        })
        .catch(function (err) {
            console.error('[RockMorph] High-DPI Export failed:', err);
            // Emergency revert
            Plotly.relayout(gd, originalStylePatch);
            Plotly.restyle(gd, originalTracePatch);
        });
}