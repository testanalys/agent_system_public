"""`harness` - the command line every agent works through.

The gate is a tool, not a hook. An agent finishes its work and runs
``harness submit``; the classifier decides what happens; and when
something is blocked, the re-prompt is written to stdout - which lands
in the agent's context, in the same session, with all its working state
intact. That is where the second pass of reasoning gets forced.

    harness submit                  classify, check, merge or re-prompt
    harness propose                 turn blocked work into a proposal
    harness withdraw                stand a change down (a real outcome)
    harness evidence --script P     register a new measurement as evidence
    harness catalogue               what this system measures
    harness triggers                evaluate and show master triggers
    harness docket                  compose the master's docket
    harness dispose <id> --approve  record a disposition
    harness plan --plans F          run the master gates
    harness sweep                   post-session backstop
    harness classify --json         CI entry point
"""
from __future__ import annotations

import argparse
import json
import os
import subprocess
import sys
from pathlib import Path

from . import (catalogue, classify as classify_mod, docket as docket_mod,
               gates, oracles, paths, proposal as proposal_mod, records,
               reprompts, triggers as triggers_mod)
from .diff import SessionDiff
from .skeleton import Skeleton, SkeletonError

EXIT_OK = 0
EXIT_BLOCKED = 1
EXIT_ERROR = 2
PROPOSAL_DRAFT = "proposal.draft.json"


def _git(repo: Path, *args: str, check: bool = True) -> str:
    proc = subprocess.run(["git", *args], cwd=repo, capture_output=True, text=True)
    if check and proc.returncode != 0:
        raise RuntimeError(f"git {' '.join(args)}: {proc.stderr.strip()}")
    return proc.stdout.strip()


def _base_branch(skeleton: Skeleton) -> str:
    explicit = (os.getenv("TURNSTILE_BASE_BRANCH") or "").strip()
    if explicit:
        return explicit
    return skeleton.protected_branches[0]


def _current_branch(repo: Path) -> str:
    return _git(repo, "rev-parse", "--abbrev-ref", "HEAD")


def _merge_mode() -> str:
    return (os.getenv("TURNSTILE_MERGE_MODE") or "local").strip().lower()


def _context(args) -> tuple[Path, Skeleton, str, str]:
    repo = paths.repo_root()
    skeleton = Skeleton.load(repo)
    agent = getattr(args, "agent", None) or records.agent_id()
    session = getattr(args, "session", None) or records.session_id()
    return repo, skeleton, agent, session


def _signature_and_closures(repo: Path, signature: str | None) -> tuple[str | None, list[dict]]:
    if not signature:
        return None, []
    registry = paths.state_dir(repo) / "signatures.json"
    if not registry.exists():
        return signature, []
    try:
        data = json.loads(registry.read_text(encoding="utf-8"))
    except json.JSONDecodeError:
        return signature, []
    for sig in data.get("signatures", []):
        if sig.get("id") == signature:
            return signature, sig.get("closures", [])
    return signature, []


# =============================================================== submit

