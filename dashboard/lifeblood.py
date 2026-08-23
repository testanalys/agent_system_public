"""Catalogue-backed helpers for the operator dashboard (no Streamlit)."""
from __future__ import annotations

import json
from datetime import datetime
from pathlib import Path
from typing import Any

DASHBOARD_DIR = Path(__file__).resolve().parent
AGENT_LAYER = DASHBOARD_DIR.parent
WORKSPACE = AGENT_LAYER.parent

GCS_FOLDERS = {
    "discovery": ("audio-discovery",),
    "pipeline": ("pipeline", "pipeline-dispatch"),
}

STAGE_MEANING = {
    "0": "In the corpus, not admitted to the pipeline",
    "1": "Admitted, waiting for audio acquire",
    "2": "Audio staged, waiting for process",
    "3": "Processed, waiting for publish",
    "4": "Indexed (findable)",
}

_CATALOGUE_CACHE: dict[str, dict] = {}


def gcs_folders(subsystem: str) -> tuple[str, ...]:
    return GCS_FOLDERS.get(subsystem, (subsystem,))


def _catalogue_paths(subsystem: str) -> list[Path]:
    baked = DASHBOARD_DIR / "catalogues" / f"{subsystem}.json"
    siblings = {
        "discovery": WORKSPACE / "your-discovery-repo" / "lifeblood" / "catalogue.json",
        "pipeline": WORKSPACE / "your-pipeline-repo" / "lifeblood" / "catalogue.json",
    }
    out = []
    sib = siblings.get(subsystem)
    if sib is not None:
        out.append(sib)
    out.append(baked)
    return out


def load_catalogue(subsystem: str, *, gcs_payload: dict | None = None) -> dict:
    """Prefer a live GCS catalogue, then the sibling repo, then the baked copy."""
    if gcs_payload and gcs_payload.get("metrics"):
        return gcs_payload
    if subsystem in _CATALOGUE_CACHE:
        return _CATALOGUE_CACHE[subsystem]
    for path in _catalogue_paths(subsystem):
        if not path.exists():
            continue
        try:
            data = json.loads(path.read_text(encoding="utf-8"))
        except (OSError, json.JSONDecodeError):
            continue
        if data.get("metrics"):
            _CATALOGUE_CACHE[subsystem] = data
            return data
    return {"metrics": []}


def metric_entry(catalogue: dict, metric_id: str) -> dict:
    for entry in catalogue.get("metrics") or []:
        if entry.get("id") == metric_id:
            return entry
    return {"id": metric_id}


def metric_help(entry: dict, last: dict | None = None) -> str:
    """Tooltip text from the catalogue definition, plus live capture notes."""
    parts: list[str] = []
    name = entry.get("name") or entry.get("id") or ""
    if name:
        parts.append(str(name))
    if entry.get("intent"):
        parts.append(str(entry["intent"]))
    if entry.get("insight"):
        parts.append("Read: " + str(entry["insight"]))
    unit = entry.get("unit")
    cadence = entry.get("cadence")
    meta = " · ".join(x for x in (
        f"unit {unit}" if unit else "",
        f"cadence {cadence}" if cadence else "",
    ) if x)
    if meta:
        parts.append(meta)
    last = last or {}
    if last.get("status") and last.get("status") != "ok":
        reason = last.get("reason") or last.get("status")
        parts.append(f"Latest reading: {reason}")
    ts = last.get("ts")
    if ts:
        parts.append(f"Updated {str(ts)[:19]}")
    return "\n\n".join(parts) or "No catalogue definition yet."


def display_value(last: dict) -> Any:
    if not last:
        return "—"
    if last.get("status") == "ok" and last.get("value") is not None:
        return last["value"]
    dims = last.get("dims") or {}
    if dims.get("yield_only"):
        return "yield_only"
    return last.get("status") or "—"


def last_ok_or_latest(series: list[dict]) -> tuple[dict, dict]:
    """Return (display_row, latest_row). Prefer last ok for the number."""
    latest = series[-1] if series else {}
    for row in reversed(series or []):
        if row.get("status") == "ok" and row.get("value") is not None:
            return row, latest
    return latest, latest


def staleness_note(series: list[dict], *, hours: float = 6.0) -> str:
    from datetime import datetime, timezone

    latest = series[-1] if series else {}
    ok = None
    for row in reversed(series or []):
        if row.get("status") == "ok":
            ok = row
            break
    parts = []
    if latest and latest.get("status") not in (None, "ok"):
        parts.append(
            f"latest capture {latest.get('status')}: "
            f"{latest.get('reason') or 'see dims'}"
        )
    if ok and ok.get("ts"):
        try:
            when = datetime.fromisoformat(str(ok["ts"]).replace("Z", "+00:00"))
            if when.tzinfo is None:
                when = when.replace(tzinfo=timezone.utc)
            age_h = (datetime.now(timezone.utc) - when).total_seconds() / 3600.0
            if age_h > hours:
                parts.append(
                    f"last ok reading is {age_h:.0f}h old ({str(ok['ts'])[:19]} UTC)"
                )
        except ValueError:
            parts.append(f"last ok at {str(ok.get('ts'))[:19]}")
    elif not ok:
        parts.append("no ok reading in the window")
    return " · ".join(parts)


def updated_caption(last: dict) -> str:
    ts = last.get("ts")
    if not ts:
        return ""
    try:
        when = datetime.fromisoformat(str(ts).replace("Z", "+00:00"))
        return when.strftime("updated %Y-%m-%d %H:%M UTC")
    except ValueError:
        return f"updated {str(ts)[:19]}"


def campaign_target_rows(dims: dict) -> list[dict]:
    """Flatten campaign_coverage dims into one row per harvester target."""
    rows = []
    campaigns = dims.get("campaigns") or {}
    for cid, tools in campaigns.items():
        if not isinstance(tools, dict):
            continue
        for hid, row in tools.items():
            if not isinstance(row, dict):
                continue
            rows.append({
                "campaign": cid,
                "tool": hid,
                "unique_ops": row.get("unique_ops"),
                "target_window": row.get("target_window"),
                "met": row.get("met"),
            })
    return rows


def funnel_stage_rows(dims: dict) -> list[dict]:
    by_stage = dims.get("by_stage") or {}
    meanings = dims.get("stage_meanings") or STAGE_MEANING
    rows = []
    for key in sorted(by_stage, key=lambda k: int(k) if str(k).isdigit() else 99):
        row = by_stage[key] if isinstance(by_stage[key], dict) else {"n": by_stage[key]}
        meaning = row.get("meaning") or meanings.get(str(key)) or meanings.get(
            int(key) if str(key).isdigit() else key, "")
        rows.append({
            "stage": str(key),
            "n": row.get("n", 0),
            "meaning": meaning,
        })
    if not rows:
        for stage, meaning in STAGE_MEANING.items():
            rows.append({"stage": stage, "n": 0, "meaning": meaning})
    return rows
