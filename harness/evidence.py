"""What a finished agent run lets us verify.

Replaces the old tool-side logging entirely. That design had the agent
run a `history` CLI which recorded its own invocations, because we
assumed a session was opaque from outside. The spike proved otherwise:
the run event stream carries `tool-call-started` events with
`toolCall.args.command` - the exact shell command.

So the gates read what the agent actually did, from outside, with no
cooperation required and nothing installed on its VM. An agent cannot
decline to be observed, and it cannot forge the record.
"""
from __future__ import annotations

import re
from typing import Iterable

HISTORY_COMMAND = re.compile(r"\bhistory\s+(query|diff|metric|attempts)\b")
SPAN_FLAG = re.compile(r"--span\s+(\d+)\s*([dwmy])", re.I)
PATH_FLAG = re.compile(r"--path\s+(\S+)")
SIGNATURE_FLAG = re.compile(r"--signature\s+(\S+)")
PROPOSAL_FLAG = re.compile(r"--proposal\s+(\S+)")
METRIC_FLAG = re.compile(r"--id\s+(\S+)")
SPAN_DAYS = {"d": 1, "w": 7, "m": 30, "y": 365}

# Plumbing an agent runs to orient itself. Real enough to appear in the
# stream, not evidence that it investigated anything.
ROUTINE = ("git status", "git log", "git diff", "ls", "cat", "pwd", "cd",
           "find", "head", "tail", "which", "echo")


def normalise(command: str) -> str:
    """Collapse a command to its comparable shape.

    Quoting and whitespace differ between an agent typing a command and a
    proposal quoting it, and neither difference means anything for the
    question "did you run something of your own".
    """
    return " ".join(str(command).replace('"', "").replace("'", "").split()).lower()


def is_routine(command: str) -> bool:
    return normalise(command).startswith(ROUTINE)


def coverage(evidence) -> dict:
    """What this run demonstrably looked at, parsed from its own commands."""
    commands = evidence.shell_commands()
    paths: set[str] = set()
    signatures: set[str] = set()
    proposals: set[str] = set()
    metrics: set[str] = set()
    max_span = 0
    history_calls = 0

    for command in commands:
        if not HISTORY_COMMAND.search(command):
            continue
        history_calls += 1
        paths.update(PATH_FLAG.findall(command))
        signatures.update(SIGNATURE_FLAG.findall(command))
        proposals.update(PROPOSAL_FLAG.findall(command))
        metrics.update(METRIC_FLAG.findall(command))
        for value, unit in SPAN_FLAG.findall(command):
            max_span = max(max_span, int(value) * SPAN_DAYS[unit.lower()])

    return {
        "commands": commands,
        "history_calls": history_calls,
        "paths": sorted(paths),
        "signatures": sorted(signatures),
        "proposals": sorted(proposals),
        "metrics": sorted(metrics),
        "max_span_days": max_span,
        "diffs_read": sorted(proposals),
        "tools": sorted(evidence.tools_named()),
    }


def novel_commands(evidence, cited: Iterable[str]) -> list[str]:
    """Commands the agent ran that the thing it is judging did not cite.

    Re-running someone else's evidence confirms the evidence, not the
    conclusion drawn from it. Routine orientation does not count.
    """
    already = {normalise(c) for c in cited}
    out: list[str] = []
    for command in evidence.shell_commands():
        shaped = normalise(command)
        if shaped in already or is_routine(shaped):
            continue
        if HISTORY_COMMAND.search(command):
            continue                      # hindsight, not an independent test
        out.append(command)
    return out
