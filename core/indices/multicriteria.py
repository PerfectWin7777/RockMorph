"""
core/indices/multicriteria.py

Multi-criteria synthesis of index tables: relative active tectonics index
(Iat), Analytic Hierarchy Process (AHP) and composite indices.

These are the "Studio" analyses of GSV-Studio (``studio.py`` /
``dialogs.py``), rewritten as pure functions over NumPy arrays so that the
QGIS tools only handle layers and widgets.

Iat (El Hamdouni et al., 2008)
------------------------------
Each component index is classified (class 1 = highest activity) and Iat is
the mean of the class numbers: ``Iat = S / n``. Iat itself is binned into
four classes: 1 (very high) 1–1.5, 2 (high) 1.5–2, 3 (moderate) 2–2.5,
4 (low) >= 2.5. Choices versus GSV-Studio:

* a basin missing some components still gets an Iat (as in GSV-Studio), but
  ``n_used`` is returned so the tool can show and filter it;
* the final binning uses the shared rule convention (last bound inclusive).

AHP (Saaty, 1980)
-----------------
Weights are the normalised principal eigenvector of the reciprocal pairwise
comparison matrix; consistency CR = CI / RI with CI = (λmax − n)/(n − 1).
GSV-Studio approximated the eigenvector by row means of the column-normalised
matrix while computing λmax exactly; using the eigenvector for both keeps CR
consistent with the weights. The approximation is kept as an option.

Authors: RockMorph contributors / Tony
"""

from __future__ import annotations

import math
from dataclasses import dataclass
from typing import Dict, List, Mapping, Optional, Sequence, Tuple

import numpy as np  # type: ignore

from .spec import Classification, rules

INF = float("inf")

# Iat classes (El Hamdouni et al., 2008).
IAT_CLASSIFICATION = Classification(
    "Iat_ElHamdouni_2008", "Iat classes – El Hamdouni et al. (2008)",
    "El Hamdouni et al. (2008)", "doi:10.1016/j.geomorph.2007.08.004",
    rules(
        (-INF, 1.5, "Class 1: very high activity", 1),
        (1.5, 2.0, "Class 2: high activity", 2),
        (2.0, 2.5, "Class 3: moderate activity", 3),
        (2.5, INF, "Class 4: low activity", 4),
    ),
)

# Saaty's random consistency index RI. Values for n <= 10 are Saaty's (1980)
# classic table; n = 11..16 are the values GSV-Studio used (published tables
# differ slightly in the third decimal for n > 10, see doc/07).
RANDOM_INDEX = {1: 0.0, 2: 0.0, 3: 0.58, 4: 0.90, 5: 1.12, 6: 1.24, 7: 1.32,
                8: 1.41, 9: 1.45, 10: 1.49, 11: 1.51, 12: 1.53, 13: 1.56,
                14: 1.58, 15: 1.59, 16: 1.60}

# Saaty's fundamental scale, used by the UI to label judgements.
SAATY_SCALE = {
    1: "Equal importance", 2: "Weak", 3: "Moderate importance", 4: "Moderate plus",
    5: "Strong importance", 6: "Strong plus", 7: "Very strong importance",
    8: "Very, very strong", 9: "Extreme importance",
}


# ---------------------------------------------------------------------------
# Class scores
# ---------------------------------------------------------------------------

def class_scores(values: Sequence[Optional[float]], classification: Classification,
                 score_map: Optional[Mapping[str, float]] = None) -> np.ndarray:
    """
    Score of every value according to ``classification``.

    ``score_map`` maps class labels to scores and overrides the rule scores
    (the Iat and AHP tools let users edit them). Values falling in no class,
    or in a class without a score, get NaN.
    """
    out = np.full(len(values), np.nan)
    for i, value in enumerate(values):
        label, score = classification.classify(value)
        if label is None:
            continue
        if score_map is not None and label in score_map:
            score = score_map[label]
        if score is not None:
            out[i] = float(score)
    return out


@dataclass
class IatResult:
    """Iat score, class label/number and number of components per basin."""

    score: np.ndarray
    n_used: np.ndarray
    class_label: List[Optional[str]]
    class_number: np.ndarray


