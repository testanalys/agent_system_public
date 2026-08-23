"""Pure helpers for the operator dashboard story (no Streamlit import)."""
from __future__ import annotations

from typing import Any, Callable


def stringify_rows(rows: list[dict]) -> list[dict[str, str]]:
    """Force every cell to str so Arrow/Streamlit never chokes on mixed types."""
    out: list[dict[str, str]] = []
    for row in rows:
        out.append({str(k): "" if v is None else str(v) for k, v in row.items()})
    return out


def landing_stats(rows: list[dict]) -> dict[str, Any]:
    """Derive merge vs stalled master cycles from the timeline."""
    master_spawns = [
        r for r in rows if r.get("tier") == "master" and r.get("kind") == "spawn"
    ]
    merges = [r for r in rows if r.get("kind") == "route" and r.get("outcome") == "merge"]
    operators = [
        r for r in rows if r.get("kind") == "route" and r.get("outcome") == "operator"
    ]
    holds = [r for r in rows if r.get("kind") == "route" and r.get("outcome") == "none"]
    routes = [r for r in rows if r.get("kind") == "route"]
    stalled = max(0, len(master_spawns) - len(routes))
    latest = rows[-1] if rows else None
    return {
        "master_spawns": len(master_spawns),
        "merges": len(merges),
        "operator_routes": len(operators),
        "holds": len(holds),
        "stalled": stalled,
        "latest": latest,
    }


def operator_counts(
    subs: list[str],
    *,
    load_operator: Callable[[str, str], list[dict]],
    load_escalations: Callable[[str], list[dict]],
) -> dict[str, int]:
    counts = {"open": 0, "hold": 0, "incomplete": 0, "escalations": 0}
    for sub in subs:
        counts["open"] += len(load_operator("open", sub))
        counts["hold"] += len(load_operator("hold", sub))
        counts["incomplete"] += len(load_operator("incomplete", sub))
        counts["escalations"] += len(load_escalations(sub))
    return counts
