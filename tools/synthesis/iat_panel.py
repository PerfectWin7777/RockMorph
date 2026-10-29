"""
tools/synthesis/iat_panel.py

Relative Active Tectonics index (Iat) tool — El Hamdouni et al. (2008).

Iat is the mean of the class scores of several geomorphic indices
(classically SL, AF, HI, Vf and Bs, each scored 1 = high activity to
3 = low activity), then divided into four classes:

    class 1  1.0 ≤ Iat < 1.5   very high activity
    class 2  1.5 ≤ Iat < 2.0   high
    class 3  2.0 ≤ Iat < 2.5   moderate
    class 4  Iat ≥ 2.5         low

Choices made here
-----------------
* Each component is scored with a published, editable classification
  (picked automatically when the field is named like a catalogue index), or
  taken as is when the field already holds class scores (e.g. "SL_score"
  written by the Morphometric Indices tool).
* A basin with missing components gets the mean of the available scores,
  as in El Hamdouni et al. (2008); ``Minimum components`` lets the user
  require a complete set instead. The number of components used is written
  to the ``Iat_n`` field so the choice is always visible.
* Fields not in the catalogue can still be used with user-defined
  thresholds ("Custom thresholds", 3 classes to edit).

Authors: RockMorph contributors / Tony
"""

from __future__ import annotations

from typing import Dict, List

import numpy as np  # type: ignore
from qgis.PyQt.QtCore import QCoreApplication, Qt  # type: ignore
from qgis.PyQt.QtWidgets import (  # type: ignore
    QFormLayout, QLabel, QListWidget, QListWidgetItem, QSpinBox, QVBoxLayout,
)

from ...core.indices import INDEX_BY_KEY, PRESETS
from ...core.indices.multicriteria import IAT_CLASSIFICATION, class_scores, relative_active_tectonics
from ...core.indices.spec import Classification, rules
from ...widgets.index_widgets import ClassificationEditor
from .common import OutputColumn, SynthesisPanel, TableData, section_label

INF = float("inf")


def tr(message: str) -> str:
    return QCoreApplication.translate("RockMorph", message)


IAT_PRESET = PRESETS["Iat – El Hamdouni et al. (2008)"]

# Categorical slots (reference palette, fixed order) for stacked components.
SERIES = ["#2a78d6", "#eb6834", "#1baf7a", "#eda100", "#e87ba4", "#008300", "#6250d6", "#e34948"]

# Template for fields that are not catalogue indices: three classes whose
# bounds the user must set (placeholders 0/1 make the intent obvious).
CUSTOM_TEMPLATE = Classification(
    "custom_template", "Custom thresholds (edit the bounds)", "User", "user-defined",
    rules((-INF, 0.0, "Class 1", 1), (0.0, 1.0, "Class 2", 2), (1.0, INF, "Class 3", 3)),
    description="Set the bounds so that class 1 is the most active state of this index.")


def _schemes_for(field_name: str) -> List[Classification]:
    """Scored schemes of the catalogue index named like the field, else the template."""
    spec = INDEX_BY_KEY.get(field_name)
    scored = [c for c in spec.classifications if c.is_scored] if spec else []
    return scored or [CUSTOM_TEMPLATE]


def _default_state(field_name: str):
    """Initial scoring of a field: published default, or 'already scores'."""
    spec = INDEX_BY_KEY.get(field_name)
    if spec is not None:
        scheme = spec.classification()
        if scheme is not None and scheme.is_scored:
            return scheme
        scored = [c for c in spec.classifications if c.is_scored]
        if scored:
            return scored[0]
    return ClassificationEditor.NONE_KEY  # e.g. "SL_score": values are scores


