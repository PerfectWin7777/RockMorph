"""
core/indices/spec.py

Declarative description of a geomorphic index.

Every index RockMorph can compute is described by an ``IndexSpec``. The spec
holds everything the rest of the plugin needs to know about the index without
running it:

* identity and documentation (name, family, formula, unit, references),
* the data the computation needs (``requires``),
* the user-tunable parameters (``ParameterSpec``) from which the UI builds
  its widgets automatically,
* one or more published classification schemes (``Classification``),
* the pure function that turns a ``BasinContext`` into a number.

Design choices
--------------
* The spec is pure data plus one callable. It does not import QGIS or Qt so
  the whole catalogue can be unit-tested with plain NumPy.
* Classification rules follow one convention everywhere (inherited from the
  GSV-Studio registry): the lower bound is inclusive, the upper bound is
  exclusive, except for the last rule whose finite upper bound is inclusive.
* A classification can declare a ``transform`` applied to the raw value before
  the rules are evaluated. This is how the asymmetry factor is classified on
  ``|AF - 50|`` (El Hamdouni et al., 2008) while the table still shows AF.
* Each rule may carry a ``score`` (an integer class number). The relative
  active tectonics index (Iat) and the AHP tools average these scores.

Authors: RockMorph contributors / Tony
"""

from __future__ import annotations

import math
from dataclasses import dataclass, field
from typing import Any, Callable, Dict, Mapping, Optional, Sequence, Tuple


# ---------------------------------------------------------------------------
# Data requirements
# ---------------------------------------------------------------------------
# An index declares which pieces of a BasinContext it needs. The context
# builder reads these flags to compute only what the selected indices use
# (flow routing and cross-profiles are expensive, polygon metrics are not).
#
#   "geometry"  basin polygon metrics (area, perimeter, length, width, axis)
#   "elevation" DEM values inside the basin
#   "terrain"   filled DEM window, slope and flow accumulation (pixel indices)
#   "channel"   main-channel longitudinal profile (distance, z, drainage area)
#   "network"   Horton-Strahler stream statistics
#   "asymmetry" right-bank area of the trunk stream (AF)
#   "sections"  transverse sections between the basin axis and the channel (T)
#   "valley"    valley cross-profiles across the main channel (Vf)
REQUIREMENTS = (
    "geometry", "elevation", "terrain", "channel",
    "network", "asymmetry", "sections", "valley",
)


# ---------------------------------------------------------------------------
# Parameters
# ---------------------------------------------------------------------------

@dataclass(frozen=True)
class ParameterSpec:
    """
    One user-tunable parameter of an index.

    The UI generates a widget from ``kind``:

    ========  =====================================
    kind      widget
    ========  =====================================
    "int"     integer spin box (minimum/maximum/step)
    "float"   decimal spin box (minimum/maximum/step/decimals)
    "choice"  combo box filled with ``choices``
    "bool"    check box
    ========  =====================================

    ``help`` must explain the scientific meaning of the parameter and, when
    the default comes from a publication, cite it. Researchers read it in the
    tooltip before changing anything.
    """

    key: str
    label: str
    kind: str
    default: Any
    help: str = ""
    minimum: Optional[float] = None
    maximum: Optional[float] = None
    step: Optional[float] = None
    decimals: int = 2
    unit: str = ""
    choices: Tuple[str, ...] = ()

    def coerce(self, value: Any) -> Any:
        """
        Convert ``value`` to the parameter type and clamp it to its range.

        Values coming back from the UI or from a saved project file may be
        strings or out-of-range numbers; the computation must never receive
        them as is.
        """
        if value is None:
            return self.default
        if self.kind == "bool":
            if isinstance(value, str):
                return value.strip().lower() in ("1", "true", "yes", "on")
            return bool(value)
        if self.kind == "choice":
            value = str(value)
            return value if value in self.choices else self.default
        if self.kind in ("int", "float"):
            try:
                number = float(value)
            except (TypeError, ValueError):
                return self.default
            if self.minimum is not None:
                number = max(number, self.minimum)
            if self.maximum is not None:
                number = min(number, self.maximum)
            return int(round(number)) if self.kind == "int" else number
        return value


# ---------------------------------------------------------------------------
# Classifications
# ---------------------------------------------------------------------------

@dataclass(frozen=True)
class ClassRule:
    """
    One class of a classification scheme.

    ``lower`` is inclusive and ``upper`` exclusive (see module docstring for
    the last-rule exception). ``score`` is the class number used by the Iat
    and AHP tools; ``None`` means the scheme is descriptive only.
    """

    lower: float
    upper: float
    label: str
    score: Optional[int] = None


# Value transforms a classification may apply before evaluating its rules.
# Keep this table small and explicit: each entry is a scientific convention.
VALUE_TRANSFORMS: Dict[str, Callable[[float], float]] = {
    # Asymmetry factor: deviation from a symmetric basin (AF = 50).
    "abs_minus_50": lambda value: abs(value - 50.0),
    # Transverse symmetry and similar ratios classified on their magnitude.
    "abs": abs,
}


