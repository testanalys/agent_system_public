"""Whether a wager or proposal is grounded in why the product needs it.

A discrete technical defect is the how. The why is capacity, quality,
cost, or a bottleneck those numbers reveal. The harness does not teach
agents a keyword list as the rules of the game — it only checks that
the rationale actually talks about the system's purpose, so a cycle
cannot lock onto a function name and call that a strategy.
"""
from __future__ import annotations

import re

# Product-purpose stems. Kept generic on purpose: discovery, pipeline and
# retrieval all share volume / quality / cost / capacity language.
_STEMS = (
    "yield", "coverage", "campaign", "harvest", "quality", "cost",
    "scale", "audio", "funnel", "backlog", "freshness", "attribution",
    "volume", "capacity", "drought", "output", "index", "recall",
    "precision", "throughput", "simplicity", "growth", "product",
    "verified", "findable", "token", "conversion", "leak", "stall",
    "observability",
)

_MIN_CHARS = 40


def purpose_grounded(text: str, metric: str = "") -> bool:
    """True when `text` names a product reason, not only a code seam."""
    blob = " ".join((text or "").lower().split())
    if len(blob) < _MIN_CHARS:
        return False
    metric_id = (metric or "").strip().lower()
    if metric_id and metric_id in blob:
        return True
    return any(re.search(r"\b" + re.escape(stem) + r"\b", blob) for stem in _STEMS)


def rationale_problem(rationale: str, metric: str = "") -> str | None:
    """A problem string for wager/proposal validation, or None if fine."""
    text = (rationale or "").strip()
    if not text:
        return (
            "expected_effect.rationale is empty — say why the product "
            "needs this now, not only which function is wrong"
        )
    if not purpose_grounded(text, metric):
        return (
            "expected_effect.rationale does not say why the product needs "
            "this now. Name the capacity, quality, cost, or bottleneck "
            "this move is for. A function being wrong is the how."
        )
    return None