def cmd_submit(args) -> int:
    repo, skeleton, agent, session = _context(args)
    base = _base_branch(skeleton)
    branch = _current_branch(repo)

    if branch in skeleton.protected_branches and _merge_mode() == "local":
        print(f"You are on {branch}. Work happens on a branch; create one "
              f"and re-run.", file=sys.stderr)
        return EXIT_ERROR

    _commit_pending(repo, args.message or f"{agent}: work in progress")

    diff = SessionDiff.collect(repo, base)
    if diff.is_empty:
        print("Nothing to submit - no net change against "
              f"{base}. If you intended to change something, check "
              "`git status`; if you concluded no change was needed, that is "
              "a complete outcome and the session can end.")
        records.update_session_record(session, repo, agent=agent,
                                      outcome="NO_CHANGE", branch=branch)
        return EXIT_OK

    signature, closures = _signature_and_closures(repo, args.signature)
    attempt = records.next_attempt_number(session, repo)
    result = classify_mod.classify(
        diff, agent, skeleton, signature=signature, closures=closures,
        attempt=attempt,
    )

    attempt_row = records.Attempt(
        ts=records.now(), session=session, agent=agent,
        role=records.agent_role(), tier=result.tier, attempt=attempt,
        files=diff.files_touched, net_loc=diff.net_loc, paths=diff.paths,
        signals=result.signals, zones=result.zones, signature=signature,
        head=_git(repo, "rev-parse", "HEAD"),
    )

    if result.tier != classify_mod.GREEN:
        records.record_attempt(attempt_row, repo)
        triggers_mod.persist(triggers_mod.attempt_triggers(repo), repo)
        records.update_session_record(
            session, repo, agent=agent, outcome="BLOCKED",
            tier=result.tier, branch=branch, attempt=attempt,
        )
        _commit_state(repo, base)
        print(result.reprompt)
        return EXIT_BLOCKED

    # ---- green: the acceptance checks still have to pass -------------
    check = oracles.run_oracle(result.oracle, diff.paths, repo,
                               result.oracle_timeout_s)
    if not check.ok and not check.skipped:
        attempt_row.tier = classify_mod.AMBER
        attempt_row.signals = [{"id": "ORACLE", "detail": check.reason
                                or "acceptance check failed"}]
        records.record_attempt(attempt_row, repo)
        zone = result.zones[0] if result.zones else "(unzoned)"
        print(reprompts.oracle_failed(zone, check.command, check.output))
        return EXIT_BLOCKED

    if result.requires_tests:
        if not diff.touches_tests:
            records.record_attempt(attempt_row, repo)
            untested = [p for p in diff.paths if p.endswith(".py")]
            print(reprompts.missing_tests(untested))
            return EXIT_BLOCKED
        suite = oracles.run_tests(repo, os.getenv("TURNSTILE_TEST_COMMAND"))
        if not suite.ok:
            records.record_attempt(attempt_row, repo)
            print(reprompts.tests_failed(suite.command, suite.output))
            return EXIT_BLOCKED

    merged_into = _merge(repo, base, branch) if _merge_mode() == "local" else base
    attempt_row.outcome = "merged"
    records.record_attempt(attempt_row, repo)
    records.update_session_record(
        session, repo, agent=agent, outcome="MERGED", branch=branch,
        merged_into=merged_into, files=diff.files_touched,
    )
    _commit_state(repo, base)
    print(reprompts.green(diff.paths,
                          check.command if not check.skipped else None,
                          merged_into))
    return EXIT_OK


def _commit_pending(repo: Path, message: str) -> None:
    """Commit the agent's work, leaving harness bookkeeping to _commit_state."""
    scope = [":(exclude)agent_state", ":(exclude).agent_tmp"]
    if not _git(repo, "status", "--porcelain", "--", *scope):
        return
    _git(repo, "add", "-A", "--", *scope)
    subprocess.run(["git", "commit", "-m", message], cwd=repo,
                   capture_output=True, text=True)


def _commit_state(repo: Path, base: str | None = None) -> None:
    """Commit the harness's own records, on the branch that survives.

    Two reasons this is not a plain commit on the current branch:

    · left uncommitted, attempt logs block the agent's next checkout with
      a dirty-tree error that has nothing to do with its work;
    · committed to the agent's short-lived branch, the record of a blocked
      attempt disappears the moment that branch does — and a blocked
      attempt is precisely the thing that must outlive the session, since
      it is what escalates to a master even when the agent later reduces
      the change to a clean merge.

    So the record lands on the base branch. It is excluded from every diff
    (``diff.EXCLUDED_PATHS``), so it can never inflate anyone's footprint.
    """
    branch = _current_branch(repo)
    _write_state_commit(repo)
    if not base or base == branch or _merge_mode() != "local":
        return

    # Carry the record to the base branch by snapshot-and-restore rather
    # than by stashing. Stash is stateful and fails quietly on edge cases;
    # losing a blocked attempt would silently disable the escalation this
    # whole design turns on. A file copy either works or raises.
    snapshot = _snapshot_state(repo)
    if not snapshot:
        return
    _git(repo, "checkout", base, check=False)
    try:
        _restore_state(repo, snapshot)
        _write_state_commit(repo)
    finally:
        _git(repo, "checkout", branch, check=False)


