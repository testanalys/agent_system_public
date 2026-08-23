"""History signals — the patch-over-design detectors.

Small does not mean safe. These four signals read git history and the
attempt log to answer a different question from "how big is this change":
*has this spot been fixed before, and did it hold?*

H3 is the load-bearing one. It is what makes the third attempt at a known
failure signature impossible to merge quietly, which is the specific
failure the measured record shows nine times over.
"""
from __future__ import annotations

import re
import subprocess
from dataclasses import dataclass
from datetime import datetime, timedelta, timezone
from pathlib import Path

from .diff import SessionDiff, _git

FILE_CHURN_DAYS = 30
FILE_CHURN_LIMIT = 3
REGION_CHURN_DAYS = 14
REGION_CHURN_LIMIT = 2
REGION_RADIUS = 20
SIGNATURE_CLOSURE_LIMIT = 2
OSCILLATION_DAYS = 30
REVERT_PROTECTION_DAYS = 14
HUNK_HEADER = re.compile(r"^@@ -\d+(?:,\d+)? \+(\d+)(?:,(\d+))? @@")


@dataclass(frozen=True)
class HistoryHit:
    id: str
    detail: str
    evidence: dict


def _since(days: int) -> str:
    return f"{days}.days"


def file_churn(diff: SessionDiff, limit: int = FILE_CHURN_LIMIT) -> list[HistoryHit]:
    """Files already modified `limit` times in the churn window (lifeblood S4).

    Scoped to ``diff.base`` so the session's own commits never count as
    its own history, and excluding the commit that created the file —
    writing something and then editing it is not churn, it is writing it.
    """
    hits: list[HistoryHit] = []
    for change in diff.files:
        if change.is_new:
            continue
        try:
            log = _git(diff.repo, "log", f"--since={_since(FILE_CHURN_DAYS)}",
                       "--pretty=format:%h %ad %s", "--date=short",
                       diff.base, "--", change.path)
        except Exception:
            continue
        creation = _creation_sha(diff.repo, change.path)
        commits = [line for line in log.splitlines()
                   if line.strip() and not (creation and line.startswith(creation))]
        if len(commits) >= limit:
            hits.append(HistoryHit(
                id="H1",
                detail=(f"{change.path} has been modified {len(commits)} times "
                        f"in {FILE_CHURN_DAYS} days"),
                evidence={"file": change.path, "commits": commits[:6]},
            ))
    return hits


def region_churn(diff: SessionDiff) -> list[HistoryHit]:
    """The same lines, edited again, very recently.

    Finer than file churn: a large file legitimately changes often, but
    the same twenty-line region changing twice in a fortnight means the
    previous fix did not hold.
    """
    hits: list[HistoryHit] = []
    cutoff = datetime.now(timezone.utc) - timedelta(days=REGION_CHURN_DAYS)

    for change in diff.files:
        if change.is_new or change.is_delete:
            continue
        creation = _creation_sha(diff.repo, change.path)
        for start, length in _changed_regions(diff.patch, change.path):
            lo = max(1, start - REGION_RADIUS)
            hi = start + length + REGION_RADIUS
            try:
                log = _git(diff.repo, "log", f"-L{lo},{hi}:{change.path}",
                           "--no-patch", "--pretty=format:%h|%aI|%s", diff.base)
            except Exception:
                continue
            recent = []
            for line in log.splitlines():
                parts = line.split("|", 2)
                if len(parts) != 3:
                    continue
                if creation and parts[0].startswith(creation):
                    continue
                try:
                    when = datetime.fromisoformat(parts[1])
                except ValueError:
                    continue
                if when >= cutoff:
                    recent.append(f"{parts[0]} {parts[1][:10]} {parts[2]}")
            if len(recent) >= REGION_CHURN_LIMIT:
                hits.append(HistoryHit(
                    id="H2",
                    detail=(f"{change.path}:{lo}-{hi} has been edited "
                            f"{len(recent)} times in {REGION_CHURN_DAYS} days"),
                    evidence={"file": change.path, "region": [lo, hi],
                              "commits": recent[:4]},
                ))
                break                      # one hit per file is enough
    return hits