def relative_active_tectonics(component_scores: Mapping[str, np.ndarray],
                              final: Classification = IAT_CLASSIFICATION,
                              min_components: int = 1) -> IatResult:
    """
    Iat = mean of the component class scores (El Hamdouni et al., 2008).

    Parameters
    ----------
    component_scores: {index key: array of class scores per basin}
    final           : classification of the Iat value (4 classes by default).
    min_components  : basins with fewer valid components get NaN.
    """
    if not component_scores:
        raise ValueError("At least one component index is required.")
    matrix = np.vstack([np.asarray(v, dtype=float) for v in component_scores.values()])
    n_used = np.sum(np.isfinite(matrix), axis=0)
    with np.errstate(invalid="ignore"):
        score = np.nanmean(np.where(np.isfinite(matrix), matrix, np.nan), axis=0) \
            if matrix.size else np.array([])
    score = np.where(n_used >= max(1, min_components), score, np.nan)
    labels, numbers = [], np.full(score.size, np.nan)
    for i, value in enumerate(score):
        label, number = final.classify(value if math.isfinite(value) else None)
        labels.append(label)
        if number is not None:
            numbers[i] = number
    return IatResult(score, n_used.astype(int), labels, numbers)


# ---------------------------------------------------------------------------
# AHP
# ---------------------------------------------------------------------------

@dataclass
class AhpResult:
    """Weights and consistency diagnostics of a pairwise comparison matrix."""

    weights: np.ndarray
    lambda_max: float
    consistency_index: float
    random_index: float
    consistency_ratio: float

    @property
    def is_consistent(self) -> bool:
        """Saaty's acceptance threshold CR <= 0.10."""
        return self.consistency_ratio <= 0.10


def reciprocal_matrix(upper: Mapping[Tuple[int, int], float], n: int) -> np.ndarray:
    """
    Build a reciprocal comparison matrix from judgements ``upper[(i, j)]``
    for i < j (importance of criterion i over j, Saaty scale 1/9 .. 9).
    Missing judgements default to 1 (equal importance).
    """
    matrix = np.ones((n, n), dtype=float)
    for (i, j), value in upper.items():
        if i == j:
            continue
        value = float(value)
        if value <= 0:
            raise ValueError("Judgements must be positive (Saaty scale 1/9 .. 9).")
        matrix[i, j] = value
        matrix[j, i] = 1.0 / value
    return matrix


def ahp_weights(matrix: np.ndarray, method: str = "eigenvector") -> AhpResult:
    """
    AHP weights and consistency ratio.

    method "eigenvector" (default): principal right eigenvector (Saaty).
    method "column_normalisation": row means of the column-normalised matrix
    (GSV-Studio's approximation).
    """
    matrix = np.asarray(matrix, dtype=float)
    n = matrix.shape[0]
    if matrix.shape != (n, n) or n == 0:
        raise ValueError("The comparison matrix must be square.")
    eigenvalues, eigenvectors = np.linalg.eig(matrix)
    principal = int(np.argmax(eigenvalues.real))
    lambda_max = float(eigenvalues.real[principal])
    if method == "column_normalisation":
        weights = (matrix / matrix.sum(axis=0)).mean(axis=1)
    else:
        weights = np.abs(eigenvectors[:, principal].real)
    weights = weights / weights.sum()
    ci = (lambda_max - n) / (n - 1) if n > 2 else 0.0
    ri = RANDOM_INDEX.get(n, 1.60)
    cr = ci / ri if ri > 0 else 0.0
    return AhpResult(weights, lambda_max, max(ci, 0.0), ri, max(cr, 0.0))


def weighted_sum(scores: Mapping[str, np.ndarray], weights: Mapping[str, float]) -> np.ndarray:
    """
    Weighted mean of criterion scores per basin, renormalising the weights
    over the criteria available for each basin (a missing criterion does not
    count as zero).
    """
    keys = list(scores)
    matrix = np.vstack([np.asarray(scores[k], dtype=float) for k in keys])
    w = np.asarray([float(weights.get(k, 0.0)) for k in keys])[:, None]
    valid = np.isfinite(matrix)
    total_w = np.sum(np.where(valid, w, 0.0), axis=0)
    with np.errstate(invalid="ignore", divide="ignore"):
        result = np.sum(np.where(valid, matrix * w, 0.0), axis=0) / total_w
    return np.where(total_w > 0, result, np.nan)


# ---------------------------------------------------------------------------
# Composite indices (GSV-Studio "index modeler")
# ---------------------------------------------------------------------------

