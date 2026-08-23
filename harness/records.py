"""Durable harness state: attempts, sessions, proposals, triggers.

Everything here is committed to the code repo under ``agent_state/``, so
that a master agent working in the repo can read the whole history with
git and nothing depends on a database.

The important design point is the attempt log. **Every submit is
recorded with the tier it received, including attempts that were later
reduced to a green merge.** An agent that reaches past its footing and
then trims back has still told us something about where the boundary is
wrong, and that signal is worth more than a tidy history. It also means
there is nothing to gain by gaming the shape, which is why the re-prompt
says so out loud.
"""
from __future__ import annotations

import json
import os
import re
import uuid
from dataclasses import asdict, dataclass, field
from datetime import datetime, timedelta, timezone
from pathlib import Path

from . import paths

ATTEMPTS_FILE = "attempts.jsonl"
TRIGGERS_FILE = "triggers.json"
CYCLE_FILE = "cycle.json"
PROPOSAL_STATUSES = ("open", "adjudicated", "withdrawn", "superseded")
_ID_SAFE = re.compile(r"[^A-Za-z0-9._-]")


def now() -> str:
    return datetime.now(timezone.utc).isoformat(timespec="seconds")


def _read_jsonl(path: Path) -> list[dict]:
    if not path.exists():
        return []
    out = []
    for line in path.read_text(encoding="utf-8").splitlines():
        line = line.strip()
        if not line:
            continue
        try:
            out.append(json.loads(line))
        except json.JSONDecodeError:
            continue
    return out


def _append_jsonl(path: Path, record: dict) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    with open(path, "a", encoding="utf-8") as handle:
        handle.write(json.dumps(record, default=str) + "\n")


def _write_json(path: Path, data: dict) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text(json.dumps(data, indent=2, default=str) + "\n", encoding="utf-8")


# ═══════════════════════════════════════════════════════════════ sessions

def session_id() -> str:
    """Stable per-agent-run identity.

    Set by the spawner. Falls back to a git-branch-derived id so a human
    or a stray run still records coherently rather than crashing.
    """
    explicit = (os.getenv("TURNSTILE_SESSION_ID") or "").strip()
    if explicit:
        return _ID_SAFE.sub("-", explicit)
    branch = (os.getenv("TURNSTILE_AGENT_BRANCH") or "").strip()
    if branch:
        return _ID_SAFE.sub("-", branch)
    return f"local-{uuid.uuid4().hex[:8]}"


def agent_id() -> str:
    value = (os.getenv("TURNSTILE_AGENT_ID") or "").strip()
    return _ID_SAFE.sub("-", value) if value else "unknown-agent"


def agent_role() -> str:
    """pit | retro | master | implementation — set by the spawner."""
    return (os.getenv("TURNSTILE_AGENT_ROLE") or "pit").strip().lower()


# ═══════════════════════════════════════════════════════════════ attempts

@dataclass
class Attempt:
    ts: str
    session: str
    agent: str
    role: str
    tier: str
    attempt: int
    files: int
    net_loc: int
    paths: list[str] = field(default_factory=list)
    signals: list[dict] = field(default_factory=list)
    zones: list[str] = field(default_factory=list)
    outcome: str = "blocked"          # merged | blocked | proposed | withdrawn
    proposal: str | None = None
    signature: str | None = None
    head: str | None = None


def record_attempt(attempt: Attempt, repo: Path | None = None) -> None:
    _append_jsonl(paths.state_dir(repo) / ATTEMPTS_FILE, asdict(attempt))


def load_attempts(repo: Path | None = None, days: int | None = None) -> list[dict]:
    rows = _read_jsonl(paths.state_dir(repo) / ATTEMPTS_FILE)
    if days is None:
        return rows
    cutoff = datetime.now(timezone.utc) - timedelta(days=days)
    kept = []
    for row in rows:
        try:
            if datetime.fromisoformat(row["ts"]) >= cutoff:
                kept.append(row)
        except (KeyError, ValueError):
            continue
    return kept


def attempts_this_session(session: str, repo: Path | None = None) -> list[dict]:
    return [a for a in load_attempts(repo) if a.get("session") == session]


def next_attempt_number(session: str, repo: Path | None = None) -> int:
    return len(attempts_this_session(session, repo)) + 1


def mark_attempt_outcome(session: str, outcome: str, proposal: str | None = None,
                         repo: Path | None = None) -> None:
    """Rewrite the last attempt of a session with its final outcome.

    The log is append-only in spirit, but an attempt's outcome is only
    knowable after the fact, so the last row for a session is updated in
    place rather than duplicated.
    """
    path = paths.state_dir(repo) / ATTEMPTS_FILE
    rows = _read_jsonl(path)
    for row in reversed(rows):
        if row.get("session") == session:
            row["outcome"] = outcome
            if proposal:
                row["proposal"] = proposal
            break
    else:
        return
    with open(path, "w", encoding="utf-8") as handle:
        for row in rows:
            handle.write(json.dumps(row, default=str) + "\n")


# ══════════════════════════════════════════════════════════════ proposals

@dataclass
class Proposal:
    id: str
    subsystem: str
    agent: str
    role: str
    session: str
    tier: str
    created: str
    status: str = "open"
    branch: str | None = None
    head: str | None = None
    base: str | None = None
    signature: str | None = None
    classify: dict = field(default_factory=dict)
    body: dict = field(default_factory=dict)
    verdict: dict | None = None
    evidence_scripts: list[str] = field(default_factory=list)

    @property
    def dir_name(self) -> str:
        return self.id