def _snapshot_state(repo: Path) -> dict[str, bytes]:
    root = repo / paths.STATE_DIRNAME
    if not root.is_dir():
        return {}
    return {
        str(path.relative_to(root)).replace("\\", "/"): path.read_bytes()
        for path in root.rglob("*") if path.is_file()
    }


def _restore_state(repo: Path, snapshot: dict[str, bytes]) -> None:
    """Write the snapshot over the base branch's copy.

    The snapshot is the base's own state plus this session's appends -
    the branch was cut from base and the harness only ever appends - so
    overwriting cannot drop rows that the branch did not already carry.
    """
    root = repo / paths.STATE_DIRNAME
    for relative, data in snapshot.items():
        target = root / relative
        target.parent.mkdir(parents=True, exist_ok=True)
        target.write_bytes(data)


def _write_state_commit(repo: Path) -> None:
    if not _git(repo, "status", "--porcelain", "--", paths.STATE_DIRNAME):
        return
    _git(repo, "add", "-A", "--", paths.STATE_DIRNAME, check=False)
    subprocess.run(["git", "commit", "-m", "harness: record"], cwd=repo,
                   capture_output=True, text=True)


def _merge(repo: Path, base: str, branch: str) -> str:
    _git(repo, "checkout", base)
    try:
        _git(repo, "merge", "--no-ff", "-m",
             f"harness: merge {branch} (green lane)", branch)
    except RuntimeError:
        _git(repo, "merge", "--abort", check=False)
        _git(repo, "checkout", branch, check=False)
        raise
    if os.getenv("TURNSTILE_PUSH", "").lower() in ("1", "true", "yes"):
        subprocess.run(["git", "push", "origin", base], cwd=repo,
                       capture_output=True, text=True)
    _git(repo, "checkout", branch, check=False)
    return base


# ============================================================== propose

def cmd_propose(args) -> int:
    repo, skeleton, agent, session = _context(args)
    base = _base_branch(skeleton)
    draft_path = paths.scratch_dir(repo) / PROPOSAL_DRAFT

    if args.file:
        draft_path = Path(args.file).resolve()

    if not draft_path.exists():
        diff = SessionDiff.collect(repo, base)
        attempts = records.attempts_this_session(session, repo)
        tier = attempts[-1]["tier"] if attempts else "AMBER"
        signals = attempts[-1]["signals"] if attempts else []
        template = proposal_mod.template(skeleton.subsystem, tier, signals)
        draft_path.parent.mkdir(parents=True, exist_ok=True)
        draft_path.write_text(json.dumps(template, indent=2) + "\n",
                              encoding="utf-8")
        print(_propose_intro(draft_path, diff))
        return EXIT_BLOCKED

    try:
        body = proposal_mod.ProposalBody.from_file(draft_path)
    except (json.JSONDecodeError, TypeError) as exc:
        print(f"{draft_path} is not valid proposal JSON: {exc}\n\n"
              "Fix the file and run `harness propose` again.", file=sys.stderr)
        return EXIT_ERROR

    try:
        proposal_mod.validate(body, repo)
    except proposal_mod.ProposalInvalid as exc:
        print(exc.reprompt)
        return EXIT_BLOCKED

    diff = SessionDiff.collect(repo, base)
    attempts = records.attempts_this_session(session, repo)
    last = attempts[-1] if attempts else {}
    pid = records.new_proposal_id(repo)

    proposal = records.Proposal(
        id=pid, subsystem=skeleton.subsystem, agent=agent,
        role=records.agent_role(), session=session,
        tier=last.get("tier", "AMBER"), created=records.now(),
        branch=_current_branch(repo),
        head=_git(repo, "rev-parse", "HEAD"), base=diff.base,
        signature=last.get("signature"),
        classify={
            "signals": last.get("signals", []),
            "summary": {
                "zones": last.get("zones", []),
                "paths": diff.paths,
                "files": diff.files_touched,
                "net_loc": diff.net_loc,
            },
        },
        body=body.to_dict(),
        evidence_scripts=_registered_scripts(repo, session),
    )
    target = records.save_proposal(proposal, diff.patch, repo)
    records.mark_attempt_outcome(session, "proposed", pid, repo)
    triggers_mod.persist(triggers_mod.attempt_triggers(repo), repo)
    records.update_session_record(session, repo, agent=agent,
                                  outcome="PROPOSED", proposal=pid)
    draft_path.unlink(missing_ok=True)
    _commit_state(repo, base)

    fire, pending, _ = triggers_mod.should_fire(repo)
    print(f"PROPOSED - {pid}\n\n"
          f"  {target}\n"
          f"  diff preserved: {len(diff.patch.splitlines())} lines across "
          f"{diff.files_touched} file(s)\n"
          f"  trade-off: {body.tradeoff}\n")
    print("  A master review is " + ("queued for this cycle." if fire
                                     else "not yet triggered; this proposal "
                                          "will be picked up when it is."))
    print("\nYour work here is done. End the session.")
    return EXIT_OK


