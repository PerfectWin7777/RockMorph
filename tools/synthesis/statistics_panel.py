"""
tools/synthesis/statistics_panel.py

Statistics & machine-learning tool (GSV-Studio "StatLab", improved).

Methods (``core.indices.ml``):

  PCA              principal component analysis (NumPy SVD; always available)
  K-means          clustering (scikit-learn, or SciPy kmeans2 as fallback)
  Hierarchical     agglomerative clustering + dendrogram (SciPy)
  DBSCAN           density clustering (scikit-learn)
  t-SNE, UMAP      2-D embeddings (scikit-learn / umap-learn)
  Isolation Forest, Local Outlier Factor  outlier detection (scikit-learn)

All methods work on standardised fields (z-scores), so indices with
different units weigh the same. Methods whose library is not installed are
listed but disabled with an explanation (the RockMorph installer will offer
to install scikit-learn).

Every clustering result is also drawn in the plane of the first two
principal components, which gives a stable, interpretable view of the
clusters whatever the method.

Authors: RockMorph contributors / Tony
"""

from __future__ import annotations

from typing import Dict, List

import numpy as np  # type: ignore
from qgis.PyQt.QtCore import QCoreApplication  # type: ignore
from qgis.PyQt.QtWidgets import QComboBox, QFormLayout, QLabel, QVBoxLayout  # type: ignore

from ...core.indices import ml
from ...core.indices.spec import ParameterSpec
from ...widgets.index_widgets import ParameterForm
from .common import OutputColumn, SynthesisPanel, TableData


def tr(message: str) -> str:
    return QCoreApplication.translate("RockMorph", message)


# Categorical slots (reference palette, fixed order). Beyond eight groups the
# colours repeat and the chart note says so (identity stays in the tooltip
# and in the legend).
SERIES = ["#2a78d6", "#eb6834", "#1baf7a", "#eda100", "#e87ba4", "#008300", "#6250d6", "#e34948"]
NOISE = "#9a9994"
OUTLIER = "#d03b3b"

NAN_HANDLING = ParameterSpec(
    "nan_handling", "Missing values", "choice", "Drop basins with missing values",
    help="Basins with a missing value in any selected field are either left out (safe) or have "
         "the missing value replaced by the field mean (keeps every basin, may bias results).",
    choices=("Drop basins with missing values", "Replace by the field mean"))

PARAMETERS: Dict[str, tuple] = {
    "PCA": (ParameterSpec("n_components", "Components kept", "int", 2, minimum=1, maximum=10,
                          help="Number of principal components written to the output layer."),),
    "KMeans": (ParameterSpec("n_clusters", "Number of clusters", "int", 3, minimum=2, maximum=20,
                             help="k of k-means. Try several values and compare the maps."),),
    "Hierarchical": (
        ParameterSpec("n_clusters", "Number of clusters", "int", 3, minimum=2, maximum=20,
                      help="Where the dendrogram is cut."),
        ParameterSpec("method", "Linkage", "choice", "ward",
                      choices=("ward", "average", "complete", "single"),
                      help="Ward (default) minimises within-cluster variance and needs the Euclidean "
                           "distance."),
        ParameterSpec("metric", "Distance", "choice", "euclidean",
                      choices=("euclidean", "cityblock", "cosine"),
                      help="Ignored (Euclidean) with Ward linkage."),
    ),
    "DBSCAN": (
        ParameterSpec("eps", "Neighbourhood radius ε", "float", 0.8, minimum=0.01, maximum=20.0,
                      step=0.1, help="In standard deviations (fields are standardised)."),
        ParameterSpec("min_samples", "Minimum basins per core", "int", 3, minimum=2, maximum=50,
                      help="Basins needed within ε to form a cluster core."),
    ),
    "TSNE": (ParameterSpec("perplexity", "Perplexity", "float", 10.0, minimum=2.0, maximum=50.0,
                           help="Effective number of neighbours; capped below the number of basins."),),
    "UMAP": (
        ParameterSpec("n_neighbors", "Neighbours", "int", 10, minimum=2, maximum=100,
                      help="Local neighbourhood size."),
        ParameterSpec("min_dist", "Minimum distance", "float", 0.1, minimum=0.0, maximum=1.0, step=0.05,
                      help="How tightly points may be packed in the embedding."),
    ),
    "IsolationForest": (ParameterSpec("contamination", "Expected outlier share", "float", 0.1,
                                      minimum=0.01, maximum=0.5, step=0.01,
                                      help="Fraction of basins expected to be outliers."),),
    "LOF": (
        ParameterSpec("n_neighbors", "Neighbours", "int", 10, minimum=2, maximum=100,
                      help="Neighbourhood used to compare local densities."),
        ParameterSpec("contamination", "Expected outlier share", "float", 0.1, minimum=0.01,
                      maximum=0.5, step=0.01, help="Fraction of basins expected to be outliers."),
    ),
}


