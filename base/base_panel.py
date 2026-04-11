# base/base_panel.py

from PyQt5.QtWidgets import QWidget,QVBoxLayout,QScrollArea # type: ignore
from PyQt5.QtCore import QCoreApplication,Qt # type: ignore
from abc import ABCMeta, abstractmethod
import sip # type: ignore


# Resolve metaclass conflict between QWidget (sip) and ABC
class QWidgetABCMeta(sip.wrappertype, ABCMeta):
    pass


def tr(message):
    return QCoreApplication.translate("RockMorph", message)


class BasePanel(QWidget, metaclass=QWidgetABCMeta):
    """
    Abstract base class for all RockMorph tool panels.
    Each panel owns its UI, calls its engine, and renders results.
    """

    def __init__(self, iface, parent=None):
        super().__init__(parent)
        self.iface = iface

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

        self._build_ui()

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