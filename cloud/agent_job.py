"""One execution, one agent, start to finish.

    python -m cloud.agent_job --role pipeline-acquisition-qa \
        --subsystem pipeline --trigger '{"metric": "...", "value": 0.4}'

    python -m cloud.agent_job --subsystem discovery --master

This is what the mechanical code starts the moment it detects something
worth an agent's attention, and what the reconciler starts when a master
has escalations waiting. It spawns the agent on Cursor, waits for the
run, judges the diff, re-prompts into the same session while attempts
remain, and finishes by either merging the change or escalating it. Then
it exits.

Three properties follow from that shape, and all three were missing
before:

**It owns its conversation.** Nothing else re-prompts this agent, so a
follow-up cannot arrive while a run is active. `agent_busy` is not
handled here because it cannot happen.

**It touches one repository.** The checkout is this subsystem's repo and
no other, so a branch that does not exist on some other subsystem's
remote is not an error anyone can encounter.

**Its failure is its own.** A crash ends one agent's execution. Nothing
else is queued behind it and no shared file is left half-written.

The archive at the end is best effort and nothing reads it to decide
anything. It exists so a retro or a master can see how an agent actually
reasoned, rather than inferring it from the diff it left behind.
"""
from __future__ import annotations

import argparse
import json
import os
import sys
import traceback
from datetime import datetime, timezone
from pathlib import Path

import roles as roles_mod
from dashboard import timeline
from harness import agentloop, ledger, notify, paths, records, runlog
from harness.agentloop import Decision, PROPOSAL_PATH, SpawnRequest, fetch_branch
from harness import handoff as handoff_mod
from harness import successors as successors_mod
from harness.cursor_rest import CursorRestTransport
from harness.skeleton import Skeleton
from roles import RoleContext, render

from . import launcher, workspace
from reconciler.gitport import RepoPort

DEFAULT_AGENT_MODEL = "composer-2.5"
DEFAULT_MAX_WAIT_S = 45 * 60
SKELETON_DIR = Path(__file__).resolve().parents[1] / "skeletons"


def _max_wait() -> int:
    try:
        return int((os.getenv("TURNSTILE_AGENT_MAX_WAIT_S") or "").strip()
                   or DEFAULT_MAX_WAIT_S)
    except ValueError:
        return DEFAULT_MAX_WAIT_S


def _skeleton(subsystem: str) -> Skeleton:
    return Skeleton.load_file(SKELETON_DIR / f"{subsystem}.json")


def _closures_for(repo: Path, signature: str | None) -> list[dict]:
    if not signature:
        return []
    registry = paths.state_dir(repo) / "signatures.json"
    if not registry.exists():
        return []
    try:
        data = json.loads(registry.read_text(encoding="utf-8"))
    except json.JSONDecodeError:
        return []
    for entry in data.get("signatures", []):
        if entry.get("id") == signature:
            return entry.get("closures", [])
    return []


def _registry(subsystem: str, repo: Path):
    """This subsystem's agents, read from the checkout that defines them.

    Not a module-level global any more. Which agents exist is a property
    of the repo, so it is read from the repo - which is what lets a
    master add one without an edit to this package.
    """
    return roles_mod.load(repo, repo_url=workspace.repo_url(subsystem),
                          subsystem=subsystem)


def _brief(repo: Path, role, trigger: dict) -> list[dict]:
    """Audience-scoped last / last_7 / means. Absent is fine; wrong would not be."""
    try:
        from harness import docket as docket_mod

        audience = "pit" if role.tier == "pit" else role.tier
        return docket_mod.brief_for(
            repo, audience=audience, role=role, trigger=trigger)
    except Exception:                                  # noqa: BLE001
        return []


def _now() -> str:
    return datetime.now(timezone.utc).isoformat(timespec="seconds")


# ═════════════════════════════════════════════════════════════════ escalate

def _proposal_change(git: RepoPort, branch: str | None,
                     paths: list[str]) -> str:
    """First line of reasoning from an escalation proposal, if readable."""
    if not branch:
        return ""
    for path in paths:
        if not path.endswith(".json"):
            continue
        try:
            raw = git.read_file(branch, path)
            body = json.loads(raw)
            return str(body.get("change") or "")
        except Exception:                              # noqa: BLE001
            continue
    return ""


