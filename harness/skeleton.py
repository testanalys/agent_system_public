"""Zone resolution — the agent-conditional exoskeleton.

A zone is a property of (path, agent), not of a path alone. This module
loads ``skeleton.json`` from a code repo and answers, for a given changed
path and a given agent identity: is this RED, GREEN-eligible, or AMBER?

Resolution order (first decisive answer wins):

    1. ``red`` rules            -> RED, categorically, for every agent
    2. most specific ``zones``  -> GREEN-eligible if the agent is listed
                                   in ``green_for`` and ``deny_self`` is
                                   not violated
    3. default                  -> AMBER (default-deny)

"GREEN-eligible" is not GREEN. Shape limits, history signals, the oracle
and the test suite all still have to pass; that composition lives in
``classify``.
"""
from __future__ import annotations

import fnmatch
import json
from dataclasses import dataclass, field
from pathlib import Path
from typing import Any

from . import paths

DEFAULT_LIMITS: dict[str, int] = {
    "files": 3,
    "net_loc": 80,
    "gross_churn": 200,
    "new_files_in_zone": 1,
    "new_files_outside_zone": 0,
    "new_dependencies": 0,
    "new_config_keys": 0,
    "signature_changes": 0,
    "branch_tokens": 10,
    "nesting_depth": 1,
    "dir_population": 15,
    "filename_similarity": 0.8,
}


class SkeletonError(RuntimeError):
    """Raised when skeleton.json is missing or self-inconsistent."""


@dataclass(frozen=True)
class RedRule:
    id: str
    paths: tuple[str, ...]
    reason: str
    applies_to: tuple[str, ...] = ("*",)
    except_for: tuple[str, ...] = ()

    def matches(self, path: str) -> bool:
        return any(fnmatch.fnmatch(path, pattern) for pattern in self.paths)

    def applies(self, agent_id: str) -> bool:
        """Whether this rule is categorical for this agent.

        Masters can be carved out of a path that stays 100 for PIT
        (e.g. metric instruments). Default is everyone.
        """
        if any(fnmatch.fnmatch(agent_id, pattern) for pattern in self.except_for):
            return False
        if not self.applies_to or "*" in self.applies_to:
            return True
        return any(fnmatch.fnmatch(agent_id, pattern) for pattern in self.applies_to)


@dataclass(frozen=True)
class Zone:
    id: str
    paths: tuple[str, ...]
    green_for: tuple[str, ...]
    oracle: str | None = None
    oracle_timeout_s: int = 600
    limits: dict[str, int] = field(default_factory=dict)
    deny_self: bool = False
    append_only: bool = False
    touch_cost: int = 0
    serves: str = ""
    note: str = ""

    def matches(self, path: str) -> bool:
        return any(fnmatch.fnmatch(path, pattern) for pattern in self.paths)

    def specificity(self, path: str) -> int:
        """Longest matching pattern wins — a crude but stable ordering."""
        return max(
            (len(p) for p in self.paths if fnmatch.fnmatch(path, p)),
            default=0,
        )

    def allows(self, agent_id: str) -> bool:
        return any(fnmatch.fnmatch(agent_id, pattern) for pattern in self.green_for)

    def is_self_reference(self, path: str, agent_id: str) -> bool:
        """True when the path belongs to the agent making the change.

        Deliberately loose: any path containing the agent's own id as a
        path segment or filename stem is treated as self-modification.
        A false positive here costs one review; a false negative lets an
        agent rewrite its own instructions.
        """
        if not self.deny_self:
            return False
        stem = agent_id.lower()
        parts = [p.lower() for p in Path(path).parts]
        if stem in parts:
            return True
        return Path(path).stem.lower() == stem


@dataclass(frozen=True)
class Verdict:
    """Per-path zone resolution."""
    path: str
    zone: Zone | None
    red_rule: RedRule | None
    green_eligible: bool
    reason: str

    @property
    def is_red(self) -> bool:
        return self.red_rule is not None


