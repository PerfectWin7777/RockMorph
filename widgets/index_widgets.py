"""
widgets/index_widgets.py

Qt widgets generated from the declarative index catalogue
(``core.indices``). They contain no science: every label, default, range,
help text and classification comes from the ``IndexSpec`` objects, so adding
an index to the catalogue makes it appear in the UI with no extra code.

IndexCatalogTree      searchable family → index tree with check boxes and
                      presets; emits the checked keys and the focused key.
IndexInfoCard         rich-text card: formula, unit, authors, reference,
                      implementation notes, data needed.
ParameterForm         one widget per ParameterSpec (spin box, combo, check).
ClassificationEditor  pick a published scheme and edit its thresholds; the
                      edited copy is validated before it can be used.

Authors: RockMorph contributors / Tony
"""

from __future__ import annotations

import html
import math
from typing import Dict, List, Optional, Sequence

from qgis.PyQt.QtCore import QCoreApplication, Qt, pyqtSignal  # type: ignore
from qgis.PyQt.QtWidgets import (  # type: ignore
    QAbstractItemView, QCheckBox, QComboBox, QDoubleSpinBox, QFormLayout,
    QHBoxLayout, QHeaderView, QLabel, QLineEdit, QPushButton, QSpinBox,
    QTableWidget, QTableWidgetItem, QToolButton, QTreeWidget, QTreeWidgetItem,
    QVBoxLayout, QWidget,
)

from ..core.indices import PRESETS, families
from ..core.indices.spec import Classification, ClassRule, IndexSpec, ParameterSpec


def tr(message: str) -> str:
    return QCoreApplication.translate("RockMorph", message)


# Plain-language names of the data requirements, shown in the info card so
# the user understands why, e.g., a stream network or a long run is needed.
REQUIREMENT_LABELS = {
    "geometry": "basin polygon",
    "elevation": "DEM values in the basin",
    "terrain": "slope and flow accumulation",
    "channel": "trunk-stream profile (flow routing)",
    "network": "stream network (vector layer or derived from the DEM)",
    "asymmetry": "basin split by the trunk stream",
    "sections": "transverse sections across the basin",
    "valley": "valley cross-profiles",
}


# ---------------------------------------------------------------------------
# Catalogue tree
# ---------------------------------------------------------------------------

