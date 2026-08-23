"""Oracle: an append-only zone was only appended to.

Golden query sets, canaries and frozen fixtures are the exam. An agent
that can edit the exam can pass it without doing the work, so these zones
only ever grow. Retiring an entry is a real decision and it goes through
a master and the operator.

The classifier already refuses edits and deletes in an append-only zone
by path. This runs as the zone's oracle and catches the subtler case:
a file that grew, but whose existing lines changed.

Usage:
    python -m harness.oracle_kit.append_only --base <ref> --changed <path> [...]
"""
from __future__ import annotations

import argparse
import json
import subprocess
import sys
from pathlib import Path


def _git(repo: Path, *args: str) -> tuple[int, str]:
    proc = subprocess.run(["git", *args], cwd=repo, capture_output=True, text=True)
    return proc.returncode, proc.stdout


def _repo_root(start: Path) -> Path:
    code, out = _git(start, "rev-parse", "--show-toplevel")
    return Path(out.strip()) if code == 0 and out.strip() else start


def _load_lines(text: str, path: str) -> list[str] | None:
    """Entries as comparable strings; JSON arrays compare by entry, not by line."""
    if path.endswith(".json"):
        try:
            data = json.loads(text)
        except json.JSONDecodeError:
            return None
        if isinstance(data, list):
            return [json.dumps(item, sort_keys=True) for item in data]
        if isinstance(data, dict):
            for key in ("queries", "goldens", "canaries", "items", "entries"):
                if isinstance(data.get(key), list):
                    return [json.dumps(i, sort_keys=True) for i in data[key]]
        return None
    return [line for line in text.splitlines() if line.strip()]


def check(repo: Path, base: str, path: str) -> list[str]:
    code, before_text = _git(repo, "show", f"{base}:{path}")
    if code != 0:
        return []                                    # new file: pure addition
    target = repo / path
    if not target.exists():
        return [f"{path}: deleted. Append-only entries are never removed - "
                f"retiring one is a decision, not a cleanup."]
    after_text = target.read_text(encoding="utf-8")

    before = _load_lines(before_text, path)
    after = _load_lines(after_text, path)
    if before is None or after is None:
        return [f"{path}: cannot be parsed as an entry list, so append-only "
                f"cannot be verified. Keep these files as JSON arrays or JSONL."]

    if after[:len(before)] == before:
        return []

    missing = [entry for entry in before if entry not in after]
    problems = [f"{path}: existing entries were changed or removed."]
    for entry in missing[:5]:
        problems.append(f"    lost: {entry[:110]}")
    if not missing:
        problems.append("    entries were reordered or edited in place; the "
                        "original order is part of the record.")
    problems.append(
        "    Add new entries at the end. If an existing one is genuinely "
        "wrong, that is worth a proposal - say why the exam was mis-set."
    )
    return problems


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(prog="append_only")
    parser.add_argument("--base", default="HEAD~1")
    parser.add_argument("--changed", nargs="*", default=[])
    args = parser.parse_args(argv)

    if not args.changed:
        print("No paths supplied; nothing to verify.")
        return 0

    repo = _repo_root(Path.cwd().resolve())
    problems: list[str] = []
    for path in args.changed:
        problems.extend(check(repo, args.base, path.replace("\\", "/")))

    if problems:
        print("Append-only check FAILED:")
        for problem in problems:
            print(f"  * {problem}" if not problem.startswith("    ") else problem)
        return 1
    print(f"Append-only check passed for {len(args.changed)} file(s).")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
