"""Operator cards in GCS — the HITL surface now that Slack is gone.

    agent_layer/operator/open/         branch waiting on a GitHub PR
    agent_layer/operator/hold/         master chose not to act; headline is why
    agent_layer/operator/incomplete/   master session failed its contract
                                       (should not happen; always actionable)

Sibling folders. Writing open/hold is what lets the harness close
escalations. Incomplete keeps the ledger open and parks a card here so
the operator sees the sub-reason.
"""
from __future__ import annotations

import re
from datetime import datetime, timezone

from . import objectstore

OPEN_PREFIX = "agent_layer/operator/open"
HOLD_PREFIX = "agent_layer/operator/hold"
INCOMPLETE_PREFIX = "agent_layer/operator/incomplete"
_SAFE = re.compile(r"[^A-Za-z0-9._-]+")

# Stable codes for incomplete cards. Headline shown on the dashboard is
# the human line next to each code.
SUB_REASONS = {
    "branch_not_on_origin": "INCOMPLETE — branch not on origin",
    "no_wager_produced": "INCOMPLETE — no wager produced",
    "turn_cap": "INCOMPLETE — turn cap reached",
}


def _now() -> str:
    return datetime.now(timezone.utc).isoformat(timespec="seconds")


def _key(prefix: str, subsystem: str, name: str) -> str:
    return f"{prefix}/{subsystem}/{_SAFE.sub('-', name)}.json"


def write_open(subsystem: str, card: dict) -> str | None:
    body = {**card, "kind": "open", "written_at": _now()}
    name = str(card.get("branch") or card.get("agent_id") or "unknown")
    return objectstore.put(_key(OPEN_PREFIX, subsystem, name), body)


def write_hold(subsystem: str, card: dict) -> str | None:
    body = {**card, "kind": "hold", "written_at": _now()}
    name = str(card.get("agent_id") or card.get("branch") or "unknown")
    return objectstore.put(_key(HOLD_PREFIX, subsystem, name), body)


def write_incomplete(subsystem: str, card: dict) -> str | None:
    """Park a should-not-happen master failure where the operator can see it."""
    code = str(card.get("sub_reason") or "")
    headline = (card.get("headline")
                or SUB_REASONS.get(code)
                or f"INCOMPLETE — {code or 'unknown'}")
    body = {
        **card,
        "kind": "incomplete",
        "headline": headline,
        "written_at": _now(),
    }
    name = str(card.get("agent_id") or card.get("branch") or code or "unknown")
    return objectstore.put(_key(INCOMPLETE_PREFIX, subsystem, name), body)


def list_open(subsystem: str) -> list[dict]:
    return _list(OPEN_PREFIX, subsystem)


def list_hold(subsystem: str) -> list[dict]:
    return _list(HOLD_PREFIX, subsystem)


def list_incomplete(subsystem: str) -> list[dict]:
    return _list(INCOMPLETE_PREFIX, subsystem)


def _list(prefix: str, subsystem: str) -> list[dict]:
    out: list[dict] = []
    for key in objectstore.list_keys(f"{prefix}/{subsystem}/"):
        data = objectstore.get_json(key)
        if data:
            data["_key"] = key
            out.append(data)
    return sorted(out, key=lambda r: r.get("written_at") or "")
