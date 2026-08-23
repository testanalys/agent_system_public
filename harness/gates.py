"""Master gates - G-HIND, G-INDEP, G-DISP, G-POR.

Four checks, all of them set comparisons over logs the harness already
keeps, and all of them re-prompting in session rather than rejecting
after the fact.

None of them constrain how the master reasons. G-HIND does not say what
to conclude from the history, only that the history was opened. G-INDEP
does not say what to test, only that something was tested that could have
come out the other way. That distinction is the whole design: we mandate
the notebook, not the method.
"""
from __future__ import annotations

from dataclasses import dataclass, field
from pathlib import Path

from . import docket as docket_mod
from . import proposal as proposal_mod
from . import evidence as evidence_mod
from . import records, reprompts


@dataclass
class GateResult:
    gate: str
    passed: bool
    reprompt: str = ""
    detail: dict = field(default_factory=dict)


def _passed(gate: str) -> GateResult:
    return GateResult(gate=gate, passed=True)


# =============================================================== G-HIND

def hindsight(evidence, docket: docket_mod.Docket,
              repo: Path | None = None) -> GateResult:
    """Did this session actually open the history of what it is changing?"""
    seen = evidence_mod.coverage(evidence)
    seen_paths = set(seen["paths"])
    seen_sigs = set(seen["signatures"])

    wanted_paths: set[str] = set()
    wanted_sigs: set[str] = set()
    for item in docket.items:
        wanted_paths.update(_top_dirs(item.paths))
        if item.signature:
            wanted_sigs.add(item.signature)

    missing_paths = sorted(
        p for p in wanted_paths
        if not any(p in s or s in p for s in seen_paths)
    )
    missing_sigs = sorted(wanted_sigs - seen_sigs)
    span_needed = docket.oldest_age_days
    span_seen = seen["max_span_days"]

    if missing_paths or missing_sigs or span_seen < span_needed:
        return GateResult(
            gate="G-HIND", passed=False,
            reprompt=reprompts.hindsight(missing_paths, missing_sigs,
                                         span_needed, span_seen),
            detail={"missing_paths": missing_paths,
                    "missing_signatures": missing_sigs,
                    "span_seen_days": span_seen,
                    "span_needed_days": span_needed},
        )
    return _passed("G-HIND")


def _top_dirs(paths_: list[str], depth: int = 2) -> set[str]:
    """Collapse file paths to the directories worth querying history on."""
    out: set[str] = set()
    for path in paths_:
        parts = [p for p in path.replace("\\", "/").split("/") if p]
        if len(parts) <= 1:
            out.add(path)
        else:
            out.add("/".join(parts[:depth]))
    return out


# ============================================================== G-INDEP

def independence(evidence, docket: docket_mod.Docket,
                 repo: Path | None = None) -> GateResult:
    """Did the master run anything of its own, or only re-run the evidence?

    Only applies when the cycle is adjudicating somebody else's proposal.
    A cycle woken by a trend has nothing to rubber-stamp.
    """
    proposals = [i for i in docket.items if i.kind == "proposal"]
    if not proposals:
        return _passed("G-INDEP")

    ran = {evidence_mod.normalise(c) for c in evidence.shell_commands()}
    if not ran:
        return GateResult(
            gate="G-INDEP", passed=False,
            reprompt=reprompts.independence(proposals[0].id, []),
            detail={"commands_run": 0},
        )

    cited = [str(ref) for item in proposals
             for ref in item.detail.get("evidence_refs", [])]
    novel = evidence_mod.novel_commands(evidence, cited)
    if not novel:
        return GateResult(
            gate="G-INDEP", passed=False,
            reprompt=reprompts.independence(proposals[0].id, sorted(ran)[:6]),
            detail={"commands_run": len(ran), "novel": 0},
        )

    read = set(evidence_mod.coverage(evidence)["diffs_read"])
    unread = [item.id for item in proposals
              if item.detail.get("has_diff") and item.id not in read]
    if unread:
        return GateResult(
            gate="G-INDEP", passed=False,
            reprompt=reprompts.unread_proposal_diff(unread[0]),
            detail={"unread_diffs": unread},
        )
    return _passed("G-INDEP")


