"""Get the data repos onto the reconciler's disk.

The reconciler needs a real checkout of each subsystem's repo. Not to run
its code - to do git work: fetch the branch an agent pushed, rebase it
onto current head, re-classify against the post-merge tree, run the
zone's oracle, and push the merge.

`SYSTEM.md` §8 rejects cloning application code
at container start, and that rejection stands: it applies to the code an
image is supposed to *be*, where cloning destroys the image as a
reproducible artifact. This is the opposite case. A merge queue's working
copy is not an artifact; it is scratch space that must be current by
definition, because merging into a stale checkout is how you overwrite
somebody's work.

Credentials come from `GH_TOKEN` in Secret Manager, injected by the job's
service account. The token is used to build a remote URL and is never
written to disk in the clone's config - a token in `.git/config` outlives
the container in any image layer or debug dump that captures it.
"""
from __future__ import annotations

import json
import os
import subprocess
from functools import lru_cache
from pathlib import Path

# Subsystems are declared in config/subsystems.json, not here. One repo,
# one deploy branch, one zone map each.
#
# The branch is declared per repo rather than assumed to be `main`. A
# reconciler that merged agent work into a branch nothing deploys from
# would report every merge as successful and change nothing in
# production - the same shipped-but-inert failure the deploy contract
# exists to catch.
#
# The harness repo itself is deliberately absent from this map: it is
# never cloned into a workspace an agent can reach, which is the whole
# reason it is a separate repository.

CONFIG_PATH = Path(__file__).resolve().parents[1] / "config" / "subsystems.json"


class NotConfigured(RuntimeError):
    """Raised when this deployment has no subsystems declared yet."""


def _load_config() -> dict:
    override = (os.getenv("TURNSTILE_SUBSYSTEMS") or "").strip()
    path = Path(override).expanduser() if override else CONFIG_PATH
    if not path.exists():
        raise NotConfigured(
            f"No subsystem config at {path}. Copy config/subsystems.json, "
            f"fill in your repositories, or set TURNSTILE_SUBSYSTEMS. "
            f"See INSTRUCTIONS.md.")
    raw = json.loads(path.read_text(encoding="utf-8"))
    return {k: v for k, v in raw.items() if not k.startswith("_")}


@lru_cache(maxsize=1)
def _subsystems() -> dict[str, dict]:
    return _load_config().get("subsystems") or {}


@lru_cache(maxsize=1)
def _identity() -> tuple[str, str]:
    ident = _load_config().get("commit_identity") or {}
    return (ident.get("name") or "turnstile-reconciler",
            ident.get("email") or "reconciler@example.invalid")


def _entry(subsystem: str) -> dict:
    try:
        return _subsystems()[subsystem]
    except KeyError:
        known = ", ".join(sorted(_subsystems())) or "none"
        raise ValueError(
            f"subsystem {subsystem!r} is not declared in the subsystem "
            f"config. Declared: {known}.") from None


# Kept as mappings so callers that iterate keep working.
REPOS = {name: cfg["repo"] for name, cfg in _subsystems().items()}
BRANCHES = {name: cfg.get("branch", "main")
            for name, cfg in _subsystems().items()}


def skeleton_path(subsystem: str) -> Path:
    """The zone map governing this subsystem.

    It lives here, in the harness repo, not in the repo it governs - so an
    agent working in a checkout cannot widen its own permissions.
    """
    rel = _entry(subsystem).get("skeleton") or f"skeletons/{subsystem}.json"
    return Path(__file__).resolve().parents[1] / rel


def base_branch(subsystem: str) -> str:
    """The branch this subsystem deploys from, and therefore merges into."""
    return _entry(subsystem).get("branch", "main")


def repo_url(subsystem: str) -> str:
    """The repo an agent of this subsystem is spawned against.

    Token-free on purpose: this string goes into the spawn body, crosses
    the Cursor API and sits in the agent record. The authenticated form
    is `_remote_url`, and it stays on this side.

    It is resolved here rather than declared in an agent's definition
    because the definitions are agent-writable, and one that could name
    its own repository would let an agent aim the next spawn somewhere it
    was never granted.
    """
    return f"https://github.com/{_entry(subsystem)['repo']}"

WORKSPACE = Path(os.getenv("TURNSTILE_WORKSPACE", "/workspace"))


def _remote_url(slug: str) -> str:
    token = (os.getenv("GH_TOKEN") or os.getenv("GITHUB_TOKEN") or "").strip()
    if token:
        return f"https://x-access-token:{token}@github.com/{slug}.git"
    return f"https://github.com/{slug}.git"


def _git(*args: str, cwd: Path | None = None) -> subprocess.CompletedProcess:
    return subprocess.run(["git", *args], cwd=str(cwd) if cwd else None,
                          capture_output=True, text=True)


def ensure(subsystem: str, *, base: str | None = None) -> Path:
    """A checkout of one subsystem's repo, current with its remote.

    Clones on first use, otherwise fetches and hard-resets. The reset is
    deliberate: a tick that crashed mid-rebase leaves the working copy on
    a detached head with a half-applied patch, and inheriting that state
    into the next tick would corrupt an unrelated merge.
    """
    base = base or base_branch(subsystem)
    slug = _entry(subsystem)["repo"]

    WORKSPACE.mkdir(parents=True, exist_ok=True)
    path = WORKSPACE / slug.split("/")[-1]
    url = _remote_url(slug)

    if not (path / ".git").exists():
        # Blobless partial clone. An agent execution is short-lived and
        # starts with an empty disk, so the whole history's file contents
        # get transferred to compute one diff and one rebase - which is
        # both slow and how a large repo hits `fetch-pack: early EOF` on a
        # cold container. Commits and trees still come down in full, so
        # rebase, merge and ls-tree behave identically; the blobs a diff
        # actually needs are fetched on demand.
        result = _git("clone", "--quiet", "--filter=blob:none", "--no-tags",
                      url, str(path))
        if result.returncode != 0:
            raise RuntimeError(
                f"clone {slug} failed: {_redact(result.stderr)}")
    else:
        _git("remote", "set-url", "origin", url, cwd=path)

    for args in (("fetch", "origin", base, "--force", "--prune"),
                 ("checkout", "-f", base),
                 ("reset", "--hard", f"origin/{base}"),
                 ("clean", "-fdx", "-e", "agent_state")):
        result = _git(*args, cwd=path)
        if result.returncode != 0 and args[0] in ("fetch", "reset"):
            raise RuntimeError(
                f"{args[0]} in {slug} failed: {_redact(result.stderr)}")

    _configure_identity(path)
    return path


def _configure_identity(path: Path) -> None:
    """Commits the reconciler makes are attributed to the reconciler.

    A merge that appears to come from a person is a merge somebody will
    later be asked about and will not remember making.
    """
    name, email = _identity()
    _git("config", "user.name", name, cwd=path)
    _git("config", "user.email", email, cwd=path)


def _redact(text: str) -> str:
    """Never let a token reach a log line."""
    token = (os.getenv("GH_TOKEN") or os.getenv("GITHUB_TOKEN") or "").strip()
    out = (text or "").strip()[-400:]
    return out.replace(token, "***") if token else out


def available() -> dict[str, bool]:
    """Which subsystems this deployment can actually act on."""
    return {name: bool(_remote_url(cfg["repo"]))
            for name, cfg in _subsystems().items()}
