"""The run archive. Nothing depends on it, which is the point.

Every agent execution ends by dumping what it saw - the prompt it sent,
the run's status, the tool calls it made, what it said, what the
classifier decided, and what happened to the branch. That archive exists
so a retro or a master can later read how an agent actually reasoned
rather than inferring it from a diff.

It is deliberately not on any decision path. A failed write is logged and
ignored; a missing archive changes nothing. The moment something reads
this to decide what to do, it stops being an archive and becomes state,
and state on the decision path is what made the previous design fragile.
"""
from __future__ import annotations

from datetime import datetime, timezone

from . import objectstore

PREFIX = "agent_layer/runs"


def _key(subsystem: str, agent_id: str) -> str:
    day = datetime.now(timezone.utc).strftime("%Y-%m-%d")
    return f"{PREFIX}/{subsystem}/{day}/{agent_id}.json"


def dump(subsystem: str, agent_id: str, payload: dict) -> str | None:
    """Archive one agent execution. Never raises."""
    try:
        body = dict(payload)
        body["archived_at"] = datetime.now(timezone.utc).isoformat(
            timespec="seconds")
        return objectstore.put(_key(subsystem, agent_id), body)
    except Exception:                                  # noqa: BLE001
        return None


def find(subsystem: str, agent_id: str) -> dict | None:
    """Read one archived run back, if the store still has it."""
    for key in objectstore.list_keys(f"{PREFIX}/{subsystem}/"):
        if key.endswith(f"/{agent_id}.json"):
            return objectstore.get_json(key)
    return None


def recent(subsystem: str, limit: int = 20) -> list[str]:
    """Addresses of the most recent archived runs, newest first."""
    keys = objectstore.list_keys(f"{PREFIX}/{subsystem}/")
    return list(reversed(keys))[:limit]
