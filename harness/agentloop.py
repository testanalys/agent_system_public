"""The bridge between the Cursor agent harness and ours.

A Cursor agent's inner loop is opaque and should stay that way. We do not
supervise its tool calls; we judge what came out. The run boundary is
where its loop ends and ours resumes, and everything here happens there.

**One execution owns one agent, start to finish.** Spawn it, wait for the
run, judge the diff, re-prompt into the same session while attempts
remain, and either merge it or escalate it. Then exit. Concurrency comes
from running many of these at once, which is what the platform is for.

That replaces a reconciler that spawned every agent in the system from
one scheduled tick and then had to remember, in one shared file, which
agents were in flight. Everything expensive about the old shape came from
that file: one subsystem's tick fetching another subsystem's branch, a
re-prompt racing a still-running conversation into `agent_busy`, and a
single corrupted entry disabling a whole tier. None of it is reachable
now, because there is nothing shared to corrupt.

What survives here is the judgement: `collect_facts` reads the four
raw points at the run boundary, and `dispose` turns them into what
happens next. `adjudicate` is that pair. The master loop calls the
same `collect_facts`.
"""
from __future__ import annotations

import re
from dataclasses import dataclass, field
from pathlib import Path
from typing import Any, Protocol

from . import classify as classify_mod
from . import records, reprompts
from .diff import SessionDiff
from .skeleton import Skeleton

MAX_ATTEMPTS = 3                     # hard cap on run boundaries
TERMINAL = {"finished", "error", "cancelled", "expired"}

# Procedural reprompts (empty diff, oracle retry) may repeat up to MAX_ATTEMPTS.
# Substantive blocks (RED) get one proposal-formulation turn, then escalate.
REPROMPT_PROCEDURAL = "procedural"
REPROMPT_PROPOSAL = "proposal"

# Where an escalated agent is told to commit its argument. Read back off
# the branch by the agent execution, and pointed at by the ledger entry.
PROPOSAL_PATH = "agent_state/proposals/escalation.json"


# ═══════════════════════════════════════════════════════════════ contracts

@dataclass
class SpawnRequest:
    role: str                        # diarization-qa, pipeline-retro, ...
    subsystem: str
    repo_url: str
    starting_ref: str                # pinned, so the diff base is never in doubt
    prompt: str
    trigger: dict = field(default_factory=dict)
    env_vars: dict = field(default_factory=dict)
    model: str | None = None
    signature: str | None = None


@dataclass
class AgentEvidence:
    """Everything observable at the run boundary."""
    agent_id: str
    run_id: str
    status: str
    result_text: str = ""
    branch: str | None = None
    pr_url: str | None = None
    tool_calls: list[dict] = field(default_factory=list)
    usage: dict = field(default_factory=dict)
    duration_ms: int = 0
    # Cursor often leaves GET /runs.error null; the SSE stream carries the
    # real {code, message}. Without these the loop records FAILED with an
    # empty reason and the operator sees a green Cloud Run job.
    error_code: str | None = None
    error_message: str | None = None

    @property
    def is_terminal(self) -> bool:
        return self.status in TERMINAL

    @property
    def succeeded(self) -> bool:
        return self.status == "finished"

    @property
    def produced_code(self) -> bool:
        return bool(self.branch)

    def crash_detail(self) -> str:
        parts = [p for p in (self.error_code, self.error_message,
                             self.result_text) if p]
        return " — ".join(parts)[:600] if parts else f"run ended {self.status}"

    def tools_named(self) -> set[str]:
        return {str(call.get("name", "")) for call in self.tool_calls}

    def shell_commands(self) -> list[str]:
        """Commands the agent ran, for the independence and hindsight gates.

        Read straight off the run's event stream - no cooperation from the
        agent required, and nothing to install on its VM.
        """
        out: list[str] = []
        seen: set[str] = set()
        for call in self.tool_calls:
            args = call.get("args") or {}
            command = call.get("command") or args.get("command") or args.get("cmd")
            if command and str(command) not in seen:
                seen.add(str(command))
                out.append(str(command))
        return out


