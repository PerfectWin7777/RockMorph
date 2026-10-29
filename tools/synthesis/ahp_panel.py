"""
tools/synthesis/ahp_panel.py

Analytic Hierarchy Process (AHP) weighting tool — Saaty (1977, 1980).

The user compares the criteria two by two on Saaty's 1–9 scale; the weights
are the principal eigenvector of the reciprocal comparison matrix, and the
consistency ratio CR = CI / RI tells whether the judgements are coherent
(CR ≤ 0.10 is Saaty's acceptance threshold).

Each criterion is rescaled to 0–1 (min–max over the processed basins,
reversed when "low = active") and the AHP value is the weighted mean, so
1 = most active / highest priority. Missing criteria are skipped and the
weights renormalised for that basin (a missing value is not a zero).

Choices made here
-----------------
* Weights: principal eigenvector (Saaty's definition). GSV-Studio used the
  column-normalisation approximation; it is offered as an option and gives
  the same weights for a perfectly consistent matrix.
* A matrix with CR > 0.10 blocks the run unless the user explicitly accepts
  it — Saaty recommends revising the judgements instead.
* The result can be divided into four classes (quantiles by default) with
  class 1 = highest values, as for the Iat classes.

Authors: RockMorph contributors / Tony
"""

from __future__ import annotations

from fractions import Fraction
from typing import Dict, List, Optional

import numpy as np  # type: ignore
from qgis.PyQt.QtCore import QCoreApplication, Qt  # type: ignore
from qgis.PyQt.QtWidgets import (  # type: ignore
    QCheckBox, QComboBox, QFormLayout, QHeaderView, QLabel, QSizePolicy, QTableWidget,
    QTableWidgetItem, QVBoxLayout,
)

from ...core.indices.multicriteria import (
    CLASS_BREAK_METHODS, RANDOM_INDEX, SAATY_SCALE, AhpResult, ahp_weights, min_max,
    rank_classes, reciprocal_matrix, weighted_sum,
)
from .common import CriteriaTable, OutputColumn, SynthesisPanel, TableData, section_label


def tr(message: str) -> str:
    return QCoreApplication.translate("RockMorph", message)


# Judgement choices, strongest preference for the ROW criterion first.
JUDGEMENTS = [9, 8, 7, 6, 5, 4, 3, 2, 1] + [Fraction(1, k) for k in range(2, 10)]
MAX_CRITERIA = max(RANDOM_INDEX)


def _judgement_text(value) -> str:
    """Short cell text ("5", "1/3"); the meaning is in the item tooltip."""
    return f"1/{value.denominator}" if isinstance(value, Fraction) else str(value)


def _judgement_meaning(value) -> str:
    if isinstance(value, Fraction):
        return tr("{0} of the COLUMN over the row").format(SAATY_SCALE[value.denominator].lower())
    return SAATY_SCALE[value] + ("" if value == 1 else " " + tr("of the ROW over the column"))


