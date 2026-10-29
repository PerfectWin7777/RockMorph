"""
widgets/results_window.py

Large, non-modal results window shared by the synthesis tools
(Morphometric Indices, IAT, AHP, composite index, statistics / ML).

Why a separate window: the RockMorph dock is ~420 px wide, which is enough
for parameters but not for a 60-column table or a correlation matrix. The
window can be moved to a second screen and stays open while the user keeps
working in QGIS.

The window hosts ``web/morphometry.html`` in a QWebEngineView and exposes a
small bridge to it:

* ``select_basins(json_ids)``      – the page asks to select basins on the map;
                                     forwarded to the ``basinsSelected`` signal.
* ``save_figure(url, fmt, name)``  – the page hands over a PNG/SVG data URL;
                                     the user picks the file name here.

Python pushes data with ``show_payload(payload)``; the payload format is
documented at the top of ``web/js/morphometry.js``.

Authors: RockMorph contributors / Tony
"""

from __future__ import annotations

import base64
import json
import os
import re
import urllib.parse

from qgis.PyQt.QtCore import QObject, QUrl, Qt, pyqtSignal, pyqtSlot, QCoreApplication  # type: ignore
from qgis.PyQt.QtWidgets import (  # type: ignore
    QDialog, QFileDialog, QHBoxLayout, QLabel, QPushButton, QVBoxLayout,
)

from ..base.webengine import (  # optional in some QGIS builds (doc/03_qwebengine.md)
    WEBENGINE_AVAILABLE, QWebChannel, QWebEngineSettings, QWebEngineView,
)


def tr(message: str) -> str:
    return QCoreApplication.translate("RockMorph", message)


WEB_DIR = os.path.join(os.path.dirname(os.path.dirname(__file__)), "web")


def load_html_with_local_scripts(view, html_file: str) -> None:
    """
    Load ``web/<html_file>`` with every ``src="js/…"`` rewritten to an
    absolute file URL (QWebEngine ``setHtml`` does not resolve relative
    script paths reliably on Windows).
    """
    html_path = os.path.join(WEB_DIR, html_file)
    js_dir = os.path.join(WEB_DIR, "js").replace("\\", "/")
    with open(html_path, "r", encoding="utf-8") as handle:
        html = handle.read()
    html = re.sub(r'src="js/([^"]+)"', lambda m: f'src="file:///{js_dir}/{m.group(1)}"', html)
    view.setHtml(html, QUrl.fromLocalFile(html_path))


class _ResultsBridge(QObject):
    """QObject published to the page as ``bridge``."""

    basinsSelected = pyqtSignal(list)

    def __init__(self, window: "ResultsWindow"):
        super().__init__(window)
        self._window = window

    @pyqtSlot(str)
    def select_basins(self, json_ids: str) -> None:
        try:
            ids = json.loads(json_ids)
        except ValueError:
            return
        self.basinsSelected.emit([str(i) for i in ids])

    @pyqtSlot(str, str, str)
    def save_figure(self, data_url: str, fmt: str, name: str) -> None:
        self._window.save_figure(data_url, fmt, name)

    # Kept for compatibility with web/js/bridge.js helpers.
    @pyqtSlot(str)
    def receive_export(self, data_url: str) -> None:
        self._window.save_figure(data_url, "svg", "figure")


class ResultsWindow(QDialog):
    """
    Non-modal results window.

    Signals
    -------
    basinsSelected(list[str]) : basin ids clicked in the table.
    exportTableRequested()    : the user pressed "Export table…".
    """

    basinsSelected = pyqtSignal(list)
    exportTableRequested = pyqtSignal()

    def __init__(self, title: str, parent=None):
        super().__init__(parent)
        self.setWindowTitle(title)
        self.setWindowFlags(self.windowFlags() | Qt.WindowMaximizeButtonHint | Qt.WindowMinimizeButtonHint)
        self.setModal(False)
        self.resize(1200, 760)
        self._pending = None
        self._ready = False

        layout = QVBoxLayout(self)
        layout.setContentsMargins(0, 0, 0, 0)
        if not WEBENGINE_AVAILABLE:
            label = QLabel(tr("Qt WebEngine is not available in this QGIS installation: charts "
                              "cannot be displayed. The results are still in the output layer "
                              "and can be exported with 'Export table…'."))
            label.setWordWrap(True)
            label.setMargin(16)
            layout.addWidget(label)
            self.view = None
        else:
            self.view = QWebEngineView(self)
            settings = self.view.settings()
            settings.setAttribute(QWebEngineSettings.LocalContentCanAccessFileUrls, True)
            settings.setAttribute(QWebEngineSettings.JavascriptEnabled, True)
            # Every local page and script is UTF-8 (plotly.min.js holds characters
            # such as the minus sign U+2212): declare it instead of relying on a
            # locale-dependent default.
            settings.setDefaultTextEncoding("utf-8")
            self._bridge = _ResultsBridge(self)
            self._bridge.basinsSelected.connect(self.basinsSelected)
            self._channel = QWebChannel(self.view.page())
            self._channel.registerObject("bridge", self._bridge)
            self.view.page().setWebChannel(self._channel)
            self.view.loadFinished.connect(self._on_loaded)
            load_html_with_local_scripts(self.view, "morphometry.html")
            layout.addWidget(self.view, 1)

        footer = QHBoxLayout()
        footer.setContentsMargins(12, 6, 12, 8)
        self.status = QLabel("")
        self.status.setStyleSheet("color: #52514e;")
        footer.addWidget(self.status, 1)
        export = QPushButton(tr("Export table…"))
        export.setToolTip(tr("Save the table as CSV (UTF-8, comma separated) or GeoPackage."))
        export.clicked.connect(self.exportTableRequested)
        footer.addWidget(export)
        close = QPushButton(tr("Close"))
        close.clicked.connect(self.close)
        footer.addWidget(close)
        layout.addLayout(footer)

    # -- data ---------------------------------------------------------------

    def show_payload(self, payload: dict) -> None:
        """Display ``payload`` (queued until the page has loaded)."""
        self._pending = payload
        if self._ready:
            self._push()
        self.show()
        self.raise_()
        self.activateWindow()

    def _on_loaded(self, ok: bool) -> None:
        self._ready = ok
        if ok and self._pending is not None:
            self._push()

    def _push(self) -> None:
        if self.view is None:
            return
        # NaN/inf are not valid JSON: the payload builder converts them to
        # None, and allow_nan=False makes any slip fail loudly here.
        script = "renderResults(%s);" % json.dumps(self._pending, allow_nan=False)
        self.view.page().runJavaScript(script)

    # -- exports ------------------------------------------------------------

    def save_figure(self, data_url: str, fmt: str, name: str) -> None:
        """Ask for a file name and write the PNG/SVG received from the page."""
        fmt = "svg" if fmt == "svg" else "png"
        path, _ = QFileDialog.getSaveFileName(
            self, tr("Save chart"), f"{name}.{fmt}",
            tr("SVG vector image (*.svg)") if fmt == "svg" else tr("PNG image (*.png)"))
        if not path:
            return
        header, _, data = data_url.partition(",")
        if ";base64" in header:
            content = base64.b64decode(data)
        else:
            content = urllib.parse.unquote(data).encode("utf-8")
        with open(path, "wb") as handle:
            handle.write(content)
        self.status.setText(tr("Chart saved: {0}").format(path))
