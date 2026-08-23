"""Vocabulary an agent must never be handed.

Agents are told a goal and given data. They are not told that zones,
tiers, limits or a classifier exist, because an agent told what it is
permitted to change optimises for what is permitted rather than for what
is correct - and a blocked change that was the *right* change is the most
valuable artifact this system produces. One helpful sentence in a prompt
destroys that permanently and silently.

This list is the single source of truth: `agent_defs` gates definition
changes in production against it, and the role tests assert every
composed prompt against the same list, so the two cannot drift.

The vocabulary is **tier-aware**. A master operates the system's own
process: it disposes of a docket and names a wager, so that vocabulary
is its subject matter. A PIT or retro agent should not know the proposal
schema exists until a gate hands it one.
"""
from __future__ import annotations

# Never acceptable, for any agent.
LEAK_PATTERNS: list[tuple[str, str]] = [
    (r"\bGREEN\b|\bAMBER\b|\bRED\b(?!\s*HAT)", "classifier tier names"),
    (r"\bgrowth[- ]plates?\b|\bskeleton\.json\b|\bgreen[- ]lane\b",
     "zone vocabulary"),
    (r"\bclassif\w*", "the classifier itself"),
    (r"\byou may (only )?edit\b|\ballowed paths?\b|\bpermitted (to edit|paths)\b",
     "an edit allowlist"),
    (r"\bnet[- ]loc\b|\bchurn limit\b|\bfiles? (limit|budget)\b"
     r"|\bmax(imum)? files\b", "shape limits"),
    (r"\bdo not (edit|touch|modify) (the )?(control plane|reconciler|infra)\b",
     "a protected-path list"),
    (r"\b(will be|would be|gets?|is|are) blocked\b|\bwon'?t merge\b"
     r"|\bcannot be merged\b", "the block mechanic"),
    (r"\bzones?\b(?!\s+of)", "zones"),
    (r"\bre-?prompts?\b", "the re-prompt mechanic"),
]

# Process vocabulary. Correct for a master; a leak for everyone else.
INVESTIGATOR_LEAKS: list[tuple[str, str]] = [
    (r"\bproof of reasoning\b|\bPoR\b", "the proposal schema by name"),
    (r"\bdisposition\b|\bdocket\b", "the master's adjudication vocabulary"),
    (r"\bengineering plan\b|\bHITL\b", "the approval lane"),
]

PROCESS_TIERS = frozenset({"master"})
MASTER_TIERS = PROCESS_TIERS


def patterns_for(tier: str | None) -> list[tuple[str, str]]:
    """Everything a prompt for this tier must not contain."""
    if tier in PROCESS_TIERS:
        return list(LEAK_PATTERNS)
    return LEAK_PATTERNS + INVESTIGATOR_LEAKS