def signature_recurrence(
    signature: str | None,
    closures: list[dict],
    limit: int = SIGNATURE_CLOSURE_LIMIT,
) -> list[HistoryHit]:
    """A failure signature that has already been closed `limit` times.

    `closures` comes from the subsystem's run records: prior changes that
    claimed to fix this signature, with what happened afterwards.
    """
    if not signature or len(closures) < limit:
        return []
    return [HistoryHit(
        id="H3",
        detail=(f"signature {signature} has been closed {len(closures)} times "
                f"and returned each time"),
        evidence={"signature": signature, "closures": closures[-4:]},
    )]


MIN_RESTORED_LINES = 3
RESTORE_RATIO = 0.5


def oscillation(diff: SessionDiff) -> list[HistoryHit]:
    """This change restores content a recent commit deleted.

    The discriminating direction is *restoration*: our added lines
    reappearing among a prior commit's removed lines. Editing a line that
    a recent commit wrote looks superficially similar — our removals
    match their additions — but that is ordinary evolution, and treating
    it as reversion would flag every second change to an actively
    developed file. So only the restore direction counts, and it has to
    be both substantial in absolute terms and dominant in relative ones.
    """
    if diff.is_empty:
        return []
    added = {l.strip() for l in diff.patch.splitlines()
             if l.startswith("+") and not l.startswith("+++") and l.strip(" +")}
    removed = {l.strip("- ").strip() for l in diff.patch.splitlines()
               if l.startswith("-") and not l.startswith("---") and l.strip(" -")}
    if len(added) < MIN_RESTORED_LINES:
        return []

    try:
        log = _git(diff.repo, "log", f"--since={_since(OSCILLATION_DAYS)}",
                   "--pretty=format:%h|%aI|%s", diff.base, "--", *diff.paths)
    except Exception:
        return []

    hits: list[HistoryHit] = []
    for line in log.splitlines()[:40]:
        parts = line.split("|", 2)
        if len(parts) != 3:
            continue
        sha, when, subject = parts
        try:
            patch = _git(diff.repo, "show", "--format=", sha, "--", *diff.paths)
        except Exception:
            continue
        prior_added = {l.strip("+ ").strip() for l in patch.splitlines()
                       if l.startswith("+") and not l.startswith("+++")}
        prior_removed = {l.strip("- ").strip() for l in patch.splitlines()
                         if l.startswith("-") and not l.startswith("---")}
        if not prior_removed:
            continue                        # a pure addition cannot be reverted

        restored = added & prior_removed    # we are putting back what it deleted
        undone = removed & prior_added      # corroborating, never sufficient
        if (len(restored) >= MIN_RESTORED_LINES
                and len(restored) / len(added) >= RESTORE_RATIO):
            age_days = _age_days(when)
            hits.append(HistoryHit(
                id="H4",
                detail=(f"restores {len(restored)} line(s) that {sha} "
                        f"({subject.strip()[:50]}) deleted {age_days} days ago"),
                evidence={"commit": sha, "subject": subject.strip(),
                          "age_days": age_days,
                          "restored_lines": len(restored),
                          "also_undone_lines": len(undone),
                          "within_protection": age_days <= REVERT_PROTECTION_DAYS},
            ))
            break
    return hits


def _creation_sha(repo: Path, path: str) -> str | None:
    """Short sha of the commit that added this file, if it is in history."""
    try:
        out = _git(repo, "log", "--diff-filter=A", "--format=%h", "-1", "--", path)
    except Exception:
        return None
    return out.strip() or None


def _age_days(iso: str) -> int:
    try:
        when = datetime.fromisoformat(iso)
    except ValueError:
        return 999
    return max(0, (datetime.now(timezone.utc) - when).days)


def _changed_regions(patch: str, path: str) -> list[tuple[int, int]]:
    """(start_line, length) of each hunk touching `path` in the new file."""
    regions: list[tuple[int, int]] = []
    current = ""
    for line in patch.splitlines():
        if line.startswith("+++ "):
            current = line[6:].strip()
            if current.startswith("b/"):
                current = current[2:]
        elif line.startswith("@@") and current == path:
            match = HUNK_HEADER.match(line)
            if match:
                start = int(match.group(1))
                length = int(match.group(2) or 1)
                regions.append((start, length))
    return regions


def collect_all(
    diff: SessionDiff,
    signature: str | None = None,
    closures: list[dict] | None = None,
) -> list[HistoryHit]:
    hits: list[HistoryHit] = []
    hits.extend(file_churn(diff))
    hits.extend(region_churn(diff))
    hits.extend(signature_recurrence(signature, closures or []))
    hits.extend(oscillation(diff))
    return hits
