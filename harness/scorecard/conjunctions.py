"""Cross-hit conjunctions — the epistemic signature is in the stack.

Independents already summed; these add bonuses (or scale a habit) when
co-occurrence raises likelihood of patch-over-design.
"""
from __future__ import annotations

from . import weights

_STUB_FAMILY = {"H-STUB", "H-FAKE-OK"}
_DEFER_FAMILY = {"N-DEFER", "N-TRIVIAL", "N-ONCE"}
_PRODUCTION_HABITS = {
    "H-STUB", "H-FAKE-OK", "H-DUAL-PATH", "H-SWALLOW", "H-PASS", "H-DEAD",
    "H-COMMENT-OUT", "H-NOQA", "H-BARE-EXCEPT",
}
_HISTORY = {"H1", "H2", "H3"}


def _hit(sid: str, points: int, detail: str, paths=None) -> dict:
    out = {"id": sid, "points": points, "detail": detail}
    if paths:
        out["paths"] = list(paths)
    return out


def collect(hits: list[dict]) -> list[dict]:
    """Return conjunction hits (and optional in-place habit mass bumps).

    Mutates habit hit points when J-HISTORY-HABIT applies, and returns
    additional J-* bonus hits.
    """
    by_id = {h.get("id") for h in hits}
    out: list[dict] = []

    if by_id & _STUB_FAMILY and by_id & _DEFER_FAMILY:
        paths = _union_paths(hits, _STUB_FAMILY | _DEFER_FAMILY)
        out.append(_hit(
            "J-STUB-SATD", weights.J_STUB_SATD,
            "stub/fake-ok stacked with defer/trivial/once narration "
            "(admitted incomplete design)",
            paths,
        ))

    if "H-DUAL-PATH" in by_id and "C7" in by_id:
        paths = _union_paths(hits, {"H-DUAL-PATH", "C7"})
        out.append(_hit(
            "J-DUAL-KEY", weights.J_DUAL_KEY,
            "dual-path habit stacked with a new config/env key (bypass)",
            paths,
        ))

    if "N-DOC-RATIO" in by_id:
        bonus = (weights.J_DOC_THIN_WITH_H2 if "H2" in by_id
                 else weights.J_DOC_THIN)
        detail = ("docstring rewrite with thin body change"
                  + (" on a recently churned region" if "H2" in by_id else ""))
        out.append(_hit(
            "J-DOC-THIN", bonus, detail,
            _union_paths(hits, {"N-DOC-RATIO", "H2"}),
        ))

    if "H-ASSERT-DROP" in by_id and by_id & _PRODUCTION_HABITS:
        out.append(_hit(
            "J-ORACLE-DROP", weights.J_ORACLE_DROP,
            "assert/expectation drop stacked with a production habit "
            "(suite forced to agree)",
            _union_paths(hits, {"H-ASSERT-DROP"} | _PRODUCTION_HABITS),
        ))

    if by_id & _HISTORY:
        # Multiply matching production-habit masses in place (once).
        for h in hits:
            sid = h.get("id")
            if sid not in _PRODUCTION_HABITS:
                continue
            if h.get("_history_scaled"):
                continue
            raw = int(h.get("points") or 0)
            h["points"] = int(round(raw * weights.J_HISTORY_HABIT_FACTOR))
            h["_history_scaled"] = True
            h["detail"] = (
                f"{h.get('detail', sid)} [×{weights.J_HISTORY_HABIT_FACTOR} "
                f"region/file history]"
            )
        # Record a zero-or-marker hit so the signal is visible; mass already
        # folded into the habit. Use a small explicit bonus of 0 points and
        # rely on the scaled habit — but operators need to see the id.
        out.append(_hit(
            "J-HISTORY-HABIT", 0,
            f"production habit(s) scaled ×{weights.J_HISTORY_HABIT_FACTOR} "
            f"due to H1/H2/H3 history on this change",
            _union_paths(hits, _HISTORY | _PRODUCTION_HABITS),
        ))

    return out


def _union_paths(hits: list[dict], ids: set[str]) -> list[str]:
    paths: list[str] = []
    seen: set[str] = set()
    for h in hits:
        if h.get("id") not in ids:
            continue
        for p in h.get("paths") or []:
            if p not in seen:
                seen.add(p)
                paths.append(p)
    return paths