def _propose_intro(draft: Path, diff: SessionDiff) -> str:
    return "\n".join([
        f"A proposal draft has been written for you:\n\n    {draft}\n",
        reprompts._wrap(f"""
Fill in four fields, then run `harness propose` again. Your diff
({diff.files_touched} files, {diff.net_loc:+} lines) is already captured and
travels with the proposal - do not describe the code, describe the change.

  change     what changes, in one or two plain sentences. A reader who has
             not seen your diff should understand what will be different.

  why_now    the evidence, in prose. What did you observe, in what data,
             over what window? Numbers beat adjectives.

  evidence_refs
             pointers a reader can open: file paths, metric ids
             (metric:diarization_quality), signatures (sig:S1), scripts
             you wrote (script:tools/evidence/x.py). If your evidence has
             already been used to argue this before, build a new
             measurement instead - see `harness evidence --help`.

  tradeoff   at most 30 words. What gets worse, what gets better, which
             objective moves. This is the field a human actually reads
             before deciding, and writing it honestly is the point of the
             exercise - if you cannot name a cost, look harder.

  expected_effect
             {{"metric": "<id from `harness catalogue`>", "direction":
             "up|down|flat"}}. How anyone will know whether this worked.
"""),
    ])


def _registered_scripts(repo: Path, session: str) -> list[str]:
    path = paths.scratch_dir(repo) / "evidence_scripts.json"
    if not path.exists():
        return []
    try:
        return json.loads(path.read_text(encoding="utf-8")).get(session, [])
    except json.JSONDecodeError:
        return []


# ============================================================= withdraw

def cmd_withdraw(args) -> int:
    repo, _, agent, session = _context(args)
    if args.proposal:
        proposal = records.load_proposal(args.proposal, repo)
        if not proposal:
            print(f"No proposal {args.proposal}.", file=sys.stderr)
            return EXIT_ERROR
        proposal.status = "withdrawn"
        proposal.verdict = {"verdict": "withdrawn", "by": agent,
                            "reason": args.reason, "ts": records.now()}
        records.save_proposal(proposal, repo=repo)
    records.mark_attempt_outcome(session, "withdrawn", repo=repo)
    records.update_session_record(session, repo, agent=agent,
                                  outcome="WITHDRAWN", reason=args.reason)
    print("WITHDRAWN - recorded.\n\n"
          + reprompts._wrap("""
Standing a change down after examining it is a real outcome and it is
logged as one, not as a failed session. The reasoning is preserved in the
attempt log, so the next agent that reaches this problem can see that
somebody got here, looked, and decided against - which is more useful than
an empty history.
"""))
    return EXIT_OK


# ============================================================= evidence

