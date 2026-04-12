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
// SVG export — shared by all RockMorph HTML views
// ---------------------------------------------------------------------------

/**
 * Export current Plotly figure as SVG dataURL.
 * Python receives the SVG and handles rasterization at any DPI.
 *
 * @param {string} divId - id of the Plotly div
 */
function exportViaSvg(divId) {
    var gd = document.getElementById(divId);
    if (!gd) {
        console.error('[RockMorph] exportViaSvg: div not found:', divId);
        return;
    }
    Plotly.toImage(gd, {
        format: 'svg',
        width: gd.offsetWidth || 1200,
        height: gd.offsetHeight || 800
    }).then(function (dataUrl) {
        onBridgeReady(function (bridge) {
            bridge.receive_export(dataUrl);
        });
    }).catch(function (err) {
        console.error('[RockMorph] SVG export failed:', err);
    });
}