class StatisticsPanel(SynthesisPanel):
    """Statistics and machine learning on index fields (sidebar)."""

    TITLE = "Statistics & machine learning"
    OUTPUT_PREFIX = "Statistics"
    MIN_FIELDS = 2
    INTRO = ("Explore index fields with principal component analysis, clustering, embeddings and "
             "outlier detection. Fields are standardised (z-scores) before every method.")

    def _build_settings(self, layout: QVBoxLayout) -> None:
        form = QFormLayout()
        self.method = QComboBox()
        available = ml.available_methods()
        for key, (title, library) in ml.METHODS.items():
            text = title if available[key] else f"{title} — {tr('needs')} {library}"
            self.method.addItem(text, key)
            if not available[key]:
                item = self.method.model().item(self.method.count() - 1)
                item.setEnabled(False)
                item.setToolTip(tr("Install the Python library '{0}' in QGIS to enable this method.")
                                .format(library))
        self.method.currentIndexChanged.connect(self._on_method)
        form.addRow(tr("Method"), self.method)
        layout.addLayout(form)
        self.params = ParameterForm()
        layout.addWidget(self.params)
        missing = sorted({lib for key, (_, lib) in ml.METHODS.items() if lib and not available[key]})
        self.dependencies = QLabel(
            tr("Not installed: {0}. The corresponding methods are disabled.").format(", ".join(missing))
            if missing else tr("All methods are available."))
        self.dependencies.setWordWrap(True)
        self.dependencies.setStyleSheet("color: #52514e;")
        layout.addWidget(self.dependencies)
        self._on_method()

    def _on_method(self, *args) -> None:
        key = self.method.currentData()
        self.params.set_parameters(PARAMETERS.get(key, ()) + (NAN_HANDLING,))

    def _settings_issues(self) -> List[str]:
        if not hasattr(self, "method"):
            return []
        key = self.method.currentData()
        if not ml.available_methods().get(key, False):
            return [tr("The selected method needs a library that is not installed.")]
        return []

    # -- computation ------------------------------------------------------------------

    def _compute(self, table: TableData, fields: List[str]):
        key = self.method.currentData()
        values = self.params.values()
        nan = "impute_mean" if values.pop("nan_handling", "").startswith("Replace") else "dropna"
        x = table.matrix(fields)
        labels = table.labels

        if key == "PCA":
            result = ml.run_pca(x, fields, int(values["n_components"]), nan)
        elif key == "KMeans":
            result = ml.run_kmeans(x, int(values["n_clusters"]), nan)
        elif key == "Hierarchical":
            result = ml.run_hierarchical(x, int(values["n_clusters"]), values["method"],
                                         values["metric"], nan, leaf_labels=labels)
        elif key == "DBSCAN":
            result = ml.run_dbscan(x, float(values["eps"]), int(values["min_samples"]), nan)
        elif key in ("TSNE", "UMAP"):
            result = ml.run_embedding(x, key, perplexity=float(values.get("perplexity", 10.0)),
                                      n_neighbors=int(values.get("n_neighbors", 10)),
                                      min_dist=float(values.get("min_dist", 0.1)), nan_handling=nan)
        else:
            result = ml.run_outliers(x, key, float(values["contamination"]),
                                     int(values.get("n_neighbors", 10)), nan)

        # PCA plane used to draw every result (k = 2 components).
        plane = ml.run_pca(x, fields, 2, nan) if len(fields) >= 2 else None
        figures: List[dict] = []
        style = None
        outputs: List[OutputColumn] = []
        cluster_field = next((c for c in result.columns if c.startswith("cluster")), None)

        for name, column in result.columns.items():
            kind = "int" if name.startswith("cluster") or name == "outlier" else "double"
            outputs.append(OutputColumn(name, column, kind, title=name))

        if key == "PCA":
            figures += _pca_figures(result, labels)
        if cluster_field:
            rules = _cluster_rules(result.columns[cluster_field])
            style = (cluster_field, rules, cluster_field)
            if plane is not None:
                figures.append(_plane_figure(plane, labels, result.columns[cluster_field], rules,
                                             tr("Clusters in the PC1–PC2 plane")))
        if key == "Hierarchical":
            figures.append(_dendrogram_figure(result))
        if key in ("TSNE", "UMAP"):
            figures.append(_embedding_figure(result, labels, key))
        if key in ("IsolationForest", "LOF"):
            rules = [{"label": tr("Inlier"), "score": 0, "color": "#9ec5f4"},
                     {"label": tr("Outlier"), "score": 1, "color": OUTLIER}]
            style = ("outlier", rules, tr("Outliers"))
            if plane is not None:
                figures.append(_plane_figure(plane, labels, result.columns["outlier"], rules,
                                             tr("Outliers in the PC1–PC2 plane")))

        warnings = {}
        used = set(np.flatnonzero(np.isfinite(next(iter(result.columns.values())))).tolist()) \
            if result.columns else set(range(table.size))
        for row in range(table.size):
            if row not in used:
                warnings[row] = [tr("Left out: missing value in at least one selected field.")]
        summary = " · ".join(result.summary)
        return outputs, figures, figures[0]["id"] if figures else None, style, warnings, summary


