"""
core/indices/ml.py

Unsupervised statistics on index tables (clustering, dimensionality
reduction, outlier detection), ported from GSV-Studio's
``GSVStatisticalAnalyzer``.

Dependencies
------------
* Always available (NumPy/SciPy ship with QGIS): standardisation, PCA,
  k-means (``scipy.cluster.vq.kmeans2``) and hierarchical clustering
  (``scipy.cluster.hierarchy``), including dendrogram coordinates.
* Require scikit-learn: DBSCAN, t-SNE, Isolation Forest, Local Outlier
  Factor. UMAP requires ``umap-learn``. When scikit-learn is present, k-means
  uses its implementation (k-means++ with several initialisations), exactly
  as GSV-Studio did.

``available_methods()`` tells the UI which methods can run; the installer
(planned) will offer to install the missing libraries.

Data handling
-------------
Inputs are a 2-D float array ``X`` (rows = basins, columns = indices).
Columns are standardised (mean 0, standard deviation 1) because indices have
incomparable units. Rows with missing values are either dropped (default,
GSV-Studio "dropna") or imputed with the column mean ("impute_mean", i.e. 0
after standardisation). Every result maps back to the original rows; dropped
rows get NaN / -1.

Authors: RockMorph contributors / Tony
"""

from __future__ import annotations

from dataclasses import dataclass, field
from typing import Dict, List, Optional, Sequence, Tuple

import numpy as np  # type: ignore

try:  # scikit-learn is optional (installed later by the RockMorph installer)
    from sklearn.cluster import DBSCAN, KMeans  # type: ignore
    from sklearn.ensemble import IsolationForest  # type: ignore
    from sklearn.manifold import TSNE  # type: ignore
    from sklearn.neighbors import LocalOutlierFactor  # type: ignore
    HAS_SKLEARN = True
except ImportError:  # pragma: no cover - depends on the user's install
    HAS_SKLEARN = False

try:
    import umap  # type: ignore
    HAS_UMAP = True
except ImportError:  # pragma: no cover
    HAS_UMAP = False

from scipy.cluster.hierarchy import dendrogram, fcluster, linkage  # type: ignore
from scipy.cluster.vq import kmeans2  # type: ignore

METHODS = {
    "PCA": ("Principal component analysis", None),
    "KMeans": ("K-means clustering", None),
    "Hierarchical": ("Hierarchical clustering", None),
    "DBSCAN": ("DBSCAN density clustering", "scikit-learn"),
    "TSNE": ("t-SNE embedding", "scikit-learn"),
    "UMAP": ("UMAP embedding", "umap-learn"),
    "IsolationForest": ("Isolation Forest outliers", "scikit-learn"),
    "LOF": ("Local Outlier Factor", "scikit-learn"),
}


class MissingDependencyError(RuntimeError):
    """Raised when a method needs a library that is not installed."""


def available_methods() -> Dict[str, bool]:
    """{method key: True when it can run with the installed libraries}."""
    installed = {None: True, "scikit-learn": HAS_SKLEARN, "umap-learn": HAS_UMAP}
    return {key: installed[dep] for key, (_, dep) in METHODS.items()}


def _require(method: str) -> None:
    if not available_methods()[method]:
        library = METHODS[method][1]
        raise MissingDependencyError(
            f"{METHODS[method][0]} needs the Python library '{library}', which is not "
            f"installed in this QGIS. It will be installable from RockMorph's setup.")


# ---------------------------------------------------------------------------
# Data preparation
# ---------------------------------------------------------------------------

@dataclass
class PreparedData:
    """Standardised matrix and the original row of each of its rows."""

    matrix: np.ndarray
    rows: np.ndarray
    n_total: int
    mean: np.ndarray
    std: np.ndarray


def prepare(x: np.ndarray, nan_handling: str = "dropna") -> PreparedData:
    """
    Standardise ``x`` column-wise and handle missing values.

    Constant columns get a standard deviation of 1 (they then contribute 0
    everywhere instead of producing NaN, as in GSV-Studio).
    """
    x = np.asarray(x, dtype=float)
    if x.ndim != 2 or x.shape[1] == 0:
        raise ValueError("Select at least one numeric field.")
    mean = np.nanmean(x, axis=0)
    std = np.nanstd(x, axis=0)
    std = np.where(std > 0, std, 1.0)
    z = (x - mean) / std
    if nan_handling == "impute_mean":
        rows = np.arange(x.shape[0])
        z = np.where(np.isfinite(z), z, 0.0)
    else:
        rows = np.flatnonzero(np.all(np.isfinite(z), axis=1))
        z = z[rows]
    if z.shape[0] < 2:
        raise ValueError("Fewer than two basins have values for every selected field.")
    return PreparedData(z, rows, x.shape[0], mean, std)


