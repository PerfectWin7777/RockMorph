"""
base/webengine.py

Single place where RockMorph imports Qt WebEngine and Qt WebChannel.

Why: ``qgis.PyQt`` wraps QtWebEngineWidgets but has no ``QtWebChannel``
shim, and some QGIS builds (several Windows/OSGeo4W and Linux distribution
packages) ship without WebEngine at all. Importing it directly in every
panel made the whole plugin fail to load on those builds (audit M1).

Every module imports ``QWebEngineView``, ``QWebEngineSettings`` and
``QWebChannel`` from here. When WebEngine is missing they are replaced by
small fallbacks with the same methods the panels call: the view becomes a
label explaining that charts need Qt WebEngine, JavaScript calls are
ignored, and every computation, output layer and export keeps working.

Order tried for QWebChannel: the qgis.PyQt shim (future QGIS versions),
then PyQt5 (QGIS 3), then PyQt6 (QGIS 4).

Authors: RockMorph contributors / Tony
"""

from qgis.PyQt.QtCore import QCoreApplication, QObject, Qt  # type: ignore
from qgis.PyQt.QtWidgets import QLabel  # type: ignore

WEBENGINE_AVAILABLE = False
WEBENGINE_ERROR = ""

try:
    from qgis.PyQt.QtWebEngineWidgets import QWebEngineView, QWebEngineSettings  # type: ignore
    try:
        from qgis.PyQt.QtWebChannel import QWebChannel  # type: ignore
    except ImportError:
        try:
            from PyQt5.QtWebChannel import QWebChannel  # type: ignore
        except ImportError:
            from PyQt6.QtWebChannel import QWebChannel  # type: ignore
    WEBENGINE_AVAILABLE = True
except ImportError as error:  # pragma: no cover - depends on the QGIS build
    WEBENGINE_ERROR = str(error)


def _tr(message: str) -> str:
    return QCoreApplication.translate("RockMorph", message)


class _FallbackSettings:
    """Accepts the settings calls of the panels and ignores them."""

    LocalContentCanAccessFileUrls = 0
    LocalContentCanAccessRemoteUrls = 1
    AllowRunningInsecureContent = 2
    JavascriptEnabled = 3

    def setAttribute(self, *args) -> None:
        pass

    def setDefaultTextEncoding(self, *args) -> None:
        pass


class _FallbackPage:
    """Accepts page calls (JavaScript, web channel) and ignores them."""

    def runJavaScript(self, *args, **kwargs) -> None:
        pass

    def setWebChannel(self, *args) -> None:
        pass


class _FallbackWebView(QLabel):
    """Stand-in for QWebEngineView: a message instead of the charts."""

    def __init__(self, parent=None):
        super().__init__(parent)
        self.setWordWrap(True)
        self.setAlignment(Qt.AlignCenter)
        self.setMargin(16)
        self.setStyleSheet("color: #52514e;")
        self.setText(_tr("Charts need Qt WebEngine, which is not installed in this QGIS. "
                         "Computations, map layers and exports still work."))
        self._page = _FallbackPage()
        self._settings = _FallbackSettings()

    def page(self) -> _FallbackPage:
        return self._page

    def settings(self) -> _FallbackSettings:
        return self._settings

    def setHtml(self, *args) -> None:
        pass

    def load(self, *args) -> None:
        pass


class _FallbackChannel(QObject):
    """Stand-in for QWebChannel."""

    def registerObject(self, *args) -> None:
        pass


if not WEBENGINE_AVAILABLE:  # pragma: no cover - depends on the QGIS build
    QWebEngineView = _FallbackWebView  # noqa: F811
    QWebEngineSettings = _FallbackSettings  # noqa: F811
    QWebChannel = _FallbackChannel  # noqa: F811