def cmd_evidence(args) -> int:
    repo, _, _, session = _context(args)
    script = Path(args.script)
    resolved = script if script.is_absolute() else (repo / script)
    if not resolved.exists():
        print(f"No such script: {resolved}\n\n"
              + reprompts._wrap("""
Write the script first, commit it, then register it. It has to be
committed because the point is that a reviewer - and the next agent to
meet this problem - can re-run it.
"""), file=sys.stderr)
        return EXIT_ERROR

    rel = str(resolved.relative_to(repo)).replace("\\", "/")
    store = paths.scratch_dir(repo) / "evidence_scripts.json"
    data = {}
    if store.exists():
        try:
            data = json.loads(store.read_text(encoding="utf-8"))
        except json.JSONDecodeError:
            data = {}
    data.setdefault(session, [])
    if rel not in data[session]:
        data[session].append(rel)
    store.write_text(json.dumps(data, indent=2) + "\n", encoding="utf-8")

    if args.run:
        result = oracles.run_oracle(f"python {rel}", [], repo, 600)
        print(f"$ python {rel}\n{result.output}")

    print(f"Registered as evidence: script:{rel}\n\n"
          "Cite it in the proposal as:  \"script:" + rel + "\"")
    return EXIT_OK


# ============================================================ catalogue

def cmd_catalogue(args) -> int:
    metrics = catalogue.all_metrics()
    if args.json:
        print(json.dumps(metrics, indent=2))
        return EXIT_OK
    print(f"{'id':28} {'family':7} {'lens':8} {'target':>8}  intent")
    print("-" * 100)
    for metric in metrics:
        target = metric.get("target")
        print(f"{metric['id']:28} {metric.get('family', ''):7} "
              f"{metric.get('lens', ''):8} "
              f"{('' if target is None else target):>8}  "
              f"{metric.get('intent', '')[:52]}")
    return EXIT_OK


# ============================================================= triggers

def cmd_triggers(args) -> int:
    repo = paths.repo_root()
    found = triggers_mod.evaluate(repo)
    if not args.dry_run:
        triggers_mod.persist(found, repo)
    fire, pending, why_not = triggers_mod.should_fire(repo)

    if args.json:
        print(json.dumps({
            "fire": fire,
            "evaluated": [t.__dict__ for t in found],
            "pending": pending,
            "why_not": why_not,
        }, indent=2, default=str))
        return EXIT_OK

    if not pending:
        print("No triggers set. The subsystem is running itself; no master "
              "cycle is needed.")
        return EXIT_OK
    print(f"{len(pending)} trigger(s) set:")
    for trigger in pending:
        print(f"  [{trigger['id']}] {trigger['reason']}")
        print(f"      first seen {trigger['first_seen'][:16]}, "
              f"count {trigger.get('count', 1)}")
    print("\n" + ("MASTER CYCLE DUE." if fire else f"Holding: {why_not}"))
    return EXIT_OK


# =============================================================== docket

def cmd_docket(args) -> int:
    repo = paths.repo_root()
    fire, pending, why_not = triggers_mod.should_fire(repo)
    if not fire and not args.force:
        print(json.dumps({"fire": False, "why_not": why_not}, indent=2)
              if args.json else f"No master cycle due: {why_not}")
        return EXIT_OK

    built = docket_mod.build(repo)
    docket_mod.save_docket(built, repo)

    if args.json:
        print(json.dumps(built.to_dict(), indent=2, default=str))
        return EXIT_OK

    print(f"=== {built.cycle_id} - {built.subsystem} ===")
    print(f"\nWHY YOU ARE HERE: {built.remit['headline']}")
    print(reprompts._wrap(built.remit["instruction"], indent="  "))
    for also in built.remit.get("also", []):
        print(f"\nALSO: {also['headline']}")
        print(reprompts._wrap(also["instruction"], indent="  "))
    print(f"\nDEPTH EXPECTED: {built.remit['depth']}")

    print(f"\n=== docket ({len(built.items)} items) ===")
    for item in built.items:
        print(f"\n  [{item.id}] {item.kind}  ({item.age_days}d old)")
        print(f"      {item.summary}")
        if item.detail.get("tradeoff"):
            print(f"      trade-off claimed: {item.detail['tradeoff']}")
        if item.detail.get("has_diff"):
            print(f"      diff available:    history diff --proposal {item.id}")
    print("\n=== metrics ===")
    for metric in built.metrics:
        print(f"  {metric['id']:26} {metric['value']:>10}  "
              f"Δ7d {metric['d7']}  Δ28d {metric['d28']}")
    print("\nEvery item needs a disposition before this cycle can close:")
    print("  harness dispose <id> --approve|--revise|--reject|--defer --needs ...")
    return EXIT_OK


