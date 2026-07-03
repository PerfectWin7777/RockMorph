from pathlib import Path
import json

from qgis.PyQt.QtCore import Qt, QSize # type: ignore
from qgis.PyQt.QtGui import ( # type: ignore
    QColor,
    QIcon,
    QPainter,
    QPixmap,
    QLinearGradient
)
from qgis.PyQt.QtWidgets import QComboBox # type: ignore



class MatplotlibColorMapComboBox(QComboBox):

    def __init__(self, json_path, parent=None):
        super().__init__(parent)

        self.setIconSize(QSize(120, 18))
        self.setMaxVisibleItems(20)

        with open(json_path, encoding="utf-8") as f:
            self.colormaps = json.load(f)

        self._populate()

        self.setCurrentText("terrain")
    

    def _populate(self):

        self.clear()

        for name in sorted(self.colormaps):

            icon = self._make_icon(self.colormaps[name])

            self.addItem(icon, name)

    def _make_icon(self, stops):

        w = 120
        h = 18

        pix = QPixmap(w, h)
        pix.fill(Qt.transparent)

        painter = QPainter(pix)

        gradient = QLinearGradient(0, 0, w, 0)

        for s in stops:

            gradient.setColorAt(
                s["pos"],
                QColor(
                    int(s["r"] * 255),
                    int(s["g"] * 255),
                    int(s["b"] * 255),
                )
            )

        painter.fillRect(0, 0, w, h, gradient)

        painter.setPen(Qt.black)
        painter.drawRect(0, 0, w - 1, h - 1)

        painter.end()

        return QIcon(pix)

    

    @property
    def current_name(self):

        return self.currentText()

    def stops(self):

        return self.colormaps[self.currentText()]