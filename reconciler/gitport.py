"""Git operations for adjudication - a merge queue, in about eighty lines.

The established answer to "several async workers changing one codebase"
is a merge queue: serialise candidates, rebase each onto the *current*
head, re-run the checks against the post-merge state, and merge only if
they still pass. GitHub's merge queue, Bors and Zuul all do this. Our
reconciler already serialises (one tick, one loop), so the rest is here.

**Re-running classification after the rebase is the non-negotiable part.**
A change that was GREEN against the ref its agent started from can be
AMBER against current main - another agent's merge may have pushed a file
over the churn threshold, or made this the third edit to one region.
Classifying against the original base would be a lie the moment two
agents overlap.

Nothing here trusts the agent runtime. We recorded the base ourselves at
spawn; we fetch the branch and compute the diff with our own git. Cursor
tells us only which branch to look at.
"""
from __future__ import annotations

import subprocess
import time
from pathlib import Path

from harness import oracles, reprompts
from harness.diff import SessionDiff


class MergeConflict(RuntimeError):
    pass


class RepoPort:
    """Implements agentloop.GitPort against a real checkout."""

    def __init__(self, repo: Path, remote: str = "origin",
                 base: str = "main", test_command: str | None = None):
        self.repo = Path(repo).resolve()
        self.remote = remote
        self.base = base
        self.test_command = test_command
        self._rebased: dict[str, str] = {}

    # ------------------------------------------------------------- git

    def _git(self, *args: str, check: bool = True) -> str:
        proc = subprocess.run(["git", *args], cwd=self.repo,
                              capture_output=True, text=True)
        if check and proc.returncode != 0:
            raise RuntimeError(f"git {' '.join(args)}: {proc.stderr.strip()}")
        return proc.stdout.strip()

    def fetch(self, branch: str) -> None:
        self._git("fetch", self.remote, f"{branch}:refs/agent/{branch}",
                  "--force")

    def fetch_until(self, branch: str, timeout_s: int = 120,
                    interval_s: float = 5) -> None:
        """Wait for Cursor's push to show up on origin.

        A finished run that names a branch GitHub does not have yet is a
        timing gap, not a thinking failure. Bounded wait; then fail.
        """
        deadline = time.monotonic() + timeout_s
        last: Exception | None = None
        while True:
            try:
                self.fetch(branch)
                return
            except Exception as exc:  # noqa: BLE001
                last = exc
                if time.monotonic() >= deadline:
                    raise RuntimeError(
                        f"branch {branch!r} not on {self.remote} after "
                        f"{timeout_s}s: {exc}") from last
                time.sleep(interval_s)

    def current_base(self) -> str:
        self._git("fetch", self.remote, self.base, "--force")
        return self._git("rev-parse", f"{self.remote}/{self.base}")

    def collect_diff(self, base: str, branch: str) -> SessionDiff:
        """Diff the agent's work against the head it will actually land on.

        `base` is what the agent started from; we deliberately ignore it
        in favour of the current head, because that is the state the
        change has to be correct against.
        """
        ref = self._local_ref(branch)
        return SessionDiff.collect(self.repo, self.current_base(), ref)

    def _local_ref(self, branch: str) -> str:
        return f"refs/agent/{branch}"

    # -------------------------------------------- reading a pushed branch

    def head_of(self, branch: str) -> str:
        return self._git("rev-parse", self._local_ref(branch))

    def files_under(self, branch: str, prefix: str) -> list[str]:
        """Paths committed on the branch beneath `prefix`.

        This is how an escalation is confirmed: the agent was asked to
        commit its argument, and either the file is in the tree or it is
        not. Asking git is the only check that cannot be talked out of.
        """
        out = self._git("ls-tree", "-r", "--name-only",
                        self._local_ref(branch), "--", prefix, check=False)
        return [line.strip() for line in out.splitlines() if line.strip()]

    def read_file(self, branch: str, path: str) -> str:
        return self._git("show", f"{self._local_ref(branch)}:{path}",
                         check=False)

    # ----------------------------------------------------- merge queue

    def rebase(self, branch: str) -> str:
        """Replay the branch onto current head. Returns the rebased ref."""
        head = self.current_base()
        working = f"agentmerge/{branch.replace('/', '-')}"
        self._git("checkout", "-B", working, self._local_ref(branch))
        proc = subprocess.run(["git", "rebase", head], cwd=self.repo,
                              capture_output=True, text=True)
        if proc.returncode != 0:
            self._git("rebase", "--abort", check=False)
            self._git("checkout", self.base, check=False)
            raise MergeConflict(
                f"{branch} does not replay cleanly onto {head[:8]}: "
                f"{proc.stderr.strip()[:300]}")
        self._rebased[branch] = working
        return working

    def merge_to_base(self, branch: str, base: str) -> str:
        """Replay onto current head and fast-forward the production branch.

        If the push loses because head moved, replay once more. A second
        failure is a conflict for the operator, not a retry loop.
        """
        try:
            return self._ff_push(branch)
        except MergeConflict:
            raise
        except RuntimeError as exc:
            text = str(exc).lower()
            if "non-fast-forward" not in text and "ff-only" not in text:
                raise
            self._rebased.pop(branch, None)
            try:
                return self._ff_push(branch)
            except Exception as retry_exc:  # noqa: BLE001
                raise MergeConflict(
                    f"{branch} still does not land on {self.base} after one "
                    f"replay: {retry_exc}") from retry_exc

    def _ff_push(self, branch: str) -> str:
        working = self._rebased.get(branch) or self.rebase(branch)
        self._git("checkout", self.base)
        self._git("merge", "--ff-only", working)
        self._git("push", self.remote, self.base)
        self._git("branch", "-D", working, check=False)
        self._rebased.pop(branch, None)
        return self.base

    # ------------------------------------------------------ acceptance

    def acceptance_checks(self, result, diff: SessionDiff):
        """The zone's oracle and the test suite, against the merged state."""
        check = oracles.run_oracle(result.oracle, diff.paths, self.repo,
                                   result.oracle_timeout_s)
        if not check.ok and not check.skipped:
            zone = result.zones[0] if result.zones else "(unzoned)"
            return False, reprompts.oracle_failed(zone, check.command,
                                                  check.output)
        if result.requires_tests:
            if not diff.touches_tests:
                return False, reprompts.missing_tests(
                    [p for p in diff.paths if p.endswith(".py")])
            suite = oracles.run_tests(self.repo, self.test_command)
            if not suite.ok:
                return False, reprompts.tests_failed(suite.command, suite.output)
        return True, ""