def cmd_dispose(args) -> int:
    repo = paths.repo_root()
    cycle_id = args.cycle or docket_mod.latest_cycle_id(repo)
    if not cycle_id:
        print("No open cycle. Run `harness docket` first.", file=sys.stderr)
        return EXIT_ERROR
    verdict = next(v for v in docket_mod.VALID_DISPOSITIONS
                   if getattr(args, v, False))
    try:
        docket_mod.save_disposition(cycle_id, args.item, verdict,
                                    args.reason or "", args.needs or "", repo)
    except ValueError as exc:
        print(str(exc), file=sys.stderr)
        return EXIT_BLOCKED
    print(f"{args.item}: {verdict}")
    return EXIT_OK


# =================================================================== plan

def cmd_plan(args) -> int:
    repo, _, agent, session = _context(args)
    cycle_id = args.cycle or docket_mod.latest_cycle_id(repo)
    built = docket_mod.load_docket(cycle_id, repo) if cycle_id else None
    if not built:
        print("No docket for this cycle. Run `harness docket` first.",
              file=sys.stderr)
        return EXIT_ERROR

    bodies: list[proposal_mod.ProposalBody] = []
    for plan_file in args.plans or []:
        path = Path(plan_file)
        if not path.exists():
            print(f"No such plan proof: {path}", file=sys.stderr)
            return EXIT_ERROR
        bodies.append(proposal_mod.ProposalBody.from_file(path))

    results = gates.run_all(session, cycle_id, built, bodies, repo)
    for result in results:
        if not result.passed:
            print(result.reprompt)
            return EXIT_BLOCKED

    outcome = "PLANNED" if bodies else "ADJUDICATED"
    records.record_cycle(cycle_id, outcome,
                         [t["id"] for t in built.triggers],
                         len(built.items), repo)
    records.clear_triggers([t["id"] for t in built.triggers], cycle_id, repo)
    for item in built.items:
        if item.kind == "proposal":
            proposal = records.load_proposal(item.id, repo)
            if proposal:
                disposition = docket_mod.load_dispositions(cycle_id, repo).get(item.id, {})
                proposal.status = "adjudicated"
                proposal.verdict = {**disposition, "by": agent,
                                    "cycle": cycle_id}
                records.save_proposal(proposal, repo=repo)

    print(f"CYCLE COMPLETE - {cycle_id} ({outcome})\n")
    print(f"  {len(built.items)} item(s) disposed")
    print(f"  {len(bodies)} engineering plan(s), each with its proof")
    for result in results:
        print(f"  {result.gate}: passed")
    print("\nPlans go to the operator for approval. Triggers cleared.")
    return EXIT_OK


# ================================================================== sweep