@dataclass(frozen=True)
class Classification:
    """
    A published classification scheme for one index.

    Several schemes may exist for the same index (e.g. SL according to
    El Hamdouni et al. 2008 or to Seeber & Gornitz 1983). The user picks one
    in the UI; the default is ``IndexSpec.default_classification``.
    """

    key: str
    label: str
    authors: str
    reference: str
    rules: Tuple[ClassRule, ...]
    description: str = ""
    transform: Optional[str] = None

    def classify(self, value: Optional[float]) -> Tuple[Optional[str], Optional[int]]:
        """
        Return ``(label, score)`` for ``value``, or ``(None, None)`` when the
        value is missing or falls outside every rule.
        """
        if value is None or not _is_finite(value):
            return None, None
        if self.transform:
            value = VALUE_TRANSFORMS[self.transform](value)
        rules = self.rules
        for position, rule in enumerate(rules):
            is_last = position == len(rules) - 1
            above_lower = value >= rule.lower
            if is_last and math.isfinite(rule.upper):
                below_upper = value <= rule.upper
            else:
                below_upper = value < rule.upper
            if above_lower and below_upper:
                return rule.label, rule.score
        return None, None

    @property
    def is_scored(self) -> bool:
        """True when every rule carries a class number (usable by Iat/AHP)."""
        return all(rule.score is not None for rule in self.rules)


# ---------------------------------------------------------------------------
# Index specification
# ---------------------------------------------------------------------------

ComputeFunction = Callable[[Any, Mapping[str, Any]], Optional[float]]


@dataclass(frozen=True)
class IndexSpec:
    """
    Complete description of one geomorphic index.

    Attributes
    ----------
    key            : stable identifier, also used as the attribute field name.
                     Keys inherited from GSV-Studio are kept unchanged so that
                     tables produced by both programs can be compared.
    name           : human-readable name shown in the catalogue.
    family         : catalogue section ("Basin shape", "Relief", ...).
    description    : what the index measures and how to read it.
    formula        : formula as plain text (Unicode), shown in the info card.
    unit           : unit of the returned value ("" for dimensionless).
    authors        : original author(s) and year(s).
    reference      : DOI or URL of the original publication.
    requires       : data requirements (see ``REQUIREMENTS``).
    compute        : ``compute(context, params) -> float | None``.
    parameters     : tunable parameters.
    classifications: published classification schemes.
    default_classification: key of the scheme used by default.
    notes          : scientific choices made in RockMorph's implementation
                     (deviations from the original, approximations). Shown in
                     the info card and copied to the decisions journal.
    experimental   : True for empirical relations whose source or units are
                     not settled; such indices are flagged in the UI.
    """

    key: str
    name: str
    family: str
    description: str
    formula: str
    unit: str
    authors: str
    reference: str
    requires: Tuple[str, ...]
    compute: ComputeFunction
    parameters: Tuple[ParameterSpec, ...] = ()
    classifications: Tuple[Classification, ...] = ()
    default_classification: Optional[str] = None
    notes: str = ""
    experimental: bool = False
    tags: Tuple[str, ...] = field(default_factory=tuple)

    def __post_init__(self) -> None:
        unknown = set(self.requires) - set(REQUIREMENTS)
        if unknown:
            raise ValueError(f"Index '{self.key}' declares unknown requirements: {sorted(unknown)}")
        keys = [c.key for c in self.classifications]
        if self.default_classification and self.default_classification not in keys:
            raise ValueError(
                f"Index '{self.key}': default classification "
                f"'{self.default_classification}' is not declared."
            )

    # -- parameters ---------------------------------------------------------

    def default_parameters(self) -> Dict[str, Any]:
        """Parameter values used when the user changed nothing."""
        return {p.key: p.default for p in self.parameters}

    def resolve_parameters(self, values: Optional[Mapping[str, Any]] = None) -> Dict[str, Any]:
        """
        Merge user ``values`` over the defaults, coercing every value.
        Unknown keys are ignored so that old project files keep loading.
        """
        values = values or {}
        return {p.key: p.coerce(values.get(p.key, p.default)) for p in self.parameters}

    # -- classifications ----------------------------------------------------

    def classification(self, key: Optional[str] = None) -> Optional[Classification]:
        """Return the scheme ``key`` (or the default one), or None."""
        key = key or self.default_classification
        for scheme in self.classifications:
            if scheme.key == key:
                return scheme
        return None


def _is_finite(value: Any) -> bool:
    """True for real, finite numbers (rejects None, NaN, inf, strings)."""
    try:
        return math.isfinite(float(value))
    except (TypeError, ValueError):
        return False


def finite_or_none(value: Any) -> Optional[float]:
    """Return ``float(value)`` when finite, else None (JSON/table safe)."""
    return float(value) if _is_finite(value) else None


def rules(*items: Sequence[Any]) -> Tuple[ClassRule, ...]:
    """
    Compact constructor used by the catalogue:
    ``rules((-inf, 0.5, "label", 1), (0.5, inf, "label", 2))``.
    """
    return tuple(ClassRule(*item) for item in items)
