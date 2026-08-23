"""`history` - the query tool that gives an agent its hindsight.

Every invocation is logged (see ``toollog``), which is what makes the
master's hindsight gate a set comparison rather than an act of faith.

    history query   --path <p> | --signature <s> | --agent <a>  --span 90d
    history diff    --proposal <id>
    history metric  --id <metric> --span 180d
    history attempts --span 30d
"""
from __future__ import annotations

import argparse
import json
import re
import subprocess
import sys
from datetime import datetime, timedelta, timezone
from pathlib import Path

from . import catalogue, paths, records

SPAN_RE = re.compile(r"^(\d+)\s*([dwmy])$", re.I)
SPAN_DAYS = {"d": 1, "w": 7, "m": 30, "y": 365}


def parse_span(value: str) -> int:
    match = SPAN_RE.match(value.strip())
    if not match:
        raise argparse.ArgumentTypeError(
            f"span must look like 90d, 12w, 6m or 1y (got '{value}')"
        )
    return int(match.group(1)) * SPAN_DAYS[match.group(2).lower()]


def _git(repo: Path, *args: str) -> str:
    proc = subprocess.run(["git", *args], cwd=repo, capture_output=True, text=True)
    return proc.stdout if proc.returncode == 0 else ""


# ------------------------------------------------------------------- query

def cmd_query(args, repo: Path) -> int:
    span = args.span
    since = f"--since={span}.days"
    lines: list[str] = []
    signatures: list[str] = []
    touched: list[str] = []

    if args.path:
        touched = list(args.path)
        for path in args.path:
            log = _git(repo, "log", since, "--date=short",
                       "--pretty=format:%h %ad %an %s", "--", path)
            lines.append(f"\n=== {path} - commits in the last {span} days ===")
            lines.append(log.strip() or "(no commits)")

            prior = [
                p for p in records.list_proposals(repo)
                if any(path in c for c in p.classify.get("summary", {}).get("paths", []))
                or any(path in ref for ref in p.body.get("evidence_refs", []))
            ]
            if prior:
                lines.append(f"--- proposals touching {path} ---")
                for proposal in prior:
                    lines.append(
                        f"  {proposal.id} [{proposal.status}] {proposal.tier} "
                        f"{proposal.body.get('change', '')[:80]}"
                    )

    if args.signature:
        signatures = [args.signature]
        lines.append(f"\n=== signature {args.signature} ===")
        lines.append(_signature_report(repo, args.signature, span))

    if args.agent:
        attempts = [a for a in records.load_attempts(repo, days=span)
                    if a.get("agent") == args.agent]
        lines.append(f"\n=== {args.agent} - {len(attempts)} attempt(s) "
                     f"in {span} days ===")
        for attempt in attempts:
            lines.append(
                f"  {attempt['ts'][:10]} {attempt['tier']:5} "
                f"{attempt.get('outcome', ''):9} "
                f"{attempt.get('files', 0)}f {attempt.get('net_loc', 0):+}l "
                f"{', '.join(attempt.get('paths', [])[:3])}"
            )

    if not lines:
        lines.append("Nothing queried. Pass --path, --signature or --agent.")


    print("\n".join(lines))
    return 0


def _signature_report(repo: Path, signature: str, span: int) -> str:
    registry = paths.state_dir(repo) / "signatures.json"
    out: list[str] = []
    if registry.exists():
        try:
            data = json.loads(registry.read_text(encoding="utf-8"))
            for sig in data.get("signatures", []):
                if sig.get("id") != signature:
                    continue
                out.append(f"label: {sig.get('label', '')}")
                out.append(f"occurrences: {len(sig.get('occurrences', []))}")
                for occurrence in sig.get("occurrences", [])[-8:]:
                    out.append(f"  * {occurrence}")
                closures = sig.get("closures", [])
                out.append(f"prior closure attempts: {len(closures)}")
                for closure in closures:
                    out.append(
                        f"  * {closure.get('date', '?')} {closure.get('by', '?')} "
                        f" -  {closure.get('what', '')} "
                        f"→ {closure.get('outcome', 'unknown')}"
                    )
                if len(closures) >= 2:
                    out.append(
                        "\n  NOTE: this signature has been closed "
                        f"{len(closures)} times and returned each time. A fix "
                        "at the same level as those attempts will not merge."
                    )
        except json.JSONDecodeError:
            out.append("(signatures.json is not valid JSON)")
    related = [a for a in records.load_attempts(repo, days=span)
               if a.get("signature") == signature]
    if related:
        out.append(f"\nattempts referencing it ({len(related)}):")
        for attempt in related[-8:]:
            out.append(f"  * {attempt['ts'][:10]} {attempt['agent']} "
                       f"{attempt['tier']} {attempt.get('outcome', '')}")
    return "\n".join(out) or "(no record of this signature)"