# =============================================================== G-DISP

def dispositions(cycle_id: str, docket: docket_mod.Docket,
                 repo: Path | None = None) -> GateResult:
    recorded = docket_mod.load_dispositions(cycle_id, repo)
    missing = [i.id for i in docket.items if i.id not in recorded]
    if missing:
        return GateResult(
            gate="G-DISP", passed=False,
            reprompt=reprompts.dispositions(missing, len(docket.items)),
            detail={"missing": missing, "total": len(docket.items)},
        )
    return _passed("G-DISP")


# ================================================================ G-POR

def plan_proof(bodies: list[proposal_mod.ProposalBody],
               repo: Path) -> GateResult:
    """Every engineering plan a master emits carries its own proof.

    One per plan, not one per cycle. A cycle that produces three plans
    has made three separate trade-offs and each one is reviewed on its
    own terms.
    """
    if not bodies:
        return _passed("G-POR")
    for index, body in enumerate(bodies, start=1):
        try:
            proposal_mod.validate(body, repo)
        except proposal_mod.ProposalInvalid as exc:
            prefix = (
                f"Engineering plan {index} of {len(bodies)} is missing its "
                f"proof of reasoning.\n\n"
                if len(bodies) > 1 else ""
            )
            return GateResult(
                gate="G-POR", passed=False,
                reprompt=prefix + exc.reprompt,
                detail={"plan_index": index, "gate": exc.gate, **exc.detail},
            )
    return _passed("G-POR")


def first_principles(evidence, docket: docket_mod.Docket,
                     repo: Path | None = None) -> GateResult:
    """Soft nudge when the session never opened the usual first-principles tools.

    Not a hard fail on its own — G-HIND / G-INDEP still own hard gates.
    This asks once, gently, whether the obvious next look was skipped.
    """
    cmds = " ".join(evidence.shell_commands() if hasattr(evidence, "shell_commands")
                    else []).lower()
    missing: list[str] = []
    if "history" not in cmds and "git log" not in cmds:
        missing.append(
            "Prior attempts / history — did a similar fix already fail at this level?"
        )
    if "diff" not in cmds and not any(
            "agent_state/proposals" in c for c in (
                evidence.shell_commands() if hasattr(evidence, "shell_commands")
                else [])):
        missing.append(
            "Proposal diffs — the description is intent; the diff is what was necessary."
        )
    if "engineering_plans" not in cmds and "index.md" not in cmds:
        missing.append(
            "Prior engineering plans / INDEX on this branch — already planned or abandoned?"
        )
    scale = repo / "agents" / "_scale_framework.md" if repo is not None else None
    if (scale is not None and scale.exists()
            and "scale_framework" not in cmds):
        missing.append(
            "The operator's scale direction (`agents/_scale_framework.md`) — "
            "is this cycle growing capacity, or patching an instance?"
        )
    # Only nudge when there is a real docket; empty cycles stay quiet.
    if not missing or not docket.items:
        return _passed("G-FP")
    return GateResult(
        gate="G-FP", passed=False,
        reprompt=reprompts.first_principles_nudge(missing),
        detail={"missing": missing},
    )


# ================================================================= runner

def failed(results: list[GateResult],
           *, soft: frozenset[str] = frozenset({"G-FP"})) -> list[GateResult]:
    """Hard failures only. Soft nudges do not force a prove turn."""
    return [r for r in results if not r.passed and r.gate not in soft]


def run_all(evidence, cycle_id: str, docket: docket_mod.Docket,
            plan_bodies: list[proposal_mod.ProposalBody] | None = None,
            repo: Path | None = None) -> list[GateResult]:
    """Every gate, in order, stopping at the first hard failure.

    Soft gates (G-FP) are recorded but do not stop the run — the master
    loop may still surface them as a light refine.
    """
    repo = repo or docket_mod.paths.repo_root()
    results: list[GateResult] = []
    for result in (
        hindsight(evidence, docket, repo),
        independence(evidence, docket, repo),
        first_principles(evidence, docket, repo),
        dispositions(cycle_id, docket, repo),
        plan_proof(plan_bodies or [], repo),
    ):
        results.append(result)
        if not result.passed and result.gate != "G-FP":
            break
    return results
