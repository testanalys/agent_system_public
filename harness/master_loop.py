"""The master loop: wager, then implement, then conditional turns.

PIT agents retry. Masters phase. The difference is that a master's second
turn is not a failed first attempt — it is the next stage of one piece of
work.

    ORIENT     → wager on the branch (the only hard mid-loop gate)
    IMPLEMENT  → do what the wager said (skipped on hold)
    REDUCE     → when the diff grew, or when the branch smells unfinished
    PROVE      → only the hard gates that actually failed

Transitions are computed from evidence. The agent is never told the
sequence in advance: an agent that knows a reduction pass is coming
writes a draft it expects to clean up later.

Keep this file small. The prompts carry the strategy; this file only
guarantees the cycle closes.
"""
from __future__ import annotations

from dataclasses import dataclass, field, replace
from pathlib import Path
from typing import Any

from dashboard import timeline
from roles.models import usage_fallback_model
from . import agentloop, classify as classify_mod, gates, ledger, notify, operator, rebuild
from . import reprompts, routing, wager as wager_mod
from .agentloop import AgentEvidence, AgentTransport, SpawnRequest, TurnFacts, collect_facts
from .cursor_rest import CursorAPIError, is_usage_limit_error
from .diff import SessionDiff
from .scorecard import coaching
from .skeleton import Skeleton

ORIENT, IMPLEMENT, REDUCE, PROVE = "orient", "implement", "reduce", "prove"
MAX_TURNS = 5

# Low on purpose: structural growth should almost always get a second look.
COMPLEXITY_BAR = {"files": 4, "added": 120, "new_files": 2}

# Soft nudge only — does not force a PROVE turn on its own.
SOFT_GATES = frozenset({"G-FP"})


@dataclass
class MasterReport:
    outcome: str = "INCOMPLETE"
    wager: dict | None = None
    phases: list[str] = field(default_factory=list)
    route: dict | None = None
    rebuild: dict | None = None
    notify: dict | None = None
    classify: str | None = None
    branch: str = ""
    agent_id: str = ""
    files: int = 0
    paths: list[str] = field(default_factory=list)
    detail: dict = field(default_factory=dict)

    def as_dict(self) -> dict:
        return {
            "outcome": self.outcome,
            "wager": self.wager,
            "phases": list(self.phases),
            "route": self.route,
            "rebuild": self.rebuild,
            "notify": self.notify,
            "classify": self.classify,
            "branch": self.branch,
            "agent_id": self.agent_id,
            "files": self.files,
            "paths": list(self.paths),
            "detail": self.detail,
        }


def over_bar(diff: SessionDiff | None) -> bool:
    if diff is None or diff.is_empty:
        return False
    if diff.files_touched >= COMPLEXITY_BAR["files"]:
        return True
    if diff.added_lines >= COMPLEXITY_BAR["added"]:
        return True
    if len(diff.new_files) >= COMPLEXITY_BAR["new_files"]:
        return True
    return False


def hard_failures(gate_results: list[gates.GateResult] | None) -> list[gates.GateResult]:
    return [g for g in (gate_results or [])
            if not g.passed and g.gate not in SOFT_GATES]


class _UsageFallbackTransport:
    """Master-only: retry create once on usage_limit with the cheap mid-tier.

    PIT/retro never wrap through this. Follow-up ``send`` calls keep the
    agent Cursor already opened — model is fixed at create — so the
    fallback covers every later master phase by landing the session on
    the cheaper model when advanced spawn is refused.
    """

    def __init__(self, inner: AgentTransport):
        self._inner = inner
        self.original_model: str | None = None
        self.model_used: str | None = None
        self.fell_back = False

    def create(self, request: SpawnRequest) -> tuple[str, Any]:
        self.original_model = request.model
        try:
            result = self._inner.create(request)
            self.model_used = request.model
            return result
        except CursorAPIError as exc:
            fallback = usage_fallback_model(request.model)
            if fallback is None or not is_usage_limit_error(exc):
                raise
            print(
                f"[master] usage_limit_exceeded on {request.model}; "
                f"retrying create with {fallback}",
                flush=True,
            )
            retry = replace(request, model=fallback)
            result = self._inner.create(retry)
            self.model_used = fallback
            self.fell_back = True
            return result

    def __getattr__(self, name: str):
        return getattr(self._inner, name)


def next_phase(phase: str, wager: wager_mod.Wager | None,
               diff: SessionDiff | None,
               gate_results: list[gates.GateResult] | None,
               *, hits: list[dict] | None = None) -> str | None:
    """What comes next, if anything. None means dispose."""
    if phase == ORIENT:
        if wager is None:
            return ORIENT          # stay until a wager lands
        return None if wager.holding else IMPLEMENT
    if phase == IMPLEMENT:
        if over_bar(diff) or coaching.should_coach(hits):
            return REDUCE
        return PROVE if hard_failures(gate_results) else None
    if phase == REDUCE:
        return PROVE if hard_failures(gate_results) else None
    return None


