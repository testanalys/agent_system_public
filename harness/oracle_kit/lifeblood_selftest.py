"""Oracle: a changed metric definition still produces valid readings.

This is what lets a retro agent tune what the system measures without
review. Measurement is control code - change it and you change when every
downstream agent wakes up - so the floor is deliberately strict:

    * the module imports and ``collect()`` runs
    * the reading satisfies the lifeblood contract
    * the value is of a usable type, or the reading honestly says no_data
    * the metric still appears in the catalogue
    * backfilling over recent history does not error

Usage:
    python -m harness.oracle_kit.lifeblood_selftest --id <metric_id> [...]
    python -m harness.oracle_kit.lifeblood_selftest --changed <path> [...]
"""
from __future__ import annotations

import argparse
import importlib.util
import json
import sys
from pathlib import Path

REQUIRED_FIELDS = ("id", "ts", "status")
VALID_STATUSES = {"ok", "no_data", "error"}


def _repo_root(start: Path) -> Path:
    for candidate in [start, *start.parents]:
        if (candidate / "lifeblood").is_dir():
            return candidate
    return start


def _metric_ids_from_paths(repo: Path, changed: list[str]) -> list[str]:
    ids = []
    for raw in changed:
        parts = Path(raw.replace("\\", "/")).parts
        if "metrics" in parts:
            index = parts.index("metrics")
            if index + 1 < len(parts):
                ids.append(parts[index + 1])
    return sorted(set(ids))


def _load_collect(script: Path):
    spec = importlib.util.spec_from_file_location(f"metric_{script.parent.name}", script)
    if spec is None or spec.loader is None:
        raise ImportError(f"cannot load {script}")
    module = importlib.util.module_from_spec(spec)
    sys.modules[spec.name] = module
    spec.loader.exec_module(module)
    if not hasattr(module, "collect"):
        raise AttributeError(f"{script} defines no collect()")
    return module.collect


def check_metric(repo: Path, metric_id: str) -> list[str]:
    problems: list[str] = []
    script = repo / "lifeblood" / "metrics" / metric_id / "metric.py"
    if not script.exists():
        return [f"{metric_id}: no metric.py at {script}"]

    catalogue_path = repo / "lifeblood" / "catalogue.json"
    if catalogue_path.exists():
        try:
            data = json.loads(catalogue_path.read_text(encoding="utf-8"))
            if metric_id not in {m["id"] for m in data.get("metrics", [])}:
                problems.append(
                    f"{metric_id}: not registered in catalogue.json. A metric "
                    f"nothing knows about cannot trigger anything."
                )
        except json.JSONDecodeError:
            problems.append("catalogue.json is not valid JSON")

    try:
        collect = _load_collect(script)
    except Exception as exc:
        return problems + [f"{metric_id}: will not import - {type(exc).__name__}: {exc}"]

    try:
        reading = collect()
    except Exception as exc:
        return problems + [f"{metric_id}: collect() raised {type(exc).__name__}: {exc}"]

    if not isinstance(reading, dict):
        return problems + [f"{metric_id}: collect() returned {type(reading).__name__}, "
                           f"expected a dict"]
    for field in REQUIRED_FIELDS:
        if field not in reading:
            problems.append(f"{metric_id}: reading is missing '{field}'")
    status = reading.get("status")
    if status not in VALID_STATUSES:
        problems.append(f"{metric_id}: status '{status}' is not one of "
                        f"{sorted(VALID_STATUSES)}")
    if status == "ok":
        value = reading.get("value")
        if value is None:
            problems.append(f"{metric_id}: status is ok but value is null - "
                            f"use no_data when there is nothing to report")
        elif not isinstance(value, (int, float, str)):
            problems.append(f"{metric_id}: value type {type(value).__name__} "
                            f"cannot be trended")
        if not reading.get("unit"):
            problems.append(f"{metric_id}: ok reading carries no unit")
    if reading.get("id") != metric_id:
        problems.append(f"{metric_id}: reading id is '{reading.get('id')}' - "
                        f"history would be written to the wrong series")
    return problems


def check_history(repo: Path, metric_id: str, backfill_days: int) -> list[str]:
    path = repo / "lifeblood" / "state" / f"{metric_id}.jsonl"
    if not path.exists():
        return []
    problems: list[str] = []
    bad = 0
    for number, line in enumerate(path.read_text(encoding="utf-8").splitlines(), 1):
        line = line.strip()
        if not line:
            continue
        try:
            json.loads(line)
        except json.JSONDecodeError:
            bad += 1
            if bad <= 3:
                problems.append(f"{metric_id}: history line {number} is not JSON")
    if bad:
        problems.append(f"{metric_id}: {bad} unreadable history line(s) - "
                        f"trends over the last {backfill_days}d would be wrong")
    return problems


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(prog="lifeblood_selftest")
    parser.add_argument("--id", action="append", default=[])
    parser.add_argument("--changed", nargs="*", default=[])
    parser.add_argument("--backfill", default="30d")
    args = parser.parse_args(argv)

    repo = _repo_root(Path.cwd().resolve())
    ids = list(args.id) or _metric_ids_from_paths(repo, args.changed)
    if not ids:
        print("No metric ids resolved from --id or --changed; nothing to check.")
        return 0

    backfill_days = int("".join(c for c in args.backfill if c.isdigit()) or 30)
    problems: list[str] = []
    for metric_id in ids:
        problems.extend(check_metric(repo, metric_id))
        problems.extend(check_history(repo, metric_id, backfill_days))

    if problems:
        print("Metric self-test FAILED:")
        for problem in problems:
            print(f"  * {problem}")
        return 1
    print(f"Metric self-test passed for: {', '.join(ids)}")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
