"""Freestyle → bake handoff artefacts on a branch.

Same idea as reading a proposal off a branch after escalate: the harness
asks git whether the interaction log exists rather than trusting the
agent's closing prose.
"""
from __future__ import annotations

import re

HANDOFF_ROLE = "discovery-freestyle-rd"
BAKE_ROLE = "discovery-route-bake"

_INTERACTION = re.compile(
    r"^state/[^/]+/rd_retro_handoff/interaction\.jsonl$"
)
_MANIFEST = re.compile(
    r"^state/[^/]+/rd_retro_handoff(\.json|/rd_retro_handoff\.json|/manifest\.json)$"
)


def session_id_from(trigger: dict | None, context: dict | None) -> str:
    trigger = trigger or {}
    context = context or {}
    dims = trigger.get("dims") or {}
    for source in (dims, trigger, context, context.get("harvest_summary") or {}):
        if not isinstance(source, dict):
            continue
        sid = source.get("session_id") or source.get("run_id")
        if sid:
            return str(sid)
    return ""


def interaction_paths(session_id: str) -> tuple[str, ...]:
    sid = (session_id or "").strip() or "*"
    return (
        f"state/{sid}/rd_retro_handoff/interaction.jsonl",
        f"state/{sid}/rd_retro_handoff.json",
        f"state/{sid}/rd_retro_handoff/rd_retro_handoff.json",
        f"state/{sid}/rd_retro_handoff/manifest.json",
    )


def list_handoff_files(git, branch: str | None) -> list[str]:
    """Paths under state/*/rd_retro_handoff on the branch."""
    if not branch:
        return []
    try:
        files = git.files_under(branch, "state")
    except Exception:                                  # noqa: BLE001
        return []
    out = []
    for path in files:
        norm = path.replace("\\", "/")
        if "/rd_retro_handoff" in norm or norm.endswith("rd_retro_handoff.json"):
            out.append(norm)
    return out


def handoff_complete(git, branch: str | None,
                     session_id: str = "") -> dict:
    """Whether freestyle committed a usable interaction handoff.

    Requires a non-empty-looking interaction.jsonl path on the branch.
    Manifest / rd_retro_handoff.json is preferred for bake but interaction
    alone is the gate freestyle must pass.
    """
    files = list_handoff_files(git, branch)
    interactions = [p for p in files if p.endswith("interaction.jsonl")]
    if session_id:
        prefix = f"state/{session_id}/"
        scoped = [p for p in interactions if p.startswith(prefix)]
        if scoped:
            interactions = scoped
    manifests = [
        p for p in files
        if p.endswith("rd_retro_handoff.json") or p.endswith("/manifest.json")
    ]
    ok = bool(interactions)
    return {
        "ok": ok,
        "interaction_paths": interactions,
        "manifest_paths": manifests,
        "all_handoff_paths": files,
        "session_id": session_id,
    }


def bake_trigger(session_id: str, branch: str, *,
                 handoff: dict | None = None) -> dict:
    handoff = handoff or {}
    return {
        "metric": "session",
        "value": session_id or branch,
        "dims": {
            "session_id": session_id,
            "freestyle_branch": branch,
        },
        "detail": (
            f"Freestyle finished with interaction handoff on `{branch}`. "
            f"Codify success routes from "
            f"{', '.join((handoff.get('interaction_paths') or [])[:2]) or 'the handoff'}."
        ),
    }