def cmd_sweep(args) -> int:
    """Backstop for a session that ended without calling submit."""
    repo, skeleton, agent, session = _context(args)
    base = _base_branch(skeleton)
    if records.attempts_this_session(session, repo):
        print("Session already submitted; nothing to sweep.")
        return EXIT_OK

    _commit_pending(repo, f"{agent}: swept uncommitted work")
    diff = SessionDiff.collect(repo, base)
    if diff.is_empty:
        records.update_session_record(session, repo, agent=agent,
                                      outcome="NO_CHANGE", swept=True)
        print("Swept: no change. Session recorded as NO_CHANGE.")
        return EXIT_OK

    result = classify_mod.classify(diff, agent, skeleton, attempt=1)
    records.record_attempt(records.Attempt(
        ts=records.now(), session=session, agent=agent,
        role=records.agent_role(), tier=result.tier, attempt=1,
        files=diff.files_touched, net_loc=diff.net_loc, paths=diff.paths,
        signals=result.signals, zones=result.zones,
        outcome="swept", head=_git(repo, "rev-parse", "HEAD"),
    ), repo)

    if result.tier == classify_mod.GREEN:
        records.update_session_record(session, repo, agent=agent,
                                      outcome="INCOMPLETE", swept=True,
                                      tier="GREEN")
        print("Swept: change is green but unverified (the session never ran "
              "its acceptance checks). Left unmerged for the next cycle.")
        return EXIT_OK

    pid = records.new_proposal_id(repo)
    records.save_proposal(records.Proposal(
        id=pid, subsystem=skeleton.subsystem, agent=agent,
        role=records.agent_role(), session=session, tier=result.tier,
        created=records.now(), branch=_current_branch(repo),
        head=_git(repo, "rev-parse", "HEAD"), base=diff.base,
        classify={"signals": result.signals,
                  "summary": {"paths": diff.paths, "files": diff.files_touched,
                              "net_loc": diff.net_loc}},
        body={"change": "(swept - the session ended before it explained itself)",
              "why_now": "", "evidence_refs": [], "tradeoff": "",
              "expected_effect": {}},
    ), diff.patch, repo)
    triggers_mod.persist(triggers_mod.attempt_triggers(repo), repo)
    records.update_session_record(session, repo, agent=agent,
                                  outcome="INCOMPLETE", swept=True,
                                  proposal=pid, tier=result.tier)
    _commit_state(repo, base)
    print(f"Swept: {result.tier} change preserved as {pid}. No work lost.")
    return EXIT_OK


# =========================================================== classify/CI

def cmd_classify(args) -> int:
    repo = paths.repo_root()
    skeleton = Skeleton.load(repo)
    agent = args.agent or records.agent_id()
    base = args.base or _base_branch(skeleton)
    diff = SessionDiff.collect(repo, base, args.head)
    signature, closures = _signature_and_closures(repo, args.signature)
    result = classify_mod.classify(diff, agent, skeleton, signature, closures)

    if args.json:
        print(json.dumps(result.to_dict(), indent=2, default=str))
    else:
        print(f"{result.tier}  ({diff.files_touched} files, "
              f"{diff.net_loc:+} lines, agent={agent})")
        for signal in result.signals:
            print(f"  [{signal['id']}] {signal['detail']}")
    return EXIT_OK if result.tier == classify_mod.GREEN else EXIT_BLOCKED


def cmd_status(args) -> int:
    repo = paths.repo_root()
    skeleton = Skeleton.load(repo)
    attempts = records.load_attempts(repo, days=14)
    open_proposals = records.list_proposals(repo, status="open")
    _, pending, why_not = triggers_mod.should_fire(repo)
    print(f"subsystem      {skeleton.subsystem}")
    print(f"zones          {len(skeleton.zones)} "
          f"({sum(1 for z in skeleton.zones if z.green_for)} grant green)")
    print(f"attempts 14d   {len(attempts)} "
          f"({sum(1 for a in attempts if a['tier'] == 'GREEN')} green, "
          f"{sum(1 for a in attempts if a['tier'] == 'AMBER')} amber, "
          f"{sum(1 for a in attempts if a['tier'] == 'RED')} red)")
    print(f"open proposals {len(open_proposals)}")
    print(f"triggers       {len(pending)}" + (f" - {why_not}" if why_not else ""))
    days = records.days_since_master_cycle(repo)
    print(f"last master    {'never' if days is None else f'{days}d ago'}")
    return EXIT_OK


# ================================================================= parser

