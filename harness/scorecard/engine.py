"""Compose plate + history + shape + habits + narration + conjunctions."""
from __future__ import annotations

from dataclasses import dataclass, field

from harness import history_signals
from harness.diff import SessionDiff
from harness.skeleton import Skeleton, Zone

from . import conjunctions, habits, narration, plate, shape, weights
from .weights import AMBER_FLOOR, THRESHOLD, apply_role_scale

GREEN, AMBER, RED = "GREEN", "AMBER", "RED"


@dataclass
class Scorecard:
    score: int
    threshold: int = THRESHOLD
    hits: list[dict] = field(default_factory=list)
    zones: list[str] = field(default_factory=list)
    oracle: str | None = None
    oracle_timeout_s: int = 600
    requires_tests: bool = False
    summary: dict = field(default_factory=dict)
    zone: Zone | None = None

    @property
    def routing_tier(self) -> str:
        return RED if self.score >= self.threshold else GREEN

    @property
    def band(self) -> str:
        if self.score >= self.threshold:
            return RED
        if self.score >= AMBER_FLOOR:
            return AMBER
        return GREEN


def evaluate(
    diff: SessionDiff,
    agent_id: str,
    skeleton: Skeleton,
    signature: str | None = None,
    closures: list[dict] | None = None,
) -> Scorecard:
    plate_hits, verdicts, zone = plate.collect(diff, agent_id, skeleton)
    history_hits = _history_hits(diff, signature, closures)
    limits = skeleton.limits_for(zone)
    shape_hits = shape.collect(diff, limits, zone)
    habit_hits = habits.collect(diff)
    narration_hits = narration.collect(diff, habit_hits)

    # Conjunctions see unscaled habit/narration/shape/history ids.
    preliminary = plate_hits + history_hits + shape_hits + habit_hits + narration_hits
    conj_hits = conjunctions.collect(preliminary)

    requires_tests = _requires_tests(verdicts, diff)
    test_hits: list[dict] = []
    if requires_tests and not diff.touches_tests:
        untested = [v.path for v in verdicts
                    if v.path in {f.path for f in diff.files}
                    and any(f.path == v.path and f.is_code and not f.is_test
                            for f in diff.files)]
        test_hits.append({
            "id": "C13",
            "points": weights.C13_MISSING_TESTS,
            "detail": "code changed outside a designated zone with no test change",
            "paths": untested,
        })

    # Re-collect preliminary after conjunctions may have scaled habit points
    # in place (J-HISTORY-HABIT).
    hits = (plate_hits + history_hits + shape_hits + habit_hits
            + narration_hits + conj_hits + test_hits)
    hits = apply_role_scale(hits, agent_id)
    # Drop zero-point marker hits from the sum but keep them visible.
    score = sum(int(h.get("points") or 0) for h in hits)
    zone_ids = sorted({v.zone.id for v in verdicts if v.zone})
    return Scorecard(
        score=score,
        threshold=THRESHOLD,
        hits=hits,
        zones=zone_ids,
        oracle=zone.oracle if zone else None,
        oracle_timeout_s=zone.oracle_timeout_s if zone else 600,
        requires_tests=requires_tests,
        summary={
            "files": diff.files_touched,
            "added": diff.added_lines,
            "deleted": diff.deleted_lines,
            "net_loc": diff.net_loc,
            "gross_churn": diff.gross_churn,
            "base": diff.base,
            "score": score,
            "threshold": THRESHOLD,
            "band": RED if score >= THRESHOLD else (
                AMBER if score >= AMBER_FLOOR else GREEN),
            "role_scale": weights.role_scale(agent_id),
            "agent_kind": weights.agent_kind(agent_id),
        },
        zone=zone,
    )


def _history_hits(diff, signature, closures) -> list[dict]:
    out: list[dict] = []
    for hit in history_signals.collect_all(diff, signature, closures):
        points = _history_points(hit)
        out.append({
            "id": hit.id,
            "points": points,
            "detail": hit.detail,
            "evidence": hit.evidence,
        })
    return out


def _history_points(hit) -> int:
    if hit.id == "H1":
        return weights.H1_FILE_CHURN
    if hit.id == "H2":
        return weights.H2_REGION_CHURN
    if hit.id == "H3":
        return weights.H3_SIGNATURE
    if hit.id == "H4":
        if hit.evidence.get("within_protection", False):
            return weights.H4_RESTORE_PROTECTED
        return weights.H4_RESTORE_STALE
    return 10


def _requires_tests(verdicts, diff: SessionDiff) -> bool:
    if not diff.touches_non_test_code:
        return False
    return any(v.zone is None or not v.zone.oracle for v in verdicts)