def _record_spawn(subsystem: str, role, trigger: dict) -> None:
    timeline.spawned(subsystem, role.id, role.tier, trigger)


def _substantive_prior(history: list[dict]) -> dict | None:
    """Last attempt that left work the master must see.

    Includes (1) AMBER/RED before a follow-up crash, and (2) a Cursor ERROR
    that claimed a branch — otherwise first-attempt platform failures vanish
    from the ledger and the closed loop never wakes a master.
    """
    if not history:
        return None
    for row in reversed(history[:-1] if len(history) > 1 else []):
        tier = row.get("tier")
        branch = row.get("branch")
        if tier in ("RED", "AMBER") and branch:
            return row
    last = history[-1]
    if last.get("branch") and last.get("status") in ("error", "expired", "cancelled"):
        return last
    if last.get("outcome") == "INCOMPLETE" and last.get("branch"):
        return last
    return None


def _escalate_from_history(git: RepoPort, history: list[dict],
                           request: SpawnRequest,
                           transport) -> dict | None:
    row = _substantive_prior(history)
    if not row:
        return None
    evidence = row["evidence"]
    tier = row.get("tier") or "AMBER"
    from harness import reprompts

    # Do not send a Cursor follow-up on a run that already ended ERROR —
    # follow-ups often ERROR again with a null REST payload (Cursor platform).
    # Record the ledger entry so a master (or operator incomplete board) sees it.
    skip_send = evidence.status in ("error", "expired", "cancelled")
    decision = Decision(
        "ESCALATE",
        reprompt=reprompts.escalated([], tier, PROPOSAL_PATH),
        tier=tier,
        detail={
            "signals": [],
            "paths": [],
            "reason": (
                "cursor run ended error; escalating claimed branch for master"
                if skip_send else
                "session ended after substantive block; escalating partial work"
            ),
            "cursor_status": evidence.status,
            "cursor_error": evidence.crash_detail(),
        },
    )
    if skip_send:
        return _escalate_ledger_only(git, evidence, request, decision)
    return _escalate(git, evidence, request, decision, transport)


def _escalate_ledger_only(git: RepoPort, evidence, request: SpawnRequest,
                          decision) -> dict:
    """Put a failed Cursor run on the ledger without a doomed follow-up send."""
    outcome: dict = {"asked": False, "committed": False,
                     "skipped_send": "cursor run already terminal error"}
    branch = evidence.branch
    proposal_paths: list[str] = []
    head = None
    if branch:
        try:
            fetch_branch(git, branch)
            head = git.head_of(branch)
            proposal_paths = git.files_under(branch, "agent_state/proposals")
            outcome["committed"] = bool(proposal_paths)
        except Exception as exc:  # noqa: BLE001
            outcome["fetch_error"] = f"{type(exc).__name__}: {exc}"
    outcome["proposal_paths"] = proposal_paths
    entry = ledger.Escalation.raise_for(
        subsystem=request.subsystem, role=request.role,
        agent_id=evidence.agent_id, tier=decision.tier or "AMBER",
        branch=branch, base=git.base,
        head=head,
        trigger=request.trigger,
        signals=decision.detail.get("signals", []),
        proposal_paths=proposal_paths,
        proposal_committed=bool(proposal_paths),
        files=list(decision.detail.get("paths", []) or [])[:20],
        note=(evidence.crash_detail() or "")[:600],
    )
    outcome["ledger"] = ledger.record(entry)
    outcome["entry_id"] = entry.id
    if not outcome["ledger"]:
        outcome["reason"] = ("TURNSTILE_AGENT_STORE is not configured, so this "
                             "escalation was not recorded and no master will "
                             "see it")
    _record_proposal(request.subsystem, request.role,
                     agentloop.tier_of(request.role), git, branch,
                     proposal_paths)
    return outcome