def build_parser() -> argparse.ArgumentParser:
    parser = argparse.ArgumentParser(
        prog="harness", description="Submit work, propose changes, run cycles.")
    parser.add_argument("--agent", help="override TURNSTILE_AGENT_ID")
    parser.add_argument("--session", help="override TURNSTILE_SESSION_ID")
    sub = parser.add_subparsers(dest="command", required=True)

    submit = sub.add_parser("submit", help="classify, check and merge or re-prompt")
    submit.add_argument("-m", "--message", help="commit message for pending work")
    submit.add_argument("--signature", help="failure signature this addresses")
    submit.set_defaults(func=cmd_submit)

    propose = sub.add_parser("propose", help="turn blocked work into a proposal")
    propose.add_argument("--file", help="path to a filled-in draft")
    propose.set_defaults(func=cmd_propose)

    withdraw = sub.add_parser("withdraw", help="stand a change down")
    withdraw.add_argument("--proposal")
    withdraw.add_argument("--reason", default="")
    withdraw.set_defaults(func=cmd_withdraw)

    evidence = sub.add_parser("evidence", help="register a measurement as evidence")
    evidence.add_argument("--script", required=True)
    evidence.add_argument("--run", action="store_true", help="execute it now")
    evidence.set_defaults(func=cmd_evidence)

    cat = sub.add_parser("catalogue", help="what this system measures")
    cat.add_argument("--json", action="store_true")
    cat.set_defaults(func=cmd_catalogue)

    trig = sub.add_parser("triggers", help="evaluate master triggers")
    trig.add_argument("--json", action="store_true")
    trig.add_argument("--dry-run", action="store_true")
    trig.set_defaults(func=cmd_triggers)

    dock = sub.add_parser("docket", help="compose the master docket")
    dock.add_argument("--json", action="store_true")
    dock.add_argument("--force", action="store_true")
    dock.set_defaults(func=cmd_docket)

    disp = sub.add_parser("dispose", help="record a disposition")
    disp.add_argument("item")
    disp.add_argument("--cycle")
    for verdict in docket_mod.VALID_DISPOSITIONS:
        disp.add_argument(f"--{verdict}", action="store_true")
    disp.add_argument("--reason", default="")
    disp.add_argument("--needs", default="", help="required for --defer")
    disp.set_defaults(func=cmd_dispose)

    plan = sub.add_parser("plan", help="run the master gates and close the cycle")
    plan.add_argument("--cycle")
    plan.add_argument("--plans", nargs="*", help="one proof file per plan")
    plan.set_defaults(func=cmd_plan)

    sweep = sub.add_parser("sweep", help="post-session backstop")
    sweep.set_defaults(func=cmd_sweep)

    cls = sub.add_parser("classify", help="CI entry point")
    cls.add_argument("--base")
    cls.add_argument("--head", default="HEAD")
    cls.add_argument("--signature")
    cls.add_argument("--json", action="store_true")
    cls.set_defaults(func=cmd_classify)

    status = sub.add_parser("status", help="one-screen subsystem state")
    status.set_defaults(func=cmd_status)
    return parser


def _force_utf8_output() -> None:
    """A re-prompt that cannot be encoded is a re-prompt the agent never sees.

    The text is ASCII by construction, but agent output, oracle output and
    file paths are not, and they get echoed back through these same
    streams. Never let an encoding error swallow the enforcement message.
    """
    for stream in (sys.stdout, sys.stderr):
        try:
            stream.reconfigure(encoding="utf-8", errors="replace")
        except (AttributeError, ValueError):
            pass


def main(argv: list[str] | None = None) -> int:
    _force_utf8_output()
    args = build_parser().parse_args(argv)
    try:
        return args.func(args)
    except (paths.HarnessPathError, SkeletonError) as exc:
        print(str(exc), file=sys.stderr)
        return EXIT_ERROR
    except RuntimeError as exc:
        print(f"harness: {exc}", file=sys.stderr)
        return EXIT_ERROR


if __name__ == "__main__":
    raise SystemExit(main())
