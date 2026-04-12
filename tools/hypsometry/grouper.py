# tools/hypsometry/grouper.py

"""
tools/hypsometry/grouper.py

HypsometryGrouper — smart grouping of hypsometric curves.

Receives the flat list of basin results from HypsometryEngine
and organizes them into plot-ready groups based on a chosen strategy.

Strategies:
  - none     : one basin per "group" (ungrouped mode)
  - hi       : group by Hypsometric Integral range
  - area     : group by drainage area
  - relief   : group by total relief

Design rules:
  - Pure Python — zero QGIS / Qt dependencies
  - Fully unit-testable in isolation
  - Never mutates input results
  - Always respects max_per_group hard limit
  - Handles edge cases: empty bins, single-basin groups, duplicates

Authors: RockMorph contributors / Tony
"""

from __future__ import annotations
import math


# ---------------------------------------------------------------------------
# Public API
# ---------------------------------------------------------------------------

def group_results(
    results:       list[dict],
    strategy:      str = "none",
    max_per_group: int = 6,
    n_bins:        int = 4,
) -> list[dict]:
    """
    Group basin results into plot-ready groups.

    Parameters
    ----------
    results       : flat list of basin dicts from HypsometryEngine
    strategy      : 'none' | 'hi' | 'area' | 'relief'
    max_per_group : hard maximum number of curves per group
    n_bins        : number of bins for hi/area/relief strategies

    Returns
    -------
    list of group dicts:
        {
            "label"   : str,          # group label for UI
            "members" : list[dict],   # basin result dicts in this group
            "stats"   : dict,         # aggregate stats for the group
        }
    """
    if not results:
        return []

    if strategy == "none":
        groups = _group_none(results)
    elif strategy == "hi":
        groups = _group_by_field(results, "hi",       n_bins, "HI")
    elif strategy == "area":
        groups = _group_by_field(results, "area_km2", n_bins, "Area")
    elif strategy == "relief":
        groups = _group_by_field(results, "relief",   n_bins, "Relief")
    else:
        groups = _group_none(results)

    # Apply max_per_group hard limit — split oversized groups
    groups = _enforce_max(groups, max_per_group)

    # Compute aggregate stats per group
    for group in groups:
        group["stats"] = _group_stats(group["members"])

    return groups


def move_to_ungrouped(
    groups:    list[dict],
    fid:       int,
) -> list[dict]:
    """
    Remove a basin from its current group and place it
    in a special 'Ungrouped' group at the end of the list.

    Parameters
    ----------
    groups : current group list (not mutated — returns new list)
    fid    : feature id of the basin to move

    Returns
    -------
    New group list with the basin moved to 'Ungrouped'.
    """
    target  = None
    new_groups = []

    for group in groups:
        remaining = [m for m in group["members"] if m["fid"] != fid]
        if len(remaining) < len(group["members"]):
            # Found the basin
            target = next(m for m in group["members"] if m["fid"] == fid)

        if remaining:
            new_group = {
                "label":   group["label"],
                "members": remaining,
                "stats":   _group_stats(remaining),
            }
            new_groups.append(new_group)
        # Empty groups are dropped

    if target is None:
        return groups   # fid not found — return unchanged

    # Add to or create Ungrouped
    ungrouped = next(
        (g for g in new_groups if g["label"] == "Ungrouped"), None
    )
    if ungrouped:
        ungrouped["members"].append(target)
        ungrouped["stats"] = _group_stats(ungrouped["members"])
    else:
        new_groups.append({
            "label":   "Ungrouped",
            "members": [target],
            "stats":   _group_stats([target]),
        })

    return new_groups


# ---------------------------------------------------------------------------
# Strategies
# ---------------------------------------------------------------------------

def _group_none(results: list[dict]) -> list[dict]:
    """
    No grouping — each basin is its own group.
    Navigation ◄ ► moves one basin at a time.
    """
    return [
        {
            "label":   r["label"],
            "members": [r],
            "stats":   {},   # filled later by group_results
        }
        for r in results
    ]