def _capture_bake_metrics(repo: Path) -> None:
    """Append bake brief metrics from the discovery checkout (FP014)."""
    try:
        utils = repo / "utils"
        if str(utils.parent) not in sys.path:
            sys.path.insert(0, str(utils.parent))
        from utils.metric_capture import capture as cap  # noqa: PLC0415
        cap(["harvest_tool_growth", "audio_per_tool"], append=True)
    except Exception:                                  # noqa: BLE001
        pass


def _spawn_bake(subsystem: str, session_id: str, branch: str,
                handoff: dict, context: dict | None, *,
                repo: Path | None = None,
                successor_role: str = "discovery-route-bake") -> dict:
    """Deterministic bake spawn after freestyle leaves a complete handoff.

    Does not require freestyle GREEN. No closeout / campaign backstop —
    only the freestyle agent_job calls this when interaction.jsonl is on
    the branch.
    """
    if repo is not None:
        _capture_bake_metrics(repo)
    trigger = handoff_mod.bake_trigger(session_id, branch, handoff=handoff)
    ctx = {
        "session_id": session_id,
        "freestyle_branch": branch,
        "handoff": handoff,
        **(context or {}),
    }
    args = launcher.agent_args(
        successor_role, subsystem, trigger, ctx)
    return launcher.launch(args)


def _record_proposal(subsystem: str, role_id: str, tier: str,
                     git: RepoPort, branch: str | None,
                     paths: list[str]) -> None:
    change = _proposal_change(git, branch, paths)
    if change:
        timeline.proposal_headline(
            subsystem, role_id, tier, change, branch=branch or "",
            ref=paths[0] if paths else "")


def _escalate(git: RepoPort, evidence, request: SpawnRequest,
              decision, transport) -> dict:
    """Ask for the argument, wait for it, and put it in front of a master.

    The re-prompt goes into the agent's own session, which still holds
    everything it learned. What comes back is a commit on the branch it
    already pushed. We then ask git whether that commit exists rather
    than taking the agent's word for it, and record a pointer either way -
    an agent that was asked for its reasoning and did not give it is a
    finding the master should see, not a silence.
    """
    outcome: dict = {"asked": True, "committed": False}
    branch = evidence.branch
    proposal_paths: list[str] = []
    head = None
    if branch:
        try:
            fetch_branch(git, branch)
            head = git.head_of(branch)
            proposal_paths = git.files_under(branch, "agent_state/proposals")
        except Exception as exc:                       # noqa: BLE001
            outcome["fetch_error"] = f"{type(exc).__name__}: {exc}"

    if not proposal_paths:
        try:
            handle = transport.send(evidence.agent_id, decision.reprompt)
            final = transport.wait(handle, max_wait_s=_max_wait())
            outcome["final_status"] = final.status
            branch = final.branch or branch
            if branch:
                fetch_branch(git, branch)
                head = git.head_of(branch)
                proposal_paths = git.files_under(branch, "agent_state/proposals")
        except Exception as exc:                           # noqa: BLE001
            outcome["reason"] = f"{type(exc).__name__}: {exc}"
            final = evidence
    else:
        final = evidence
        outcome["skipped_send"] = "proposal already on branch"

    outcome["committed"] = bool(proposal_paths)
    outcome["proposal_paths"] = proposal_paths

    entry = ledger.Escalation.raise_for(
        subsystem=request.subsystem, role=request.role,
        agent_id=evidence.agent_id, tier=decision.tier or "AMBER",
        branch=branch, base=git.base,
        head=head,
        trigger=request.trigger,
        signals=decision.detail.get("signals", []),
        proposal_paths=proposal_paths,
        proposal_committed=bool(proposal_paths),
        files=list(decision.detail.get("paths", []) or [])[:20],
        note=(final.result_text or "")[:600],
    )
    outcome["ledger"] = ledger.record(entry)
    outcome["entry_id"] = entry.id
    if not outcome["ledger"]:
        # The one place durability actually matters. Say so loudly rather
        # than exiting 0 on a change nobody will ever look at again.
        outcome["reason"] = ("TURNSTILE_AGENT_STORE is not configured, so this "
                             "escalation was not recorded and no master will "
                             "see it")
    _record_proposal(request.subsystem, request.role,
                     agentloop.tier_of(request.role), git, branch,
                     proposal_paths)
    return outcome


