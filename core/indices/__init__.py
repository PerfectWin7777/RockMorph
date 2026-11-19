"""
core/indices

Pure (QGIS-free) geomorphic index library of RockMorph.

Modules
-------
spec          IndexSpec / ParameterSpec / Classification data model.
context       BasinContext: the data one basin offers to the formulas.
flow          NumPy flow routing (filling, D8, accumulation, Strahler).
profiles      Channel and valley profile analyses (SL, ksn, chi, Vf).
formulas      One function per index.
catalog       The declared catalogue and presets.
multicriteria Iat, AHP and composite indices.
ml            Clustering, embeddings and outlier detection.

Only NumPy and SciPy are required (both ship with QGIS); scikit-learn and
umap-learn unlock extra methods in ``ml``.

Authors: RockMorph contributors / Tony
"""

from .catalog import FAMILIES, INDEX_BY_KEY, PRESETS, SPECS, families, get  # noqa: F401
from .context import BasinContext  # noqa: F401
from .spec import Classification, ClassRule, IndexSpec, ParameterSpec  # noqa: F401


def compute_index(key, context, params=None):
    """
    Compute index ``key`` for one ``BasinContext``.

    ``params`` are merged over the index defaults and coerced to their
    declared types. Returns None (never raises) when the value cannot be
    computed; the exception message, if any, is appended to
    ``context.warnings`` so the user sees why.
    """
    spec = get(key)
    resolved = spec.resolve_parameters(params)
    try:
        value = spec.compute(context, resolved)
    except Exception as error:  # a bad basin must never stop the whole run
        context.warnings.append(f"{key}: {error}")
        return None
    if value is None:
        return None
    try:
        value = float(value)
    except (TypeError, ValueError):
        return None
    return value if value == value and abs(value) != float("inf") else None
