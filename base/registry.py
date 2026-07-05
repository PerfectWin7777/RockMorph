"""
base/registry.py

Centralized Tool Registry and Classification for RockMorph.

Provides a unified dictionary of all geomorphological tools, classified by family.
Supports dynamic lazy loading of UI panels to maintain lightning-fast QGIS startup times.

To add or reorganize tools, modify this registry dict. The QGIS top menus, 
toolbars, and quick search widgets will adapt dynamically on launch.

Authors: RockMorph contributors / Tony
"""

from PyQt5.QtCore import QCoreApplication  # type: ignore

def tr(message):
    return QCoreApplication.translate("RockMorph", message)

# ── CENTRAL TOOL CLASSIFICATION REGISTRY ─────────────────────────────
# Grouped by geomorphological/tectonic families.
# Each tool declares its module path and class name for lazy loading.
TOOL_REGISTRY = {
    "structural": {
        "name": tr("Structural Geology"),
        "icon": "structural.png",  # Placeholder for future toolbar icons
        "tools": {
            "rose": {
                "name": tr("Rose Diagram"),
                "module_path": "rockmorph.tools.rose.panel",
                "class_name": "RosePanel",
                "desc": tr("Plots structural strike and dip directions on a polar grid.")
            },
            "digitizer": {
                "name": tr("Geological Digitizer"),
                "module_path": "rockmorph.tools.digitizer.panel",
                "class_name": "DigitizerPanel",
                "desc": tr("Specialized topological tools for digitizing geological features.")
            }
        }
    },
    "fluvial": {
        "name": tr("Fluvial Geomorphology"),
        "icon": "fluvial.png",
        "tools": {
            "fluvial_toolbox": {
                "name": tr("Fluvial Toolbox"),
                "module_path": "rockmorph.tools.fluvial.panel",
                "class_name": "FluvialPanel",
                "desc": tr("Computes geomorphic indices like continuous k_sn and Chi-plots.")
            },
            "watershed": {
                "name": tr("Watershed Extraction"),
                "module_path": "rockmorph.tools.watershed.panel",
                "class_name": "WatershedPanel",
                "desc": tr("Automated catchment boundary and hydrological routing extraction.")
            },
            "smf": {
                "name": tr("Mountain Front Sinuosity (Smf)"),
                "module_path": "rockmorph.tools.smf.panel",
                "class_name": "SMFPanel",
                "desc": tr("Extracts and measures scarp sinuosity and tectonic activity classes.")
            },
            "ncp": {
                "name": tr("Normalized Channel Profile"),
                "module_path": "rockmorph.tools.ncp.panel",
                "class_name": "NCPPanel",
                "desc": tr("Plots and analyzes normalized longitudinal river profiles.")
            },
            "hydroflow": {
                "name": tr("HydroFlow Routing"),
                "module_path": "rockmorph.tools.hydroflow.panel",
                "class_name": "HydroFlowPanel",
                "desc": tr("Computes hydrological routing and extracts topologically ordered networks [1.13.2].")
            }
        }
    },
    "topography": {
        "name": tr("Topographic Analysis"),
        "icon": "terrain.png",
        "tools": {
            "swath": {
                "name": tr("Swath Profile"),
                "module_path": "rockmorph.tools.swath.panel",
                "class_name": "SwathPanel",
                "desc": tr("Extracts topographic swath envelope profiles along paths.")
            },
            "hypsometry": {
                "name": tr("Hypsometric Curve"),
                "module_path": "rockmorph.tools.hypsometry.panel",
                "class_name": "HypsometryPanel",
                "desc": tr("Computes basin-scale hypsometric curves and integral values.")
            }
        }
    },
    "visualization": {
        "name": tr("3D & Exploration"),
        "icon": "visual.png",
        "tools": {
            "explorer3d": {
                "name": tr("3D Explorer"),
                "module_path": "rockmorph.tools.explorer3d.panel",
                "class_name": "Explorer3DPanel",
                "desc": tr("Interactive 3D rendering and terrain model fly-throughs.")
            }
        }
    }
}


def get_all_tools() -> dict:
    """
    Helper to flatten the hierarchical registry into a flat dictionary
    keyed by unique tool_id, used for rapid search and lookup.
    """
    flat_tools = {}
    for cat_id, cat_info in TOOL_REGISTRY.items():
        for tool_id, tool_info in cat_info["tools"].items():
            flat_tools[tool_id] = {
                "id": tool_id,
                "category_id": cat_id,
                "category_name": cat_info["name"],
                **tool_info
            }
    return flat_tools