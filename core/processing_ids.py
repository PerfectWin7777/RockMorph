"""
core/processing_ids.py

Resolution of QGIS Processing algorithm identifiers that changed between
QGIS versions (audit M5).

* GRASS: QGIS 3.36 renamed the provider "grass7" to "grass"; "grass7:..."
  still works as an alias in QGIS 3.44 but is deprecated. ``grass_id`` returns
  whichever id is registered, preferring the current one.
* Sink filling: "native:filldepressions" never existed; the native
  Wang & Liu (2006) algorithm is "native:fillsinkswangliu" (QGIS ≥ 3.40),
  the SAGA NextGen one "sagang:fillsinkswangliu".

Missing providers raise ``AlgorithmUnavailable`` with a message the user can
act on, instead of a bare Processing error.

Authors: RockMorph contributors / Tony
"""

from __future__ import annotations

from typing import Optional, Sequence

from qgis.core import QgsApplication  # type: ignore


class AlgorithmUnavailable(RuntimeError):
    """No registered algorithm matches; the message tells the user what to install."""


def first_available(candidates: Sequence[str]) -> Optional[str]:
    """First algorithm id of ``candidates`` registered in Processing, or None."""
    registry = QgsApplication.processingRegistry()
    for algorithm_id in candidates:
        if registry.algorithmById(algorithm_id) is not None:
            return algorithm_id
    return None


def grass_id(name: str) -> str:
    """'grass:<name>' (QGIS ≥ 3.36) or 'grass7:<name>' (older QGIS)."""
    found = first_available((f"grass:{name}", f"grass7:{name}"))
    if found is None:
        raise AlgorithmUnavailable(
            f"The GRASS algorithm '{name}' is not available. Enable the GRASS provider in "
            "Processing > Options > Providers, or install QGIS with GRASS "
            "(e.g. the 'qgis-plugin-grass' package on Linux).")
    return found


# Wang & Liu (2006) sink filling, native first.
FILL_SINKS_IDS = ("native:fillsinkswangliu", "sagang:fillsinkswangliu")
