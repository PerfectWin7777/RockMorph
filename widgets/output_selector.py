"""
widgets/output_selector.py — Reusable Output Selection Widget for RockMorph

Provides a clean QCheckBox (for optional outputs) or a QLabel (for mandatory outputs) 
combined with a collapsible QLineEdit and browse QPushButton to manage temporary 
(in-memory) or physical file outputs dynamically [2].

Authors: RockMorph contributors
"""

import os
import tempfile
from PyQt5.QtWidgets import (QWidget, # type: ignore
QHBoxLayout, QVBoxLayout, QCheckBox, QLineEdit, QPushButton, QFileDialog,
QLabel
                              )
from PyQt5.QtCore import pyqtSignal, Qt, QCoreApplication # type: ignore

def tr(message: str) -> str:
    return QCoreApplication.translate("RockMorph", message)


class OutputSelectorWidget(QWidget):
    """
    A unified, reusable widget that manages output selection.
    Supports optional checkbox-toggled paths or mandatory permanent paths [2].
    """
    stateChanged = pyqtSignal(bool)

    def __init__(
        self,
        label_text: str,
        default_filename: str,
        file_filter: str,
        is_checked: bool = True,
        show_checkbox: bool = True,  # If False, displays as a mandatory path (no checkbox) [2]
        parent=None
    ):
        super().__init__(parent)
        self.default_filename = default_filename
        self.file_filter = file_filter
        self.show_checkbox = show_checkbox

        # Generate a real, dynamic system temporary file path
        temp_dir = tempfile.gettempdir()
        self.default_temp_path = os.path.join(temp_dir, default_filename).replace("\\", "/")

        self._build_ui(label_text, is_checked)

    def _build_ui(self, label_text: str, is_checked: bool):
        layout = QVBoxLayout(self)
        layout.setContentsMargins(0, 0, 0, 0)
        layout.setSpacing(4)

        # 1. Label or Checkbox Header
        if self.show_checkbox:
            self.checkbox = QCheckBox(label_text)
            self.checkbox.setChecked(is_checked)
            self.checkbox.toggled.connect(self._on_toggled)
            layout.addWidget(self.checkbox)
        else:
            self.label = QLabel(label_text)
            self.label.setStyleSheet("font-weight: bold; color: #2c3e50;")
            layout.addWidget(self.label)

        # 2. Path Selection Container
        self.path_container = QWidget()
        path_layout = QVBoxLayout(self.path_container)
        # Only indent path inputs if there is a checkbox on top [2]
        path_layout.setContentsMargins(18 if self.show_checkbox else 0, 0, 0, 0)
        path_layout.setSpacing(6)

        # Row A (Horizontal): Path LineEdit + Browse Button
        row_file = QWidget()
        row_file_layout = QHBoxLayout(row_file)
        row_file_layout.setContentsMargins(0, 0, 0, 0)
        row_file_layout.setSpacing(6)

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

        row_file_layout.addWidget(self.txt_path, stretch=1)
        row_file_layout.addWidget(self.btn_browse)
        path_layout.addWidget(row_file) # Added as first row

        layout.addWidget(self.path_container)

        # Visibility logic
        if self.show_checkbox:
            self.path_container.setVisible(is_checked)
        else:
            self.path_container.setVisible(True) # Always visible for mandatory outputs

    def _on_toggled(self, checked: bool):
        if self.show_checkbox:
            self.path_container.setVisible(checked)
            self.stateChanged.emit(checked)

    def _on_browse(self):
        """Opens a QFileDialog to select a physical save location."""
        path, _ = QFileDialog.getSaveFileName(
            self,
            tr("Save Output Layer"),
            self.default_filename,
            self.file_filter
        )
        if path:
            self.txt_path.setText(path)
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
        if not self.show_checkbox:
            return True # Mandatory outputs are always active [2]
        return self.checkbox.isChecked()

    def setChecked(self, state: bool):
        if self.show_checkbox:
            self.checkbox.setChecked(state)

    def filePath(self) -> str:
        """
        Returns the absolute file path. If the checkbox is unchecked,
        returns 'TEMPORARY_OUTPUT' for in-memory layer generation [2].
        """
        if self.show_checkbox and not self.checkbox.isChecked():
            return "TEMPORARY_OUTPUT"
        return self.txt_path.text().strip()
    
    def addSettingsWidget(self, widget: QWidget) -> None:
        """
        Dynamically appends a custom settings widget (e.g. azimuth/altitude sliders)
        inside the collapsible path container [2].
        """
        # Append the custom settings layout to the existing path container layout
        self.path_container.layout().addWidget(widget)