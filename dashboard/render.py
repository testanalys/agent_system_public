"""Print the timeline. Deliberately a table in a terminal, for now.

    python -m dashboard.render --subsystem pipeline --limit 50

The operator's question is chronological - what has been happening, in
order, with enough on each line to decide whether to look closer. A table
answers that, and it works over SSH into the reconciler, which is where
the question usually gets asked. When the master loop starts writing
strategy and declared-surface columns there will be something worth
rendering as a page; there is not yet, and building the page first would
mean designing a view of data nobody has looked at.
"""
from __future__ import annotations

import argparse

from . import timeline, validate

COLUMNS = (
    ("ts", 14), ("agent", 24), ("kind", 8), ("strategy", 11),
    ("classify", 8), ("outcome", 10), ("files", 5), ("headline", 48),
)


def _when(ts: str) -> str:
    """`2026-08-11T02:22:12+00:00` -> `08-11 02:22`."""
    try:
        date, _, clock = ts.partition("T")
        return f"{date[5:]} {clock[:5]}"
    except (AttributeError, IndexError):
        return str(ts)[:14]


def _cell(row: dict, name: str, width: int) -> str:
    if name == "ts":
        value = _when(row.get("ts", ""))
    elif name == "headline":
        value = row.get("headline") or row.get("trigger") or ""
    elif name == "strategy":
        value = row.get("strategy") or ""
    else:
        value = row.get(name, "")
    text = str(value)
    if len(text) > width:
        text = text[: width - 1] + "~"
    return text.ljust(width)


def as_table(rows: list[dict]) -> str:
    if not rows:
        return ("No timeline rows. Either nothing has run yet, or "
                "TURNSTILE_AGENT_STORE is not set in this environment.")
    header = "  ".join(name.upper().ljust(width) for name, width in COLUMNS)
    lines = [header, "-" * len(header)]
    for row in rows:
        lines.append("  ".join(_cell(row, name, width)
                               for name, width in COLUMNS))
    return "\n".join(lines)


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(prog="dashboard.render")
    parser.add_argument("--subsystem", default=None)
    parser.add_argument("--limit", type=int, default=100)
    parser.add_argument("--validate", action="store_true",
                        help="report gaps in the PIT/retro/master chain")
    args = parser.parse_args(argv)

    rows = timeline.read(args.subsystem, args.limit)
    print(as_table(rows))
    if args.validate:
        problems = validate.flow(rows, subsystem=args.subsystem)
        if problems:
            print("\nFlow gaps:")
            for line in problems:
                print(f"  - {line}")
            return 1
        print("\nFlow OK: spawn → verdict → (proposal) → master wager → route")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
