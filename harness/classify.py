"""The classifier — the only thing in this system that assigns a status.

Agents never label their own work. They write code and push a branch;
the harness scores that branch and decides what happens next.

    GREEN   score < 100: the change merges, subject to the zone's
            acceptance check
    RED     score >= 100: protected surface, or enough stacked smells
            that this agent does not land it

AMBER is a dashboard band (40–99), not a routing colour. File-count and
churn live in that band on purpose so a slightly large clean change still
merges.

The rules live in `harness.scorecard`. This module is the public face:
the Classification dataclass, GREEN/AMBER/RED names, and `classify()`.
"""
from __future__ import annotations

from dataclasses import dataclass, field
from pathlib import Path

from . import reprompts
from .diff import SessionDiff
from .scorecard import evaluate as score_diff
from .scorecard.weights import THRESHOLD
from .skeleton import Skeleton

GREEN, AMBER, RED = "GREEN", "AMBER", "RED"
DEFAULT_ATTEMPTS_ALLOWED = 2      # first submit + one re-submit


@dataclass
class Classification:
    tier: str
    signals: list[dict] = field(default_factory=list)
    zones: list[str] = field(default_factory=list)
    oracle: str | None = None
    oracle_timeout_s: int = 600
    requires_tests: bool = False
    summary: dict = field(default_factory=dict)
    reprompt: str = ""
    score: int = 0
    threshold: int = THRESHOLD
    band: str = GREEN

    @property
    def blocked(self) -> bool:
        return self.tier == RED

    def to_dict(self) -> dict:
        return {
            "tier": self.tier,
            "band": self.band,
            "score": self.score,
            "threshold": self.threshold,
            "signals": self.signals,
            "zones": self.zones,
            "oracle": self.oracle,
            "requires_tests": self.requires_tests,
            "summary": self.summary,
        }


def classify(
    diff: SessionDiff,
    agent_id: str,
    skeleton: Skeleton,
    signature: str | None = None,
    closures: list[dict] | None = None,
    attempt: int = 1,
    attempts_allowed: int = DEFAULT_ATTEMPTS_ALLOWED,
    *,
    shape_blind: bool = False,
) -> Classification:
    summary = {
        "files": diff.files_touched,
        "added": diff.added_lines,
        "deleted": diff.deleted_lines,
        "net_loc": diff.net_loc,
        "gross_churn": diff.gross_churn,
        "base": diff.base,
        "score": 0,
        "threshold": THRESHOLD,
        "band": GREEN,
    }

    if diff.is_empty:
        return Classification(tier=GREEN, band=GREEN, summary=summary,
                              reprompt="No changes to submit.")

    card = score_diff(diff, agent_id, skeleton, signature, closures)
    # shape_blind is a no-op: masters are scored with cheaper off-plate
    # and shape weights rather than ignoring size. Kept so old callers
    # do not break.
    _ = shape_blind

    if card.routing_tier == RED:
        return Classification(
            tier=RED,
            band=RED,
            score=card.score,
            threshold=card.threshold,
            signals=card.hits,
            zones=card.zones,
            summary=card.summary,
            reprompt=reprompts.red(card.hits, attempt),
        )

    if card.band == AMBER:
        hint = ", ".join(card.zones) if card.zones else None
        return Classification(
            tier=GREEN,
            band=AMBER,
            score=card.score,
            threshold=card.threshold,
            signals=card.hits,
            zones=card.zones,
            oracle=card.oracle,
            oracle_timeout_s=card.oracle_timeout_s,
            requires_tests=card.requires_tests,
            summary=card.summary,
            reprompt=reprompts.amber(
                card.hits, attempt, attempts_allowed, hint),
        )

    return Classification(
        tier=GREEN,
        band=GREEN,
        score=card.score,
        threshold=card.threshold,
        signals=card.hits,
        zones=card.zones,
        oracle=card.oracle,
        oracle_timeout_s=card.oracle_timeout_s,
        requires_tests=card.requires_tests,
        summary=card.summary,
    )


def classify_repo(
    repo: Path,
    agent_id: str,
    base: str,
    head: str = "HEAD",
    signature: str | None = None,
    closures: list[dict] | None = None,
    attempt: int = 1,
) -> tuple[Classification, SessionDiff, Skeleton]:
    """Convenience entry point used by the CLI and CI."""
    skeleton = Skeleton.load(repo)
    diff = SessionDiff.collect(repo, base, head)
    result = classify(diff, agent_id, skeleton, signature, closures, attempt)
    return result, diff, skeleton