@dataclass
class Decision:
    outcome: str                     # MERGED | REPROMPT | ESCALATE | INCOMPLETE | FAILED
    reprompt: str = ""
    tier: str | None = None
    detail: dict = field(default_factory=dict)

    @property
    def done(self) -> bool:
        return self.outcome != "REPROMPT"


class AgentTransport(Protocol):
    """What we need from an agent runtime. Cursor is one implementation."""

    def create(self, request: SpawnRequest) -> tuple[str, Any]: ...
    def wait(self, handle: Any) -> AgentEvidence: ...
    def poll(self, agent_id: str, run_id: str) -> AgentEvidence: ...
    def send(self, agent_id: str, text: str) -> Any: ...


class GitPort(Protocol):
    """Repository operations, so adjudication is testable without a network."""

    def fetch(self, branch: str) -> None: ...
    def merge_to_base(self, branch: str, base: str) -> str: ...
    def collect_diff(self, base: str, branch: str) -> SessionDiff: ...


def fetch_branch(git: GitPort, branch: str) -> None:
    """Fetch, waiting briefly if the port knows how.

    Cursor can report finished before GitHub has the branch. The same
    execution waits; a later tick is not a retry of this fetch.
    """
    waiter = getattr(git, "fetch_until", None)
    if callable(waiter):
        waiter(branch)
        return
    git.fetch(branch)


_CHECKOUT = re.compile(
    r"\bgit(?:\s+-C\s+\S+)?\s+(?:checkout\s+-b|switch\s+-c)\s+(\S+)",
    re.IGNORECASE,
)
_PUSH = re.compile(
    r"\bgit(?:\s+-C\s+\S+)?\s+push\b([^;&\n]*)",
    re.IGNORECASE,
)


def checkout_branch(commands: list[str]) -> str | None:
    """Last ``git checkout -b`` / ``git switch -c`` the VM ran, if any."""
    name = None
    for command in commands:
        for match in _CHECKOUT.finditer(command):
            candidate = match.group(1).strip().strip("'\"")
            if candidate and not candidate.startswith("-"):
                name = candidate
    return name


def _clean_ref(name: str | None) -> str | None:
    if not name:
        return None
    name = name.strip().strip("'\"")
    if name.lower().startswith("refs/heads/"):
        name = name[len("refs/heads/"):]
    if name.lower().startswith("origin/"):
        name = name[len("origin/"):]
    if not name or name.startswith("-") or name in {".", "HEAD", "head"}:
        return None
    return name


def pushed_branches(commands: list[str]) -> list[str]:
    """Dest refs from ``git push``, in the order the VM ran them."""
    names: list[str] = []
    for command in commands:
        for match in _PUSH.finditer(command):
            tokens = [t.strip("'\"") for t in match.group(1).split()
                      if t and not t.startswith("-")]
            dest = None
            if len(tokens) >= 2:
                spec = tokens[1]
                dest = spec.split(":")[-1] if ":" in spec else spec
            elif len(tokens) == 1 and tokens[0] not in {"origin", "upstream"}:
                dest = tokens[0]
            if dest:
                names.append(dest)
    return names


def session_branch_candidates(claimed: str | None,
                              commands: list[str]) -> list[str]:
    """Names this session may have published, strongest first.

    Origin is the authority. Cursor's create-time name is only a hint.
    A push dest beats checkout -b beats the API name.
    """
    checkout = _clean_ref(checkout_branch(commands))
    out: list[str] = []

    def add(name: str | None) -> None:
        if (name or "").upper() == "HEAD":
            name = checkout
        name = _clean_ref(name)
        if name and name not in out:
            out.append(name)

    for raw in reversed(pushed_branches(commands)):
        add(raw)
    add(checkout)
    add(claimed)
    return out


def _on_origin(git: GitPort, branch: str) -> bool:
    try:
        git.fetch(branch)
        return True
    except Exception:  # noqa: BLE001
        return False