class IndexCatalogTree(QWidget):
    """
    Family → index tree with check boxes.

    Signals
    -------
    selectionChanged(list[str]) : checked index keys (catalogue order).
    focusChanged(str)           : key of the index clicked (for the info card).
    """

    selectionChanged = pyqtSignal(list)
    focusChanged = pyqtSignal(str)

    def __init__(self, include_experimental: bool = True, parent=None):
        super().__init__(parent)
        self._items: Dict[str, QTreeWidgetItem] = {}
        self._updating = False

        layout = QVBoxLayout(self)
        layout.setContentsMargins(0, 0, 0, 0)
        layout.setSpacing(4)

        presets_row = QHBoxLayout()
        self.preset_combo = QComboBox()
        self.preset_combo.addItem(tr("Preset…"), None)
        for name in PRESETS:
            self.preset_combo.addItem(name, name)
        self.preset_combo.setToolTip(tr("Tick a published set of indices in one click "
                                        "(adds to the current selection)."))
        self.preset_combo.activated.connect(self._apply_preset)
        presets_row.addWidget(self.preset_combo, 1)
        clear = QToolButton()
        clear.setText(tr("Clear"))
        clear.setToolTip(tr("Untick every index."))
        clear.clicked.connect(lambda: self.set_selection([]))
        presets_row.addWidget(clear)
        layout.addLayout(presets_row)

        self.search = QLineEdit()
        self.search.setPlaceholderText(tr("Search an index (name, key, author)…"))
        self.search.setClearButtonEnabled(True)
        self.search.textChanged.connect(self._filter)
        layout.addWidget(self.search)

        self.tree = QTreeWidget()
        self.tree.setHeaderHidden(True)
        self.tree.setUniformRowHeights(True)
        self.tree.setMinimumHeight(220)
        layout.addWidget(self.tree)

        self.count_label = QLabel()
        self.count_label.setStyleSheet("color: #52514e;")
        layout.addWidget(self.count_label)

        for family, specs in families():
            if not include_experimental:
                specs = [s for s in specs if not s.experimental]
            if not specs:
                continue
            parent_item = QTreeWidgetItem(self.tree, [family])
            parent_item.setFlags(parent_item.flags() | Qt.ItemIsUserCheckable | Qt.ItemIsAutoTristate)
            parent_item.setCheckState(0, Qt.Unchecked)
            for spec in specs:
                text = f"{spec.name}  [{spec.key}]"
                if spec.experimental:
                    text += "  ⚗"
                item = QTreeWidgetItem(parent_item, [text])
                item.setData(0, Qt.UserRole, spec.key)
                item.setFlags(item.flags() | Qt.ItemIsUserCheckable)
                item.setCheckState(0, Qt.Unchecked)
                tip = spec.description
                if spec.experimental:
                    tip += "\n\n" + tr("Experimental: empirical relation whose units or source are "
                                       "not settled; interpret with care.")
                item.setToolTip(0, tip)
                self._items[spec.key] = item
        self.tree.itemChanged.connect(self._on_item_changed)
        self.tree.currentItemChanged.connect(self._on_current_changed)
        self._update_count()

    # -- public ---------------------------------------------------------------

    def selected_keys(self) -> List[str]:
        return [key for key, item in self._items.items() if item.checkState(0) == Qt.Checked]

    def set_selection(self, keys: Sequence[str]) -> None:
        wanted = set(keys)
        self._updating = True
        for key, item in self._items.items():
            item.setCheckState(0, Qt.Checked if key in wanted else Qt.Unchecked)
        self._updating = False
        self._emit_selection()

    def focus_key(self, key: str) -> None:
        """Make ``key`` the current item (shows its settings)."""
        item = self._items.get(key)
        if item is not None:
            self.tree.setCurrentItem(item)

    # -- internals ------------------------------------------------------------

    def _apply_preset(self, row: int) -> None:
        name = self.preset_combo.itemData(row)
        self.preset_combo.setCurrentIndex(0)
        if not name:
            return
        self.set_selection(set(self.selected_keys()) | set(PRESETS[name]))
        self.focus_key(PRESETS[name][0])

    def _filter(self, text: str) -> None:
        needle = text.strip().lower()
        root = self.tree.invisibleRootItem()
        for i in range(root.childCount()):
            family = root.child(i)
            visible = 0
            for j in range(family.childCount()):
                item = family.child(j)
                hit = not needle or needle in item.text(0).lower() or needle in item.toolTip(0).lower()
                item.setHidden(not hit)
                visible += hit
            family.setHidden(visible == 0)
            family.setExpanded(bool(needle) and visible > 0)

    def _on_item_changed(self, item: QTreeWidgetItem, column: int) -> None:
        if not self._updating:
            self._emit_selection()

    def _emit_selection(self) -> None:
        self._update_count()
        self.selectionChanged.emit(self.selected_keys())

    def _update_count(self) -> None:
        n = len(self.selected_keys())
        self.count_label.setText(tr("{0} index(es) selected").format(n) if n
                                 else tr("No index selected"))

    def _on_current_changed(self, current: QTreeWidgetItem, previous) -> None:
        if current is not None and current.data(0, Qt.UserRole):
            self.focusChanged.emit(current.data(0, Qt.UserRole))


# ---------------------------------------------------------------------------
# Info card
# ---------------------------------------------------------------------------

