"""
tools/synthesis/composite_panel.py

Composite index tool (GSV-Studio "index modeler", improved).

Several index fields are rescaled to 0–1 (min–max over the processed
basins, reversed for "low = active" criteria) and combined into one value
per basin by:

  Mean                      equal-weight mean of the rescaled criteria;
  Weighted mean             user weights (renormalised over the available
                            criteria of each basin);
  Minimum / Maximum         most / least favourable criterion (limiting
                            factor reading);
  First principal component PC1 of the standardised criteria, oriented so
                            that its loadings sum is positive and rescaled to
                            0–1; the loadings and explained variance are
                            reported because they tell what PC1 measures.

Improvement over GSV-Studio: each criterion has a direction, so indices
where LOW values mean activity (Vf, class scores) no longer pull the
composite the wrong way.

Authors: RockMorph contributors / Tony
"""

from __future__ import annotations

from typing import List

import numpy as np  # type: ignore
from qgis.PyQt.QtCore import QCoreApplication  # type: ignore
from qgis.PyQt.QtWidgets import QComboBox, QFormLayout, QLabel, QLineEdit, QVBoxLayout  # type: ignore

from ...core.indices.multicriteria import (
    CLASS_BREAK_METHODS, COMPOSITE_METHODS, composite_index, min_max, rank_classes,
)
from .common import CriteriaTable, OutputColumn, SynthesisPanel, TableData, section_label


def tr(message: str) -> str:
    return QCoreApplication.translate("RockMorph", message)


class CompositePanel(SynthesisPanel):
    """Composite index of several fields (sidebar)."""

    TITLE = "Composite index"
    OUTPUT_PREFIX = "Composite"
    MIN_FIELDS = 2
    INTRO = ("Combine several index fields into one composite value per basin (mean, weighted "
             "mean, minimum, maximum or first principal component), each criterion rescaled to "
             "0–1 with its own direction.")

    def _build_settings(self, layout: QVBoxLayout) -> None:
        form = QFormLayout()
        self.method = QComboBox()
        self.method.addItems(list(COMPOSITE_METHODS))
        self.method.currentTextChanged.connect(self._on_method)
        form.addRow(tr("Method"), self.method)
        self.output_name = QLineEdit("composite")
        self.output_name.setToolTip(tr("Name of the result field (letters, digits, underscore)."))
        self.output_name.textChanged.connect(self._refresh)
        form.addRow(tr("Result field"), self.output_name)
        self.class_method = QComboBox()
        self.class_method.addItems(list(CLASS_BREAK_METHODS))
        self.class_method.setCurrentText("Quantiles")
        form.addRow(tr("Result classes"), self.class_method)
        layout.addLayout(form)
        layout.addWidget(section_label(tr("<b>Criteria</b>")))
        self.criteria = CriteriaTable(with_weights=True)
        self.criteria.changed.connect(self._refresh)
        layout.addWidget(self.criteria)
        self._on_method(self.method.currentText())

    def _on_method(self, method: str) -> None:
        self.criteria.table.setColumnHidden(2, method != "Weighted mean")
        self._refresh()

    def _on_fields_changed(self) -> None:
        if hasattr(self, "criteria"):
            self.criteria.set_criteria(self.checked_fields())

    def _settings_issues(self) -> List[str]:
        if not hasattr(self, "output_name"):
            return []
        issues = []
        name = self.output_name.text().strip()
        if not name or not name.replace("_", "a").isalnum() or name[0].isdigit():
            issues.append(tr("The result field name must start with a letter and contain only letters, "
                             "digits and underscores."))
        if self.method.currentText() == "Weighted mean" and \
                sum(self.criteria.weights().values()) <= 0:
            issues.append(tr("Give at least one criterion a positive weight."))
        return issues

    def _compute(self, table: TableData, fields: List[str]):
        method = self.method.currentText()
        directions = self.criteria.directions()
        name = self.output_name.text().strip()
        value, info = composite_index({n: table.columns[n] for n in fields}, method, directions,
                                      self.criteria.weights())
        outputs = [OutputColumn(f"{n}_norm", min_max(table.columns[n], directions.get(n, 1)), "double",
                                title=tr("{0} rescaled 0–1").format(n)) for n in fields]
        class_method = self.class_method.currentText()
        number, breaks = rank_classes(value, class_method, 4)
        rule_list, classes, style = None, None, None
        if class_method != "None":
            rule_list = [{"label": tr("Class {0}").format(k) + (tr(" (highest)") if k == 1 else
                                                                   tr(" (lowest)") if k == 4 else ""),
                          "score": k} for k in range(1, 5)]
            classes = [(None, None) if np.isnan(c) else (rule_list[int(c) - 1]["label"], int(c)) for c in number]
            style = (f"{name}_class", rule_list, name)
        outputs.append(OutputColumn(name, value, "double", title=tr("Composite index ({0})").format(method),
                                    classes=classes, class_rules=rule_list,
                                    class_scheme=class_method if class_method != "None" else ""))
        if class_method != "None":
            outputs.append(OutputColumn(f"{name}_class", number, "int", title=tr("Composite class")))

        figures = []
        summary = tr("{0} computed for {1} basin(s) ({2}).").format(name, int(np.sum(np.isfinite(value))), method)
        if info.get("loadings"):
            loadings = info["loadings"]
            figures.append({
                "id": "loadings", "title": tr("PC1 loadings"),
                "data": [{"type": "bar", "x": list(loadings), "y": list(loadings.values()),
                          "marker": {"color": "#2a78d6"},
                          "hovertemplate": "%{x}: %{y:.3f}<extra></extra>"}],
                "layout": {"yaxis": {"title": tr("Loading on PC1")}, "xaxis": {"type": "category"}},
                "note": tr("PC1 explains {0:.1f} % of the variance of the rescaled criteria.").format(
                    100 * info["explained_variance"]),
            })
            summary += " " + tr("PC1 explains {0:.1f} % of the variance.").format(100 * info["explained_variance"])
        if breaks:
            summary += " " + tr("Class breaks: {0}.").format(", ".join(f"{b:.3f}" for b in breaks))
        warnings = {}
        for row in range(table.size):
            missing = [n for n in fields if not np.isfinite(table.columns[n][row])]
            if missing:
                warnings[row] = [tr("Missing criteria: {0}.").format(", ".join(missing))]
        return outputs, figures, figures[0]["id"] if figures else None, style, warnings, summary