def _fetch_any(git: GitPort, branches: list[str]) -> str:
    """Wait until any candidate is on origin."""
    waiter = getattr(git, "fetch_until_any", None)
    if callable(waiter):
        return waiter(branches)
    last: Exception | None = None
    for name in branches:
        try:
            git.fetch(name)
            return name
        except Exception as exc:  # noqa: BLE001
            last = exc
    raise RuntimeError(
        f"none of {branches!r} on origin: {last}"
    ) from last


def resolve_origin_branch(git: GitPort, claimed: str | None,
                          commands: list[str]) -> str:
    """Origin ref that holds this run.

    Cursor assigns a name at create. That is a reservation, not a
    published ref. The VM often checkout -b a different name and pushes
    that. Ask origin which of the names this session mentioned exist;
    prefer push dest, then checkout, then the API name. Wait on the set.
    """
    candidates = session_branch_candidates(claimed, commands)
    if not candidates:
        raise RuntimeError("no branch name from Cursor or the VM")
    for name in candidates:
        if _on_origin(git, name):
            return name
    return _fetch_any(git, candidates)


# ═════════════════════════════════════════════════════════════ facts / dispose

@dataclass
class TurnFacts:
    """The four raw points at the run boundary, plus the diff if origin has it.

    status / error come from Cursor. branch is the origin ref after
    resolve_origin_branch. on_origin is git's answer.
    Cursor ERROR is a field here, not a gate in front of git.
    """
    status: str
    error: str | None
    branch: str | None
    on_origin: bool
    diff: SessionDiff | None
    result_text: str = ""
    tool_names: tuple[str, ...] = ()
    fetch_error: str | None = None


def collect_facts(evidence: AgentEvidence, git: GitPort,
                  starting_ref: str) -> TurnFacts:
    """Observe. Do not classify."""
    error = evidence.crash_detail() or None
    tools = tuple(sorted(evidence.tools_named()))
    commands = evidence.shell_commands()
    claimed = evidence.branch
    candidates = session_branch_candidates(claimed, commands)
    if not candidates:
        return TurnFacts(
            status=evidence.status, error=error, branch=None,
            on_origin=False, diff=None,
            result_text=evidence.result_text, tool_names=tools)
    try:
        branch = resolve_origin_branch(git, claimed, commands)
    except Exception as exc:  # noqa: BLE001
        return TurnFacts(
            status=evidence.status, error=error, branch=candidates[0],
            on_origin=False, diff=None,
            result_text=evidence.result_text, tool_names=tools,
            fetch_error=f"{type(exc).__name__}: {exc}")
    evidence.branch = branch
    diff = git.collect_diff(starting_ref, branch)
    return TurnFacts(
        status=evidence.status, error=error, branch=branch,
        on_origin=True, diff=diff,
        result_text=evidence.result_text, tool_names=tools)


def dispose(
    facts: TurnFacts,
    request: SpawnRequest,
    skeleton: Skeleton,
    git: GitPort,
    attempt: int,
    closures: list[dict] | None = None,
    run_checks=None,
    *,
    proposal_reprompt_sent: bool = False,
) -> Decision:
    """Act on observed facts. Cursor status is not an outcome."""
    if not facts.branch:
        if facts.status != "finished":
            return Decision("FAILED", tier=None, detail={
                "reason": f"run ended {facts.status}",
                "result": facts.error,
            })
        return _no_code_decision(facts, attempt)

    if not facts.on_origin:
        return Decision("INCOMPLETE", detail={
            "reason": "branch not on origin after wait",
            "status": facts.status,
            "branch": facts.branch,
            "error": facts.error,
            "fetch_error": facts.fetch_error,
        })

    successor = _successor_gap(facts, request, git, attempt)
    if successor is not None:
        return successor

    diff = facts.diff
    if diff is None or diff.is_empty:
        return _no_code_decision(facts, attempt)

    result = classify_mod.classify(
        diff, request.role, skeleton,
        signature=request.signature, closures=closures or [],
        attempt=attempt, attempts_allowed=MAX_ATTEMPTS,
    )

    if result.tier != classify_mod.GREEN:
        if proposal_reprompt_sent:
            return _escalate(result, diff)
        return Decision(
            "REPROMPT",
            reprompt=reprompts.proposal_formulation(
                result.tier, PROPOSAL_PATH, result.signals),
            tier=result.tier,
            detail={"signals": result.signals, "kind": REPROMPT_PROPOSAL,
                    "files": diff.files_touched,
                    "paths": list(diff.paths)},
        )

    if run_checks is not None:
        ok, failure_reprompt = run_checks(result, diff)
        if not ok:
            if attempt >= MAX_ATTEMPTS:
                return _escalate(result, diff, tier=classify_mod.AMBER)
            return Decision("REPROMPT", reprompt=failure_reprompt,
                            tier=classify_mod.AMBER,
                            detail={"kind": REPROMPT_PROCEDURAL})

    merged = git.merge_to_base(facts.branch, request.starting_ref)
    return Decision("MERGED", tier=classify_mod.GREEN,
                    detail={"branch": facts.branch, "merged_into": merged,
                            "files": diff.files_touched,
                            "paths": list(diff.paths)})