def run(request: SpawnRequest, transport: AgentTransport,
        repo: Path, skeleton: Skeleton, git,
        *, docket=None, cycle_id: str = "",
        closures: list[dict] | None = None,
        max_wait_s: int = 45 * 60,
        on_turn=None) -> MasterReport:
    """Drive one master session from orient through dispose."""
    report = MasterReport()
    transport = _UsageFallbackTransport(transport)
    agent_id, handle = transport.create(request)
    report.agent_id = agent_id
    if transport.fell_back:
        report.detail["model_fallback"] = {
            "from": transport.original_model,
            "to": transport.model_used,
            "reason": "usage_limit_exceeded",
        }
    evidence: AgentEvidence | None = None
    wager: wager_mod.Wager | None = None
    diff: SessionDiff | None = None
    classification = None
    gate_results: list[gates.GateResult] = []
    phase = ORIENT
    turns_in_phase = 0

    for turn in range(1, MAX_TURNS + 1):
        evidence = transport.wait(handle, max_wait_s=max_wait_s)
        report.branch = evidence.branch or report.branch
        report.phases.append(phase)
        if on_turn:
            on_turn(turn, phase, evidence)

        facts = collect_facts(evidence, git, request.starting_ref)
        early = _apply_facts(report, facts, request.subsystem)
        if early is not None:
            return early
        diff = facts.diff

        if phase == ORIENT:
            wager, prompt = _orient(
                git, facts, repo, base=request.starting_ref)
            if wager is None:
                turns_in_phase += 1
                if turns_in_phase >= 2:
                    return _incomplete(
                        report, request.subsystem,
                        sub_reason="no_wager_produced",
                        branch=facts.branch or report.branch,
                        detail={"reason": "no wager after re-prompt"},
                    )
                handle = transport.send(agent_id, prompt)
                continue
            report.wager = wager.to_dict()
            timeline.wagered(
                request.subsystem, request.role, wager,
                branch=facts.branch or "")
            turns_in_phase = 0
            nxt = next_phase(ORIENT, wager, diff, None)
            if nxt is None:
                return _dispose(report, wager, None, None, repo,
                                request.subsystem, git, request.role,
                                skeleton)
            phase = nxt
            timeline.phase(request.subsystem, request.role, ORIENT, phase,
                           wager=wager)
            handle = transport.send(agent_id, reprompts.implement(wager.to_dict()))
            continue

        # IMPLEMENT / REDUCE / PROVE — score, then decide.
        if facts.branch and diff is not None and not diff.is_empty:
            classification = classify_mod.classify(
                diff, request.role, skeleton,
                signature=request.signature, closures=closures or [],
            )
            report.classify = classification.tier
            report.detail["score"] = classification.score

        if docket is not None and evidence is not None:
            gate_results = gates.run_all(
                evidence, cycle_id or evidence.agent_id, docket, repo=repo)
            soft = [g for g in gate_results
                    if not g.passed and g.gate in SOFT_GATES]
            hard = hard_failures(gate_results)
            if phase != PROVE and soft and not hard and not over_bar(diff):
                # Light refine, does not consume a phase transition.
                handle = transport.send(agent_id, soft[0].reprompt)
                continue

        nxt = next_phase(
            phase, wager, diff, gate_results,
            hits=(classification.signals if classification else None))
        if nxt is None:
            return _dispose(report, wager, classification, diff, repo,
                            request.subsystem, git, request.role, skeleton)

        timeline.phase(request.subsystem, request.role, phase, nxt,
                       wager=wager, files=report.files)
        phase = nxt
        if phase == REDUCE:
            handle = transport.send(
                agent_id,
                reprompts.reduce(
                    report.files, (diff.added_lines if diff else 0),
                    report.paths,
                    hits=(classification.signals if classification else None),
                ))
        elif phase == PROVE:
            failed = hard_failures(gate_results)
            prompt = (failed[0].reprompt if failed
                      else "A required check is still open — address it, "
                           "commit, and push.")
            handle = transport.send(agent_id, prompt)
        else:
            handle = transport.send(agent_id, reprompts.implement(
                (wager or wager_mod.Wager("repair", "continue")).to_dict()))

    return _incomplete(
        report, request.subsystem,
        sub_reason="turn_cap",
        branch=report.branch,
        detail={"reason": f"hit turn cap ({MAX_TURNS})",
                "phases": list(report.phases)},
    )


def _apply_facts(report: MasterReport, facts: TurnFacts,
                 subsystem: str) -> MasterReport | None:
    """Stamp the report from observed facts. Return an early exit, or None.

    Cursor ERROR is not an outcome. A name we have that origin lacks is
    INCOMPLETE. A crash with no branch to fetch is FAILED.
    """
    report.branch = facts.branch or report.branch
    if facts.diff is not None:
        report.files = facts.diff.files_touched
        report.paths = list(facts.diff.paths)
    if facts.branch and not facts.on_origin:
        return _incomplete(
            report, subsystem,
            sub_reason="branch_not_on_origin",
            branch=facts.branch or "",
            detail={
                "reason": "branch not on origin after wait",
                "branch_claimed": facts.branch,
                "error": facts.fetch_error or facts.error,
                "status": facts.status,
            },
        )
    if facts.status != "finished" and not facts.on_origin:
        report.outcome = "FAILED"
        report.detail = {
            **report.detail,
            "reason": f"run ended {facts.status}",
            "result": (facts.error or facts.result_text or "")[:400],
        }
        return report
    return None


