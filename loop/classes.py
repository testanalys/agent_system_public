"""The four agent classes.

A class is defined by what the agent is asked to produce. That one property
sets its model, its turn budget, its judge and what counts as success.
Where its trigger comes from is a separate question (see wake.py).

Every class runs the same way: its own multi-turn loop in the provider's
CLI harness, on its own VM, against a fresh clone. Only the judge differs.
"""
from __future__ import annotations

from dataclasses import dataclass


@dataclass(frozen=True)
class AgentClass:
    name: str
    asked_to: str
    produces: str
    judged_by: str
    started_by: str
    failure_becomes: str


CLASSES = {
    "review": AgentClass(
        name="review",
        asked_to="look at one stage's output and say what is wrong",
        produces="DQ issues with evidence",
        judged_by="issue schema check",
        started_by="the production job that produced the output",
        failure_becomes="-",
    ),
    "repair": AgentClass(
        name="repair",
        asked_to="investigate one specific failure and make it hold",
        produces="a branch in one zone",
        judged_by="classifier + zone acceptance check -> auto-merge",
        started_by="the code that saw the failure, at the moment it saw it",
        failure_becomes="a `blocked` or `health` signal",
    ),
    "task": AgentClass(
        name="task",
        asked_to="deliver a named piece of work against acceptance criteria",
        produces="a branch, plus a link written back to the row that asked",
        judged_by="classifier + zone acceptance check -> auto-merge",
        started_by="the reconciler, from an external work row",
        failure_becomes="a `task_open` signal",
    ),
    "master": AgentClass(
        name="master",
        asked_to="find the design pain behind many signals and fix it, or hold",
        produces="branches across repos, and write-backs that close signals",
        judged_by="classifier per repo at master weights; protected surface -> operator",
        started_by="the reconciler, from a deterministic wake rule",
        failure_becomes="an operator card",
    ),
}