# ---------------------------------------------------------------------------
# Figures (Plotly JSON built in Python, rendered by web/js/morphometry.js)
# ---------------------------------------------------------------------------

def _cluster_rules(column: np.ndarray) -> List[dict]:
    values = sorted({int(v) for v in column if np.isfinite(v)})
    rules = []
    for n, value in enumerate(values):
        if value == -1:
            rules.append({"label": tr("Noise"), "score": -1, "color": NOISE})
        else:
            rules.append({"label": tr("Cluster {0}").format(value), "score": value,
                          "color": SERIES[n % len(SERIES)]})
    return rules


def _pca_figures(result, labels: List[str]) -> List[dict]:
    variance = result.figures["explained_variance"]
    rows = result.figures["rows"]
    scores = np.asarray(result.figures["scores"])
    loadings = result.figures["loadings"]
    scree = {
        "id": "scree", "title": tr("PCA: explained variance"),
        "data": [{"type": "bar", "x": [f"PC{i + 1}" for i in range(len(variance))],
                  "y": [round(100 * v, 2) for v in variance], "marker": {"color": "#2a78d6"},
                  "hovertemplate": "%{x}: %{y:.1f} %<extra></extra>"}],
        "layout": {"yaxis": {"title": tr("Variance explained (%)")}, "xaxis": {"type": "category"}},
        "note": tr("Share of the total variance carried by each principal component."),
    }
    if scores.ndim != 2 or scores.shape[1] < 2:
        return [scree]
    # Biplot: basins as points, fields as arrows scaled to the score cloud.
    scale = float(np.max(np.abs(scores))) or 1.0
    data = [{"type": "scatter", "mode": "markers+text", "x": scores[:, 0].tolist(), "y": scores[:, 1].tolist(),
             "text": [labels[r] for r in rows], "textposition": "top center", "textfont": {"size": 10},
             "marker": {"size": 8, "color": "#2a78d6", "line": {"color": "#fcfcfb", "width": 2}},
             "name": tr("Basins"), "hovertemplate": "%{text}<br>PC1 %{x:.2f} · PC2 %{y:.2f}<extra></extra>"}]
    for name, load in loadings.items():
        if len(load) < 2:
            continue
        data.append({"type": "scatter", "mode": "lines+text", "x": [0, load[0] * scale], "y": [0, load[1] * scale],
                     "text": ["", name], "textposition": "top right", "line": {"color": "#eb6834", "width": 2},
                     "name": name, "hovertemplate": f"{name}<br>loadings %{{x:.2f}}, %{{y:.2f}}<extra></extra>"})
    biplot = {
        "id": "biplot", "title": tr("PCA: biplot (PC1 × PC2)"), "data": data,
        "layout": {"xaxis": {"title": f"PC1 ({100 * variance[0]:.1f} %)", "zeroline": True},
                   "yaxis": {"title": f"PC2 ({100 * variance[1]:.1f} %)", "zeroline": True,
                             "scaleanchor": "x"}},
        "note": tr("Points: basins (component scores). Orange arrows: field loadings, scaled to the "
                   "score cloud; fields pointing the same way vary together."),
    }
    return [biplot, scree]


