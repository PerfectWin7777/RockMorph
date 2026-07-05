"""
widgets/output_selector.py — Reusable Output Selection Widget for RockMorph

Provides a clean QCheckBox combined with a collapsible QLineEdit and browse QPushButton
to manage temporary (in-memory) or physical file outputs dynamically [2].

Authors: RockMorph contributors
"""

import tempfile
import os
from PyQt5.QtWidgets import (QWidget, # type: ignore
QHBoxLayout, QVBoxLayout, QCheckBox, QLineEdit, QPushButton, QFileDialog
                              )
from PyQt5.QtCore import pyqtSignal, Qt, QCoreApplication # type: ignore

def tr(message: str) -> str:
    return QCoreApplication.translate("RockMorph", message)


class OutputSelectorWidget(QWidget):
    """
    A unified, reusable widget that exposes a checkbox, and dynamically displays
    a path field with a file browser only if checked [2].
    """
    stateChanged = pyqtSignal(bool) # Emitted when checked state changes

    def __init__(
        self,
        label_text: str,
        default_filename: str,
        file_filter: str,
        is_checked: bool = False,
        parent=None
    ):
        super().__init__(parent)
        self.default_filename = default_filename
        self.file_filter = file_filter

        temp_dir = tempfile.gettempdir()
        self.default_temp_path = os.path.join(temp_dir, default_filename).replace("\\", "/")

        self._build_ui(label_text, is_checked)

    def _build_ui(self, label_text: str, is_checked: bool):
        layout = QVBoxLayout(self)
        layout.setContentsMargins(0, 0, 0, 0)
        layout.setSpacing(4)

        # 1. Primary Checkbox
        self.checkbox = QCheckBox(label_text)
        self.checkbox.setChecked(is_checked)
        self.checkbox.toggled.connect(self._on_toggled)
        layout.addWidget(self.checkbox)

        # 2. Path Selection Container (Collapsible) [2]
        self.path_container = QWidget()
        path_layout = QHBoxLayout(self.path_container)
        path_layout.setContentsMargins(18, 0, 0, 0) # Indent for visual hierarchy
        path_layout.setSpacing(6)

        self.txt_path = QLineEdit()
        self.txt_path.setText(self.default_temp_path)
        # self.txt_path.setReadOnly(True)
        self.txt_path.setStyleSheet("""
            QLineEdit {
                background-color: #f5f5f5;
                
                border: 1px solid #ccc;
                border-radius: 4px;
                padding: 3px 6px;
                font-size: 11px;
            }
        """)
        
        self.btn_browse = QPushButton("...")
        self.btn_browse.setFixedWidth(28)
        self.btn_browse.setFixedHeight(20)
        self.btn_browse.clicked.connect(self._on_browse)

        path_layout.addWidget(self.txt_path, stretch=1)
        path_layout.addWidget(self.btn_browse)

        layout.addWidget(self.path_container)

        # Apply initial collapsed/expanded state
        self.path_container.setVisible(is_checked)

    def _on_toggled(self, checked: bool):
        self.path_container.setVisible(checked)
        self.stateChanged.emit(checked)

    def _on_browse(self):
        """Opens a QFileDialog to select a physical save location [2]."""
        path, _ = QFileDialog.getSaveFileName(
            self,
            tr("Save Output Layer"),
            self.default_filename,
            self.file_filter
        )
        if path:
            self.txt_path.setText(path)
            # Make text readable (normal style) since it is now a real path
            self.txt_path.setStyleSheet("""
                QLineEdit {
                    background-color: #fff;
                    color: #000;
                    border: 1px solid #3a7fc1;
                    border-radius: 4px;
                    padding: 3px 6px;
                    font-size: 11px;
                }
            """)

    # ── Public APIs ──

    def isChecked(self) -> bool:
        return self.checkbox.isChecked()

    def setChecked(self, state: bool):
        self.checkbox.setChecked(state)

    def filePath(self) -> str:
        """
        Returns the absolute file path, or 'TEMPORARY_OUTPUT' if the user
        retains the default in-memory behavior [2].
        """
        text = self.txt_path.text().strip()
        if text == tr("Temporary Memory Layer") or not text:
            return "TEMPORARY_OUTPUT"
        return text