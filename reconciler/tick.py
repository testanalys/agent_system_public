"""The reconciler - and it now does one thing.

    python -m reconciler.tick --subsystem pipeline

Ask whether this subsystem has escalations waiting for a master, and if
it does and no master is already reading them, start one.

That is the whole tick. It makes no decision about which point-in-time or
retro agents should run, because it is the wrong place to make it: by the
time a scheduled process notices that something went wrong, the run that
went wrong has ended, its context is gone, and the delay is however long
is left on the timer. Those agents are started by the mechanical code, at
the moment and at the point in the process where the problem is detected.

What is left here is the part that genuinely needs a periodic sweep. An
escalation is raised by one agent execution and consumed by another, and
nothing joins the two except this scan.

It touches no git checkout, spawns nothing itself, and holds no state, so
there is very little for it to fail at - which matters, because when it
did fail it used to take every subsystem down with it.
"""
from __future__ import annotations

import argparse
import json
import sys

from harness import ledger


def scan(subsystem: str, *, launch=None, in_flight=None,
         dry_run: bool = False) -> dict:
    """One subsystem's escalations, and a master if they need one.

    `launch` and `in_flight` are injected so this is testable without a
    cloud: `launch(subsystem) -> dict`, `in_flight(subsystem) -> list`.
    """
    entries = ledger.open_entries(subsystem)
    report: dict = {"subsystem": subsystem, **ledger.summarise(entries),
                    "escalations": [e.id for e in entries]}

    if not entries:
        report["master"] = {"skipped": "nothing escalated"}
        return report

    cooling = ledger.cooldown_active(subsystem)
    if cooling:
        report["master"] = {
            "skipped": "incomplete cooldown; escalations stay open",
            "until": cooling.get("until"),
            "reason": cooling.get("reason") or "",
        }
        return report

    running = list(in_flight(subsystem)) if in_flight else []
    if running:
        # Not a lock. The master already reading these will close them,
        # and starting a second one would hand it the same docket. Both
        # would then write dispositions for the same branches.
        report["master"] = {"skipped": "a master is already reading these",
                            "executions": running}
        return report

    if dry_run or launch is None:
        report["master"] = {"would_launch": True}
        return report

    report["master"] = launch(subsystem)
    return report


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(prog="reconciler.tick")
    parser.add_argument("--subsystem", required=True)
    parser.add_argument("--dry-run", action="store_true")
    parser.add_argument("--json", action="store_true")
    args = parser.parse_args(argv)

    from cloud import launcher

    report = scan(
        args.subsystem,
        launch=lambda s: launcher.launch(launcher.master_args(s)),
        in_flight=lambda s: launcher.in_flight(["--master", "--subsystem", s]),
        dry_run=args.dry_run,
    )

    if args.json:
        print(json.dumps(report, indent=2, default=str))
    else:
        master = report.get("master") or {}
        print(f"[{args.subsystem}] {report['open']} escalation(s) open "
              f"-> {master.get('skipped') or master.get('execution') or master}")
    return 0


if __name__ == "__main__":
    sys.exit(main())
