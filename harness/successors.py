"""Named inter-agent chains. Git is the bus; this table is the edge.

An agent definition must not decide who it wakes. The mechanical crawler
must not spawn bake. After a named role finishes, this module says
whether a successor exists and what git artefact it requires.

Today there is one chain: freestyle's interaction log on a pushed branch
is the only thing that may start bake, and it starts from the same Cloud
Run execution that owned freestyle.
"""
from __future__ import annotations

from dataclasses import dataclass

from . import handoff as handoff_mod


@dataclass(frozen=True)
class Successor:
    spawn: str
    require: str


CHAINS = {
    "discovery-freestyle-rd": Successor(
        spawn="discovery-route-bake",
        require="interaction_handoff",
    ),
}


def after(role_id: str) -> Successor | None:
    return CHAINS.get(role_id)


def satisfied(require: str, git, branch: str | None,
              session_id: str = "") -> dict:
    if require == "interaction_handoff":
        return handoff_mod.handoff_complete(git, branch, session_id)
    return {"ok": False, "reason": f"unknown successor requirement {require!r}"}


def bake_trigger(session_id: str, branch: str, *,
                 handoff: dict | None = None) -> dict:
    return handoff_mod.bake_trigger(session_id, branch, handoff=handoff)