class IatPanel(SynthesisPanel):
    """Relative Active Tectonics index (sidebar)."""

    TITLE = "Relative active tectonics (Iat)"
    OUTPUT_PREFIX = "Iat"
    MIN_FIELDS = 1
    INTRO = ("Combine the class scores of several indices into the Iat of El Hamdouni et al. (2008). "
             "Tick the index fields (SL, AF, HI, VF, Bs are ticked automatically when present), "
             "check how each one is scored, then run.")

    def __init__(self, iface, parent=None):
        self._states: Dict[str, object] = {}
        self._focus = None
        super().__init__(iface, parent)

    # -- settings ---------------------------------------------------------------

    def _build_settings(self, layout: QVBoxLayout) -> None:
        layout.addWidget(section_label(tr("<b>Component scoring</b> — click a component to check or edit "
                                   "its classification.")))
        self.components = QListWidget()
        self.components.setMaximumHeight(120)
        self.components.currentItemChanged.connect(self._on_component)
        layout.addWidget(self.components)
        self.component_editor = ClassificationEditor()
        self.component_editor.changed.connect(self._store_component)
        layout.addWidget(self.component_editor)

        layout.addWidget(section_label(tr("<b>Iat classes</b>")))
        self.final_editor = ClassificationEditor()
        self.final_editor.set_schemes([IAT_CLASSIFICATION], IAT_CLASSIFICATION.key, allow_none=False)
        self.final_editor.changed.connect(self._refresh)
        layout.addWidget(self.final_editor)

        form = QFormLayout()
        self.min_components = QSpinBox()
        self.min_components.setRange(1, 20)
        self.min_components.setValue(1)
        self.min_components.setToolTip(tr("Basins with fewer scored components get no Iat. 1 = mean of the "
                                          "available components (El Hamdouni et al., 2008)."))
        self.min_components.valueChanged.connect(self._refresh)
        form.addRow(tr("Minimum components"), self.min_components)
        layout.addLayout(form)

    def _on_layer(self, layer) -> None:
        super()._on_layer(layer)
        # Tick the classical Iat components when the layer has them.
        present = [k for k in IAT_PRESET if k in {self.field_list.item(i).data(Qt.UserRole)
                                                  for i in range(self.field_list.count())}]
        if present:
            self.set_checked_fields(present)

    def _on_fields_changed(self) -> None:
        if not hasattr(self, "components"):
            return
        fields = self.checked_fields()
        current = self._focus
        self.components.blockSignals(True)
        self.components.clear()
        for name in fields:
            self._states.setdefault(name, _default_state(name))
            item = QListWidgetItem(self._component_text(name))
            item.setData(Qt.UserRole, name)
            self.components.addItem(item)
        self.components.blockSignals(False)
        if fields:
            row = fields.index(current) if current in fields else 0
            self.components.setCurrentRow(row)
            self._on_component(self.components.item(row), None)
        else:
            self._focus = None
            self.component_editor.set_schemes(None, None)

    def _component_text(self, name: str) -> str:
        state = self._states.get(name)
        if state is None:
            how = tr("invalid thresholds")
        elif state == ClassificationEditor.NONE_KEY:
            how = tr("values used as class scores")
        else:
            how = state.label
        return f"{name}  —  {how}"

    def _on_component(self, item, _previous) -> None:
        if item is None:
            return
        name = item.data(Qt.UserRole)
        self._focus = None
        self.component_editor.set_schemes(_schemes_for(name), None, self._states.get(name),
                                          allow_none=True,
                                          none_label=tr("Values are already class scores"))
        self._focus = name

    def _store_component(self) -> None:
        if not self._focus:
            return
        self._states[self._focus] = self.component_editor.choice()
        for i in range(self.components.count()):
            item = self.components.item(i)
            if item.data(Qt.UserRole) == self._focus:
                item.setText(self._component_text(self._focus))
        self._refresh()

    def _settings_issues(self) -> List[str]:
        if not hasattr(self, "final_editor"):
            return []
        issues = [tr("{0}: the edited classification is invalid.").format(name)
                  for name in self.checked_fields() if self._states.get(name, "") is None]
        for name in self.checked_fields():
            state = self._states.get(name)
            if isinstance(state, Classification) and state.key == CUSTOM_TEMPLATE.key:
                issues.append(tr("{0}: set its class bounds (custom thresholds are still the "
                                 "0/1 placeholders).").format(name))
        if self.final_editor.classification() is None:
            issues.append(tr("The Iat class table is invalid."))
        if self.min_components.value() > max(1, len(self.checked_fields())):
            issues.append(tr("'Minimum components' exceeds the number of ticked fields."))
        return issues

    # -- computation ---------------------------------------------------------------

    def _compute(self, table: TableData, fields: List[str]):
        scores: Dict[str, np.ndarray] = {}
        outputs: List[OutputColumn] = []
        for name in fields:
            state = self._states.get(name)
            values = table.columns[name]
            if isinstance(state, Classification):
                component = class_scores(values.tolist(), state)
            else:
                component = np.where(np.isfinite(values), values, np.nan)
            scores[name] = component
            outputs.append(OutputColumn(f"{name}_iat", component, "int",
                                        title=tr("{0} class score").format(name)))
        final = self.final_editor.classification()
        result = relative_active_tectonics(scores, final, self.min_components.value())

        rule_list = [{"label": r.label, "score": r.score} for r in final.rules]
        classes = [(result.class_label[i], None if np.isnan(result.class_number[i])
                    else int(result.class_number[i])) for i in range(table.size)]
        outputs += [
            OutputColumn("Iat", result.score, "double", title=tr("Relative active tectonics index"),
                         classes=classes, class_rules=rule_list, class_scheme=final.label),
            OutputColumn("Iat_class", result.class_number, "int", title=tr("Iat class")),
            OutputColumn("Iat_n", result.n_used, "int", title=tr("Components used")),
            OutputColumn("Iat_label", result.class_label, "text"),
        ]

        warnings = {}
        for row in range(table.size):
            missing = [n for n in fields if not np.isfinite(scores[n][row])]
            if missing:
                warnings[row] = [tr("No class score for: {0}.").format(", ".join(missing))]

        order = np.argsort(np.where(np.isfinite(result.score), result.score, np.inf))
        labels = [table.labels[i] for i in order]
        stacked = []
        for n, name in enumerate(fields):
            stacked.append({
                "type": "bar", "name": name, "x": labels,
                "y": [None if not np.isfinite(scores[name][i]) else float(scores[name][i]) for i in order],
                "marker": {"color": SERIES[n % len(SERIES)], "line": {"color": "#fcfcfb", "width": 1}},
                "hovertemplate": "%{x}<br>" + name + " score %{y}<extra></extra>",
            })
        figures = [{
            "id": "components", "title": tr("Iat: component scores by basin"), "data": stacked,
            "layout": {"barmode": "stack", "showlegend": True, "legend": {"orientation": "h", "y": 1.08, "traceorder": "normal"},
                       "xaxis": {"type": "category", "tickangle": -45, "title": tr("Basins (most active left)")},
                       "yaxis": {"title": tr("Sum of class scores")}},
            "note": tr("Each bar stacks the class scores of the components (1 = most active). "
                       "Iat = mean of the scores; basins are sorted by Iat."),
        }]
        counts = {r.score: int(np.sum(result.class_number == r.score)) for r in final.rules}
        summary = tr("Iat computed for {0} of {1} basin(s). Classes: {2}.").format(
            int(np.sum(np.isfinite(result.score))), table.size,
            ", ".join(f"{k}: {v}" for k, v in counts.items()))
        return outputs, figures, "components", ("Iat_class", rule_list, "Iat"), warnings, summary
