"""Trade-off keyword groups.

The trade-off statement is capped at 30 words and must answer three
questions: what gets worse, what gets better, and which objective moves.
That is only possible if the statement engages more than one axis - so
the soft check is that words from at least two groups appear.

When a group is missing, its snippet is returned to the agent. The point
is not to make the sentence longer. It is to make the agent ask whether
it has actually considered that axis, and to re-examine the change if it
has not.
"""
from __future__ import annotations

import re
from dataclasses import dataclass

MIN_GROUPS = 2
MAX_WORDS = 30


@dataclass(frozen=True)
class Group:
    id: str
    label: str
    words: tuple[str, ...]
    snippet: str


GROUPS: tuple[Group, ...] = (
    Group(
        id="engineering",
        label="Engineering shape (priorities 1–2: no tech debt, simplicity)",
        words=(
            "complexity", "complex", "simple", "simpler", "simplicity", "simplify",
            "code", "codebase", "component", "components", "module", "modules",
            "file", "files", "function", "functions", "abstraction", "interface",
            "coupling", "decoupl", "dependency", "dependencies", "debt",
            "refactor", "delete", "deletes", "removes", "removal", "handoff",
            "handoffs", "surface", "engineering", "architecture", "structural",
            "duplication", "indirection", "branching", "state", "moving",
        ),
        snippet=(
            "ENGINEERING SHAPE is not mentioned. Simplicity and tech debt are the "
            "top two priorities in this system, so a change that ignores them is "
            "usually a change that has not been costed.\n"
            "  Does this add components, files, branches, config, or a new "
            "dependency between parts? Name what it adds.\n"
            "  If it removes complexity instead, say that - it is the strongest "
            "justification available to you and it is being left on the table."
        ),
    ),
    Group(
        id="economics",
        label="Economics (priority 3: cost, and the speed that drives it)",
        words=(
            "cost", "costs", "cheaper", "expensive", "spend", "budget", "egress",
            "token", "tokens", "compute", "vcpu", "storage", "bytes", "transfer",
            "speed", "faster", "slower", "latency", "throughput", "runtime",
            "duration", "minutes", "seconds", "hours", "per-run", "economics",
            "efficiency", "efficient", "overhead",
        ),
        snippet=(
            "ECONOMICS is not mentioned. Every change makes the system pay "
            "something forever.\n"
            "  What does this cost per run - in compute, storage, egress, tokens, "
            "or wall-clock time? If it saves, quantify the saving.\n"
            "  If your honest answer is 'nothing measurable', say so explicitly. "
            "That is a valid and useful claim; silence is not."
        ),
    ),
    Group(
        id="data_quality",
        label="Data quality (priority 5, and the trust half of the north star)",
        words=(
            "quality", "accuracy", "accurate", "precision", "recall", "correct",
            "correctness", "fidelity", "attribution", "attributed", "speaker",
            "diarization", "diarisation", "transcript", "transcription",
            "mislabel", "unattributed", "error", "errors", "trust", "verified",
            "verification", "provenance", "reliability", "reliable",
        ),
        snippet=(
            "DATA QUALITY is not mentioned. Wrong speaker attribution is worse "
            "than missing content, so quality is rarely neutral in this system.\n"
            "  Does this change what the corpus ends up containing, or how "
            "confidently a statement can be attributed?\n"
            "  If quality is genuinely untouched, say 'no quality effect' - a "
            "reader needs to know you checked."
        ),
    ),
    Group(
        id="reach",
        label="Reach (priority 4 volume, and the freshness/breadth north star)",
        words=(
            "volume", "throughput", "breadth", "coverage", "catalogue", "catalog",
            "corpus", "sources", "source", "discovery", "harvest", "ingest",
            "ingestion", "index", "indexed", "indexing", "retrieval", "search",
            "findable", "findability", "freshness", "fresh", "lag", "recall@k",
            "users", "reach", "scale", "scales", "scaling", "items", "hours",
        ),
        snippet=(
            "REACH is not mentioned. The product's value is how much of the "
            "spoken record is captured and how quickly it becomes findable.\n"
            "  Does this change how much audio the system processes, how many "
            "sources it can reach, or how fast content becomes searchable?\n"
            "  If it is purely internal with no reach effect, say so."
        ),
    ),
)

_WORD_RE = re.compile(r"[A-Za-z][A-Za-z0-9'-]*")


def word_count(statement: str) -> int:
    return len(_WORD_RE.findall(statement or ""))


def groups_present(statement: str) -> list[Group]:
    """Groups with at least one stem match. Prefix matching is deliberate.

    'simplifies' should satisfy 'simplify' without the table carrying every
    inflection, so a group hits when any of its stems is a prefix of a word
    in the statement (or vice versa for short stems).
    """
    tokens = [t.lower() for t in _WORD_RE.findall(statement or "")]
    hits: list[Group] = []
    for group in GROUPS:
        for token in tokens:
            if any(token.startswith(stem) or stem.startswith(token) and len(token) >= 4
                   for stem in group.words):
                hits.append(group)
                break
    return hits


def missing_groups(statement: str) -> list[Group]:
    present = {g.id for g in groups_present(statement)}
    return [g for g in GROUPS if g.id not in present]


def check(statement: str) -> dict:
    """Full trade-off lint result. Word cap is hard; coverage is soft."""
    words = word_count(statement)
    present = groups_present(statement)
    return {
        "words": words,
        "over_word_cap": words > MAX_WORDS,
        "empty": words == 0,
        "groups_present": [g.id for g in present],
        "groups_missing": [g.id for g in missing_groups(statement)],
        "enough_coverage": len(present) >= MIN_GROUPS,
    }
