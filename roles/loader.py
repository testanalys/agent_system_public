"""Build the registry by reading a repo, not by importing this package.

Roles used to be Python objects constructed at import time, which meant
the set of agents that existed was a property of the operating system.
It is a property of the subsystem: a pipeline agent is pipeline knowledge
and it changes when the pipeline changes.

So the registry is built from a checkout. ``load(repo)`` reads every
``agents/*.md`` in the repo the agent is about to work on, which is
already on disk by the time anything needs it - ``workspace.ensure()``
runs before the prompt is composed.

Two properties this ordering buys, both of which used to be impossible:

**A master can add an agent.** A new file in ``agents/`` is a new role
the next spawn can resolve, so building an investigator is an ordinary
code change judged by the ordinary classifier, rather than an edit to a
repository the master cannot see.

**The operating system holds no subsystem knowledge.** Nothing under
``roles/`` names a metric, a failure mode or a domain. What is left is
the shape of a role and the order its prompt is assembled in.
"""
from __future__ import annotations

from pathlib import Path

from . import definition as defn
from .base import Registry, Role
from .models import model_for_tier

AGENTS_DIRNAME = "agents"
ORIENTATION_FILENAME = "_orientation.md"
SCALE_FRAMEWORK_FILENAME = "_scale_framework.md"


class NoAgentsDirectory(FileNotFoundError):
    """The repo carries no agent definitions."""


def agents_dir(repo: Path) -> Path:
    return Path(repo) / AGENTS_DIRNAME


def definition_files(repo: Path) -> list[Path]:
    """Every definition in the repo. ``_`` prefixed files are not roles."""
    directory = agents_dir(repo)
    if not directory.is_dir():
        return []
    return sorted(p for p in directory.glob("*.md")
                  if not p.name.startswith("_"))


def load_orientation(repo: Path) -> dict[str, str]:
    path = agents_dir(repo) / ORIENTATION_FILENAME
    if not path.exists():
        return {}
    try:
        return defn.parse_orientation(path.read_text(encoding="utf-8"))
    except OSError:
        return {}


def load_scale_framework(repo: Path) -> str:
    path = agents_dir(repo) / SCALE_FRAMEWORK_FILENAME
    if not path.exists():
        return ""
    try:
        return path.read_text(encoding="utf-8")
    except OSError:
        return ""


def to_role(item: defn.AgentDefinition, *, repo_url: str = "",
            subsystem: str = "") -> Role:
    """A parsed definition plus the decisions that are not the repo's.

    The repository and the model come from here rather than from the
    file, because both are reach: a definition that named its own repo
    would let an agent aim the next spawn at somewhere it was never
    granted, and one that named its own model would let it promote
    itself onto the expensive tier.
    """
    return Role(
        id=item.name,
        tier=item.tier,
        subsystem=item.subsystem or subsystem,
        summary=item.description,
        repos=(repo_url,) if repo_url else (),
        model=model_for_tier(item.tier),
        body=item.body,
        signature=item.signature,
        metrics=item.metrics,
        capture=item.capture,
    )


def load(repo: Path, *, repo_url: str = "", subsystem: str = "",
         strict: bool = True) -> Registry:
    """Every agent this repo declares.

    ``strict`` raises on the first unreadable definition. That is the
    right default on the spawn path: a subsystem whose agent set does
    not parse should fail loudly at the moment it is asked for, not
    spawn a partial fleet nobody notices is partial.
    """
    registry = Registry()
    for path in definition_files(repo):
        try:
            item = defn.load_file(path)
        except defn.DefinitionError:
            if strict:
                raise
            continue
        registry.add(to_role(item, repo_url=repo_url, subsystem=subsystem))
    registry.orientation = load_orientation(repo)
    registry.scale_framework = load_scale_framework(repo)
    return registry
