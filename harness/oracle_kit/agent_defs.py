"""Oracle: a changed agent definition is safe to spawn from.

This guards the sharpest surface in the system. A master agent may write
the instructions of the agents that investigate its subsystem, which is
genuine self-improvement and also the fastest way to degrade a fleet if
it goes wrong once. So the floor is high and every check is mechanical.

    python -m harness.oracle_kit.agent_defs --changed <path> [...] \
        [--repo <dir>] [--agent <id>]

Four checks, in the order a failure is cheapest to explain:

**It parses.** Frontmatter is well-formed, the required fields are there,
the tier is one the harness knows, the filename matches the declared
name. A definition that does not parse takes a subsystem's whole agent
set down at spawn time, because the loader is deliberately strict.

**It does not leak the rules.** Agents are not told that tiers, limits or
a grading mechanism exist, because an agent told what it is permitted to
change optimises for what is permitted rather than for what is correct -
and the held-back change that was the *right* change is the most valuable
artifact this system produces. This check mattered less when prompts were
hand-edited in the harness by a person who knew the invariant. It is
load-bearing now that agents author them.

**Nothing it names is a ghost.** An agent no condition can trigger is
dead weight, and a metric outside the catalogue gives it an empty brief
forever. Both fail silently and permanently, which is exactly the class
of defect this system exists to stop accumulating.

**It is not the author's own definition.** Enforced in the zone map too,
via ``deny_self``; repeated here because an oracle that only works when
the zone is configured correctly is not much of an oracle.

There is no check that the prompt is *good*. Shape is all a static check
can see, so treat every wording change as unverified behaviour change
until the timeline shows what it did.
"""
from __future__ import annotations

import argparse
import json
import re
import sys
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parents[2]))

from roles import definition as defn                            # noqa: E402

from .leak_vocabulary import patterns_for                       # noqa: E402

MAX_WORDS = 1200
GOAL_HINTS = ("## your goal", "## goal")
BREACH_FILES = ("agents/agent_utils/breaches.py", "scripts/breaches.py")
# Tiers whose spawn is not condition-driven, so a missing trigger is not
# a ghost: the reconciler raises masters; the mechanical run raises retro.
NO_TRIGGER_NEEDED = (defn.MASTER, defn.RETRO)


def check_parses(path: Path) -> tuple[defn.AgentDefinition | None, list[str]]:
    try:
        return defn.load_file(path), []
    except defn.DefinitionError as exc:
        return None, [str(exc)]


def check_leakage(item: defn.AgentDefinition) -> list[str]:
    problems: list[str] = []
    for pattern, label in patterns_for(item.tier):
        match = re.search(pattern, item.body, re.I)
        if not match:
            continue
        line = item.body[:match.start()].count("\n") + 1
        excerpt = item.body.splitlines()[line - 1].strip()[:90]
        problems.append(
            f"line {line} of the body leaks {label} - \"{excerpt}\". "
            f"Describe the goal and the data; the harness owns the boundary.")
    return problems


def check_shape(item: defn.AgentDefinition) -> list[str]:
    """Enough structure to produce a usable agent, and no more.

    The old version of this check also demanded that every prompt tell
    the agent how to finish. It does not need to any more: the closing
    is composed by the harness for every tier, so a definition that
    tried to state its own exit would be duplicating - and eventually
    contradicting - the one the agent actually receives.
    """
    problems: list[str] = []
    lowered = item.body.lower()
    if not any(hint in lowered for hint in GOAL_HINTS):
        problems.append(
            "the body never states the agent's goal. Give it a '## Your goal' "
            "section - a prompt that does not say what the agent is for "
            "produces an agent that guesses.")
    words = len(item.body.split())
    if words > MAX_WORDS:
        problems.append(
            f"{words} words (cap {MAX_WORDS}). A prompt is re-read on every "
            f"spawn and every token is paid for forever; cut restatement first.")
    if len(item.description.split()) < 3:
        problems.append(
            "description is too thin to identify this agent in a docket or a "
            "timeline row. One clause saying what it investigates.")
    return problems


def check_self_reference(item: defn.AgentDefinition,
                         changed_by: str | None) -> list[str]:
    if changed_by and item.name.lower() == changed_by.lower():
        return [f"{item.name} is the agent making this change. An agent does "
                f"not rewrite its own instructions."]
    return []


def _triggers_in(repo: Path) -> str:
    text = []
    for name in BREACH_FILES:
        path = repo / name
        if path.exists():
            try:
                text.append(path.read_text(encoding="utf-8"))
            except OSError:
                continue
    return "\n".join(text)


def check_no_ghost(item: defn.AgentDefinition, repo: Path | None) -> list[str]:
    """An agent nothing can wake, or a metric nothing collects."""
    problems: list[str] = []
    if repo is None:
        return problems

    if item.tier not in NO_TRIGGER_NEEDED:
        conditions = _triggers_in(repo)
        if not conditions:
            problems.append(
                "no spawn conditions file was found in this repo, so nothing "
                "can wake this agent. Expected one of: "
                + ", ".join(BREACH_FILES))
        elif item.name not in conditions:
            problems.append(
                f"nothing wakes {item.name}. Add the condition that should "
                f"trigger it to {BREACH_FILES[0]} or {BREACH_FILES[1]} in this "
                f"same change - an agent no condition names never runs, and "
                f"never runs is indistinguishable from never needed.")

    known = _catalogue_ids(repo)
    if known:
        unknown = [m for m in item.metrics if m not in known]
        if unknown:
            problems.append(
                "these metrics are not in lifeblood/catalogue.json: "
                + ", ".join(unknown)
                + ". An agent oriented on a metric nobody collects gets an "
                  "empty brief on every spawn, forever, and nothing reports it.")
    return problems


def _catalogue_ids(repo: Path) -> set[str]:
    path = repo / "lifeblood" / "catalogue.json"
    if not path.exists():
        return set()
    try:
        data = json.loads(path.read_text(encoding="utf-8"))
    except (OSError, json.JSONDecodeError):
        return set()
    return {str(entry.get("id")) for entry in data.get("metrics", [])
            if entry.get("id")}


def check(path: Path, repo: Path | None = None,
          changed_by: str | None = None) -> list[str]:
    item, problems = check_parses(path)
    if item is None:
        return problems
    return (check_leakage(item)
            + check_shape(item)
            + check_self_reference(item, changed_by)
            + check_no_ghost(item, repo))


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(prog="agent_defs")
    parser.add_argument("--changed", nargs="*", default=[])
    parser.add_argument("--repo", default=None,
                        help="repo root, for the ghost checks")
    parser.add_argument("--agent", default=None,
                        help="the agent making this change")
    args = parser.parse_args(argv)

    repo = Path(args.repo).resolve() if args.repo else None
    targets = [Path(p) for p in args.changed
               if Path(p).suffix == ".md" and not Path(p).name.startswith("_")]
    if not targets:
        print("No agent definitions in the change; nothing to check.")
        return 0

    failed = False
    for path in targets:
        if not path.exists():
            print(f"{path}: missing")
            failed = True
            continue
        problems = check(path, repo, args.agent)
        if problems:
            failed = True
            print(f"\n{path}:")
            for problem in problems:
                print(f"  * {problem}")

    if failed:
        print("\nAgent definition check FAILED.")
        return 1
    print(f"Agent definitions passed for {len(targets)} file(s).")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