def _scatter_back(prepared: PreparedData, values: np.ndarray, fill) -> np.ndarray:
    """Put per-row results back at their original row positions."""
    shape = (prepared.n_total,) + values.shape[1:]
    out = np.full(shape, fill, dtype=float)
    out[prepared.rows] = values
    return out


# ---------------------------------------------------------------------------
# Results
# ---------------------------------------------------------------------------

@dataclass
class AnalysisResult:
    """
    Outcome of one analysis.

    columns : new attribute columns {name: array over all original rows}
              (cluster label, outlier score, component score...).
    figures : data the UI turns into charts (embedding coordinates,
              loadings, explained variance, dendrogram segments...).
    summary : short text lines for the report panel.
    """

    method: str
    columns: Dict[str, np.ndarray] = field(default_factory=dict)
    figures: Dict[str, object] = field(default_factory=dict)
    summary: List[str] = field(default_factory=list)


# ---------------------------------------------------------------------------
# Methods
# ---------------------------------------------------------------------------

def run_pca(x: np.ndarray, feature_names: Sequence[str], n_components: int = 2,
            nan_handling: str = "dropna") -> AnalysisResult:
    """
    Principal component analysis by singular value decomposition of the
    standardised matrix (identical to scikit-learn's PCA with full SVD).
    Component signs are fixed so that each loading vector sums to >= 0.
    """
    data = prepare(x, nan_handling)
    k = int(max(1, min(n_components, min(data.matrix.shape))))
    u, s, vt = np.linalg.svd(data.matrix - data.matrix.mean(axis=0), full_matrices=False)
    signs = np.where(vt.sum(axis=1) < 0, -1.0, 1.0)
    vt = vt * signs[:, None]
    u = u * signs[None, :]
    scores = u[:, :k] * s[:k]
    variance = s ** 2 / np.sum(s ** 2)
    result = AnalysisResult("PCA")
    for i in range(k):
        result.columns[f"PC{i + 1}"] = _scatter_back(data, scores[:, i], np.nan)
    result.figures = {
        "explained_variance": variance.tolist(),
        "loadings": {name: vt[:k, j].tolist() for j, name in enumerate(feature_names)},
        "scores": scores[:, :2].tolist() if k >= 2 else scores.tolist(),
        "rows": data.rows.tolist(),
    }
    result.summary = [f"PC{i + 1}: {100 * variance[i]:.1f}% of variance" for i in range(k)]
    return result


def run_kmeans(x: np.ndarray, n_clusters: int, nan_handling: str = "dropna",
               seed: int = 42) -> AnalysisResult:
    """
    K-means clustering. scikit-learn's KMeans (random_state=42, as in
    GSV-Studio) when available, otherwise SciPy's kmeans2 with k-means++
    seeding. Cluster labels start at 1 in the output field (0 is easy to
    mistake for "no cluster").
    """
    data = prepare(x, nan_handling)
    n = int(n_clusters)
    if n < 2 or n > data.matrix.shape[0]:
        raise ValueError("The number of clusters must be between 2 and the number of basins.")
    if HAS_SKLEARN:
        model = KMeans(n_clusters=n, random_state=seed, n_init=10)
        labels = model.fit_predict(data.matrix)
        inertia = float(model.inertia_)
    else:
        rng = np.random.default_rng(seed)
        centroids, labels = kmeans2(data.matrix, n, minit="++", seed=rng)
        inertia = float(np.sum((data.matrix - centroids[labels]) ** 2))
    result = AnalysisResult("KMeans")
    result.columns["cluster_km"] = _scatter_back(data, labels.astype(float) + 1, np.nan)
    result.figures = {"rows": data.rows.tolist(), "labels": (labels + 1).tolist()}
    result.summary = [f"{n} clusters, within-cluster sum of squares = {inertia:.2f}"]
    return result


def run_hierarchical(x: np.ndarray, n_clusters: int, method: str = "ward",
                     metric: str = "euclidean", nan_handling: str = "dropna",
                     leaf_labels: Optional[Sequence[str]] = None) -> AnalysisResult:
    """
    Agglomerative hierarchical clustering (SciPy), plus dendrogram segments.

    Ward linkage requires the Euclidean metric (forced, as in GSV-Studio).
    """
    data = prepare(x, nan_handling)
    metric = "euclidean" if method == "ward" else metric
    tree = linkage(data.matrix, method=method, metric=metric)
    labels = fcluster(tree, t=int(n_clusters), criterion="maxclust")
    labels_for_leaves = None
    if leaf_labels is not None:
        labels_for_leaves = [str(leaf_labels[i]) for i in data.rows]
    dendro = dendrogram(tree, no_plot=True, labels=labels_for_leaves)
    result = AnalysisResult("Hierarchical")
    result.columns["cluster_hc"] = _scatter_back(data, labels.astype(float), np.nan)
    result.figures = {
        "icoord": dendro["icoord"], "dcoord": dendro["dcoord"],
        "leaves": dendro["ivl"], "rows": data.rows.tolist(), "labels": labels.tolist(),
    }
    result.summary = [f"{int(n_clusters)} clusters, {method} linkage"]
    return result