@dataclass(frozen=True)
class Skeleton:
    repo: Path
    subsystem: str
    red_rules: tuple[RedRule, ...]
    zones: tuple[Zone, ...]
    limits: dict[str, int]
    protected_branches: tuple[str, ...]

    # ---------------------------------------------------------------- load

    @classmethod
    def load(cls, repo: Path | None = None) -> "Skeleton":
        repo = (repo or paths.repo_root()).resolve()
        path = paths.skeleton_path(repo)
        if not path.exists():
            raise SkeletonError(
                f"No skeleton.json in {repo}. Every code repo needs one; "
                f"without it nothing can be classified and nothing can merge."
            )
        return cls._from_json(json.loads(path.read_text(encoding="utf-8")), repo)

    @classmethod
    def load_file(cls, path: Path, repo: Path | None = None) -> "Skeleton":
        """Load a zone map that lives outside the repo it governs.

        Skeletons live in the agent layer, not in the repo being judged.
        An agent works in a checkout that does not contain its own zone
        map, so it cannot widen its own permissions - which is a stronger
        guarantee than any path rule inside the map could give.
        """
        path = Path(path)
        if not path.exists():
            raise SkeletonError(
                f"No zone map at {path}. Without one nothing can be "
                f"classified and nothing can merge.")
        return cls._from_json(json.loads(path.read_text(encoding="utf-8")),
                              (repo or Path.cwd()).resolve())

    @classmethod
    def _from_json(cls, raw: dict, repo: Path) -> "Skeleton":

        red_rules = tuple(
            RedRule(
                id=r["id"],
                paths=tuple(r["paths"]),
                reason=r["reason"],
                applies_to=tuple(r.get("applies_to") or ("*",)),
                except_for=tuple(r.get("except_for") or ()),
            )
            for r in raw.get("red", [])
        )
        limits = {**DEFAULT_LIMITS, **raw.get("limits", {})}
        zones = tuple(
            Zone(
                id=z["id"],
                paths=tuple(z["paths"]),
                green_for=tuple(z.get("green_for", ())),
                oracle=z.get("oracle"),
                oracle_timeout_s=int(z.get("oracle_timeout_s", 600)),
                limits={**limits, **z.get("limits", {})},
                deny_self=bool(z.get("deny_self", False)),
                append_only=bool(z.get("append_only", False)),
                touch_cost=int(z.get("touch_cost", 0) or 0),
                serves=z.get("serves", ""),
                note=z.get("note", ""),
            )
            for z in raw.get("zones", [])
        )
        skeleton = cls(
            repo=repo,
            subsystem=raw["subsystem"],
            red_rules=red_rules,
            zones=zones,
            limits=limits,
            protected_branches=tuple(raw.get("protected_branches", ["main", "master"])),
        )
        skeleton.lint()
        return skeleton

    # ---------------------------------------------------------------- lint

    def lint(self) -> None:
        """A zone that grants green without an oracle is a config error.

        Degrading to AMBER rather than raising would hide the mistake, so
        this is loud. The fail-safe direction is toward review either way.
        """
        broken = [z.id for z in self.zones if z.green_for and not z.oracle]
        if broken:
            raise SkeletonError(
                "Zones grant green_for but declare no oracle: "
                + ", ".join(broken)
                + ". A plate without an acceptance test is not a plate. "
                "Either add an oracle or remove green_for."
            )
        seen: set[str] = set()
        for z in self.zones:
            if z.id in seen:
                raise SkeletonError(f"Duplicate zone id: {z.id}")
            seen.add(z.id)

    # ------------------------------------------------------------ resolve

    def resolve(self, path: str, agent_id: str) -> Verdict:
        norm = path.replace("\\", "/")

        for rule in self.red_rules:
            if rule.matches(norm) and rule.applies(agent_id):
                return Verdict(norm, None, rule, False, rule.reason)

        candidates = [z for z in self.zones if z.matches(norm)]
        if not candidates:
            return Verdict(
                norm, None, None, False,
                "outside every declared zone",
            )

        zone = max(candidates, key=lambda z: z.specificity(norm))

        if zone.is_self_reference(norm, agent_id):
            return Verdict(
                norm, zone, None, False,
                f"defines or configures {agent_id} itself",
            )
        if not zone.allows(agent_id):
            return Verdict(
                norm, zone, None, False,
                f"zone '{zone.id}' is not open to {agent_id}",
            )
        return Verdict(norm, zone, None, True, f"zone '{zone.id}'")

    def zone_for(self, path: str) -> Zone | None:
        norm = path.replace("\\", "/")
        candidates = [z for z in self.zones if z.matches(norm)]
        if not candidates:
            return None
        return max(candidates, key=lambda z: z.specificity(norm))

    def limits_for(self, zone: Zone | None) -> dict[str, int]:
        return zone.limits if zone else self.limits
