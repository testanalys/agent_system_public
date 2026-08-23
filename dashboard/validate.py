"""Check that a timeline shows the expected agent-layer chain.

Used after a dogfood run to confirm PIT → escalation → master completed
without opening every run archive.
"""
from __future__ import annotations

MASTER_KINDS = frozenset({"wager", "phase", "route"})
PIT_ESCALATION = frozenset({"spawn", "verdict"})
RETRO_KINDS = frozenset({"spawn", "verdict"})


def flow(rows: list[dict], *, subsystem: str | None = None) -> list[str]:
    """Return human-readable gaps in the expected chain."""
    if subsystem:
        rows = [r for r in rows if r.get("subsystem") == subsystem]
    if not rows:
        return ["no timeline rows"]

    problems: list[str] = []
    by_tier = _index_by_tier(rows)

    pit = by_tier.get("pit", [])
    if pit:
        kinds = {r.get("kind") for r in pit}
        if "spawn" not in kinds:
            problems.append("pit: no spawn row")
        if "verdict" not in kinds:
            problems.append("pit: no verdict row")
        escalated = any(r.get("outcome") == "ESCALATE" for r in pit
                        if r.get("kind") == "verdict")
        if escalated and "proposal" not in kinds:
            problems.append("pit: escalated without a proposal headline")

    retro = by_tier.get("retro", [])
    if retro:
        kinds = {r.get("kind") for r in retro}
        if "spawn" not in kinds:
            problems.append("retro: no spawn row")
        if "verdict" not in kinds:
            problems.append("retro: no verdict row")

    master = by_tier.get("master", [])
    if master:
        kinds = {r.get("kind") for r in master}
        if "spawn" not in kinds:
            problems.append("master: no spawn row")
        if "wager" not in kinds:
            problems.append("master: no wager row")
        if "route" not in kinds:
            problems.append("master: no route row (cycle did not dispose)")
        elif not any(r.get("outcome") in ("merge", "operator", "none")
                     for r in master if r.get("kind") == "route"):
            problems.append("master: route row has no outcome")

    if pit and any(r.get("outcome") == "ESCALATE"
                   for r in pit if r.get("kind") == "verdict"):
        if not master:
            problems.append("pit escalated but no master rows on the timeline")

    return problems


def _index_by_tier(rows: list[dict]) -> dict[str, list[dict]]:
    out: dict[str, list[dict]] = {}
    for row in rows:
        tier = row.get("tier") or ""
        out.setdefault(tier, []).append(row)
    return out