def run_dbscan(x: np.ndarray, eps: float, min_samples: int,
               nan_handling: str = "dropna") -> AnalysisResult:
    """DBSCAN (scikit-learn). Label -1 marks noise (outliers)."""
    _require("DBSCAN")
    data = prepare(x, nan_handling)
    labels = DBSCAN(eps=float(eps), min_samples=int(min_samples)).fit_predict(data.matrix)
    result = AnalysisResult("DBSCAN")
    result.columns["cluster_db"] = _scatter_back(data, labels.astype(float), np.nan)
    clusters = len(set(labels) - {-1})
    result.figures = {"rows": data.rows.tolist(), "labels": labels.tolist()}
    result.summary = [f"{clusters} clusters, {int(np.sum(labels == -1))} noise points"]
    return result


def run_embedding(x: np.ndarray, method: str, perplexity: float = 10.0,
                  n_neighbors: int = 10, min_dist: float = 0.1,
                  nan_handling: str = "dropna", seed: int = 42) -> AnalysisResult:
    """
    2-D embedding with t-SNE (scikit-learn) or UMAP (umap-learn).
    The perplexity is capped below the number of samples (t-SNE requirement).
    """
    _require(method)
    data = prepare(x, nan_handling)
    n = data.matrix.shape[0]
    if method == "TSNE":
        model = TSNE(n_components=2, perplexity=float(min(perplexity, max(1.0, n - 1.0))),
                     random_state=seed, init="pca")
    else:
        model = umap.UMAP(n_components=2, n_neighbors=int(min(n_neighbors, n - 1)),
                          min_dist=float(min_dist), random_state=seed)
    coords = model.fit_transform(data.matrix)
    result = AnalysisResult(method)
    prefix = "tsne" if method == "TSNE" else "umap"
    result.columns[f"{prefix}_1"] = _scatter_back(data, coords[:, 0], np.nan)
    result.columns[f"{prefix}_2"] = _scatter_back(data, coords[:, 1], np.nan)
    result.figures = {"rows": data.rows.tolist(), "coords": coords.tolist()}
    result.summary = [f"{METHODS[method][0]} of {n} basins"]
    return result


def run_outliers(x: np.ndarray, method: str, contamination: float = 0.1,
                 n_neighbors: int = 10, nan_handling: str = "dropna",
                 seed: int = 42) -> AnalysisResult:
    """
    Outlier detection with Isolation Forest or Local Outlier Factor.

    Output fields: ``outlier`` (1 = outlier, 0 = inlier) and ``out_score``
    (higher = more anomalous, i.e. the negated scikit-learn score).
    """
    _require(method)
    data = prepare(x, nan_handling)
    n = data.matrix.shape[0]
    if method == "IsolationForest":
        model = IsolationForest(contamination=float(contamination), random_state=seed)
        flags = model.fit_predict(data.matrix)
        score = -model.score_samples(data.matrix)
    else:
        model = LocalOutlierFactor(n_neighbors=int(min(n_neighbors, n - 1)),
                                   contamination=float(contamination))
        flags = model.fit_predict(data.matrix)
        score = -model.negative_outlier_factor_
    result = AnalysisResult(method)
    result.columns["outlier"] = _scatter_back(data, (flags == -1).astype(float), np.nan)
    result.columns["out_score"] = _scatter_back(data, score.astype(float), np.nan)
    result.figures = {"rows": data.rows.tolist(), "flags": (flags == -1).astype(int).tolist(),
                      "score": score.tolist()}
    result.summary = [f"{int(np.sum(flags == -1))} outliers among {n} basins"]
    return result


def correlation_matrix(x: np.ndarray) -> np.ndarray:
    """Pearson correlation between columns, using pairwise-complete rows."""
    x = np.asarray(x, dtype=float)
    k = x.shape[1]
    corr = np.full((k, k), np.nan)
    for i in range(k):
        for j in range(i, k):
            ok = np.isfinite(x[:, i]) & np.isfinite(x[:, j])
            if ok.sum() >= 3 and np.std(x[ok, i]) > 0 and np.std(x[ok, j]) > 0:
                corr[i, j] = corr[j, i] = float(np.corrcoef(x[ok, i], x[ok, j])[0, 1])
    return corr
