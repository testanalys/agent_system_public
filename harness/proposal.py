"""The proposal - four fields, and the gates that interrogate them.

A proposal is written when a change is blocked, or when an agent has a
case and no diff. The diff already shows *what*; this exists to make the
agent reconcile its intent into human-readable prose grounded in evidence
and in the system's design principles, which is itself a second pass over
the logic.

    change            what changes, in plain language
    why_now           the evidence, in prose
    evidence_refs     pointers a reader can open
    tradeoff          <=30 words: what gets worse, what gets better,
                      which objective moves
    expected_effect   {metric, direction, rationale} - how we will know,
                      and why the product needs it now

Three gates, all of which re-prompt rather than reject.
"""
from __future__ import annotations

import json
import re
from dataclasses import dataclass, field
from pathlib import Path

from . import catalogue, grounding, keywords, paths, records, reprompts

DIRECTIONS = ("up", "down", "flat")
REQUIRED_TEXT_FIELDS = ("change", "why_now", "tradeoff")
_REF_PREFIX = re.compile(r"^(metric|sig|script|run|ep|proposal|doc):(.+)$", re.I)
_PATH_REF = re.compile(r"^(?P<path>[^\s:#]+?)(?::(?P<line>\d+(?:-\d+)?))?(?:#.*)?$")


class ProposalInvalid(Exception):
    """Carries the re-prompt text the agent should see."""

    def __init__(self, reprompt: str, gate: str, detail: dict | None = None):
        super().__init__(f"{gate}: proposal incomplete")
        self.reprompt = reprompt
        self.gate = gate
        self.detail = detail or {}


@dataclass
class ProposalBody:
    change: str = ""
    why_now: str = ""
    evidence_refs: list[str] = field(default_factory=list)
    tradeoff: str = ""
    expected_effect: dict = field(default_factory=dict)
    irreducible_because: str = ""      # set when the agent used its re-submit

    @classmethod
    def from_file(cls, path: Path) -> "ProposalBody":
        data = json.loads(path.read_text(encoding="utf-8"))
        return cls.from_dict(data)

    @classmethod
    def from_dict(cls, data: dict) -> "ProposalBody":
        known = {f for f in cls.__dataclass_fields__}
        return cls(**{k: v for k, v in data.items() if k in known})

    def to_dict(self) -> dict:
        return {
            "change": self.change,
            "why_now": self.why_now,
            "evidence_refs": list(self.evidence_refs),
            "tradeoff": self.tradeoff,
            "expected_effect": dict(self.expected_effect),
            "irreducible_because": self.irreducible_because,
        }


# ======================================================= evidence resolution

def _search_roots(repo: Path) -> list[Path]:
    roots = [repo]
    # Separately, and in this order: a missing context repo must not stop
    # the code repos being searched. When these shared one try block, the
    # raise from context_root() skipped code_repos() entirely and every
    # cross-repo evidence reference failed to resolve.
    context = paths.find_context_root()
    if context is not None:
        roots.append(context)
    try:
        roots.extend(p for p in paths.code_repos() if p != repo)
    except Exception:                              # noqa: BLE001
        pass
    return roots


def resolve_ref(ref: str, repo: Path) -> tuple[bool, str]:
    """(resolved, reason). Forgiving about form, strict about existence."""
    ref = ref.strip()
    if not ref:
        return False, "empty reference"

    prefixed = _REF_PREFIX.match(ref)
    if prefixed:
        kind, value = prefixed.group(1).lower(), prefixed.group(2).strip()
        if kind == "metric":
            return (catalogue.known_metric(value, repo),
                    "not in the lifeblood catalogue")
        if kind == "sig":
            return (_known_signature(value, repo),
                    "no run record mentions this signature")
        if kind == "script":
            return _resolve_path(value, repo)
        if kind == "run":
            return (bool(records.load_session_record(value, repo)),
                    "no session record with that id")
        if kind in ("ep", "proposal"):
            if kind == "ep":
                hits = list(paths.engineering_plans_dir().glob(f"{value}*"))
                return bool(hits), "no engineering plan with that id"
            return (records.load_proposal(value, repo) is not None,
                    "no proposal with that id")
        if kind == "doc":
            return _resolve_path(value, repo)

    return _resolve_path(ref, repo)


def _resolve_path(ref: str, repo: Path) -> tuple[bool, str]:
    match = _PATH_REF.match(ref)
    if not match:
        return False, "not a recognisable path or reference"
    rel = match.group("path")
    for root in _search_roots(repo):
        candidate = (root / rel)
        if candidate.exists():
            return True, ""
        hits = list(root.glob(f"**/{Path(rel).name}"))
        if hits and Path(rel).name == rel:
            return True, ""
    return False, "path does not exist in any repo"


def _known_signature(sig: str, repo: Path) -> bool:
    registry = paths.state_dir(repo) / "signatures.json"
    if registry.exists():
        try:
            data = json.loads(registry.read_text(encoding="utf-8"))
            if sig in {s.get("id") for s in data.get("signatures", [])}:
                return True
        except json.JSONDecodeError:
            pass
    return any(sig == a.get("signature") for a in records.load_attempts(repo))