# ══════════════════════════════════════════════════════════════════ the run

def run_agent(role_id: str, subsystem: str, trigger: dict,
              context: dict | None = None, *, dry_run: bool = False) -> dict:
    base = workspace.base_branch(subsystem)
    report: dict = {"role": role_id, "subsystem": subsystem, "base": base,
                    "started_at": _now(), "trigger": trigger}

    # The checkout comes first: it is what declares which agents exist.
    repo = workspace.ensure(subsystem, base=base)
    git = RepoPort(repo, base=base)
    skeleton = _skeleton(subsystem)
    registry = _registry(subsystem, repo)
    role = registry.get(role_id)

    prompt = render(role, RoleContext(
        trigger=trigger,
        brief=_brief(repo, role, trigger),
        signature_history={"closures": _closures_for(repo, role.signature)},
        run_record=context or {},
    ), registry.orientation, registry.scale_framework)
    request = SpawnRequest(
        role=role.id, subsystem=subsystem, repo_url=role.repos[0],
        starting_ref=git.current_base(), prompt=prompt, trigger=trigger,
        env_vars=role.env_vars(),
        model=role.model or os.getenv("TURNSTILE_AGENT_MODEL", DEFAULT_AGENT_MODEL),
        signature=role.signature,
    )

    if dry_run:
        report["would_spawn"] = True
        report["prompt_chars"] = len(prompt)
        return report

    _record_spawn(subsystem, role, trigger)
    transport = CursorRestTransport()

    def announce(attempt: int, evidence, decision) -> None:
        print(f"[{subsystem}] attempt {attempt}: {decision.outcome}"
              + (f" ({decision.tier})" if decision.tier else ""), flush=True)

    decision, history = agentloop.run_to_completion(
        request, transport, repo, skeleton, git,
        closures=_closures_for(repo, role.signature),
        run_checks=git.acceptance_checks, on_attempt=announce,
        max_wait_s=_max_wait())

    evidence = history[-1]["evidence"] if history else None
    agent_id = history[-1]["agent_id"] if history else "unknown"
    report["agent_id"] = agent_id
    report["attempts"] = [{k: v for k, v in row.items() if k != "evidence"}
                          for row in history]
    report["outcome"] = decision.outcome
    report["tier"] = decision.tier

    if decision.outcome == "ESCALATE":
        report["escalation"] = _escalate(git, evidence, request, decision,
                                         transport)
    elif decision.outcome in ("FAILED", "INCOMPLETE"):
        recovery = _escalate_from_history(git, history, request, transport)
        if recovery:
            report["escalation"] = recovery
            if decision.outcome == "FAILED":
                report["outcome"] = "ESCALATE"
                prior = _substantive_prior(history)
                report["tier"] = (prior.get("tier") if prior else None) or "AMBER"
                decision = Decision("ESCALATE", tier=report.get("tier"))
    elif decision.outcome == "MERGED":
        report["merged_into"] = decision.detail.get("merged_into")
        report["paths"] = decision.detail.get("paths", [])

    # Successor chain (freestyle → bake): git is the bus. Same execution
    # that owned the first role starts the next iff the required file is
    # on the pushed branch.
    chain = successors_mod.after(role_id)
    if chain and evidence and evidence.branch:
        sid = handoff_mod.session_id_from(trigger, context)
        try:
            fetch_branch(git, evidence.branch)
        except Exception as exc:  # noqa: BLE001
            report["successor"] = {
                "spawned": False,
                "reason": f"branch not on origin: {type(exc).__name__}: {exc}",
            }
        else:
            status = successors_mod.satisfied(
                chain.require, git, evidence.branch, sid)
            report["handoff"] = status
            if status.get("ok"):
                report["bake"] = _spawn_bake(
                    subsystem, sid, evidence.branch, status, context,
                    repo=repo, successor_role=chain.spawn)
            else:
                report["bake"] = {
                    "spawned": False,
                    "reason": f"{chain.require} incomplete; not spawning {chain.spawn}",
                }

    report["finished_at"] = _now()

    # One timeline row per turn, so the operator can read what has been
    # happening without opening a run archive. Best-effort by construction.
    timeline.decided(
        subsystem, role.id, role.tier,
        verdict=decision.tier or "",
        outcome=decision.outcome,
        branch=(evidence.branch if evidence else ""),
        files=len(decision.detail.get("paths", []) or []),
        paths=list(decision.detail.get("paths", []) or [])[:20],
        headline=(trigger.get("detail") or trigger.get("metric") or "")[:200],
        ref=agent_id,
    )
    # Archive every attempt's evidence, not only the last row. A timed-out
    # follow-up poll would otherwise erase the branch and tool stream from
    # the run that actually did the work.
    archive_evidence = evidence
    for row in reversed(history):
        prior = row.get("evidence")
        if prior and (prior.branch or prior.tool_calls or prior.result_text):
            archive_evidence = prior
            break
    runlog.dump(subsystem, agent_id, {
        "role": role_id, "subsystem": subsystem, "trigger": trigger,
        "prompt": prompt, "report": report,
        "attempts_evidence": [
            {
                "attempt": row["attempt"],
                "status": row["evidence"].status,
                "branch": row["evidence"].branch,
                "result_text": row["evidence"].result_text,
                "tool_calls": row["evidence"].tool_calls,
                "shell_commands": row["evidence"].shell_commands(),
                "usage": row["evidence"].usage,
                "duration_ms": row["evidence"].duration_ms,
            }
            for row in history
        ],
        "result_text": archive_evidence.result_text if archive_evidence else "",
        "tool_calls": archive_evidence.tool_calls if archive_evidence else [],
        "shell_commands": (archive_evidence.shell_commands()
                           if archive_evidence else []),
        "usage": archive_evidence.usage if archive_evidence else {},
        "duration_ms": archive_evidence.duration_ms if archive_evidence else 0,
    })
    return report