class IndexInfoCard(QLabel):
    """Rich-text description of one index."""

    def __init__(self, parent=None):
        super().__init__(parent)
        self.setWordWrap(True)
        self.setTextFormat(Qt.RichText)
        self.setOpenExternalLinks(True)
        self.setTextInteractionFlags(Qt.TextBrowserInteraction)
        self.setStyleSheet("QLabel { background: #fcfcfb; border: 1px solid #dcdbd6; "
                           "border-radius: 4px; padding: 6px; }")
        self.show_spec(None)

    def show_spec(self, spec: Optional[IndexSpec]) -> None:
        if spec is None:
            self.setText(tr("<i>Click an index in the catalogue to see its formula, "
                            "references and settings.</i>"))
            return
        esc = html.escape
        ref = spec.reference
        link = f'<a href="{esc(ref if ref.startswith("http") else "https://doi.org/" + ref)}">{esc(ref)}</a>' \
            if ref else ""
        needs = ", ".join(REQUIREMENT_LABELS.get(r, r) for r in spec.requires)
        parts = [
            f"<b>{esc(spec.name)}</b> <span style='color:#7a7974'>[{esc(spec.key)}]</span>",
            f"<div style='margin:4px 0'><code>{esc(spec.formula)}</code>"
            + (f" &nbsp;<span style='color:#52514e'>unit: {esc(spec.unit)}</span>" if spec.unit else "")
            + "</div>",
            f"<div>{esc(spec.description)}</div>",
            f"<div style='color:#52514e;margin-top:4px'>{esc(spec.authors)} {link}</div>",
            f"<div style='color:#52514e'>{tr('Needs')}: {esc(needs)}</div>",
        ]
        if spec.notes:
            parts.append(f"<div style='margin-top:4px'><i>{tr('Implementation')}:</i> {esc(spec.notes)}</div>")
        if spec.experimental:
            parts.append(f"<div style='color:#d03b3b'>⚗ {tr('Experimental index: interpret with care.')}</div>")
        self.setText("".join(parts))


# ---------------------------------------------------------------------------
# Parameter form
# ---------------------------------------------------------------------------

class ParameterForm(QWidget):
    """
    Widgets generated from a list of ``ParameterSpec``.

    ``values()`` returns the current values; ``changed`` fires on any edit.
    """

    changed = pyqtSignal()

    def __init__(self, parent=None):
        super().__init__(parent)
        self._layout = QFormLayout(self)
        self._layout.setContentsMargins(0, 0, 0, 0)
        self._layout.setFieldGrowthPolicy(QFormLayout.AllNonFixedFieldsGrow)
        self._widgets: Dict[str, QWidget] = {}
        self._specs: Dict[str, ParameterSpec] = {}

    def set_parameters(self, specs: Sequence[ParameterSpec], values: Optional[dict] = None) -> None:
        while self._layout.rowCount():
            self._layout.removeRow(0)
        self._widgets.clear()
        self._specs = {p.key: p for p in specs}
        values = values or {}
        if not specs:
            note = QLabel(tr("<i>This index has no parameter.</i>"))
            self._layout.addRow(note)
            return
        for spec in specs:
            widget = self._make_widget(spec, values.get(spec.key, spec.default))
            label = QLabel(spec.label + (f" ({spec.unit})" if spec.unit else ""))
            label.setToolTip(spec.help)
            widget.setToolTip(spec.help)
            self._layout.addRow(label, widget)
            self._widgets[spec.key] = widget

    def _make_widget(self, spec: ParameterSpec, value) -> QWidget:
        value = spec.coerce(value)
        if spec.kind == "int":
            widget = QSpinBox()
            widget.setRange(int(spec.minimum if spec.minimum is not None else -10 ** 9),
                            int(spec.maximum if spec.maximum is not None else 10 ** 9))
            widget.setSingleStep(int(spec.step or 1))
            widget.setValue(int(value))
            widget.valueChanged.connect(self.changed)
        elif spec.kind == "float":
            widget = QDoubleSpinBox()
            widget.setDecimals(spec.decimals)
            widget.setRange(spec.minimum if spec.minimum is not None else -1e12,
                            spec.maximum if spec.maximum is not None else 1e12)
            widget.setSingleStep(spec.step or 10 ** -spec.decimals)
            widget.setValue(float(value))
            widget.valueChanged.connect(self.changed)
        elif spec.kind == "choice":
            widget = QComboBox()
            widget.addItems(list(spec.choices))
            widget.setCurrentText(str(value))
            widget.currentIndexChanged.connect(self.changed)
        else:
            widget = QCheckBox()
            widget.setChecked(bool(value))
            widget.toggled.connect(self.changed)
        return widget

    def values(self) -> dict:
        result = {}
        for key, widget in self._widgets.items():
            if isinstance(widget, (QSpinBox, QDoubleSpinBox)):
                result[key] = widget.value()
            elif isinstance(widget, QComboBox):
                result[key] = widget.currentText()
            elif isinstance(widget, QCheckBox):
                result[key] = widget.isChecked()
        return result


