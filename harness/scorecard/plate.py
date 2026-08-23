"""Path × agent: protected surfaces, plates, and former-red caution.

Categorical skeleton red rules still score 100 when they apply to this
agent. PIT agents also score 100 for leaving their plate. Masters pay a
small caution when they touch a path that used to be categorical red
(`touch_cost` on the zone) instead of an automatic 100.
"""
from __future__ import annotations

from harness.diff import SessionDiff
from harness.skeleton import Skeleton, Zone

from . import weights


def _hit(sid: str, points: int, detail: str, paths=None) -> dict:
    out = {"id": sid, "points": points, "detail": detail}
    if paths:
        out["paths"] = list(paths)
    return out


def collect(diff: SessionDiff, agent_id: str, skeleton: Skeleton
            ) -> tuple[list[dict], list, Zone | None]:
    """Return (hits, verdicts, effective green zone)."""
    verdicts = [skeleton.resolve(f.path, agent_id) for f in diff.files]
    hits: list[dict] = []
    by_path = {f.path: f for f in diff.files}

    for verdict in verdicts:
        if verdict.is_red:
            hits.append(_hit(
                verdict.red_rule.id, weights.CATEGORICAL,
                f"{verdict.path} — {verdict.reason}",
                [verdict.path],
            ))

    for verdict in verdicts:
        zone = verdict.zone
        change = by_path.get(verdict.path)
        if zone and zone.append_only and change and not change.is_new:
            hits.append(_hit(
                "R9", weights.APPEND_ONLY,
                f"{verdict.path} — zone '{zone.id}' is append-only; "
                f"existing entries are never edited or deleted",
                [verdict.path],
            ))

    off_paths: list[str] = []
    off_reason = ""
    self_paths: list[str] = []
    self_reason = ""
    for verdict in verdicts:
        if verdict.is_red:
            continue
        if verdict.green_eligible:
            cost = (verdict.zone.touch_cost if verdict.zone else 0) or 0
            if cost:
                hits.append(_hit(
                    "Z-CAUTION", cost,
                    f"{verdict.path} — former protected surface; "
                    f"caution +{cost} (zone '{verdict.zone.id}')",
                    [verdict.path],
                ))
            continue
        if "itself" in (verdict.reason or ""):
            self_paths.append(verdict.path)
            self_reason = verdict.reason
        else:
            off_paths.append(verdict.path)
            off_reason = verdict.reason

    if self_paths:
        hits.append(_hit(
            "Z-SELF", weights.DENY_SELF,
            f"{self_paths[0]} — {self_reason}",
            self_paths,
        ))
    if off_paths:
        hits.append(_hit(
            "Z1", weights.off_plate_points(agent_id),
            f"{off_paths[0]} — {off_reason}"
            + (f" (+{len(off_paths) - 1} more)" if len(off_paths) > 1 else ""),
            off_paths,
        ))

    eligible = [v.zone for v in verdicts if v.zone and v.green_eligible]
    zone: Zone | None = None
    if eligible:
        zones = {z.id: z for z in eligible}
        if len(zones) == 1:
            zone = next(iter(zones.values()))
        else:
            zone = max(zones.values(),
                       key=lambda z: (z.limits.get("net_loc", 0),
                                      z.limits.get("files", 0)))
    return hits, verdicts, zone