def _plane_figure(plane, labels: List[str], column: np.ndarray, rules: List[dict], title: str) -> dict:
    rows = plane.figures["rows"]
    scores = np.asarray(plane.figures["scores"])
    data = []
    for rule in rules:
        pick = [k for k, r in enumerate(rows) if np.isfinite(column[r]) and int(column[r]) == rule["score"]]
        if not pick:
            continue
        data.append({"type": "scatter", "mode": "markers", "name": rule["label"],
                     "x": scores[pick, 0].tolist(), "y": scores[pick, 1].tolist(),
                     "text": [labels[rows[k]] for k in pick],
                     "marker": {"size": 10, "color": rule["color"], "line": {"color": "#fcfcfb", "width": 2}},
                     "hovertemplate": "%{text}<br>" + rule["label"] + "<extra></extra>"})
    note = tr("Basins in the plane of the first two principal components, coloured by result.")
    if len(rules) > len(SERIES):
        note += " " + tr("More than eight groups: colours repeat; read the group in the tooltip.")
    return {"id": "plane", "title": title, "data": data,
            "layout": {"showlegend": True, "xaxis": {"title": "PC1"}, "yaxis": {"title": "PC2"}},
            "note": note}


def _dendrogram_figure(result) -> dict:
    icoord, dcoord, leaves = result.figures["icoord"], result.figures["dcoord"], result.figures["leaves"]
    data = [{"type": "scatter", "mode": "lines", "x": xs, "y": ys, "line": {"color": "#52514e", "width": 1.5},
             "hoverinfo": "skip", "showlegend": False} for xs, ys in zip(icoord, dcoord)]
    ticks = [5 + 10 * i for i in range(len(leaves))]
    return {"id": "dendrogram", "title": tr("Hierarchical clustering: dendrogram"), "data": data,
            "layout": {"xaxis": {"tickmode": "array", "tickvals": ticks, "ticktext": leaves, "tickangle": -60},
                       "yaxis": {"title": tr("Linkage distance")}},
            "note": tr("Cutting the tree at the chosen number of clusters gives the cluster_hc field.")}


def _embedding_figure(result, labels: List[str], key: str) -> dict:
    rows = result.figures["rows"]
    coords = np.asarray(result.figures["coords"])
    return {"id": "embedding", "title": tr("{0}: 2-D embedding").format(ml.METHODS[key][0]),
            "data": [{"type": "scatter", "mode": "markers+text", "x": coords[:, 0].tolist(),
                      "y": coords[:, 1].tolist(), "text": [labels[r] for r in rows],
                      "textposition": "top center", "textfont": {"size": 10},
                      "marker": {"size": 9, "color": "#2a78d6", "line": {"color": "#fcfcfb", "width": 2}},
                      "hovertemplate": "%{text}<extra></extra>"}],
            "layout": {"xaxis": {"title": tr("Dimension 1")}, "yaxis": {"title": tr("Dimension 2")}},
            "note": tr("Nearby basins have similar index profiles; distances between distant groups are "
                       "not meaningful in t-SNE/UMAP.")}
