"""Acceptance checks — what makes a zone trustworthy without review.

A zone that grants an agent the right to merge unreviewed has to prove
the change does what it claims. That proof is the oracle: an acceptance
test with a floor, declared in ``skeleton.json`` beside the zone it
governs.

    --min-recall-delta 0        rewrite the heuristic freely; do not
                                lower recall on the frozen fixtures
    --require-verified 1        a new harvest route must actually produce
                                one verified-downloadable op
    --assert-no-new-false-positives
                                a new spawn condition must not fire
                                spuriously when replayed over real runs

An oracle that exceeds its timeout counts as not-run, which lands the
change in AMBER. A slow oracle is an engineering problem, not a policy
question, and the fail-safe direction is toward review.
"""
from __future__ import annotations

import os
import re
import shlex
import subprocess
from dataclasses import dataclass
from pathlib import Path

TEST_COMMAND_DEFAULT = "python -m pytest -q"


@dataclass
class CheckResult:
    ok: bool
    command: str
    output: str
    duration_s: float
    timed_out: bool = False
    skipped: bool = False
    reason: str = ""


def _run(command: str, repo: Path, timeout_s: int) -> CheckResult:
    import time
    started = time.monotonic()
    try:
        proc = subprocess.run(
            command, cwd=repo, shell=True, capture_output=True,
            text=True, timeout=timeout_s,
        )
    except subprocess.TimeoutExpired as exc:
        return CheckResult(
            ok=False, command=command,
            output=(exc.stdout or "") + (exc.stderr or ""),
            duration_s=time.monotonic() - started, timed_out=True,
            reason=f"exceeded {timeout_s}s timeout",
        )
    except OSError as exc:
        return CheckResult(
            ok=False, command=command, output=str(exc),
            duration_s=time.monotonic() - started,
            reason=f"could not execute: {exc}",
        )
    return CheckResult(
        ok=proc.returncode == 0,
        command=command,
        output=(proc.stdout or "") + (proc.stderr or ""),
        duration_s=time.monotonic() - started,
    )


_ENV_REF = re.compile(r"\$\{([A-Za-z_][A-Za-z0-9_]*)\}|\$([A-Za-z_][A-Za-z0-9_]*)")


def expand(template: str, changed_paths: list[str], repo: Path) -> str:
    """Fill ``{changed}``, ``{changed_modules}`` and ``$VAR`` in a template.

    Environment references are expanded here rather than left to the shell
    because ``shell=True`` means cmd.exe on Windows, where ``${VAR}`` is a
    literal. Oracles have to behave identically on a workstation and in a
    Linux container or the local run tells you nothing.
    """
    modules = [p[:-3].replace("/", ".") for p in changed_paths if p.endswith(".py")]
    filled = (template
              .replace("{changed_modules}", " ".join(shlex.quote(m) for m in modules))
              .replace("{changed}", " ".join(shlex.quote(p) for p in changed_paths))
              .replace("{repo}", shlex.quote(str(repo))))
    return _ENV_REF.sub(
        lambda m: os.environ.get(m.group(1) or m.group(2), ""), filled
    )


def run_oracle(template: str | None, changed_paths: list[str], repo: Path,
               timeout_s: int = 600) -> CheckResult:
    if not template:
        return CheckResult(True, "", "", 0.0, skipped=True,
                           reason="zone declares no oracle")
    return _run(expand(template, changed_paths, repo), repo, timeout_s)


def run_tests(repo: Path, command: str | None = None,
              timeout_s: int = 1800) -> CheckResult:
    return _run(command or TEST_COMMAND_DEFAULT, repo, timeout_s)
