"""The escalation ledger - the only state this layer keeps.

When an agent's change is classified AMBER or RED it is told, in its own
session, that the change is being escalated and that it must write the
proposal into its branch. The proposal itself therefore lives in git,
which is where it belongs: reviewable, diffable, attached to the code it
argues for, and updated by whoever touches it next.

But a proposal sitting on a branch nobody knows about is not an
escalation, it is a lost file. Something has to remember that a branch
exists and is waiting on a master. That is all this is:

    id, subsystem, role, agent_id, branch, tier, when, where the
    proposal was committed

Everything else - the argument, the diff, the evidence - is read from the
branch by the master when it gets there. This is an index, not a copy,
and an index that disagreed with the branch would be worse than none.

    open_entries(subsystem)   what the master must dispose of
    record(entry)             an escalation was raised
    close(entry)              a master disposed of it

One object per escalation, so two agents escalating at the same moment
cannot overwrite each other. There is no read-modify-write anywhere in
this module, which is what the shared `pending_agents.json` got wrong.
"""
from __future__ import annotations

import json
import re
from dataclasses import asdict, dataclass, field
from datetime import datetime, timedelta, timezone

from . import objectstore

OPEN_PREFIX = "agent_layer/escalations/open"
CLOSED_PREFIX = "agent_layer/escalations/closed"
COOLDOWN_PREFIX = "agent_layer/escalations/cooldown"
_SAFE = re.compile(r"[^A-Za-z0-9._-]+")


@dataclass
class Escalation:
    """One blocked change, waiting for a master."""

    id: str
    subsystem: str
    role: str
    agent_id: str
    tier: str                       # AMBER | RED
    branch: str | None = None
    base: str = ""
    head: str | None = None
    raised_at: str = ""
    trigger: dict = field(default_factory=dict)
    signals: list[dict] = field(default_factory=list)
    proposal_paths: list[str] = field(default_factory=list)
    proposal_committed: bool = False
    files: list[str] = field(default_factory=list)
    note: str = ""

    @classmethod
    def raise_for(cls, *, subsystem: str, role: str, agent_id: str, tier: str,
                  branch: str | None, base: str, **rest) -> "Escalation":
        return cls(
            id=entry_id(subsystem, agent_id),
            subsystem=subsystem, role=role, agent_id=agent_id, tier=tier,
            branch=branch, base=base,
            raised_at=datetime.now(timezone.utc).isoformat(timespec="seconds"),
            **rest,
        )


def entry_id(subsystem: str, agent_id: str) -> str:
    """Stable per agent, so a retried execution updates rather than duplicates."""
    return f"E-{_SAFE.sub('-', subsystem)}-{_SAFE.sub('-', agent_id)}"


def _key(prefix: str, subsystem: str, entry_id_: str) -> str:
    return f"{prefix}/{subsystem}/{entry_id_}.json"


def record(entry: Escalation) -> str | None:
    """Persist a raised escalation. Returns its address, or None."""
    return objectstore.put(_key(OPEN_PREFIX, entry.subsystem, entry.id),
                           asdict(entry))


def open_entries(subsystem: str) -> list[Escalation]:
    """Everything this subsystem's master has not yet disposed of."""
    out: list[Escalation] = []
    for key in objectstore.list_keys(f"{OPEN_PREFIX}/{subsystem}/"):
        data = objectstore.get_json(key)
        if not data:
            continue
        try:
            out.append(Escalation(**data))
        except TypeError:                 # a shape we no longer understand
            continue
    return sorted(out, key=lambda e: e.raised_at)


def close(entry: Escalation, *, disposed_by: str, outcome: str = "disposed") -> None:
    """Move an escalation out of the master's way, keeping the record."""
    body = asdict(entry)
    body["status"] = "closed"
    body["disposed_by"] = disposed_by
    body["outcome"] = outcome
    body["closed_at"] = datetime.now(timezone.utc).isoformat(timespec="seconds")
    objectstore.put(_key(CLOSED_PREFIX, entry.subsystem, entry.id), body)
    objectstore.delete(_key(OPEN_PREFIX, entry.subsystem, entry.id))


def set_cooldown(subsystem: str, *, minutes: int = 30, reason: str = "",
                 agent_id: str = "") -> str | None:
    """Stop the reconciler launching another master immediately.

    Used after INCOMPLETE. Escalations stay open; we just do not hammer
    Cursor every five minutes for a session that already failed to finish.
    """
    until = datetime.now(timezone.utc) + timedelta(minutes=minutes)
    body = {
        "subsystem": subsystem,
        "until": until.isoformat(timespec="seconds"),
        "reason": reason,
        "agent_id": agent_id,
        "minutes": minutes,
    }
    return objectstore.put(f"{COOLDOWN_PREFIX}/{subsystem}.json", body)


def cooldown_active(subsystem: str) -> dict | None:
    data = objectstore.get_json(f"{COOLDOWN_PREFIX}/{subsystem}.json")
    if not data:
        return None
    until = str(data.get("until") or "")
    try:
        when = datetime.fromisoformat(until)
    except ValueError:
        return None
    if when.tzinfo is None:
        when = when.replace(tzinfo=timezone.utc)
    if when <= datetime.now(timezone.utc):
        objectstore.delete(f"{COOLDOWN_PREFIX}/{subsystem}.json")
        return None
    return data


def docket_block(entries: list[Escalation]) -> str:
    """What the master is handed: pointers, not copies.

    Deliberately does not inline the argument. The master fetches the
    branch and reads the proposal there, against the code it changes -
    the same discipline the classifier follows, and the reason a stale
    summary can never mislead a disposition.
    """
    if not entries:
        return ""
    lines = ["## What is waiting for you", "",
             f"{len(entries)} change(s) were blocked and escalated. Each one "
             f"lives on its own branch. Fetch it, read the proposal committed "
             f"there, read the diff it argues for, and dispose of it.", ""]
    for entry in entries:
        lines.append(f"### {entry.id}  [{entry.tier}]  {entry.role}")
        lines.append(f"- branch: `{entry.branch or '(none pushed)'}`"
                     + (f" @ `{entry.head[:12]}`" if entry.head else ""))
        if entry.proposal_paths:
            lines.append("- proposal: "
                         + ", ".join(f"`{p}`" for p in entry.proposal_paths))
        elif entry.branch:
            lines.append("- proposal: **the agent was asked for one and did not "
                         "commit it.** That absence is itself a finding about "
                         "the agent or the prompt; say what you make of it.")
        if entry.trigger.get("metric"):
            lines.append(f"- woken by: {entry.trigger['metric']} = "
                         f"{entry.trigger.get('value')} against "
                         f"{entry.trigger.get('target')}")
        for signal in entry.signals[:4]:
            lines.append(f"- classifier: {signal.get('detail', signal.get('id'))}")
        if entry.files:
            lines.append(f"- touches: {', '.join(entry.files[:6])}")
        lines.append(f"- raised: {entry.raised_at}")
        lines.append("")
    lines.append("Give every one of them a disposition: approve, revise, "
                 "reject, or defer. A defer names the evidence that would "
                 "settle it.")
    return "\n".join(lines)


def summarise(entries: list[Escalation]) -> dict:
    tiers: dict[str, int] = {}
    for entry in entries:
        tiers[entry.tier] = tiers.get(entry.tier, 0) + 1
    return {"open": len(entries), "by_tier": tiers,
            "oldest": entries[0].raised_at if entries else None}


def to_json(entries: list[Escalation]) -> str:
    return json.dumps([asdict(e) for e in entries], indent=2, default=str)
