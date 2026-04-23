# base/base_panel.py

from abc import ABCMeta, abstractmethod
import os

from PyQt5.QtWebChannel import QWebChannel # type: ignore
from PyQt5.QtWebEngineWidgets import QWebEngineView, QWebEngineSettings  # type: ignore
from PyQt5.QtWidgets import (QWidget,QFrame,# type: ignore
    QVBoxLayout,QScrollArea,QProgressBar,QLabel
    ) 
from PyQt5.QtCore import (  # type: ignore
    Qt, QUrl, QObject, QThread, pyqtSignal, pyqtSlot, QCoreApplication
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
    
    @pyqtSlot(float)
    def on_plot_hover(self, distance):
        """Receives distance from JS when user hovers over the plot."""
        if hasattr(self._panel, "_on_plot_hover"):
            self._panel._on_plot_hover(distance)

    @pyqtSlot()
    def on_plot_leave(self):
        """Hides the map marker when mouse leaves the plot area."""
        if hasattr(self._panel, "_on_plot_leave"):
            self._panel._on_plot_leave()
    
    @pyqtSlot(int)
    def receive_click_id(self, fid: int):
        # Pass clicked feature ID to panel's click handler, if it exists
        if hasattr(self._panel, "_on_plot_click"):
            self._panel._on_plot_click(fid)

    




# ---------------------------------------------------------------------------
# Background worker — keeps UI responsive during long compute
# ---------------------------------------------------------------------------

class ComputeWorker(QThread):
    """
    Example
    Runs Engine.compute() in a background thread.
    Emits finished(result_dict) or error(message) and progress(value, message) signals.
    """
    finished = pyqtSignal(dict)
    error    = pyqtSignal(str)
    progress = pyqtSignal(int, str)

    def __init__(self, engine, params:dict):
        super().__init__()
        self._engine = engine
         # We copy params to avoid modifying the original dict in the UI thread
        self._params = params.copy()

    def run(self):
        try:
           # Inject the progress signal as a callback function.
            # The Engine doesn't need to know about Qt or Signals, 
            # it just calls a function: callback(int, str).
            self._params["progress_callback"] = self.progress.emit
            
            # Execute the heavy computation
            result = self._engine.compute(**self._params)
            self.finished.emit(result)
        except Exception as e:
              # Catch any crash to prevent QGIS from closing abruptly
            import traceback
            traceback.print_exc()
            self.error.emit(str(e))



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

        self._worker : ComputeWorker | None = None
    
        self._last_data: dict | None = None
        self._pending_export_path: str = ""
        self._pending_export_dpi  = 300
        self.div_id = 'plot-main'  # div ID for plotly export

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


        # --- FEEDBACK SYSTEM (Progress Bar + Label) ---
        # We create it here so it's ready for subclasses
        self._progress_container = QFrame()
        self._progress_container.setVisible(False) # Hidden by default
        prog_layout = QVBoxLayout(self._progress_container)
        prog_layout.setContentsMargins(0, 5, 0, 5)
        prog_layout.setSpacing(2)

        self._progress_label = QLabel(tr("Initializing..."))
        self._progress_label.setStyleSheet("color: #2d6a9f; font-weight: bold; font-size: 11px;")
        
        self._progress_bar = QProgressBar()
        self._progress_bar.setFixedHeight(12)
        self._progress_bar.setTextVisible(False)
        self._progress_bar.setStyleSheet("""
            QProgressBar {
                border: 1px solid #bbb;
                border-radius: 6px;
                background-color: #eee;
            }
            QProgressBar::chunk {
                background-color: #3498db;
                border-radius: 5px;
            }
        """)

        prog_layout.addWidget(self._progress_label)
        prog_layout.addWidget(self._progress_bar)



        # WebEngineView — created here so subclasses can use it in _build_ui
        self.webview = QWebEngineView()
        self._setup_webchannel()
    
        self._build_ui()
        self._load_html()
    
     # ------------------------------------------------------------------
    # Feedback Control Methods (The "Pro" Way)
    # ------------------------------------------------------------------

    def set_loading_state(self, loading: bool, message: str = "", total: int = 0):
        """
        Global toggle for the loading UI.
        
        :param loading: True to show progress, False to hide.
        :param message: The text to display.
        :param total: If > 0, sets the progress bar range. If 0, stays in 'busy' mode.
        """
        if loading:
            self._progress_label.setText(message)
            if total > 0:
                self._progress_bar.setRange(0, total)
                self._progress_bar.setValue(0)
            else:
                self._progress_bar.setRange(0, 0) # Indeterminate mode
            
            self._progress_container.show()
            
            # Optionally disable the compute button if the subclass has one
            if hasattr(self, 'compute_btn'):
                self.compute_btn.setEnabled(False)
        else:
            self._progress_container.hide()
            if hasattr(self, 'compute_btn'):
                self.compute_btn.setEnabled(True)

    def update_progress(self, value: int, message: str = None):
        """Update the progress bar value and optionally the message."""
        self._progress_bar.setValue(value)
        if message:
            self._progress_label.setText(message)


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
            html_content = f.read()

        # Patch relative JS src → absolute file:/// paths
        for script in ("qwebchannel.js", "plotly.min.js", "bridge.js"):
            html_content = html_content.replace(
                f'src="js/{script}"',
                f'src="file:///{js_dir}/{script}"'
            )

        # Write patched HTML to a temp file next to the original
        # so that relative resources (images, etc.) still resolve
        # temp_path = os.path.join(web_dir, f"_temp_{self._html_file()}")
        # with open(temp_path, "w", encoding="utf-8") as f:
        #     f.write(html)

        # self.webview.load(QUrl.fromLocalFile(html_path))
        self.webview.setHtml(html_content, QUrl.fromLocalFile(html_path))


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

    def _save_export(self, svg_data_url: str) -> None:
        """Called by JS bridge after Plotly.toImage()."""
        self._exporter.save_image( svg_data_url, self._pending_export_path)
