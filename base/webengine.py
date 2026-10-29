"""
base/webengine.py

Single place where RockMorph imports Qt WebEngine and Qt WebChannel.

Why: ``qgis.PyQt`` wraps QtWebEngineWidgets but has no ``QtWebChannel``
shim, and some QGIS builds (notably several Windows/OSGeo4W and Linux
distribution packages) ship without WebEngine at all. Importing it directly
in every panel made the whole plugin fail to load on those builds (audit
C9). Panels import from here and check ``WEBENGINE_AVAILABLE`` instead.

Order tried for QWebChannel: the qgis.PyQt shim (future QGIS versions),
then PyQt5 (QGIS 3), then PyQt6 (QGIS 4).

Authors: RockMorph contributors / Tony
"""

WEBENGINE_AVAILABLE = False
WEBENGINE_ERROR = ""
QWebEngineView = None
QWebEngineSettings = None
QWebChannel = None

try:
    from qgis.PyQt.QtWebEngineWidgets import QWebEngineView, QWebEngineSettings  # type: ignore  # noqa: F811
    try:
        from qgis.PyQt.QtWebChannel import QWebChannel  # type: ignore  # noqa: F811
    except ImportError:
        try:
            from PyQt5.QtWebChannel import QWebChannel  # type: ignore  # noqa: F811
        except ImportError:
            from PyQt6.QtWebChannel import QWebChannel  # type: ignore  # noqa: F811
    WEBENGINE_AVAILABLE = True
except ImportError as error:  # pragma: no cover - depends on the QGIS build
    WEBENGINE_ERROR = str(error)