def min_max(values: np.ndarray, direction: int = 1) -> np.ndarray:
    """
    Min-max normalisation to [0, 1]; ``direction = -1`` reverses the scale
    so that 1 always means "more active" (e.g. for Vf or Smf, low = active).
    Constant columns become 0.5, NaN stays NaN (GSV-Studio behaviour).
    """
    x = np.asarray(values, dtype=float)
    finite = np.isfinite(x)
    if not np.any(finite):
        return np.full(x.shape, np.nan)
    lo, hi = float(np.min(x[finite])), float(np.max(x[finite]))
    if hi - lo < 1e-12:
        out = np.where(np.isfinite(x), 0.5, np.nan)
    else:
        out = (x - lo) / (hi - lo)
    return 1.0 - out if direction < 0 else out


COMPOSITE_METHODS = ("Mean", "Weighted mean", "Minimum", "Maximum", "First principal component")


def composite_index(columns: Mapping[str, np.ndarray], method: str,
                    directions: Optional[Mapping[str, int]] = None,
                    weights: Optional[Mapping[str, float]] = None) -> Tuple[np.ndarray, Dict]:
    """
    Combine several index columns into one composite value per basin.

    Every column is min-max normalised (reversed when its direction is -1)
    before aggregation. Returns ``(values, info)`` where ``info`` holds the
    PCA loadings and explained variance when the PC1 method is used.

    Choice: GSV-Studio could not reverse indices where low values mean high
    activity; ``directions`` fixes that. PC1 is oriented so that its loadings
    sum is positive, which makes "high = more of the selected indices".
    """
    directions = directions or {}
    keys = list(columns)
    normalised = {k: min_max(columns[k], directions.get(k, 1)) for k in keys}
    matrix = np.vstack([normalised[k] for k in keys])
    info: Dict = {}
    if method == "Weighted mean":
        result = weighted_sum(normalised, weights or {k: 1.0 for k in keys})
    elif method == "Minimum":
        result = np.nanmin(matrix, axis=0)
    elif method == "Maximum":
        result = np.nanmax(matrix, axis=0)
    elif method == "First principal component":
        complete = np.all(np.isfinite(matrix), axis=0)
        result = np.full(matrix.shape[1], np.nan)
        if complete.sum() >= 2 and len(keys) >= 2:
            data = matrix[:, complete].T
            data = (data - data.mean(axis=0)) / np.where(data.std(axis=0) > 0, data.std(axis=0), 1.0)
            _, s, vt = np.linalg.svd(data, full_matrices=False)
            loadings = vt[0]
            if loadings.sum() < 0:
                loadings = -loadings
            scores = data @ loadings
            result[complete] = min_max(scores)
            variance = s ** 2 / np.sum(s ** 2)
            info = {"loadings": dict(zip(keys, loadings.tolist())),
                    "explained_variance": float(variance[0])}
    else:
        with np.errstate(invalid="ignore"):
            result = np.nanmean(matrix, axis=0)
    return result, info


# ---------------------------------------------------------------------------
# Classes of a continuous synthesis value (AHP, composite)
# ---------------------------------------------------------------------------

CLASS_BREAK_METHODS = ("None", "Quantiles", "Equal intervals")


def rank_classes(values: np.ndarray, method: str = "Quantiles",
                 n_classes: int = 4) -> Tuple[np.ndarray, List[float]]:
    """
    Divide a continuous result into ``n_classes`` classes, class 1 holding
    the HIGHEST values (most active / highest priority, the convention of
    the Iat classes).

    "Quantiles": equal numbers of basins per class (robust to skewed
    values, the usual choice for relative maps). "Equal intervals": equal
    value ranges between min and max (keeps the meaning of the scale).
    Returns the class number per basin (NaN when the value is missing) and
    the inner break values in increasing order.
    """
    x = np.asarray(values, dtype=float)
    out = np.full(x.shape, np.nan)
    finite = np.isfinite(x)
    if method == "None" or not np.any(finite) or n_classes < 2:
        return out, []
    if method == "Equal intervals":
        lo, hi = float(np.min(x[finite])), float(np.max(x[finite]))
        breaks = list(np.linspace(lo, hi, n_classes + 1)[1:-1])
    else:
        breaks = list(np.quantile(x[finite], np.linspace(0, 1, n_classes + 1)[1:-1]))
    # Position 0 = lowest values; class 1 = highest values.
    position = np.searchsorted(np.asarray(breaks), x[finite], side="right")
    out[finite] = n_classes - position
    return out, [float(b) for b in breaks]
