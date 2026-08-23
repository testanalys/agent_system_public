"""The scheduled entrypoint: scan every subsystem for waiting escalations.

    python -m cloud.tick_all --all

This is all `turnstile-reconciler` does now. It reads a small index of
escalations out of object storage and, where a subsystem has some and no
master is already reading them, starts a master execution. It clones
nothing, spawns no point-in-time agents, calls no Cursor API, and holds
no state, so a tick is a couple of list calls and exits.

Every agent that is not a master is started by the mechanical code that
found the problem, in the same process, at the moment it found it.

A subsystem that fails is one subsystem: the loop keeps going and the
exit code only reports failure if every subsystem failed. A tick that
exited non-zero because one repository was briefly unreachable used to
mark the whole schedule red and hide the two subsystems that worked.
"""
from __future__ import annotations

import argparse
import json
import sys
import traceback
from typing import Any

from reconciler.tick import scan

from . import launcher, workspace

EXIT_OK, EXIT_FAILED = 0, 1


def _master_status(master: dict) -> str:
    """Human line for one subsystem's master launch outcome."""
    if master.get("skipped"):
        return str(master["skipped"])
    if master.get("would_launch"):
        return "would launch master (dry-run)"
    if not master.get("launched", True):
        return f"FAILED TO LAUNCH: {master.get('reason', 'unknown')}"
    execution = master.get("execution")
    if execution:
        return str(execution).rsplit("/", 1)[-1]
    return "launched (no execution id returned)"


def tick_subsystem(subsystem: str, *, dry_run: bool = False) -> dict[str, Any]:
    report = scan(
        subsystem,
        launch=lambda s: launcher.launch(launcher.master_args(s)),
        in_flight=lambda s: launcher.in_flight(["--master", "--subsystem", s]),
        dry_run=dry_run,
    )
    return {"ok": True, **report}


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(prog="cloud.tick_all")
    parser.add_argument("--subsystem", action="append", dest="subsystems",
                        choices=sorted(workspace.REPOS),
                        help="repeatable; defaults to every subsystem")
    parser.add_argument("--all", action="store_true")
    parser.add_argument("--dry-run", action="store_true")
    parser.add_argument("--json", action="store_true")
    args = parser.parse_args(argv)

    subsystems = args.subsystems or sorted(workspace.REPOS)
    reports, failures = [], []
    for subsystem in subsystems:
        try:
            reports.append(tick_subsystem(subsystem, dry_run=args.dry_run))
        except Exception as exc:                       # noqa: BLE001
            failures.append(subsystem)
            reports.append({"subsystem": subsystem, "ok": False,
                            "error": f"{type(exc).__name__}: {exc}",
                            "traceback": traceback.format_exc()[-1200:]})

    payload = {"ticked": subsystems, "failed": failures, "reports": reports}
    if args.json:
        print(json.dumps(payload, indent=2, default=str))
    else:
        for report in reports:
            if not report.get("ok"):
                print(f"[{report['subsystem']}] FAILED {report['error']}")
                continue
            master = report.get("master") or {}
            print(f"[{report['subsystem']}] {report.get('open', 0)} open "
                  f"-> {_master_status(master)}")

    # One subsystem failing is one subsystem's problem. Only a tick that
    # achieved nothing at all is worth marking the schedule red for.
    return EXIT_FAILED if len(failures) == len(subsystems) else EXIT_OK


if __name__ == "__main__":
    sys.exit(main())