# ═══════════════════════════════════════════════════════════════════ master

def run_master(subsystem: str, *, dry_run: bool = False) -> dict:
    """The master reads escalations, wagers, and either merges or waits."""
    from harness import docket as docket_mod, master_loop
    from harness.docket import Docket, DocketItem, compose_remit, new_cycle_id

    base = workspace.base_branch(subsystem)
    repo = workspace.ensure(subsystem, base=base)
    registry = _registry(subsystem, repo)
    roles = registry.for_subsystem(subsystem, tier="master")
    if not roles:
        return {"subsystem": subsystem,
                "skipped": f"no master definition in {subsystem}/agents/"}

    entries = ledger.open_entries(subsystem)
    if not entries:
        return {"subsystem": subsystem, "skipped": "nothing escalated"}

    role = roles[0]
    trigger = {
        "metric": "escalations",
        "value": len(entries),
        "detail": ledger.docket_block(entries),
    }
    report: dict = {"role": role.id, "subsystem": subsystem, "base": base,
                    "started_at": _now(), "trigger": trigger,
                    "escalations_considered": [e.id for e in entries]}

    if dry_run:
        report["would_spawn"] = True
        return report

    _record_spawn(subsystem, role, trigger)
    git = RepoPort(repo, base=base)
    skeleton = _skeleton(subsystem)
    items = [
        DocketItem(
            id=e.id, kind="proposal",
            summary=f"[{e.tier}] {e.role}: {(e.note or '')[:110]}",
            detail={"tier": e.tier, "agent": e.role,
                    "has_diff": bool(e.branch),
                    "branch": e.branch,
                    "proposal_paths": list(e.proposal_paths or [])},
            paths=list(e.files or []),
        )
        for e in entries
    ]
    docket = Docket(
        cycle_id=new_cycle_id(), subsystem=subsystem, created=_now(),
        triggers=[{"id": "MT1", "reason": f"{len(entries)} escalations"}],
        items=items,
        remit=compose_remit([{"id": "MT1"}]),
    )
    prompt = render(role, RoleContext(
        trigger=trigger,
        brief=_brief(repo, role, trigger),
        signature_history={"closures": _closures_for(repo, role.signature)},
    ), registry.orientation, registry.scale_framework)

    request = SpawnRequest(
        role=role.id, subsystem=subsystem, repo_url=role.repos[0],
        starting_ref=git.current_base(), prompt=prompt, trigger=trigger,
        env_vars=role.env_vars(),
        model=role.model or os.getenv("TURNSTILE_AGENT_MODEL", DEFAULT_AGENT_MODEL),
        signature=role.signature,
    )

    def announce(turn, phase, evidence) -> None:
        print(f"[{subsystem}] master turn {turn} ({phase}): "
              f"{evidence.status}", flush=True)

    result = master_loop.run(
        request, CursorRestTransport(), repo, skeleton, git,
        docket=docket, cycle_id=docket.cycle_id,
        max_wait_s=_max_wait(), on_turn=announce,
    )
    report.update(result.as_dict())
    report["finished_at"] = _now()

    if result.outcome in ("MERGED", "HOLD", "AWAITING_OPERATOR"):
        for entry in entries:
            ledger.close(entry, disposed_by=result.agent_id or "unknown",
                         outcome=result.outcome)
        report["closed"] = len(entries)
    elif result.outcome == "INCOMPLETE":
        report["closed"] = 0
        report["escalations_remain"] = [e.id for e in entries]
        # Re-write the incomplete card so the still-open escalation ids
        # are on the operator board, not only in the run archive.
        report["notify"] = notify.raise_incomplete(
            subsystem,
            sub_reason=str((result.detail or {}).get("sub_reason")
                           or "unknown"),
            agent_id=result.agent_id or "",
            branch=result.branch or "",
            detail=result.detail or {},
            escalations=[e.id for e in entries],
        )

    runlog.dump(subsystem, result.agent_id or "master", {
        "role": role.id, "subsystem": subsystem, "trigger": trigger,
        "prompt": prompt, "report": report,
    })
    return report


