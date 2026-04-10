
from qgis.PyQt.QtWidgets import QWidget, QVBoxLayout, QLabel # type: ignore
from qgis.PyQt.QtCore import Qt # type: ignore


class PlaceholderPanel(QWidget):

    def __init__(self, tool_name: str, parent=None):
        super().__init__(parent)
        layout = QVBoxLayout(self)
        layout.setAlignment(Qt.AlignCenter)

        label = QLabel(f"{tool_name}\n(coming soon)")
        label.setAlignment(Qt.AlignCenter)
        label.setStyleSheet("color: #888; font-size: 14px;")
        layout.addWidget(label)