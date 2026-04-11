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
 * Build a Plotly relayout patch that scales all text to remain
 * readable at high export resolutions.
 *
 * @param {number} fontScale  - ratio: exportWidth / BASE_EXPORT_WIDTH
 * @param {string[]} axisKeys - list of axis prefixes present in this plot
 *                              e.g. ['xaxis','yaxis','yaxis2'] for swath
 *                              e.g. ['polar'] for rose
 * @returns {Object} patch object ready for Plotly.relayout()
 */
function buildFontPatch(fontScale, axisKeys) {
    var s = fontScale;
    var patch = {
        'title.font.size': Math.round(14 * s),
        'legend.font.size': Math.round(11 * s),
    };

    axisKeys.forEach(function (key) {
        if (key === 'polar') {
            patch['polar.angularaxis.tickfont.size'] = Math.round(12 * s);
            patch['polar.radialaxis.tickfont.size'] = Math.round(9 * s);
        } else {
            patch[key + '.title.font.size'] = Math.round(12 * s);
            patch[key + '.tickfont.size'] = Math.round(11 * s);
        }
    });

    return patch;
}

/**
 * Scale trace line widths for high-DPI export.
 * Returns updated traces array — does NOT mutate the originals.
 *
 * @param {Array}  traces    - current Plotly traces (gd.data)
 * @param {number} lineScale - same ratio as fontScale
 * @returns {Object} relayout-style patch — use Plotly.restyle()
 */
function buildLineWidthPatch(traces, lineScale) {
    var widths = [];
    for (var i = 0; i < traces.length; i++) {
        var base = (traces[i].line && traces[i].line.width) ? traces[i].line.width : 1.5;
        widths.push(Math.max(1, base * lineScale));
    }
    return { 'line.width': widths };
}

/**
 * Run a high-DPI export on a Plotly div.
 * Scales fonts and line widths, exports, then restores original state.
 *
 * @param {string}   divId    - id of the Plotly div
 * @param {string}   format   - 'png' | 'jpeg' | 'svg'
 * @param {number}   width    - export width in pixels
 * @param {number}   height   - export height in pixels
 * @param {string[]} axisKeys - axis prefixes for this plot type
 * @param {Function} callback - called with dataUrl when done
 */
function exportHighDpi(divId, format, width, height, axisKeys, callback) {
    var BASE = 1920;
    var gd = document.getElementById(divId);
    if (!gd || !gd.data) { return; }

    var fontScale = Math.max(1.0, width / BASE);
    var fontPatch = buildFontPatch(fontScale, axisKeys);
    var linePatch = buildLineWidthPatch(gd.data, fontScale);

    // Store originals for restore
    var restoreFontPatch = buildFontPatch(1.0, axisKeys);
    var restoreLinePatch = buildLineWidthPatch(gd.data, 1.0);

    Plotly.relayout(gd, fontPatch)
        .then(function () { return Plotly.restyle(gd, linePatch); })
        .then(function () {
            return Plotly.toImage(gd, { format: format, width: width, height: height });
        })
        .then(function (dataUrl) {
            // Restore before calling back — UI must look normal even if callback is slow
            return Plotly.relayout(gd, restoreFontPatch)
                .then(function () { return Plotly.restyle(gd, restoreLinePatch); })
                .then(function () { return dataUrl; });
        })
        .then(function (dataUrl) { callback(dataUrl); })
        .catch(function (err) {
            console.error('[RockMorph] export failed:', err);
            // Always restore on error
            Plotly.relayout(gd, restoreFontPatch);
            Plotly.restyle(gd, restoreLinePatch);
        });
}