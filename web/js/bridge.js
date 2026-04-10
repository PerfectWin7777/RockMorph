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