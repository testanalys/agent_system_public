"""Map scorecard hits to coaching prose the agent can act on.

The classifier stays silent. These paragraphs name what is on the branch
— a stubbed body, a file rewritten too often, two paths left standing —
and ask the next question. They never name a rule id, a colour, a limit,
or the fact that a judge exists.

PIT and retro sessions are blocked by score when the stack is high
enough; they do not get this text. Masters get it at the reduce turn so
a smell that would not by itself stop a merge still gets a second look.
"""
from __future__ import annotations

from collections import OrderedDict

# Habits / narration / conjunctions that mean "the design is unfinished
# or being papered over". Shape-only hits (C1–C3) are already the reduce
# prompt's file/line counts; they do not get a second paragraph.
_COACH = (
    "H-STUB", "H-FAKE-OK", "H-PASS", "H-DEAD", "H-COMMENT-OUT",
    "H-DUAL-PATH", "H-SWALLOW", "H-BARE-EXCEPT", "H-ASSERT-DROP", "H-NOQA",
    "N-DEFER", "N-TRIVIAL", "N-ONCE", "N-KEEP-BOTH", "N-DOC-LIE", "N-STRAIN",
    "N-DOC-RATIO", "N-CONTRAST",
    "J-STUB-SATD", "J-DUAL-KEY", "J-DOC-THIN", "J-ORACLE-DROP",
    "J-HISTORY-HABIT",
    "H1", "H2",
)

_FAMILY = {
    "incomplete": {
        "H-STUB", "H-FAKE-OK", "H-PASS", "H-DEAD", "H-COMMENT-OUT",
        "J-STUB-SATD",
    },
    "dual": {"H-DUAL-PATH", "N-KEEP-BOTH", "J-DUAL-KEY"},
    "swallow": {"H-SWALLOW", "H-BARE-EXCEPT"},
    "oracle": {"H-ASSERT-DROP", "J-ORACLE-DROP", "H-NOQA"},
    "postpone": {"N-DEFER", "N-TRIVIAL", "N-ONCE", "N-CONTRAST"},
    "story": {"N-DOC-LIE", "N-STRAIN", "N-DOC-RATIO", "J-DOC-THIN"},
    "churn": {"H1", "H2", "J-HISTORY-HABIT"},
}


def should_coach(hits: list[dict] | None) -> bool:
    """True when reduce should fire even if the diff is under the size bar."""
    return bool(actionable(hits))


def actionable(hits: list[dict] | None) -> list[dict]:
    out: list[dict] = []
    for hit in hits or []:
        sid = str(hit.get("id") or "")
        if sid in _COACH and int(hit.get("points") or 0) > 0:
            out.append(hit)
    return out


def families(hits: list[dict] | None) -> list[str]:
    present: set[str] = set()
    for hit in actionable(hits):
        sid = str(hit.get("id") or "")
        for name, ids in _FAMILY.items():
            if sid in ids:
                present.add(name)
    order = ("incomplete", "dual", "swallow", "oracle", "postpone",
             "story", "churn")
    return [name for name in order if name in present]


def paragraphs(hits: list[dict] | None) -> list[str]:
    """Coaching paragraphs, one per family that fired, with specifics."""
    by_family: OrderedDict[str, list[dict]] = OrderedDict()
    for name in families(hits):
        ids = _FAMILY[name]
        by_family[name] = [
            h for h in actionable(hits) if str(h.get("id") or "") in ids
        ]
    out: list[str] = []
    for name, group in by_family.items():
        fn = _RENDER.get(name)
        if fn:
            text = fn(group)
            if text:
                out.append(text)
    return out


def for_reduce(hits: list[dict] | None) -> str:
    parts = paragraphs(hits)
    if not parts:
        return ""
    return "Look at these specifically:\n\n" + "\n\n".join(parts)


def _details(group: list[dict], limit: int = 4) -> list[str]:
    seen: set[str] = set()
    lines: list[str] = []
    for hit in group:
        detail = " ".join(str(hit.get("detail") or "").split())
        if not detail or detail in seen:
            continue
        seen.add(detail)
        lines.append(f"  * {detail}")
        if len(lines) >= limit:
            break
    return lines


def _with_details(body: str, group: list[dict]) -> str:
    extra = _details(group)
    if not extra:
        return body
    return body.rstrip() + "\n\n" + "\n".join(extra)


def _incomplete(group: list[dict]) -> str:
    return _with_details(
        "Some functions on this branch now return an empty or "
        "success-shaped value, replace a working body with `pass`, or "
        "leave the old body sitting after an early return. That is a "
        "half-change. Finish the design you started — delete the leftover, "
        "make the new path the only path — or put the old behaviour back. "
        "Do not leave both. A stub that 'keeps the option open' is an "
        "unfinished design, and unfinished designs are how this codebase "
        "grows without getting simpler.",
        group,
    )


def _dual(group: list[dict]) -> str:
    return _with_details(
        "This branch kept the old behaviour beside the new one — a flag, "
        "a compat helper, a legacy path. Two ways to do the same thing "
        "means neither contract is honest. Pick one. If the old path must "
        "exist, say why in the wager; if it must not, delete it in this "
        "same change rather than promising to come back.",
        group,
    )


def _swallow(group: list[dict]) -> str:
    return _with_details(
        "Failure is being turned into silence: a broad except that "
        "returns None, logs and continues, or no longer raises. That "
        "makes this run look fine and hides the next one. Let the "
        "failure stay visible, or translate it into a named error this "
        "subsystem already uses. Do not make the symptom go away by "
        "muting it.",
        group,
    )


def _oracle(group: list[dict]) -> str:
    return _with_details(
        "The tests or diagnostics were changed in the same breath as the "
        "code they were supposed to judge — asserts removed, a test "
        "deleted, a warning silenced on a line you just wrote. If the "
        "behaviour changed, the check should still be able to fail. "
        "Restore a real expectation, or say plainly that the old check "
        "was wrong and what replaces it.",
        group,
    )


def _postpone(group: list[dict]) -> str:
    return _with_details(
        "The comments and docstrings on this change talk as if the real "
        "work is later, or as if this is a small temporary patch. If that "
        "is true, the change does not belong on this branch. If it is not "
        "true, take the hedge out of the prose and commit to the design "
        "you actually shipped.",
        group,
    )


def _story(group: list[dict]) -> str:
    return _with_details(
        "The write-up moved more than the mechanism — a docstring that "
        "now claims behaviour the body does not do, or a story that "
        "admits a conflict and then proceeds as if it were settled. "
        "Make the code and the description agree. If the honest "
        "description is 'we did not finish this', finish it or revert it.",
        group,
    )


def _churn(group: list[dict]) -> str:
    return _with_details(
        "This same surface has been rewritten recently. When a file or a "
        "region keeps moving, the usual miss is a design root that each "
        "patch cannot name. Before you add another edit, answer: is this "
        "file even the right seam? Would one change further up make these "
        "edits unnecessary? 'The same fix, done properly this time' is "
        "not a different plan — say what is structurally different, or "
        "stop and wager a different landmark.",
        group,
    )


_RENDER = {
    "incomplete": _incomplete,
    "dual": _dual,
    "swallow": _swallow,
    "oracle": _oracle,
    "postpone": _postpone,
    "story": _story,
    "churn": _churn,
}