# ═════════════════════════════════════════════════════════════════════ main

def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(prog="cloud.agent_job")
    parser.add_argument("--subsystem", required=True,
                        choices=sorted(workspace.REPOS))
    parser.add_argument("--role", help="role id; omit with --master")
    parser.add_argument("--master", action="store_true",
                        help="dispose of this subsystem's open escalations")
    parser.add_argument("--trigger", default="{}",
                        help="JSON: what this agent was woken by")
    parser.add_argument("--context", default="",
                        help="JSON: the mechanical run record, for orientation")
    parser.add_argument("--dry-run", action="store_true")
    parser.add_argument("--json", action="store_true")
    args = parser.parse_args(argv)

    try:
        trigger = json.loads(args.trigger or "{}")
        context = json.loads(args.context) if args.context else None
    except json.JSONDecodeError as exc:
        print(f"bad --trigger/--context JSON: {exc}", file=sys.stderr)
        return 2

    try:
        if args.master:
            report = run_master(args.subsystem, dry_run=args.dry_run)
        elif args.role:
            report = run_agent(args.role, args.subsystem, trigger, context,
                               dry_run=args.dry_run)
        else:
            print("--role is required unless --master is given", file=sys.stderr)
            return 2
    except Exception as exc:                           # noqa: BLE001
        traceback.print_exc()
        print(json.dumps({"subsystem": args.subsystem, "role": args.role,
                          "error": f"{type(exc).__name__}: {exc}"}, indent=2))
        return 1

    print(json.dumps(report, indent=2, default=str))
    # Cloud Run "success" with FAILED inside is how the closed loop looked
    # healthy while every PIT died. Only terminal success / intentional wait
    # / dry-run exit 0.
    outcome = str(report.get("outcome") or report.get("skipped") or "")
    if args.dry_run or report.get("would_spawn") or report.get("skipped"):
        return 0
    if outcome in ("MERGED", "ESCALATE", "HOLD", "AWAITING_OPERATOR",
                   "NO_CHANGE"):
        return 0
    if outcome in ("FAILED", "INCOMPLETE"):
        return 1
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