def new_proposal_id(repo: Path | None = None) -> str:
    stamp = datetime.now(timezone.utc).strftime("%Y-%m-%d")
    existing = {p.name for p in paths.proposals_dir(repo).iterdir() if p.is_dir()}
    for n in range(1, 1000):
        candidate = f"P-{stamp}-{n:03d}"
        if candidate not in existing:
            return candidate
    raise RuntimeError("proposal id space exhausted for today")


def save_proposal(proposal: Proposal, patch: str | None = None,
                  repo: Path | None = None) -> Path:
    target = paths.proposals_dir(repo) / proposal.dir_name
    target.mkdir(parents=True, exist_ok=True)
    _write_json(target / "proposal.json", asdict(proposal))
    if patch is not None:
        (target / "diff.patch").write_text(patch, encoding="utf-8")
    return target


def load_proposal(pid: str, repo: Path | None = None) -> Proposal | None:
    path = paths.proposals_dir(repo) / pid / "proposal.json"
    if not path.exists():
        return None
    data = json.loads(path.read_text(encoding="utf-8"))
    return Proposal(**data)


def list_proposals(repo: Path | None = None, status: str | None = None) -> list[Proposal]:
    out: list[Proposal] = []
    root = paths.proposals_dir(repo)
    for child in sorted(root.iterdir()):
        if not child.is_dir():
            continue
        proposal = load_proposal(child.name, repo)
        if proposal and (status is None or proposal.status == status):
            out.append(proposal)
    return out


def proposal_patch(pid: str, repo: Path | None = None) -> str:
    path = paths.proposals_dir(repo) / pid / "diff.patch"
    return path.read_text(encoding="utf-8") if path.exists() else ""


def proposal_age_days(proposal: Proposal) -> int:
    try:
        created = datetime.fromisoformat(proposal.created)
    except ValueError:
        return 0
    return max(0, (datetime.now(timezone.utc) - created).days)


# ═══════════════════════════════════════════════════════════════ triggers

def load_triggers(repo: Path | None = None) -> dict:
    path = paths.state_dir(repo) / TRIGGERS_FILE
    if not path.exists():
        return {"set": {}, "history": []}
    try:
        return json.loads(path.read_text(encoding="utf-8"))
    except json.JSONDecodeError:
        return {"set": {}, "history": []}


def save_triggers(state: dict, repo: Path | None = None) -> None:
    _write_json(paths.state_dir(repo) / TRIGGERS_FILE, state)


def set_trigger(trigger_id: str, reason: str, evidence: dict | None = None,
                repo: Path | None = None) -> None:
    """Triggers are durable state, not events.

    They stay set until a master cycle completes its gates, so a failed
    or timed-out master spawn loses nothing.
    """
    state = load_triggers(repo)
    existing = state["set"].get(trigger_id)
    if existing:
        existing["reason"] = reason
        existing["evidence"] = evidence or existing.get("evidence", {})
        existing["last_seen"] = now()
        existing["count"] = existing.get("count", 1) + 1
    else:
        state["set"][trigger_id] = {
            "id": trigger_id, "reason": reason, "evidence": evidence or {},
            "first_seen": now(), "last_seen": now(), "count": 1,
        }
    save_triggers(state, repo)


def clear_triggers(consumed: list[str], cycle_id: str, repo: Path | None = None) -> None:
    state = load_triggers(repo)
    for trigger_id in consumed:
        entry = state["set"].pop(trigger_id, None)
        if entry:
            entry["cleared_by"] = cycle_id
            entry["cleared_at"] = now()
            state["history"].append(entry)
    save_triggers(state, repo)


# ══════════════════════════════════════════════════════════════ cycles

def load_cycle(repo: Path | None = None) -> dict:
    """Master cycle history, in the repo it belongs to.

    This used to be mirrored to object storage so that a stateless
    reconciler container could survive a restart with it. Nothing decides
    anything from it any more - a master runs when the escalation ledger
    has entries, which is a fact about branches rather than about how long
    ago the last cycle was - so it is a plain committed file again.
    """
    path = paths.state_dir(repo) / CYCLE_FILE
    if not path.exists():
        return {"last_master_cycle": None, "cycles": []}
    try:
        return json.loads(path.read_text(encoding="utf-8"))
    except (json.JSONDecodeError, OSError):
        return {"last_master_cycle": None, "cycles": []}


def record_cycle(cycle_id: str, outcome: str, triggers: list[str],
                 dispositions: int = 0, repo: Path | None = None) -> None:
    state = load_cycle(repo)
    state["last_master_cycle"] = now()
    state["cycles"].append({
        "id": cycle_id, "ts": now(), "outcome": outcome,
        "triggers": triggers, "dispositions": dispositions,
    })
    state["cycles"] = state["cycles"][-100:]
    _write_json(paths.state_dir(repo) / CYCLE_FILE, state)


def days_since_master_cycle(repo: Path | None = None) -> int | None:
    state = load_cycle(repo)
    last = state.get("last_master_cycle")
    if not last:
        return None
    try:
        return max(0, (datetime.now(timezone.utc) - datetime.fromisoformat(last)).days)
    except ValueError:
        return None


# ═══════════════════════════════════════════════════════ session records

def write_session_record(record: dict, repo: Path | None = None) -> Path:
    path = paths.sessions_dir(repo) / f"{record['session']}.json"
    _write_json(path, record)
    return path


def load_session_record(session: str, repo: Path | None = None) -> dict | None:
    path = paths.sessions_dir(repo) / f"{session}.json"
    if not path.exists():
        return None
    try:
        return json.loads(path.read_text(encoding="utf-8"))
    except json.JSONDecodeError:
        return None


def update_session_record(session: str, repo: Path | None = None, **fields) -> dict:
    record = load_session_record(session, repo) or {"session": session, "started": now()}
    record.update(fields)
    record["updated"] = now()
    write_session_record(record, repo)
    return record
