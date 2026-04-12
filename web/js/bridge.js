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
       // Boost main title and push it up slightly (y: 0.95 to 0.98)
        'title.font.size': Math.round(18 * s),
        'title.y': 0.98, 
        
        // Legend scaling
        'legend.font.size': Math.round(12 * s),
        'legend.tracegroupgap': Math.round(10 * s),

        // Global margins must expand at high DPI to prevent labels from being cut off
        'margin.t': Math.round(30 * s),
        'margin.b': Math.round(30 * s),
        'margin.l': Math.round(50 * s),
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

            // CRITICAL: This prevents the title from overlapping the tick numbers
            // 'standoff' is the distance between the title and the axis
            patch[key + '.title.standoff'] = Math.round(20 * s);

            // Allow Plotly to automatically push margins if text is too long
            patch[key + '.automargin'] = true;

            // Grid and axis lines
            patch[key + '.showline'] = true;
            patch[key + '.gridwidth'] = Math.max(0.5, 0.9 * s);
            patch[key + '.linewidth'] = Math.max(1, 1.3 * s);
            patch[key + '.tickwidth'] = Math.max(1, 1.3 * s);
            patch[key + '.ticklen'] =   Math.max(1, 5.0 * s);

            // We scale the minor grid width (base was 0.5)
            // We use a slightly lower multiplier to keep them "secondary"
            patch[key + '.minor.gridwidth'] = Math.max(0.2, 0.8 * s);
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

// Scale dash patterns for high-DPI
function buildDashPatch(traces, scaleFactor) {
    var dashMap = {
        'dot': [2, 3],      // base: 2px on, 3px off
        'dash': [8, 4],      // base: 8px on, 4px off
        'dashdot': [8, 4, 2, 4] // base pattern
    };

    var dashes = [];
    for (var i = 0; i < traces.length; i++) {
        var currentDash = (traces[i].line && traces[i].line.dash)
            ? traces[i].line.dash
            : 'solid';

        if (currentDash === 'solid' || !dashMap[currentDash]) {
            dashes.push(currentDash); // solid reste solid
        } else {
            // Scale the pattern values
            var pattern = dashMap[currentDash];
            var scaled = pattern.map(function (v) {
                return Math.round(v * scaleFactor) + 'px';
            });
            dashes.push(scaled.join(','));
        }
    }
    return { 'line.dash': dashes };
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
    const exportDashPatch = buildDashPatch(gd.data, scaleFactor);

    // 2. Store original state for restoration
    const originalStylePatch = buildExportStylePatch(1.0, axisKeys);
    const originalTracePatch = buildTraceWidthPatch(gd.data, 1.0);
    const originalDashPatch = buildDashPatch(gd.data, 1.0);

    // 3. Execution chain
    Plotly.relayout(gd, exportStylePatch)
        .then(function () { return Plotly.restyle(gd, exportTracePatch); })
        .then(function () { return Plotly.restyle(gd, exportDashPatch); }) 
        .then(function () {
            return Plotly.toImage(gd, { format: format, width: width, height: height });
        })
        .then(function (dataUrl) {
            // Revert UI to screen-friendly sizes immediately after capture
            Plotly.relayout(gd, originalStylePatch);
            Plotly.restyle(gd, originalTracePatch);
            Plotly.restyle(gd, originalDashPatch);
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
            Plotly.restyle(gd, originalDashPatch);
        });
}