def adjudicate(
    evidence: AgentEvidence,
    request: SpawnRequest,
    repo: Path,
    skeleton: Skeleton,
    git: GitPort,
    attempt: int,
    closures: list[dict] | None = None,
    run_checks=None,
    *,
    proposal_reprompt_sent: bool = False,
) -> Decision:
    """Observe, then act. Same pair PIT executions and tests call."""
    if not evidence.is_terminal:
        return Decision("FAILED", detail={"reason": "run is not terminal"})
    facts = collect_facts(evidence, git, request.starting_ref)
    return dispose(
        facts, request, skeleton, git, attempt, closures, run_checks,
        proposal_reprompt_sent=proposal_reprompt_sent)


def _successor_gap(facts: TurnFacts, request: SpawnRequest,
                   git: GitPort, attempt: int) -> Decision | None:
    """Named chains (freestyle → bake) require a git artefact before merge.

    The table lives in successors.py so a role string does not fork
    classify. Missing artefact is a re-prompt, then INCOMPLETE.
    """
    from . import handoff as handoff_mod
    from . import successors as successors_mod

    chain = successors_mod.after(request.role)
    if chain is None or not facts.branch:
        return None
    sid = handoff_mod.session_id_from(request.trigger, None)
    status = successors_mod.satisfied(
        chain.require, git, facts.branch, sid)
    if status.get("ok"):
        return None
    if attempt >= MAX_ATTEMPTS:
        return Decision("INCOMPLETE", detail={
            "reason": f"{chain.require} missing; not spawning {chain.spawn}",
            "handoff": status,
        })
    return Decision(
        "REPROMPT",
        reprompt=reprompts.freestyle_handoff_missing(sid),
        detail={"kind": REPROMPT_PROCEDURAL, "handoff": status})


def _escalate(result: classify_mod.Classification, diff: SessionDiff,
              tier: str | None = None) -> Decision:
    """Attempts are spent. Ask for the argument and hand it to a master.

    The re-prompt attached here is not a request for a smaller change. It
    tells the agent its work is escalating and asks it to commit the
    reasoning to its own branch, which is the artifact the master reads.
    """
    return Decision(
        "ESCALATE",
        reprompt=reprompts.escalated(result.signals, tier or result.tier,
                                     PROPOSAL_PATH),
        tier=tier or result.tier,
        detail={"signals": result.signals,
                "files": diff.files_touched,
                "paths": list(diff.paths)},
    )


