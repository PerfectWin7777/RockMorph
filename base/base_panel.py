# base/base_panel.py

from abc import ABCMeta, abstractmethod
import os

from PyQt5.QtWebChannel import QWebChannel # type: ignore
from PyQt5.QtWebEngineWidgets import QWebEngineView, QWebEngineSettings  # type: ignore
from PyQt5.QtWidgets import QWidget,QVBoxLayout,QScrollArea # type: ignore
from PyQt5.QtCore import (  # type: ignore
    Qt, QUrl, QObject, pyqtSlot, QCoreApplication
)
import sip # type: ignore

from ..core.exporter import RockMorphExporter

# Resolve metaclass conflict between QWidget (sip) and ABC
class QWidgetABCMeta(sip.wrappertype, ABCMeta):
    pass


def tr(message):
    return QCoreApplication.translate("RockMorph", message)


# ---------------------------------------------------------------------------
# Generic bridge — shared by all panels
# ---------------------------------------------------------------------------

class _BaseBridge(QObject):
    """
    QObject exposed to JavaScript via QWebChannel.
    Generic — works for every panel. No subclassing needed.
    """

    def __init__(self, panel, parent=None):
        super().__init__(parent)
        self._panel = panel

    @pyqtSlot(str)
    def receive_export(self, data_url: str):
        self._panel._save_export(data_url)



class BasePanel(QWidget, metaclass=QWidgetABCMeta):
    """
    Abstract base class for all RockMorph tool panels.
    Each panel owns its UI, calls its engine, and renders results.

    Subclasses must implement:
        _html_file()       → filename only, e.g. "rose.html"
        _build_ui()        → build all Qt widgets into self._inner
        _on_compute()      → read UI → call engine → call _on_result()
        _on_result(data)   → send data dict to webview

    Subclasses inherit for free:
        self.webview       → QWebEngineView, ready to use
        _setup_webchannel()
        _load_html()
        _save_export()
        _on_export()
        show_error() / show_info()

    Subclasses may override:
        _csv_headers()     → list of column names
        _build_csv_rows()  → list of row dicts
        cleanup()          → called on unload (rubber bands, signals, etc.)

    """

    def __init__(self, iface, parent=None):
        super().__init__(parent)
        self._exporter = RockMorphExporter(iface)
        self.iface = iface
    
        self._last_data: dict | None = None
        self._pending_export_path: str = ""

        # Root layout with scroll
        root = QVBoxLayout(self)
        root.setContentsMargins(0, 0, 0, 0)
        root.setSpacing(0)

        scroll = QScrollArea()
        scroll.setWidgetResizable(True)
        scroll.setHorizontalScrollBarPolicy(Qt.ScrollBarAlwaysOff)
        scroll.setVerticalScrollBarPolicy(Qt.ScrollBarAsNeeded)

        # Inner widget — this is what subclasses build into
        self._inner = QWidget()
        scroll.setWidget(self._inner)
        root.addWidget(scroll)

        # WebEngineView — created here so subclasses can use it in _build_ui
        self.webview = QWebEngineView()
        self._setup_webchannel()
    
        self._build_ui()
        self._load_html()
    

    # ------------------------------------------------------------------
    # Hooks — subclasses must implement these
    # ------------------------------------------------------------------

    @abstractmethod
    def _html_file(self) -> str:
        """
        Return the HTML filename for this tool.
        Example: return "rose.html"
        The file must live in the plugin's web/ directory.
        """
        pass

    @abstractmethod
    def _build_ui(self) -> None:
        """Build all Qt widgets for this tool."""
        pass

    @abstractmethod
    def _on_compute(self) -> None:
        """
        Triggered by the Compute button.
        Reads UI inputs → calls engine → passes result to _on_result().
        """
        pass

    @abstractmethod
    def _on_result(self, data: dict) -> None:
        """
        Receives the engine output dict.
        Updates the webview or any other display widget.
        """
        pass
    
    # ------------------------------------------------------------------
    # WebChannel — shared, never overridden
    # ------------------------------------------------------------------

    def _setup_webchannel(self) -> None:
        """
        Configure QWebEngineView settings and attach QWebChannel.
        Called once from __init__, before _build_ui.
        """
        settings = self.webview.settings()
        settings.setAttribute(
            QWebEngineSettings.LocalContentCanAccessFileUrls, True
        )
        settings.setAttribute(
            QWebEngineSettings.LocalContentCanAccessRemoteUrls, True
        )
        settings.setAttribute(
            QWebEngineSettings.AllowRunningInsecureContent, True
        )
        settings.setAttribute(
            QWebEngineSettings.JavascriptEnabled, True
        )

        self._bridge  = _BaseBridge(self)
        self._channel = QWebChannel()
        self._channel.registerObject("bridge", self._bridge)
        self.webview.page().setWebChannel(self._channel)

    # ------------------------------------------------------------------
    # HTML loading — shared, never overridden
    # ------------------------------------------------------------------

    def _web_dir(self) -> str:
        """
        Absolute path to the plugin's web/ directory.
        Works regardless of nesting depth because we anchor on this file.
        """
        return os.path.join(
            os.path.dirname(  # base/
            os.path.dirname(  # rockmorph/
                __file__
            )),
            "web"
        )

    def _load_html(self) -> None:
        """
        Resolve JS paths to absolute file:/// URIs and load the HTML.
        Subclass only needs to provide _html_file().
        """
        web_dir   = self._web_dir()
        html_path = os.path.join(web_dir, self._html_file())
        js_dir    = os.path.join(web_dir, "js").replace("\\", "/")

        with open(html_path, "r", encoding="utf-8") as f:
            html = f.read()

        # Patch relative JS src → absolute file:/// paths
        for script in ("qwebchannel.js", "plotly.min.js", "bridge.js"):
            html = html.replace(
                f'src="js/{script}"',
                f'src="file:///{js_dir}/{script}"'
            )

        # Write patched HTML to a temp file next to the original
        # so that relative resources (images, etc.) still resolve
        # temp_path = os.path.join(web_dir, f"_temp_{self._html_file()}")
        # with open(temp_path, "w", encoding="utf-8") as f:
        #     f.write(html)

        self.webview.load(QUrl.fromLocalFile(html_path))


    def _csv_headers(self) -> list:
        """
        Override in subclass to define CSV column names.
        Required if the tool supports CSV export.
        """
        return []

    def _build_csv_rows(self) -> list:
        """
        Override in subclass to build CSV row dicts.
        Required if the tool supports CSV export.
        """
        return []

    def show_error(self, message: str) -> None:
        """Push a warning message to the QGIS message bar."""
        self.iface.messageBar().pushWarning("RockMorph", message)

    def show_info(self, message: str) -> None:
        """Push an info message to the QGIS message bar."""
        self.iface.messageBar().pushInfo("RockMorph", message)

    def _save_export(self, data_url: str) -> None:
        """Called by JS bridge after Plotly.toImage()."""
        self._exporter.save_image(data_url, self._pending_export_path)
