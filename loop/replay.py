"""The replay oracle: how a change to the gate itself is judged.

Master (Agents) may change the zone maps and the scorecard weights. That
is the gate improving itself, and it needs a judge it cannot loosen.

Every classification writes a corpus record: the stored diff, the agent,
the verdict, whether it merged. A proposed gate is re-run over the whole
corpus. Nothing is re-merged and nothing is rebased; the only question is
which verdicts would change.

The proposal passes only if:

    every categorical RED stays RED       the fence cannot be moved
    every merged GREEN stays below RED    work we accepted stays acceptable
    every synthetic bad change stays RED  tests/test_design_subversion.py

What it reports as the benefit: scored REDs that flip to GREEN. Those are
the gate's false positives, found by the loop rather than by a person.

The oracle itself, the classifier engine, and Master (Agents)' own
definition are not in any zone it may write. It can retune the gate. It
cannot retune the thing that judges the retuning.
"""
from __future__ import annotations

from dataclasses import dataclass, field
from typing import Callable

from harness.classify import RED


@dataclass
class Record:
    branch: str
    agent: str
    verdict: str              # GREEN | RED
    categorical: bool
    merged: bool
    rescore: Callable[[object], str]   # proposed skeleton -> new verdict


@dataclass
class Report:
    passed: bool
    broken: list[str] = field(default_factory=list)
    benefit: list[str] = field(default_factory=list)


def run(corpus: list[Record], proposed) -> Report:
    report = Report(passed=True)
    for rec in corpus:
        after = rec.rescore(proposed)
        if rec.categorical and after != RED:
            report.broken.append(f"{rec.branch}: categorical RED would merge")
        elif rec.merged and after == RED:
            report.broken.append(f"{rec.branch}: accepted work would now be blocked")
        elif rec.verdict == RED and not rec.categorical and after != RED:
            report.benefit.append(rec.branch)
    report.passed = not report.broken
    return report
