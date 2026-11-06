"""The merge gate: the one place an agent's work can reach production.

An agent session ends with a branch. Everything after that is code:

    classify    score the net diff against the zone map, scaled by role
    check       run the zone's acceptance command against the branch
    merge       fast-forward the deploy branch
    rebuild     the deploy branch is what production builds from, so a
                merge *is* a deploy

A branch that does not merge is never just dropped. It becomes a signal:

    categorical RED   it touched a protected surface -> operator card.
                      No agent may move the fence it is judged by.
    scored RED        stacked smells -> `blocked` signal. Either the agent
                      did something corrosive, or the gate is miscalibrated.
                      Master (Agents) reads these to find out which.
    check failed      the change did not work -> re-prompt in the same session

That last split is the point. The gate's own mistakes arrive as data, so
the gate can be improved by the same loop it polices.
"""
from __future__ import annotations

from dataclasses import dataclass, field

from harness.classify import RED, Classification
from harness.skeleton import Skeleton

from .signals import Signal


@dataclass
class Outcome:
    agent: str
    branch: str
    verdict: str                  # MERGED | REPROMPT | OPERATOR | BLOCKED
    score: int
    signals: list[str] = field(default_factory=list)
    ref: str | None = None
    raised: Signal | None = None


def categorical(result: Classification, skeleton: Skeleton) -> bool:
    """A protected surface, or an agent editing its own definition.

    Leaving a zone (Z1) also scores a full threshold, but it is not
    categorical: it is often the zone map that is too narrow, and that is
    exactly what Master (Agents) is there to find out.
    """
    fence = {r.id for r in skeleton.red_rules} | {"Z-SELF"}
    return result.tier == RED and any(s.get("id") in fence for s in result.signals)


def decide(agent: str, branch: str, result: Classification, skeleton: Skeleton, *,
           check_passed: bool, tick: int, merge, rebuild) -> Outcome:
    ids = [s["id"] for s in result.signals]
    if result.tier == RED:
        if categorical(result, skeleton):
            return Outcome(agent, branch, "OPERATOR", result.score, ids)
        sig = Signal(kind="blocked", component=agent.rsplit("-", 1)[0],
                     title=f"{branch}: {' '.join(sorted(ids))}",
                     source=agent, severity="medium", raised_at=tick)
        return Outcome(agent, branch, "BLOCKED", result.score, ids, raised=sig)
    if not check_passed:
        return Outcome(agent, branch, "REPROMPT", result.score, ids)
    ref = merge(branch)
    rebuild(ref)
    return Outcome(agent, branch, "MERGED", result.score, ids, ref=ref)