def _no_code_decision(facts: TurnFacts, attempt: int) -> Decision:
    """An empty diff is never a terminal outcome.

    We do not read the agent's closing prose for words like "blocked" and
    guess at what it meant. Guessing at intent from vocabulary misfiles
    the two cases that matter most, in opposite directions.

    Instead the agent is asked for one line in its ledger. "Nothing was
    needed" then exists as a positive artifact rather than an absence,
    and an absence is precisely what nobody can audit later. If it was
    actually stuck, the same note says so and becomes the proposal.

    Twice empty ends the run INCOMPLETE, on the record, where the retro
    agent handles it - a PIT agent returning nothing twice is a finding
    about prompting or spawning, not a mystery.
    """
    if attempt < MAX_ATTEMPTS:
        return Decision("REPROMPT",
                        reprompt=reprompts.no_diff(facts.result_text),
                        detail={"no_diff": True, "kind": REPROMPT_PROCEDURAL,
                                "tools_used": list(facts.tool_names)})
    return Decision("INCOMPLETE", detail={
        "no_diff": True,
        "reason": "no diff after the re-prompt; left for the retro agent",
        "tools_used": list(facts.tool_names),
        "conclusion": facts.result_text[:600],
    })


# ════════════════════════════════════════════════════════════════ the loop

def run_to_completion(request: SpawnRequest, transport: AgentTransport,
                      repo: Path, skeleton: Skeleton, git: GitPort,
                      closures: list[dict] | None = None,
                      run_checks=None, on_attempt=None,
                      **wait_kwargs) -> tuple[Decision, list[dict]]:
    """Spawn, judge, re-prompt, judge again. One conversation, one caller.

    The re-prompt goes back into the session that produced the change, so
    the agent still has everything it learned getting there. Sending it
    from anywhere else - a later tick, another process - either arrives
    while a run is active and is rejected, or arrives into a session whose
    context has moved on.

    Returns the final decision and one row per attempt.
    """
    agent_id, handle = transport.create(request)
    history: list[dict] = []
    decision = Decision("FAILED", detail={"reason": "no attempt was made"})
    proposal_reprompt_sent = False

    for attempt in range(1, MAX_ATTEMPTS + 1):
        evidence = transport.wait(handle, **wait_kwargs)
        decision = adjudicate(
            evidence, request, repo, skeleton, git, attempt, closures,
            run_checks, proposal_reprompt_sent=proposal_reprompt_sent)
        if (decision.outcome == "REPROMPT"
                and decision.detail.get("kind") == REPROMPT_PROPOSAL):
            proposal_reprompt_sent = True
        record_attempt(request, evidence, decision, attempt, repo)
        history.append({"attempt": attempt, "agent_id": agent_id,
                        "status": evidence.status, "branch": evidence.branch,
                        "outcome": decision.outcome, "tier": decision.tier,
                        "evidence": evidence})
        if on_attempt:
            on_attempt(attempt, evidence, decision)
        if decision.outcome != "REPROMPT":
            break
        handle = transport.send(agent_id, decision.reprompt)

    return decision, history


# ═══════════════════════════════════════════════════════════════ recording

OUTCOME_TO_LEDGER = {"MERGED": "merged", "ESCALATE": "proposed",
                     "REPROMPT": "blocked", "INCOMPLETE": "no_change",
                     "FAILED": "failed"}


def record_attempt(request: SpawnRequest, evidence: AgentEvidence,
                   decision: Decision, attempt: int,
                   repo: Path | None) -> None:
    """One ledger row per adjudicated run, in the repo it acted on.

    Best effort by design. This is the audit trail a human reads with
    `git log`; nothing in the loop branches on it, so a checkout that
    cannot be written to costs a record, not an outcome.
    """
    try:
        records.record_attempt(records.Attempt(
            ts=records.now(),
            session=evidence.agent_id,
            agent=request.role,
            role=tier_of(request.role),
            tier=decision.tier or ("GREEN" if decision.outcome == "MERGED"
                                   else "NONE"),
            attempt=attempt,
            files=int(decision.detail.get("files", 0) or 0),
            net_loc=0,
            paths=list(decision.detail.get("paths", []) or []),
            signals=decision.detail.get("signals", []),
            zones=[],
            outcome=OUTCOME_TO_LEDGER.get(decision.outcome, "blocked"),
            signature=request.signature,
            head=evidence.branch,
        ), repo)
    except Exception:                              # noqa: BLE001
        pass


def tier_of(role: str) -> str:
    if role.endswith("-retro"):
        return "retro"
    if role.endswith("-master") or role.startswith("master"):
        return "master"
    return "pit"