def _incomplete(report: MasterReport, subsystem: str, *,
                sub_reason: str, branch: str = "",
                detail: dict | None = None) -> MasterReport:
    """Should-not-happen exit: operator card + cooldown, ledger stays open."""
    report.outcome = "INCOMPLETE"
    report.detail = {**(detail or {}), "sub_reason": sub_reason}
    if branch:
        report.branch = branch
    reason = operator.SUB_REASONS.get(sub_reason) or sub_reason
    ledger.set_cooldown(subsystem, reason=reason, agent_id=report.agent_id)
    report.notify = notify.raise_incomplete(
        subsystem,
        sub_reason=sub_reason,
        agent_id=report.agent_id,
        branch=report.branch,
        detail=report.detail,
    )
    return report


def _orient(git, facts: TurnFacts, repo: Path,
            *, base: str | None = None
            ) -> tuple[wager_mod.Wager | None, str]:
    if not facts.branch or not facts.on_origin:
        return None, reprompts.wager_missing(
            "Commit the wager on a branch and push.")
    try:
        return wager_mod.load_from_branch(
            git, facts.branch, repo, base=base), ""
    except wager_mod.WagerInvalid as exc:
        if any("empty" in p or "missing" in p or "leftover" in p
               for p in exc.problems):
            return None, reprompts.wager_missing("; ".join(exc.problems))
        return None, reprompts.wager_invalid(exc.problems)


def _dispose(report: MasterReport, wager: wager_mod.Wager | None,
             classification, diff: SessionDiff | None,
             repo: Path, subsystem: str, git, role: str = "",
             skeleton: Skeleton | None = None) -> MasterReport:
    route = routing.route(wager, classification, branch=report.branch)
    report.route = route.as_dict()

    if route.action == routing.MERGE and report.branch:
        report = _land(report, wager, classification, diff, repo, subsystem,
                       git, role, skeleton)
        if report.route:
            route = routing.Route(
                report.route["action"], report.route["reason"],
                red_rules=tuple(report.route.get("red_rules") or ()),
                notify=report.route.get("notify", True),
            )
    elif route.action == routing.OPERATOR:
        report.outcome = "AWAITING_OPERATOR"
    else:
        report.outcome = "HOLD"

    if wager is not None:
        report.notify = notify.notify_master(
            wager, route, subsystem=subsystem, branch=report.branch,
            files=report.files,
            classify=report.classify or "",
            agent_id=report.agent_id,
            score=report.detail.get("score"),
            paths=report.paths,
        )
        timeline.routed(
            subsystem, report.agent_id or "master", wager, route,
            classify=report.classify or "",
            branch=report.branch, files=report.files,
            paths=report.paths,
        )
    return report


def _land(report: MasterReport, wager, classification, diff,
          repo: Path, subsystem: str, git, role: str,
          skeleton: Skeleton | None) -> MasterReport:
    """Replay onto current head, re-score, run the zone check, push.

    The agent container does not rebuild the image. The push is the
    signal for Cloud Build.
    """
    try:
        if hasattr(git, "rebase"):
            git.rebase(report.branch)
        if hasattr(git, "collect_diff"):
            try:
                diff = git.collect_diff(getattr(git, "base", ""), report.branch)
                report.files = diff.files_touched
                report.paths = list(diff.paths)
            except Exception:  # noqa: BLE001
                pass
        if classification is not None and diff is not None and skeleton is not None:
            classification = classify_mod.classify(diff, role, skeleton)
            report.classify = classification.tier
            report.detail["score"] = classification.score
        route = routing.route(wager, classification, branch=report.branch)
        report.route = route.as_dict()
        if route.action != routing.MERGE:
            report.outcome = "AWAITING_OPERATOR"
            return report

        checks = getattr(git, "acceptance_checks", None)
        if checks is not None and classification is not None and diff is not None:
            ok, msg = checks(classification, diff)
            if not ok:
                report.outcome = "AWAITING_OPERATOR"
                report.route = routing.Route(
                    routing.OPERATOR,
                    f"acceptance check failed after replay. {msg}",
                    notify=True,
                ).as_dict()
                return report

        merged = git.merge_to_base(report.branch, git.base)
        report.outcome = "MERGED"
        report.detail["merged_into"] = merged
        report.rebuild = rebuild.rebuild(subsystem, repo)
    except Exception as exc:  # noqa: BLE001
        report.outcome = "AWAITING_OPERATOR"
        report.detail["merge_error"] = f"{type(exc).__name__}: {exc}"
        report.route = routing.Route(
            routing.OPERATOR,
            f"merge failed: {exc}. Branch `{report.branch}` needs the operator.",
            notify=True,
        ).as_dict()
    return report
