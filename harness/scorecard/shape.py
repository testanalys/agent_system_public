"""Size and structure of the net diff.

File count and churn add points but are capped cheaply: a slightly large
clean change stays GREEN. Dirty-code habits in `habits.py` are what
accumulate with these toward a breach.
"""
from __future__ import annotations

from harness.diff import SessionDiff
from harness.skeleton import Zone

from . import weights


def _hit(sid: str, points: int, detail: str, value=None, limit=None,
         paths=None) -> dict:
    out = {"id": sid, "points": points, "detail": detail}
    if value is not None:
        out["value"] = value
    if limit is not None:
        out["limit"] = limit
    if paths:
        out["paths"] = list(paths)
    return out


def collect(diff: SessionDiff, limits: dict, zone: Zone | None) -> list[dict]:
    out: list[dict] = []

    def over(key: str, sid: str, points: int, value: int, label: str,
             paths=None) -> None:
        limit = limits.get(key)
        if limit is not None and value > limit and label:
            out.append(_hit(sid, points, label, value, limit, paths))

    over("files", "C1", weights.C1_FILES, diff.files_touched,
         f"{diff.files_touched} files changed", diff.paths)
    over("net_loc", "C2", weights.C2_NET_LOC, diff.net_loc,
         f"net {diff.net_loc:+} lines")
    over("gross_churn", "C3", weights.C3_GROSS_CHURN, diff.gross_churn,
         f"{diff.gross_churn} lines churned")

    new_in_zone = [f.path for f in diff.new_files] if zone else []
    new_outside = [] if zone else [f.path for f in diff.new_files]
    if zone:
        over("new_files_in_zone", "C4", weights.C4_NEW_IN_ZONE,
             len(new_in_zone),
             f"{len(new_in_zone)} new file(s) in zone '{zone.id}'",
             new_in_zone)
    else:
        over("new_files_outside_zone", "C5", weights.C5_NEW_OUTSIDE,
             len(new_outside),
             f"{len(new_outside)} new file(s) outside any designated zone",
             new_outside)

    deps = diff.new_dependencies()
    over("new_dependencies", "C6", weights.C6_NEW_DEPS, len(deps),
         f"new dependency: {', '.join(deps)}" if deps else "", deps)

    keys = diff.new_config_keys()
    over("new_config_keys", "C7", weights.C7_NEW_KEYS, len(keys),
         f"new config/env key: {', '.join(keys)}" if keys else "", keys)

    sigs = diff.signature_changes()
    if sigs and len(sigs) > limits.get("signature_changes", 0):
        detail = "; ".join(
            f"{s['symbol']} in {s['file']} ({s['callers']} callers)"
            for s in sigs[:3]
        )
        out.append(_hit(
            "C8", weights.C8_SIGNATURE,
            f"signature changed on called code: {detail}",
            len(sigs), limits.get("signature_changes", 0),
        ))

    over("branch_tokens", "C9", weights.C9_BRANCH_TOKENS,
         diff.branch_tokens(),
         f"{diff.branch_tokens()} net new control-flow branches")
    over("nesting_depth", "C10", weights.C10_NESTING,
         diff.nesting_depth_delta(),
         f"nesting deepened by {diff.nesting_depth_delta()} level(s)")

    for crowded in diff.crowded_directories(limits.get("dir_population", 15)):
        out.append(_hit(
            "C12", weights.C12_CROWDED,
            f"{crowded['file']} lands in {crowded['directory']}/ which already "
            f"holds {crowded['population']} files",
            crowded["population"], limits.get("dir_population"),
            [crowded["file"]],
        ))

    for dup in diff.near_duplicate_files(limits.get("filename_similarity", 0.8)):
        out.append(_hit(
            "C12b", weights.C12B_NEAR_DUP,
            f"{dup['file']} closely resembles existing {dup['resembles']} "
            f"({dup['similarity']})",
            paths=[dup["file"]],
        ))

    return [h for h in out if h["detail"]]
