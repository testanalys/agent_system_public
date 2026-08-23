"""Which metrics a role orients on — the FP014 exposure map, as mechanism.

The map itself used to live here: five dictionaries keyed by role id,
naming pipeline and discovery metrics inside the operating system. Those
are subsystem knowledge and they moved to the definitions that declare
them (`metrics:` in the frontmatter, `_orientation.md` beside it), so a
master can retune what an agent looks at without an edit to the harness.

What survives is the selection rule, which is the same for every
subsystem: the metric that fired wins as primary, the declared metrics
follow, and a PIT brief stops at two rows because a point agent given
every number reads none of them.
"""
from __future__ import annotations

from .definition import MASTER, PIT, RETRO

PIT_BRIEF_ROWS = 2

# Trigger `metric` values that name an event rather than a measurement.
# A docket is not a number and must not displace the real primary.
NOT_A_METRIC = frozenset({"run", "session", "docket", "escalations", "cycle"})


def metrics_for_role(role, trigger: dict | None = None) -> list[str]:
    """Ordered metric ids for this role's brief.

    PIT is capped at two rows; retro and master take everything their
    definition declares, because breadth is the point of those tiers.
    """
    trigger = trigger or {}
    declared = list(getattr(role, "metrics", ()) or ())

    ids: list[str] = []
    fired = trigger.get("metric")
    if fired and isinstance(fired, str) and fired not in NOT_A_METRIC:
        ids.append(fired)
    for metric_id in declared:
        if metric_id not in ids:
            ids.append(metric_id)

    tier = getattr(role, "tier", PIT)
    if tier == PIT:
        return ids[:PIT_BRIEF_ROWS]
    return ids


def capture_for_role(role, trigger: dict | None = None) -> list[str]:
    """Metrics worth a fresh collect before this role spawns.

    A subset of the brief: reading a stale number at the moment a
    condition fired is how an agent is sent to investigate something
    that already recovered.
    """
    declared = list(getattr(role, "capture", ()) or ())
    if declared:
        return declared
    return metrics_for_role(role, trigger)


def orientation_for(metric_ids: list[str],
                    orientation: dict[str, str] | None = None) -> str:
    """How to read the numbers in the brief, in the subsystem's own words.

    The text comes from the repo's `agents/_orientation.md`. A metric
    with no entry contributes nothing rather than a placeholder - an
    empty heading teaches an agent that this section is noise.
    """
    guidance = orientation or {}
    chunks = [guidance[m] for m in metric_ids if guidance.get(m)]
    if not chunks:
        return ""
    lines = ["## Reading these numbers", ""]
    for chunk in chunks:
        lines.append(f"- {chunk}")
    lines.append("")
    lines.append(
        "last is this run; last_7 is the daily mean bar chart; mean_7d / "
        "mean_28d are averages over every instance in the window. When bars "
        "and means disagree, trust the bars and open the underlying samples."
    )
    return "\n".join(lines)


def retro_metrics(registry) -> list[str]:
    """Everything the retro definitions in this repo declare, deduplicated."""
    out: list[str] = []
    for role in registry.all():
        if role.tier != RETRO:
            continue
        for metric_id in role.metrics:
            if metric_id not in out:
                out.append(metric_id)
    return out


def master_metrics(registry) -> list[str]:
    out: list[str] = []
    for role in registry.all():
        if role.tier != MASTER:
            continue
        for metric_id in role.metrics:
            if metric_id not in out:
                out.append(metric_id)
    return out
