"""When a master is worth starting, and what it is handed. No model involved.

    Master (DQ)      severity-weighted open issues >= DQ_PRESSURE, or a
                     critical issue open for more than CRITICAL_TICKS
    Master (Agents)  >= BLOCKED_BAR blocked branches, or any task row left
                     open, or any crashed execution

Each rule has a pre-gate: if nothing the master would read has changed
since its last run, it does not start, whatever the totals say. A master
woken twice on the same evidence produces the same answer twice, at full
price.

The docket is ranked the same way every time: severity, then how often
the problem recurred, then age. Two runs over the same signals hand the
master the same brief, which is what makes a master run replayable.
"""
from __future__ import annotations

import hashlib
import json
from dataclasses import dataclass

from .signals import AGENTS, PRODUCT, Signal, SignalLog

DQ_PRESSURE = 12
CRITICAL_TICKS = 1
BLOCKED_BAR = 3          # 10 in production; lowered so the demo wakes in a few ticks


@dataclass
class Wake:
    master: str
    start: bool
    reason: str
    docket: list[Signal]
    state: str


def _state(rows: list[Signal]) -> str:
    key = sorted((s.id, s.status, s.occurrences) for s in rows)
    return hashlib.sha256(json.dumps(key).encode()).hexdigest()[:12]


def rank(rows: list[Signal]) -> list[Signal]:
    return sorted(rows, key=lambda s: (-s.weight, -s.occurrences, s.raised_at, s.id))


def dq(log: SignalLog, last_state: str | None, tick: int) -> Wake:
    rows = log.live(PRODUCT)
    state = _state(rows)
    docket = rank(rows)
    if not rows:
        return Wake("master-dq", False, "no open issue", docket, state)
    if state == last_state:
        return Wake("master-dq", False, "nothing new since the last run", docket, state)
    load = sum(s.weight for s in rows)
    if load >= DQ_PRESSURE:
        return Wake("master-dq", True, f"pressure {load} >= {DQ_PRESSURE}", docket, state)
    stale = [s for s in rows if s.severity == "critical" and tick - s.raised_at > CRITICAL_TICKS]
    if stale:
        return Wake("master-dq", True, f"{len(stale)} critical issue(s) left open", docket, state)
    return Wake("master-dq", False, f"pressure {load} below {DQ_PRESSURE}", docket, state)


def agents(log: SignalLog, last_state: str | None) -> Wake:
    rows = log.live(AGENTS)
    state = _state(rows)
    docket = rank(rows)
    if state == last_state:
        return Wake("master-agents", False, "nothing new since the last run", docket, state)
    blocked = [s for s in rows if s.kind == "blocked"]
    if len(blocked) >= BLOCKED_BAR:
        return Wake("master-agents", True, f"{len(blocked)} blocked branches", docket, state)
    if any(s.kind == "task_open" for s in rows):
        return Wake("master-agents", True, "a task row was left open", docket, state)
    if any(s.kind == "health" for s in rows):
        return Wake("master-agents", True, "an agent execution crashed", docket, state)
    return Wake("master-agents", False, "below every wake bar", docket, state)
