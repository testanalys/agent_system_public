"""One row per agent turn, written where the harness decides something.

Storage is one object per row rather than an appended file, because the
backing store is GCS and GCS cannot append. Read-modify-write on a shared
log is a lost-update race between two agents finishing at once, and the
thing being raced for is a monitoring row - not worth a lock. Keys sort
lexicographically by timestamp, so listing a prefix returns the timeline
in order for free.

    dashboard/timeline/<subsystem>/<ts>-<agent>.json

Every write is best-effort. `record` swallows everything, because the
call sites are the merge path and the spawn path, and a monitoring row
that can fail a merge has inverted the priorities of the whole system.
"""
from __future__ import annotations

import re
import uuid
from dataclasses import asdict, dataclass, field
from datetime import datetime, timezone

from harness import objectstore

PREFIX = "dashboard/timeline"
_SAFE = re.compile(r"[^a-zA-Z0-9._-]+")


def _now() -> str:
    return datetime.now(timezone.utc).isoformat(timespec="seconds")


@dataclass
class Row:
    """One thing that happened, flat enough to put in a table.

    Fixed early and on purpose. `strategy`, `surface_declared` and
    `divergence` belong to the master's wager, which does not exist yet;
    they are declared here so the schema does not change under the
    dashboard later, and so `surface_actual` accumulates history that a
    declared surface can eventually be compared against.
    """
    subsystem: str
    agent: str
    tier: str
    kind: str                        # spawn | verdict | escalation | merge | hitl
    ts: str = field(default_factory=_now)

    trigger: str = ""                # what woke it, in one clause
    headline: str = ""               # the human-readable line for the table
    outcome: str = ""                # merged | escalated | held | none
    classify: str = ""               # the verdict, when there was one
    branch: str = ""
    files: int = 0
    surface_actual: tuple[str, ...] = ()

    # Populated by the master loop when it exists.
    strategy: str = ""
    surface_declared: tuple[str, ...] = ()

    ref: str = ""                    # run archive key, proposal id, PR - anything

    def key(self) -> str:
        stamp = _SAFE.sub("-", self.ts)
        agent = _SAFE.sub("-", self.agent or "unknown")
        return (f"{PREFIX}/{_SAFE.sub('-', self.subsystem or 'unknown')}"
                f"/{stamp}-{agent}-{uuid.uuid4().hex[:6]}.json")

    def as_dict(self) -> dict:
        data = asdict(self)
        data["surface_actual"] = list(self.surface_actual)
        data["surface_declared"] = list(self.surface_declared)
        return data

    @property
    def divergence(self) -> float | None:
        """Share of the touched surface the agent did not declare.

        None until a wager declares a surface. Not acted on anywhere -
        it is here to be looked at for a while before anyone decides it
        deserves to change behaviour.
        """
        if not self.surface_declared:
            return None
        declared = set(self.surface_declared)
        actual = set(self.surface_actual)
        if not actual:
            return 0.0
        return round(len(actual - declared) / len(actual), 3)


def record(row: Row) -> str | None:
    """Write one row. Never raises, never blocks a decision."""
    try:
        return objectstore.put(row.key(), row.as_dict())
    except Exception:                                  # noqa: BLE001
        return None


def read(subsystem: str | None = None, limit: int = 200) -> list[dict]:
    """The most recent rows, oldest first. Empty when there is no store."""
    prefix = f"{PREFIX}/{subsystem}/" if subsystem else f"{PREFIX}/"
    try:
        keys = objectstore.list_keys(prefix)
    except Exception:                                  # noqa: BLE001
        return []
    rows = []
    for key in keys[-limit:]:
        data = objectstore.get_json(key)
        if data:
            rows.append(data)
    return sorted(rows, key=lambda r: r.get("ts", ""))


def spawned(subsystem: str, agent: str, tier: str, trigger: dict | None,
            *, headline: str = "") -> str | None:
    """An agent was woken."""
    trigger = trigger or {}
    detail = (trigger.get("detail") or trigger.get("metric")
              or trigger.get("kind") or "")
    return record(Row(
        subsystem=subsystem, agent=agent, tier=tier, kind="spawn",
        trigger=str(detail)[:200],
        headline=headline or f"woken by {detail}" if detail else "woken",
    ))


def decided(subsystem: str, agent: str, tier: str, *, verdict: str,
            outcome: str, branch: str = "", files: int = 0,
            paths: list[str] | None = None, headline: str = "",
            ref: str = "") -> str | None:
    """A turn was judged."""
    return record(Row(
        subsystem=subsystem, agent=agent, tier=tier, kind="verdict",
        classify=verdict, outcome=outcome, branch=branch, files=files,
        surface_actual=tuple(paths or ()), headline=headline, ref=ref,
    ))


def wagered(subsystem: str, agent: str, wager, *, branch: str = "") -> str | None:
    """A master committed to a bet."""
    return record(Row(
        subsystem=subsystem, agent=agent, tier="master", kind="wager",
        strategy=getattr(wager, "mode", "") or "",
        headline=getattr(wager, "headline", "") or "",
        surface_declared=tuple(getattr(wager, "surface", ()) or ()),
        branch=branch,
        outcome="hold" if getattr(wager, "holding", False) else "committed",
    ))


def phase(subsystem: str, agent: str, frm: str, to: str, *,
          wager=None, files: int = 0) -> str | None:
    """A phase transition fired."""
    return record(Row(
        subsystem=subsystem, agent=agent, tier="master", kind="phase",
        headline=f"{frm} → {to}",
        strategy=getattr(wager, "mode", "") if wager else "",
        files=files,
        outcome=to,
    ))


def routed(subsystem: str, agent: str, wager, route, *,
           classify: str = "", branch: str = "", files: int = 0,
           paths: list[str] | None = None) -> str | None:
    """End of a master cycle: merge, operator, or hold."""
    action = getattr(route, "action", "") or ""
    return record(Row(
        subsystem=subsystem, agent=agent, tier="master", kind="route",
        strategy=getattr(wager, "mode", "") or "",
        headline=getattr(wager, "headline", "") or "",
        classify=classify,
        outcome=action,
        branch=branch,
        files=files,
        surface_declared=tuple(getattr(wager, "surface", ()) or ()),
        surface_actual=tuple(paths or ()),
        ref=getattr(route, "reason", "")[:200],
    ))


def proposal_headline(subsystem: str, agent: str, tier: str,
                      change: str, *, branch: str = "",
                      ref: str = "") -> str | None:
    """One line from a PIT/retro proof of reasoning, for the timeline."""
    headline = (change or "").strip().splitlines()[0][:120] if change else ""
    return record(Row(
        subsystem=subsystem, agent=agent, tier=tier, kind="proposal",
        headline=headline, branch=branch, ref=ref,
    ))