# -------------------------------------------------------------------- diff

def cmd_diff(args, repo: Path) -> int:
    proposal = records.load_proposal(args.proposal, repo)
    if not proposal:
        print(f"No proposal {args.proposal}.", file=sys.stderr)
        return 1


    print(f"=== {proposal.id} [{proposal.status}] {proposal.tier} "
          f"by {proposal.agent} ===")
    for key in ("change", "why_now", "tradeoff"):
        value = proposal.body.get(key)
        if value:
            print(f"\n{key}:\n  {value}")
    if proposal.body.get("evidence_refs"):
        print("\nevidence:")
        for ref in proposal.body["evidence_refs"]:
            print(f"  * {ref}")
    if proposal.body.get("irreducible_because"):
        print(f"\nirreducible because:\n  {proposal.body['irreducible_because']}")
    print("\nclassifier said:")
    for signal in proposal.classify.get("signals", []):
        print(f"  * [{signal['id']}] {signal['detail']}")

    patch = records.proposal_patch(args.proposal, repo)
    print(f"\n=== diff ({len(patch.splitlines())} lines) ===")
    print(patch if patch else "(no diff - this proposal carries an argument only)")
    return 0


# ------------------------------------------------------------------ metric

def cmd_metric(args, repo: Path) -> int:
    rows = catalogue.readings(args.id, repo=repo)
    cutoff = datetime.now(timezone.utc) - timedelta(days=args.span)
    kept = []
    for row in rows:
        try:
            if datetime.fromisoformat(row["ts"]) >= cutoff:
                kept.append(row)
        except (KeyError, ValueError):
            continue


    if not kept:
        print(f"No readings for '{args.id}' in the last {args.span} days.")
        print("Known metrics: " + ", ".join(catalogue.metric_ids(repo)))
        return 1
    print(f"=== {args.id} - {len(kept)} readings over {args.span} days ===")
    for row in kept:
        print(f"  {row['ts'][:16]}  {row.get('value')}  {row.get('unit', '')}")
    values = [r["value"] for r in kept if isinstance(r.get("value"), (int, float))]
    if len(values) >= 2:
        print(f"\n  first {values[0]} → last {values[-1]} "
              f"(Δ {values[-1] - values[0]:+.4g})")
    return 0


# --------------------------------------------------------------- attempts

def cmd_attempts(args, repo: Path) -> int:
    rows = records.load_attempts(repo, days=args.span)
    print(f"=== {len(rows)} attempts in {args.span} days ===")
    for row in rows:
        print(f"  {row['ts'][:16]} {row['agent']:22} {row['tier']:5} "
              f"{row.get('outcome', ''):9} {row.get('files', 0)}f "
              f"{row.get('net_loc', 0):+}l")
    return 0


def build_parser() -> argparse.ArgumentParser:
    parser = argparse.ArgumentParser(
        prog="history",
        description="Query this system's technical history.",
    )
    sub = parser.add_subparsers(dest="command", required=True)

    query = sub.add_parser("query", help="commits, proposals and attempts")
    query.add_argument("--path", action="append")
    query.add_argument("--signature")
    query.add_argument("--agent")
    query.add_argument("--span", type=parse_span, default=90)
    query.set_defaults(func=cmd_query)

    diff = sub.add_parser("diff", help="read what a proposal actually built")
    diff.add_argument("--proposal", required=True)
    diff.set_defaults(func=cmd_diff)

    metric = sub.add_parser("metric", help="readings for one lifeblood metric")
    metric.add_argument("--id", required=True)
    metric.add_argument("--span", type=parse_span, default=180)
    metric.set_defaults(func=cmd_metric)

    attempts = sub.add_parser("attempts", help="what agents have tried to merge")
    attempts.add_argument("--span", type=parse_span, default=30)
    attempts.set_defaults(func=cmd_attempts)
    return parser


def main(argv: list[str] | None = None) -> int:
    for stream in (sys.stdout, sys.stderr):
        try:
            stream.reconfigure(encoding="utf-8", errors="replace")
        except (AttributeError, ValueError):
            pass
    args = build_parser().parse_args(argv)
    try:
        repo = paths.repo_root()
    except paths.HarnessPathError as exc:
        print(str(exc), file=sys.stderr)
        return 2
    return args.func(args, repo)


if __name__ == "__main__":
    raise SystemExit(main())
