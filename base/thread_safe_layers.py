"""
base/thread_safe_layers.py

Thread-safe stand-ins for the QGIS layers handed to a background computation
(audit M4).

QGIS map layers are not thread-safe: a layer shown in the project may be
redrawn, edited or removed by the main thread while a worker thread reads
it, which can crash QGIS at random. RockMorph's engines used to receive the
project layers themselves inside a QThread.

``LayerSnapshot`` is taken on the MAIN thread when a computation starts. It
records how to reopen the layer (source, provider, name, CRS, subset
filter, selection) and ``open()`` — called inside the worker thread —
creates a private layer that only the worker uses:

* file- or database-based layers are reopened from their source with the
  same CRS, filter and selected features;
* memory layers cannot be reopened from a source: they are copied
  (materialised) on the main thread and the copy is handed to the worker.
  Feature ids of such copies are renumbered by QGIS;
* if a layer cannot be reopened (unusual provider), the original layer is
  used as before, and a message is printed.

``original_layer_id(layer)`` returns the id of the project layer a stand-in
was made from, for engines that label their outputs with layer ids.

Authors: RockMorph contributors / Tony
"""

from __future__ import annotations

from typing import Any, Optional

from qgis.core import (  # type: ignore
    QgsFeatureRequest, QgsMapLayer, QgsRasterLayer, QgsVectorLayer,
)

ORIGINAL_ID_PROPERTY = "rockmorph/original_layer_id"


def original_layer_id(layer: QgsMapLayer) -> str:
    """Id of the project layer ``layer`` stands for (its own id otherwise)."""
    value = layer.customProperty(ORIGINAL_ID_PROPERTY)
    return str(value) if value else layer.id()


class LayerSnapshot:
    """Everything needed to reopen a layer in another thread."""

    def __init__(self, layer: QgsMapLayer):
        self.original = layer
        self.layer_id = layer.id()
        self.name = layer.name()
        self.source = layer.source()
        self.provider = layer.providerType()
        self.crs = layer.crs()
        self.is_vector = isinstance(layer, QgsVectorLayer)
        self.subset = layer.subsetString() if self.is_vector else ""
        self.selected = list(layer.selectedFeatureIds()) if self.is_vector else []
        self.memory_copy: Optional[QgsVectorLayer] = None
        if self.is_vector and self.provider == "memory":
            # Copied now, on the main thread, while nothing else touches it.
            self.memory_copy = layer.materialize(QgsFeatureRequest())
            self.memory_copy.setCustomProperty(ORIGINAL_ID_PROPERTY, self.layer_id)

    def open(self) -> QgsMapLayer:
        """Private layer for the worker thread (call it in that thread)."""
        if self.memory_copy is not None:
            return self.memory_copy
        if self.is_vector:
            layer = QgsVectorLayer(self.source, self.name, self.provider)
        else:
            layer = QgsRasterLayer(self.source, self.name, self.provider)
        if not layer.isValid():
            print(f"[RockMorph] Could not reopen layer '{self.name}' for the background "
                  "computation; using the project layer.")
            return self.original
        if layer.crs() != self.crs:
            layer.setCrs(self.crs)          # user-assigned CRS overrides the file's
        if self.is_vector:
            if self.subset:
                layer.setSubsetString(self.subset)
            if self.selected:
                layer.selectByIds(self.selected)
        layer.setCustomProperty(ORIGINAL_ID_PROPERTY, self.layer_id)
        return layer


def snapshot_value(value: Any) -> Any:
    """Replace layers (also inside lists/tuples) by snapshots; keep other values."""
    if isinstance(value, QgsMapLayer):
        return LayerSnapshot(value)
    if isinstance(value, (list, tuple)) and any(isinstance(v, QgsMapLayer) for v in value):
        return type(value)(snapshot_value(v) for v in value)
    return value


def open_value(value: Any) -> Any:
    """Inverse of ``snapshot_value``, called in the worker thread."""
    if isinstance(value, LayerSnapshot):
        return value.open()
    if isinstance(value, (list, tuple)) and any(isinstance(v, LayerSnapshot) for v in value):
        return type(value)(open_value(v) for v in value)
    return value