def _group_by_field(
    results: list[dict],
    field:   str,
    n_bins:  int,
    prefix:  str,
) -> list[dict]:
    """
    Generic binning strategy.
    Splits the value range of `field` into n_bins equal intervals.
    Empty bins are dropped. Non-finite values go to a fallback group.
    """
    # Separate valid and non-finite values
    valid   = [r for r in results if _is_finite(r.get(field))]
    invalid = [r for r in results if not _is_finite(r.get(field))]

    if not valid:
        return _group_none(results)

    v_min = min(r[field] for r in valid)
    v_max = max(r[field] for r in valid)

    # Degenerate case — all values identical
    if v_max - v_min < 1e-9:
        groups = [{
            "label":   f"{prefix} {_fmt(v_min)}",
            "members": valid,
            "stats":   {},
        }]
        if invalid:
            groups.append({
                "label":   "No data",
                "members": invalid,
                "stats":   {},
            })
        return groups

    bin_edges = _linspace(v_min, v_max, n_bins + 1)
    groups    = []

    for i in range(n_bins):
        lo  = bin_edges[i]
        hi  = bin_edges[i + 1]

        # Include upper bound in last bin
        if i == n_bins - 1:
            members = [r for r in valid if lo <= r[field] <= hi]
        else:
            members = [r for r in valid if lo <= r[field] < hi]

        if not members:
            continue

        label = f"{prefix} {_fmt(lo)} – {_fmt(hi)}"
        groups.append({
            "label":   label,
            "members": members,
            "stats":   {},
        })

    # Fallback group for non-finite values
    if invalid:
        groups.append({
            "label":   "No data",
            "members": invalid,
            "stats":   {},
        })

    return groups if groups else _group_none(results)


# ---------------------------------------------------------------------------
# Max per group enforcement
# ---------------------------------------------------------------------------

def _enforce_max(groups: list[dict], max_per_group: int) -> list[dict]:
    """
    Split any group exceeding max_per_group into sub-groups.
    Sub-groups inherit the parent label with a numeric suffix.
    """
    if max_per_group < 1:
        max_per_group = 1

    result = []
    for group in groups:
        members = group["members"]
        if len(members) <= max_per_group:
            result.append(group)
            continue

        # Split into chunks
        chunks = _chunks(members, max_per_group)
        total  = len(chunks)
        for idx, chunk in enumerate(chunks):
            suffix = f" ({idx + 1}/{total})" if total > 1 else ""
            result.append({
                "label":   group["label"] + suffix,
                "members": chunk,
                "stats":   {},
            })

    return result


# ---------------------------------------------------------------------------
# Aggregate stats
# ---------------------------------------------------------------------------

def _group_stats(members: list[dict]) -> dict:
    """
    Compute aggregate statistics for a group of basins.
    All fields are optional — missing keys are skipped gracefully.
    """
    if not members:
        return {}

    def safe_vals(key):
        return [m[key] for m in members if _is_finite(m.get(key))]

    hi_vals     = safe_vals("hi")
    area_vals   = safe_vals("area_km2")
    relief_vals = safe_vals("relief")

    stats = {"n": len(members)}

    if hi_vals:
        stats["hi_min"]  = round(min(hi_vals), 3)
        stats["hi_max"]  = round(max(hi_vals), 3)
        stats["hi_mean"] = round(sum(hi_vals) / len(hi_vals), 3)

    if area_vals:
        stats["area_total_km2"] = round(sum(area_vals), 1)
        stats["area_mean_km2"]  = round(sum(area_vals) / len(area_vals), 1)

    if relief_vals:
        stats["relief_mean"] = round(sum(relief_vals) / len(relief_vals), 1)

    return stats


# ---------------------------------------------------------------------------
# Utilities — pure Python, no dependencies
# ---------------------------------------------------------------------------

def _chunks(lst: list, size: int) -> list[list]:
    """Split list into chunks of at most `size`."""
    return [lst[i: i + size] for i in range(0, len(lst), size)]


def _linspace(start: float, stop: float, n: int) -> list[float]:
    """Pure Python linspace — avoids numpy dependency in this module."""
    if n < 2:
        return [start]
    step = (stop - start) / (n - 1)
    return [start + i * step for i in range(n)]


def _is_finite(val) -> bool:
    """True if val is a real finite number."""
    try:
        return math.isfinite(float(val))
    except (TypeError, ValueError):
        return False


def _fmt(val: float) -> str:
    """
    Format a float for group labels.
    Uses 2 decimals for HI (0–1 range), integers for large values.
    """
    if abs(val) < 10:
        return f"{val:.2f}"
    return f"{val:.0f}"