def prior_uses(refs: list[str], repo: Path, exclude: str | None = None) -> list[dict]:
    """Evidence already cited by an earlier proposal."""
    out: list[dict] = []
    wanted = {r.strip() for r in refs}
    for proposal in records.list_proposals(repo):
        if proposal.id == exclude:
            continue
        cited = set(proposal.body.get("evidence_refs", []))
        for ref in sorted(wanted & cited):
            out.append({"ref": ref, "cited_by": proposal.id,
                        "status": proposal.status})
    return out


# ================================================================== gates

def gate_evidence(body: ProposalBody, repo: Path,
                  exclude: str | None = None) -> None:
    """P1 - references resolve, and stale evidence gets challenged."""
    if not body.evidence_refs:
        raise ProposalInvalid(
            reprompts.proposal_evidence(
                [{"ref": "(none supplied)", "reason": "the proposal cites nothing"}],
                [],
            ),
            gate="P1",
        )

    unresolved = []
    for ref in body.evidence_refs:
        ok, reason = resolve_ref(ref, repo)
        if not ok:
            unresolved.append({"ref": ref, "reason": reason})

    reused = prior_uses(body.evidence_refs, repo, exclude=exclude)
    all_reused = reused and len(reused) >= len(body.evidence_refs)
    has_fresh_script = any(
        r.strip().lower().startswith("script:") for r in body.evidence_refs
    )

    if unresolved or (all_reused and not has_fresh_script):
        raise ProposalInvalid(
            reprompts.proposal_evidence(unresolved, reused if all_reused else []),
            gate="P1",
            detail={"unresolved": unresolved, "reused": reused},
        )


def gate_metric(body: ProposalBody, repo: Path) -> None:
    """P2 - the expected effect names something the system collects."""
    effect = body.expected_effect or {}
    metric = str(effect.get("metric", "")).strip()
    direction = str(effect.get("direction", "")).strip().lower()

    if not metric or not catalogue.known_metric(metric, repo):
        raise ProposalInvalid(
            reprompts.proposal_metric(metric or "(empty)",
                                      catalogue.metric_ids(repo)),
            gate="P2",
            detail={"metric": metric},
        )
    if direction not in DIRECTIONS:
        raise ProposalInvalid(
            f"expected_effect.direction must be one of {', '.join(DIRECTIONS)} "
            f"(got '{direction}'). Which way should this metric move if the "
            f"change works?",
            gate="P2",
            detail={"direction": direction},
        )
    problem = grounding.rationale_problem(
        str(effect.get("rationale") or ""), metric)
    if problem:
        raise ProposalInvalid(
            reprompts.proposal_purpose(),
            gate="P2",
            detail={"rationale": str(effect.get("rationale") or "")},
        )


def gate_tradeoff(body: ProposalBody) -> None:
    """P3 - <=30 words, and it must engage more than one axis."""
    result = keywords.check(body.tradeoff)
    if result["empty"] or result["over_word_cap"] or not result["enough_coverage"]:
        raise ProposalInvalid(
            reprompts.proposal_tradeoff(result, body.tradeoff),
            gate="P3",
            detail=result,
        )


def gate_completeness(body: ProposalBody) -> None:
    missing = [f for f in REQUIRED_TEXT_FIELDS if not str(getattr(body, f)).strip()]
    if missing:
        raise ProposalInvalid(
            "The proposal is missing: " + ", ".join(missing) + ".\n\n"
            "  change    - what changes, in one or two plain sentences\n"
            "  why_now   - the evidence, in prose; what you observed and where\n"
            "  tradeoff  - <=30 words: what gets worse, what gets better\n\n"
            "Fill them in and run `harness propose` again.",
            gate="P0",
            detail={"missing": missing},
        )


def validate(body: ProposalBody, repo: Path, exclude: str | None = None) -> None:
    """Run every gate in order. The first failure carries its re-prompt.

    Order is deliberate: completeness, then evidence, then measurability,
    then the trade-off. The trade-off gate is last because it is the one
    that asks the agent to re-examine the change itself, and that question
    is only worth asking once the change is coherently described.
    """
    gate_completeness(body)
    gate_evidence(body, repo, exclude=exclude)
    gate_metric(body, repo)
    gate_tradeoff(body)


def template(subsystem: str, tier: str, signals: list[dict]) -> dict:
    """Skeleton file the agent fills in, pre-seeded with what we know."""
    tripped = "; ".join(s["detail"] for s in signals[:4])
    return {
        "change": "",
        "why_now": "",
        "evidence_refs": [],
        "tradeoff": "",
        "expected_effect": {"metric": "", "direction": "", "rationale": ""},
        "irreducible_because": "",
        "_hint": {
            "subsystem": subsystem,
            "tier": tier,
            "classifier_said": tripped,
            "tradeoff_cap_words": keywords.MAX_WORDS,
            "tradeoff_must_touch": f"at least {keywords.MIN_GROUPS} of: "
                                   + ", ".join(g.id for g in keywords.GROUPS),
        },
    }