# ---------------------------------------------------------------------------
# Classification editor
# ---------------------------------------------------------------------------

def _format_bound(value: float) -> str:
    if math.isinf(value):
        return "-inf" if value < 0 else "inf"
    return f"{value:g}"


def _parse_bound(text: str) -> float:
    text = text.strip().lower().replace(",", ".").replace("∞", "inf")
    if text in ("", "inf", "+inf"):
        return math.inf
    if text == "-inf":
        return -math.inf
    return float(text)


class ClassificationEditor(QWidget):
    """
    Choose a published classification of an index and edit its thresholds.

    The table holds one row per class: lower bound (inclusive), upper bound
    (exclusive; the last finite upper bound is inclusive), label and score.
    Editing creates a "custom" copy; ``validation_error()`` explains what is
    wrong (gaps, overlaps, non-numeric bounds) and the run stays blocked
    until it is fixed.
    """

    changed = pyqtSignal()

    NONE_KEY = "__none__"

    def __init__(self, parent=None):
        super().__init__(parent)
        self._spec: Optional[IndexSpec] = None
        self._loading = False
        layout = QVBoxLayout(self)
        layout.setContentsMargins(0, 0, 0, 0)
        layout.setSpacing(4)

        row = QHBoxLayout()
        self.scheme_combo = QComboBox()
        self.scheme_combo.currentIndexChanged.connect(self._on_scheme_changed)
        row.addWidget(self.scheme_combo, 1)
        self.reset_btn = QToolButton()
        self.reset_btn.setText(tr("Reset"))
        self.reset_btn.setToolTip(tr("Restore the published thresholds."))
        self.reset_btn.clicked.connect(lambda: self._load_rules(self._published()))
        row.addWidget(self.reset_btn)
        layout.addLayout(row)

        self.reference = QLabel()
        self.reference.setWordWrap(True)
        self.reference.setStyleSheet("color: #52514e;")
        layout.addWidget(self.reference)

        self.table = QTableWidget(0, 4)
        self.table.setHorizontalHeaderLabels([tr("From ≥"), tr("To <"), tr("Class label"), tr("Score")])
        self.table.horizontalHeader().setSectionResizeMode(2, QHeaderView.Stretch)
        for column in (0, 1, 3):
            self.table.horizontalHeader().setSectionResizeMode(column, QHeaderView.ResizeToContents)
        self.table.verticalHeader().setVisible(False)
        self.table.setSelectionMode(QAbstractItemView.NoSelection)
        self.table.setToolTip(tr("Edit a threshold by double-clicking it. Use 'inf' / '-inf' for open "
                                 "bounds. Classes must follow each other without gap or overlap."))
        self.table.itemChanged.connect(self._on_edit)
        layout.addWidget(self.table)

        self.error = QLabel()
        self.error.setStyleSheet("color: #d03b3b;")
        self.error.setWordWrap(True)
        layout.addWidget(self.error)
        self.set_index(None)

    # -- public ---------------------------------------------------------------

    def set_index(self, spec: Optional[IndexSpec], current=None) -> None:
        """
        Show the schemes of ``spec``. ``current`` restores a previous choice:
        a ``Classification`` (published or edited), ``NONE_KEY`` when the
        user chose "No classification", or None for the index default.
        """
        self._spec = spec
        self._loading = True
        self.scheme_combo.clear()
        self.scheme_combo.addItem(tr("No classification"), self.NONE_KEY)
        if spec is not None:
            for scheme in spec.classifications:
                self.scheme_combo.addItem(scheme.label, scheme.key)
        self._loading = False
        has = spec is not None and bool(spec.classifications)
        self.setEnabled(spec is not None)
        if not has:
            self.scheme_combo.setCurrentIndex(0)
            self._load_rules(None)
            self.reference.setText(tr("No published classification for this index.") if spec else "")
            return
        if current == self.NONE_KEY:
            self.scheme_combo.setCurrentIndex(0)
            self._load_rules(None)
            return
        key = current.key.replace(":custom", "") if current is not None else spec.default_classification
        position = self.scheme_combo.findData(key) if key else 0
        self._loading = True
        self.scheme_combo.setCurrentIndex(max(position, 0))
        self._loading = False
        self._load_rules(self._published())
        if current is not None and current.key.endswith(":custom"):
            self._load_rules(current)

    def choice(self):
        """State to store per index: a Classification, NONE_KEY, or None if invalid."""
        if self._published() is None:
            return self.NONE_KEY
        return self.classification()

    def classification(self) -> Optional[Classification]:
        """Current (possibly edited) scheme, or None when invalid / not chosen."""
        published = self._published()
        if published is None or self.validation_error():
            return None
        rules = self._read_rules()
        if rules == list(published.rules):
            return published
        return Classification(
            key=f"{published.key}:custom", label=f"{published.label} (edited)",
            authors=published.authors, reference=published.reference, rules=tuple(rules),
            description=published.description, transform=published.transform)

    def validation_error(self) -> str:
        if self._published() is None:
            return ""
        try:
            rules = self._read_rules()
        except ValueError as error:
            return str(error)
        for a, b in zip(rules[:-1], rules[1:]):
            if a.upper != b.lower:
                return tr("Classes '{0}' and '{1}' must share their bound (gap or overlap).") \
                    .format(a.label, b.label)
        for rule in rules:
            if not rule.lower < rule.upper:
                return tr("Class '{0}': 'From' must be lower than 'To'.").format(rule.label)
        return ""

    # -- internals ------------------------------------------------------------

    def _published(self) -> Optional[Classification]:
        key = self.scheme_combo.currentData()
        if self._spec is None or key in (None, self.NONE_KEY):
            return None
        return self._spec.classification(key)

    def _on_scheme_changed(self, _row: int) -> None:
        if self._loading:
            return
        self._load_rules(self._published())

    def _load_rules(self, scheme: Optional[Classification]) -> None:
        self._loading = True
        self.table.setRowCount(0)
        if scheme is not None:
            for rule in scheme.rules:
                row = self.table.rowCount()
                self.table.insertRow(row)
                for column, text in enumerate((_format_bound(rule.lower), _format_bound(rule.upper),
                                               rule.label, "" if rule.score is None else str(rule.score))):
                    self.table.setItem(row, column, QTableWidgetItem(text))
            transform = {"abs_minus_50": tr("Classes apply to |value − 50|."),
                         "abs": tr("Classes apply to |value|.")}.get(scheme.transform or "", "")
            self.reference.setText(f"{scheme.authors} — {scheme.reference}"
                                   + (f"\n{scheme.description}" if scheme.description else "")
                                   + (f"\n{transform}" if transform else ""))
        else:
            self.reference.setText("")
        self.table.setVisible(scheme is not None)
        self.reset_btn.setEnabled(scheme is not None)
        # Size the table to its rows (no inner scroll bar for 3-5 classes).
        # Section sizes are used rather than rowHeight(): rows of a hidden
        # table report 0 until the first layout pass.
        header = self.table.horizontalHeader().sizeHint().height()
        row = self.table.verticalHeader().defaultSectionSize()
        height = header + row * self.table.rowCount() + 2 * self.table.frameWidth()
        self.table.setFixedHeight(min(max(height, 40), 220))
        self._loading = False
        self.error.setText("")
        self.changed.emit()

    def _read_rules(self) -> List[ClassRule]:
        rules = []
        for row in range(self.table.rowCount()):
            cells = [self.table.item(row, c).text() if self.table.item(row, c) else "" for c in range(4)]
            try:
                lower, upper = _parse_bound(cells[0]), _parse_bound(cells[1])
            except ValueError:
                raise ValueError(tr("Row {0}: bounds must be numbers, 'inf' or '-inf'.").format(row + 1))
            score = None
            if cells[3].strip():
                try:
                    score = int(cells[3])
                except ValueError:
                    raise ValueError(tr("Row {0}: the score must be an integer.").format(row + 1))
            rules.append(ClassRule(lower, upper, cells[2].strip() or f"Class {row + 1}", score))
        return rules

    def _on_edit(self, _item) -> None:
        if self._loading:
            return
        self.error.setText(self.validation_error())
        self.changed.emit()
