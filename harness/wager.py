"""The master's bet, written before any code.

A wager is deliberately smaller than an engineering plan. It names a
landmark strategy, a headline, why now, what should move, and which paths
it expects to touch — enough to commit to a direction without rehearsing
the implementation.

    agent_state/wager.json

`hold` is a first-class outcome: the master reviewed the evidence and
concluded acting would be premature. That cycle is complete.
"""
from __future__ import annotations

import json
from dataclasses import asdict, dataclass, field
from pathlib import Path

from . import catalogue, grounding

MODES = ("build_agent", "new_module", "reduce", "repair", "hold")
DIRECTIONS = ("up", "down", "flat")
WAGER_PATH = "agent_state/wager.json"
HEADLINE_CAP = 100


class WagerInvalid(ValueError):
    """Carries the list of problems the agent should fix."""

    def __init__(self, problems: list[str]):
        super().__init__("; ".join(problems))
        self.problems = problems


@dataclass(frozen=True)
class Wager:
    mode: str
    headline: str
    why_now: str = ""
    expected_effect: dict = field(default_factory=dict)
    surface: tuple[str, ...] = ()
    evidence_refs: tuple[str, ...] = ()

    @property
    def holding(self) -> bool:
        return self.mode == "hold"

    def to_dict(self) -> dict:
        data = asdict(self)
        data["surface"] = list(self.surface)
        data["evidence_refs"] = list(self.evidence_refs)
        return data


def from_dict(data: dict) -> Wager:
    if not isinstance(data, dict):
        raise WagerInvalid(["wager must be a JSON object"])
    return Wager(
        mode=str(data.get("mode") or "").strip().lower(),
        headline=str(data.get("headline") or "").strip(),
        why_now=str(data.get("why_now") or "").strip(),
        expected_effect=dict(data.get("expected_effect") or {}),
        surface=tuple(str(p) for p in (data.get("surface") or []) if p),
        evidence_refs=tuple(
            str(r) for r in (data.get("evidence_refs") or []) if r),
    )


def validate(wager: Wager, repo: Path | None = None) -> None:
    """Thin on purpose. Mode, headline, and a real metric when not holding."""
    problems: list[str] = []
    if wager.mode not in MODES:
        problems.append(
            f"mode {wager.mode!r} is not one of: " + ", ".join(MODES))
    if not wager.headline:
        problems.append("headline is empty")
    elif len(wager.headline) > HEADLINE_CAP:
        problems.append(
            f"headline is {len(wager.headline)} chars (cap {HEADLINE_CAP})")
    if not wager.holding:
        if not wager.why_now:
            problems.append("why_now is empty")
        metric = (wager.expected_effect or {}).get("metric")
        direction = (wager.expected_effect or {}).get("direction")
        if not metric:
            problems.append("expected_effect.metric is missing")
        elif repo is not None:
            known = {e["id"] for e in catalogue.catalogue_for(repo).get(
                "metrics", []) if e.get("id")}
            if known and metric not in known:
                problems.append(
                    f"expected_effect.metric {metric!r} is not in "
                    f"lifeblood/catalogue.json")
        if direction and direction not in DIRECTIONS:
            problems.append(
                f"expected_effect.direction {direction!r} is not one of: "
                + ", ".join(DIRECTIONS))
        rationale = (wager.expected_effect or {}).get("rationale")
        problem = grounding.rationale_problem(str(rationale or ""), str(metric or ""))
        if problem:
            problems.append(problem)
        if not wager.surface:
            problems.append(
                "surface is empty — name the paths you expect to touch")
    if problems:
        raise WagerInvalid(problems)


def load_from_branch(git, branch: str, repo: Path | None = None,
                     *, base: str | None = None) -> Wager:
    """Read and validate the wager the master committed.

    ``base`` is the production-branch SHA this session started from. A
    wager identical to the one already on that SHA is leftover from a
    previous cycle — not this session's bet.
    """
    try:
        raw = git.read_file(branch, WAGER_PATH)
    except Exception as exc:  # noqa: BLE001
        raise WagerInvalid(
            [f"{WAGER_PATH} is missing on the branch ({exc})"]) from exc
    if not raw or not str(raw).strip():
        raise WagerInvalid([f"{WAGER_PATH} is empty"])
    if base:
        try:
            prior = git.read_file(base, WAGER_PATH)
        except Exception:  # noqa: BLE001
            prior = ""
        if prior and str(prior).strip() == str(raw).strip():
            raise WagerInvalid([
                f"{WAGER_PATH} is leftover from a previous cycle. "
                "Write this cycle's bet — do not reuse the last one."
            ])
    try:
        data = json.loads(raw)
    except json.JSONDecodeError as exc:
        raise WagerInvalid([f"{WAGER_PATH} is not valid JSON: {exc}"]) from exc
    wager = from_dict(data)
    validate(wager, repo)
    return wager


def load_from_repo(repo: Path) -> Wager | None:
    """Read a wager from a working tree, if present."""
    path = Path(repo) / WAGER_PATH
    if not path.exists():
        return None
    try:
        data = json.loads(path.read_text(encoding="utf-8"))
    except (OSError, json.JSONDecodeError):
        return None
    try:
        wager = from_dict(data)
        validate(wager, repo)
        return wager
    except WagerInvalid:
        return None
