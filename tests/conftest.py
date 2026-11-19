"""
tests/conftest.py

Pytest configuration for RockMorph.

The pure index library (``core/indices``) is imported as the top-level
package ``core`` so these tests run without QGIS. Tests that need QGIS live
under ``tests/qgis`` and are skipped when ``qgis.core`` cannot be imported.

Authors: RockMorph contributors / Tony
"""

import os
import sys

REPO_ROOT = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
if REPO_ROOT not in sys.path:
    sys.path.insert(0, REPO_ROOT)
