"""Signals: the data points a running system produces without being asked.

Nobody schedules an agent here. A deterministic job runs on its own
schedule, and as a side effect it leaves evidence behind. That evidence is
the only thing that starts work.

    dq_issue     a review agent (or an end-user agent consuming the
                 output) met a defect in what the system produced
    blocked      the merge gate turned a branch RED without it touching a
                 protected surface: either the agent or the gate is wrong
    task_open    a task agent ran out of attempts with the row still open
    health       an agent execution crashed or never finished

Two families, two owners. `dq_issue` is about the product and goes to
Master (DQ). The other three are about the agents and the gate, and go to
Master (Agents).

Every signal carries a fingerprint. When a problem a master "fixed" comes
back, the reviewer raises it again, the fingerprint matches, and the old
row reopens with its history instead of a fresh row pretending it is new.
"""
from __future__ import annotations

import hashlib
from dataclasses import dataclass, field

PRODUCT = {"dq_issue"}
AGENTS = {"blocked", "task_open", "health"}

SEVERITY_WEIGHT = {"critical": 8, "high": 4, "medium": 2, "low": 1}


def fingerprint(kind: str, component: str, title: str) -> str:
    """Stable id for 'the same problem', independent of wording drift."""
    key = f"{kind}|{component.strip().lower()}|{' '.join(sorted(title.lower().split()))}"
    return hashlib.sha256(key.encode()).hexdigest()[:12]


@dataclass
class Signal:
    kind: str
    component: str
    title: str
    source: str
    severity: str = "medium"
    raised_at: int = 0                  # tick number, so the demo is deterministic
    occurrences: int = 1
    status: str = "open"                # open | fixed | reopened
    fix_ref: str | None = None
    history: list[str] = field(default_factory=list)

    @property
    def id(self) -> str:
        return fingerprint(self.kind, self.component, self.title)

    @property
    def weight(self) -> int:
        return SEVERITY_WEIGHT.get(self.severity, 1)

    @property
    def live(self) -> bool:
        return self.status in ("open", "reopened")


class SignalLog:
    """Append-only in spirit: rows change status, they are never deleted."""

    def __init__(self) -> None:
        self.rows: dict[str, Signal] = {}

    def raise_(self, sig: Signal) -> Signal:
        known = self.rows.get(sig.id)
        if known is None:
            self.rows[sig.id] = sig
            return sig
        known.occurrences += 1
        if known.status == "fixed":
            known.history.append(f"reopened at tick {sig.raised_at}; fix {known.fix_ref} did not hold")
            known.status = "reopened"
            known.fix_ref = None
        return known

    def close(self, ids: list[str], ref: str) -> int:
        n = 0
        for i in ids:
            row = self.rows.get(i)
            if row and row.live:
                row.status, row.fix_ref = "fixed", ref
                row.history.append(f"fixed by {ref}")
                n += 1
        return n

    def live(self, family: set[str]) -> list[Signal]:
        return [s for s in self.rows.values() if s.kind in family and s.live]
