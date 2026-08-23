"""Operator surface after a master session: GitHub PR, hold, or incomplete.

Slack is not part of this. AWAITING_OPERATOR writes `operator/open` and
opens a PR. HOLD writes `operator/hold` with the wager headline.
INCOMPLETE writes `operator/incomplete` with a sub-reason — those
sessions should not happen, so the operator always sees them.
"""
from __future__ import annotations

from . import operator, pulls
from .routing import Route
from .wager import Wager

try:
    from cloud import workspace
except ImportError:  # tests importing harness in isolation
    workspace = None  # type: ignore[assignment]


def master_message(wager: Wager, route: Route, *,
                   subsystem: str, branch: str = "",
                   files: int = 0, classify: str = "") -> str:
    """PR body / hold note an operator can scan."""
    if route.action == "operator":
        verb = "NEEDS OPERATOR"
    elif route.action == "merge":
        verb = "MERGED"
    else:
        verb = "HELD"
    lines = [
        f"{verb} · `{subsystem}` · `{wager.mode}`",
        wager.headline,
        route.reason,
    ]
    if branch:
        lines.append(f"branch `{branch}` · {files} files · {classify or '—'}")
    if route.red_rules:
        lines.append("RED rules: " + ", ".join(route.red_rules))
        lines.append(
            "Do not merge until the protected surface change is reviewed.")
    return "\n".join(lines)


def notify_master(wager: Wager, route: Route, *,
                  subsystem: str, branch: str = "",
                  files: int = 0, classify: str = "",
                  agent_id: str = "", score: int | None = None,
                  paths: list[str] | None = None) -> dict:
    card = {
        "subsystem": subsystem,
        "branch": branch,
        "headline": wager.headline,
        "why_now": wager.why_now,
        "mode": wager.mode,
        "classify": classify,
        "score": score,
        "files": files,
        "paths": list(paths or [])[:20],
        "agent_id": agent_id,
        "reason": route.reason,
        "red_rules": list(route.red_rules),
    }

    if wager.holding or route.action == "none":
        if not wager.holding:
            return {"ok": True, "skipped": "no operator action for this route"}
        address = operator.write_hold(subsystem, card)
        return {"ok": True, "hold": address, "headline": wager.headline}

    if route.action == "operator":
        pr = _open_pr(subsystem, branch, wager, route, classify=classify,
                      files=files)
        card["pr_url"] = pr.get("url") or ""
        card["pr"] = pr
        address = operator.write_open(subsystem, card)
        return {"ok": bool(address) or pr.get("ok"), "operator": address,
                "pr": pr}

    return {"ok": True, "skipped": "merge; production-branch push is the rebuild signal"}


def raise_incomplete(subsystem: str, *,
                     sub_reason: str,
                     agent_id: str = "",
                     branch: str = "",
                     detail: dict | None = None,
                     escalations: list[str] | None = None) -> dict:
    """Surface a should-not-happen master failure on the operator dashboard.

    Ledger escalations stay open. This card is the alert that something in
    the loop broke its contract.
    """
    code = sub_reason if sub_reason in operator.SUB_REASONS else sub_reason
    headline = operator.SUB_REASONS.get(
        code, f"INCOMPLETE — {code or 'unknown'}")
    card = {
        "subsystem": subsystem,
        "sub_reason": code,
        "headline": headline,
        "agent_id": agent_id,
        "branch": branch,
        "detail": dict(detail or {}),
        "escalations_still_open": list(escalations or []),
    }
    address = operator.write_incomplete(subsystem, card)
    return {"ok": bool(address), "incomplete": address, "headline": headline,
            "sub_reason": code}


def _open_pr(subsystem: str, branch: str, wager: Wager, route: Route, *,
             classify: str, files: int) -> dict:
    slug = ""
    base = "master"
    if workspace is not None:
        slug = workspace.REPOS.get(subsystem) or ""
        base = workspace.BRANCHES.get(subsystem) or "master"
    title = pulls.operator_title(subsystem, wager.headline)
    body = master_message(
        wager, route, subsystem=subsystem, branch=branch,
        files=files, classify=classify)
    return pulls.open_pr(slug, branch, base, title, body)
