"""Path resolution for the harness.

Two roots, deliberately separate:

  * **layer root** - where this package lives. Always resolvable,
    because it is derived from this file.
  * **plans root** - an optional sibling checkout holding long-form
    engineering plans. **Optional is load-bearing.** It exists on a
    workstation and does not exist in the reconciler's container.

Conflating the two is what made the first containerised tick fail: every
subsystem died looking for a plans directory while doing work that had
nothing to do with plans. Anything that genuinely needs it says so and
degrades when it is absent.

Workstation layout:

    <workspace>/
        turnstile/            <- this package
        agent_plans/          <- long-form plans (optional)
        <your-repo>/          <- a repo agents work in
        <your-other-repo>/    <- another one

Container layout: this package at /app, repos cloned to
$TURNSTILE_WORKSPACE. No plans directory.

Set TURNSTILE_PLANS_ROOT to put the plans directory somewhere else.

Per code repo:

    skeleton.json                 <- zone config (committed)
    agent_state/                  <- durable harness artifacts (committed)
        proposals/<id>/
        attempts.jsonl
        triggers.json
        sessions/<session_id>.json
    .agent_tmp/                   <- session scratch (gitignored)
"""
from __future__ import annotations

import os
import subprocess
from pathlib import Path

CONTEXT_MARKER = "PROTOCOL.md"
SKELETON_FILENAME = "skeleton.json"
STATE_DIRNAME = "agent_state"
SCRATCH_DIRNAME = ".agent_tmp"


class HarnessPathError(RuntimeError):
    """Raised when a required root cannot be resolved."""


def layer_root() -> Path:
    """Root of this repository - where the harness itself lives."""
    return Path(__file__).resolve().parents[1]


def find_context_root() -> Path | None:
    """`agent_plans`, if this environment has one. Never raises."""
    override = (os.getenv("TURNSTILE_CONTEXT_ROOT") or "").strip()
    if override:
        path = Path(override).expanduser().resolve()
        return path if path.is_dir() else None

    for start in (layer_root(), Path.cwd().resolve()):
        for candidate in (start, *start.parents):
            sibling = candidate / "agent_plans"
            if (sibling / CONTEXT_MARKER).exists():
                return sibling
    return None


def context_root() -> Path:
    """`agent_plans`, or raise. Only for callers that truly need it."""
    found = find_context_root()
    if found is None:
        raise HarnessPathError(
            "Cannot locate agent_plans. Set TURNSTILE_CONTEXT_ROOT. "
            "Only engineering-plan lookups need it; nothing else should "
            "be calling this.")
    return found


def workspace_root() -> Path:
    """Directory holding the code repos.

    In the container that is `TURNSTILE_WORKSPACE`, where the reconciler
    clones them. On a workstation it is the parent of this repo.
    """
    override = (os.getenv("TURNSTILE_WORKSPACE") or "").strip()
    if override:
        path = Path(override).expanduser().resolve()
        if path.is_dir():
            return path
    return layer_root().parent


def repo_root(start: Path | str | None = None) -> Path:
    """Git root of the repo we are currently operating in."""
    cwd = Path(start).resolve() if start else Path.cwd().resolve()
    try:
        out = subprocess.run(
            ["git", "rev-parse", "--show-toplevel"],
            cwd=cwd, capture_output=True, text=True, check=True,
        ).stdout.strip()
    except (subprocess.CalledProcessError, FileNotFoundError) as exc:
        raise HarnessPathError(f"Not inside a git repository: {cwd}") from exc
    return Path(out).resolve()


def skeleton_path(repo: Path | None = None) -> Path:
    return (repo or repo_root()) / SKELETON_FILENAME


def state_dir(repo: Path | None = None) -> Path:
    path = (repo or repo_root()) / STATE_DIRNAME
    path.mkdir(parents=True, exist_ok=True)
    return path


def proposals_dir(repo: Path | None = None) -> Path:
    path = state_dir(repo) / "proposals"
    path.mkdir(parents=True, exist_ok=True)
    return path


def sessions_dir(repo: Path | None = None) -> Path:
    path = state_dir(repo) / "sessions"
    path.mkdir(parents=True, exist_ok=True)
    return path


def scratch_dir(repo: Path | None = None) -> Path:
    path = (repo or repo_root()) / SCRATCH_DIRNAME
    path.mkdir(parents=True, exist_ok=True)
    return path


def code_repos() -> list[Path]:
    """Every checked-out code repo, identified by having a lifeblood catalogue.

    Was `skeleton.json`, which no code repo carries any more - the zone
    maps moved into this repo so that an agent cannot edit the map that
    bounds it. Matching on the old marker silently returned an empty list,
    which made every cross-repo metric lookup quietly find nothing.
    """
    root = workspace_root()
    if not root.is_dir():
        return []
    return sorted(
        p for p in root.iterdir()
        if p.is_dir() and (p / "lifeblood" / "catalogue.json").exists()
    )


def engineering_plans_dir() -> Path:
    path = context_root() / "engineering_plans"
    path.mkdir(parents=True, exist_ok=True)
    return path