class AhpPanel(SynthesisPanel):
    """AHP weighting of index fields (sidebar)."""

    TITLE = "AHP weighting"
    OUTPUT_PREFIX = "AHP"
    MIN_FIELDS = 2
    INTRO = ("Weight several index fields with the Analytic Hierarchy Process (Saaty): compare the "
             "criteria two by two, check the consistency ratio, then compute the weighted index.")

    def __init__(self, iface, parent=None):
        self._judgements: Dict[tuple, object] = {}
        self._ahp: Optional[AhpResult] = None
        super().__init__(iface, parent)

    # -- settings -----------------------------------------------------------------

    def _build_settings(self, layout: QVBoxLayout) -> None:
        layout.addWidget(section_label(tr("<b>Criteria</b>")))
        self.criteria = CriteriaTable(with_weights=False)
        self.criteria.changed.connect(self._refresh)
        layout.addWidget(self.criteria)

        layout.addWidget(section_label(tr("<b>Pairwise comparisons</b> — how much more important is the "
                                   "ROW criterion than the COLUMN criterion?")))
        self.matrix = QTableWidget(0, 0)
        self.matrix.horizontalHeader().setSectionResizeMode(QHeaderView.ResizeToContents)
        self.matrix.verticalHeader().setSectionResizeMode(QHeaderView.ResizeToContents)
        self.matrix.setToolTip(tr("Saaty scale: 1 equal, 3 moderate, 5 strong, 7 very strong, 9 extreme "
                                  "importance of the row over the column; 1/3 … 1/9 the reverse. The "
                                  "lower triangle is filled with the reciprocals."))
        # The table scrolls inside the narrow dock instead of widening it.
        self.matrix.setSizePolicy(QSizePolicy.Ignored, QSizePolicy.Fixed)
        layout.addWidget(self.matrix)
        legend = QLabel(tr("Saaty scale: 1 equal · 3 moderate · 5 strong · 7 very strong · 9 extreme "
                           "(2, 4, 6, 8 intermediate). 1/x = the column is x times more important."))
        legend.setWordWrap(True)
        legend.setStyleSheet("color: #52514e;")
        layout.addWidget(legend)

        self.diagnostics = QLabel()
        self.diagnostics.setWordWrap(True)
        self.diagnostics.setTextFormat(Qt.RichText)
        layout.addWidget(self.diagnostics)

        form = QFormLayout()
        self.method = QComboBox()
        self.method.addItem(tr("Principal eigenvector (Saaty)"), "eigenvector")
        self.method.addItem(tr("Column normalisation (GSV-Studio)"), "column_normalisation")
        self.method.currentIndexChanged.connect(self._update_weights)
        form.addRow(tr("Weights"), self.method)
        self.accept_inconsistent = QCheckBox(tr("Accept CR > 0.10 (not recommended)"))
        self.accept_inconsistent.toggled.connect(self._refresh)
        form.addRow("", self.accept_inconsistent)
        self.class_method = QComboBox()
        self.class_method.addItems(list(CLASS_BREAK_METHODS))
        self.class_method.setCurrentText("Quantiles")
        self.class_method.setToolTip(tr("Divide the AHP value into 4 classes (class 1 = highest values)."))
        form.addRow(tr("Result classes"), self.class_method)
        layout.addLayout(form)

    def _on_fields_changed(self) -> None:
        if not hasattr(self, "matrix"):
            return
        names = self.checked_fields()
        self.criteria.set_criteria(names)
        self._rebuild_matrix(names)

    def _rebuild_matrix(self, names: List[str]) -> None:
        self.matrix.blockSignals(True)
        n = len(names)
        self.matrix.clear()
        self.matrix.setRowCount(n)
        self.matrix.setColumnCount(n)
        short = [name if len(name) <= 10 else name[:9] + "…" for name in names]
        self.matrix.setHorizontalHeaderLabels(short)
        self.matrix.setVerticalHeaderLabels(short)
        for i in range(n):
            for j in range(n):
                if i == j:
                    item = QTableWidgetItem("1")
                    item.setFlags(Qt.ItemIsEnabled)
                    item.setTextAlignment(Qt.AlignCenter)
                    self.matrix.setItem(i, j, item)
                elif i < j:
                    combo = QComboBox()
                    for value in JUDGEMENTS:
                        combo.addItem(_judgement_text(value), value)
                        combo.setItemData(combo.count() - 1, _judgement_meaning(value), Qt.ToolTipRole)
                    key = (names[i], names[j])
                    previous = self._judgements.get(key, 1)
                    combo.setCurrentIndex(JUDGEMENTS.index(previous) if previous in JUDGEMENTS else 8)
                    combo.setToolTip(f"{names[i]} vs {names[j]}")
                    combo.currentIndexChanged.connect(
                        lambda _=0, a=names[i], b=names[j], c=combo: self._on_judgement(a, b, c))
                    self.matrix.setCellWidget(i, j, combo)
                else:
                    item = QTableWidgetItem("")
                    item.setFlags(Qt.ItemIsEnabled)
                    item.setTextAlignment(Qt.AlignCenter)
                    self.matrix.setItem(i, j, item)
        self.matrix.blockSignals(False)
        height = (self.matrix.horizontalHeader().sizeHint().height()
                  + self.matrix.verticalHeader().defaultSectionSize() * max(1, n) + 4)
        self.matrix.setFixedHeight(min(height, 340))
        self.matrix.setVisible(n >= 2)
        self._update_weights()

    def _on_judgement(self, a: str, b: str, combo: QComboBox) -> None:
        self._judgements[(a, b)] = combo.currentData()
        self._update_weights()

    def _judgement_matrix(self, names: List[str]) -> np.ndarray:
        upper = {}
        for i in range(len(names)):
            for j in range(i + 1, len(names)):
                upper[(i, j)] = float(self._judgements.get((names[i], names[j]), 1))
        return reciprocal_matrix(upper, len(names))

    def _update_weights(self) -> None:
        names = self.checked_fields()
        if len(names) < 2:
            self._ahp = None
            self.diagnostics.setText("")
            self._refresh()
            return
        matrix = self._judgement_matrix(names)
        for i in range(len(names)):
            for j in range(i):
                item = self.matrix.item(i, j)
                if item is not None:
                    item.setText(_format_ratio(matrix[i, j]))
        self._ahp = ahp_weights(matrix, self.method.currentData())
        ok = self._ahp.is_consistent
        weights = ", ".join(f"{n} {100 * w:.1f} %" for n, w in zip(names, self._ahp.weights))
        color = "#0c7a0c" if ok else "#d03b3b"
        verdict = tr("consistent") if ok else tr("inconsistent: revise the judgements")
        self.diagnostics.setText(
            f"<b>{tr('Weights')}:</b> {weights}<br>"
            f"λmax = {self._ahp.lambda_max:.3f} · CI = {self._ahp.consistency_index:.3f} · "
            f"RI = {self._ahp.random_index:.2f} · <span style='color:{color}'><b>CR = "
            f"{self._ahp.consistency_ratio:.3f}</b> ({verdict})</span>")
        self._refresh()

    def _settings_issues(self) -> List[str]:
        issues = []
        names = self.checked_fields() if hasattr(self, "field_list") else []
        if len(names) > MAX_CRITERIA:
            issues.append(tr("AHP is limited to {0} criteria (Saaty's random index table).")
                          .format(MAX_CRITERIA))
        if (self._ahp is not None and not self._ahp.is_consistent
                and not self.accept_inconsistent.isChecked()):
            issues.append(tr("The judgements are inconsistent (CR > 0.10): revise them or tick "
                             "'Accept CR > 0.10'."))
        return issues

    # -- computation -----------------------------------------------------------------

    def _compute(self, table: TableData, fields: List[str]):
        self._update_weights()
        ahp = self._ahp
        directions = self.criteria.directions()
        normalised = {n: min_max(table.columns[n], directions.get(n, 1)) for n in fields}
        weights = dict(zip(fields, ahp.weights.tolist()))
        value = weighted_sum(normalised, weights)
        outputs = [OutputColumn(f"{n}_norm", normalised[n], "double",
                                title=tr("{0} rescaled 0–1").format(n)) for n in fields]

        method = self.class_method.currentText()
        style = None
        rule_list = None
        classes = None
        number, breaks = rank_classes(value, method, 4)
        if method != "None":
            rule_list = [{"label": tr("Class {0}").format(k) + (tr(" (highest)") if k == 1 else
                                                                   tr(" (lowest)") if k == 4 else ""),
                          "score": k} for k in range(1, 5)]
            classes = [(None, None) if np.isnan(c) else (rule_list[int(c) - 1]["label"], int(c))
                       for c in number]
            style = ("AHP_class", rule_list, "AHP")
        outputs.append(OutputColumn("AHP", value, "double", title=tr("AHP weighted index (0–1)"),
                                    classes=classes, class_rules=rule_list,
                                    class_scheme=method if method != "None" else ""))
        if method != "None":
            outputs.append(OutputColumn("AHP_class", number, "int", title=tr("AHP class")))

        figures = [{
            "id": "weights", "title": tr("AHP: criterion weights"),
            "data": [{"type": "bar", "x": fields, "y": [round(100 * w, 2) for w in ahp.weights],
                      "marker": {"color": "#2a78d6"}, "text": [f"{100 * w:.1f} %" for w in ahp.weights],
                      "textposition": "outside", "hovertemplate": "%{x}: %{y:.1f} %<extra></extra>"}],
            "layout": {"yaxis": {"title": tr("Weight (%)"), "rangemode": "tozero"},
                       "xaxis": {"type": "category"}},
            "note": tr("λmax = {0:.3f}, CI = {1:.3f}, RI = {2:.2f}, CR = {3:.3f}. Weights: {4}.").format(
                ahp.lambda_max, ahp.consistency_index, ahp.random_index, ahp.consistency_ratio,
                self.method.currentText()),
        }]
        warnings = {}
        for row in range(table.size):
            missing = [n for n in fields if not np.isfinite(table.columns[n][row])]
            if missing:
                warnings[row] = [tr("Missing criteria (weights renormalised): {0}.").format(", ".join(missing))]
        summary = tr("AHP index computed for {0} basin(s); CR = {1:.3f}.").format(
            int(np.sum(np.isfinite(value))), ahp.consistency_ratio)
        if breaks:
            summary += " " + tr("Class breaks ({0}): {1}.").format(method, ", ".join(f"{b:.3f}" for b in breaks))
        return outputs, figures, "weights", style, warnings, summary


def _format_ratio(value: float) -> str:
    """Reciprocal judgements as '1/5' rather than 0.2."""
    if value >= 1:
        return f"{value:g}"
    return f"1/{round(1 / value):g}"
