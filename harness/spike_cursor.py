"""Live probe of the Cursor behaviours our loop depends on.

Runs over the REST API rather than the Python SDK: the SDK is a client
for a locally launched ``cursor-agent`` bridge subprocess, which would
put a CLI binary in every container that spawns an agent, and whose
launcher calls ``os.get_blocking`` and therefore cannot run on Windows.

Five questions, none of which can be settled offline:

    Q1  Can a follow-up run be sent to an agent whose previous run has
        already reached a terminal state? THE LOOP DEPENDS ON THIS.
    Q2  Does the follow-up carry the earlier conversation, or start cold?
    Q3  Does a terminal run report the branch it pushed?
    Q4  Do stream events carry tool calls we can gate on?
    Q5  Does the pinned ref hold as the base the agent worked from?

    python -m harness.spike_cursor --repo https://github.com/you/scratch
"""
from __future__ import annotations

import argparse
import json
import sys
import time
from dataclasses import dataclass, field
from pathlib import Path

from .agentloop import SpawnRequest
from .cursor_rest import CursorAPIError, CursorRestTransport, load_api_key

MARKER_ONE = "SPIKE_ALPHA_7731"
MARKER_TWO = "SPIKE_BETA_9042"


@dataclass
class Finding:
    question: str
    verdict: str = "UNKNOWN"
    detail: str = ""
    evidence: dict = field(default_factory=dict)

    def show(self) -> None:
        icon = {"YES": "[YES]", "NO": "[NO ]", "PARTIAL": "[~~~]",
                "ERROR": "[ERR]"}.get(self.verdict, "[???]")
        print(f"\n{icon} {self.question}")
        print(f"      {self.detail}")
        for key, value in self.evidence.items():
            text = str(value)
            print(f"      {key}: {text[:220]}{' ...' if len(text) > 220 else ''}")


def _progress(evidence) -> None:
    print(f"      ... {evidence.status}", flush=True)


def run_spike(repo: str, model: str | None, ref: str,
              max_wait: int) -> list[Finding]:
    transport = CursorRestTransport(trace=False)
    q1 = Finding("Q1  follow-up accepted after a TERMINAL previous run")
    q2 = Finding("Q2  follow-up retains the earlier conversation")
    q3 = Finding("Q3  terminal run reports the branch it pushed")
    q4 = Finding("Q4  stream carries tool calls we can gate on")
    q5 = Finding("Q5  pinned ref holds as the base")

    request = SpawnRequest(
        role="spike", subsystem="spike", repo_url=repo, starting_ref=ref,
        model=model,
        prompt=(f"Create a file named spike_one.txt at the repository root "
                f"whose only content is the token {MARKER_ONE}. Then run "
                f"`git rev-parse HEAD~0` and state the commit you started "
                f"from. Commit and push your work."),
    )

    print(f"creating cloud agent: {repo}@{ref}")
    agent_id, handle = transport.create(request)
    print(f"agent {agent_id}  run {handle.run_id}")
    print("waiting for the first run (this takes minutes) ...")

    first = transport.wait(handle, max_wait_s=max_wait, on_tick=_progress)
    print(f"first run terminal: {first.status} ({first.duration_ms}ms)")

    q3.verdict = "YES" if first.branch else "NO"
    q3.detail = (f"branch reported: {first.branch}" if first.branch else
                 "no branch on the terminal run. The diff cannot be located "
                 "from the run result alone - fall back to listing remote "
                 "refs created since spawn.")
    q3.evidence = {"status": first.status, "branch": first.branch,
                   "pr_url": first.pr_url}

    q4.verdict = ("YES" if any(c.get("args") for c in first.tool_calls)
                  else "PARTIAL" if first.tool_calls else "NO")
    q4.detail = (f"{len(first.tool_calls)} tool call(s) observed; gates that "
                 f"read commands are viable"
                 if first.tool_calls else
                 "no tool calls readable from the stream - the hindsight and "
                 "independence gates must use tool-side logging instead")
    q4.evidence = {"sample": first.tool_calls[:3]}

    q5.verdict = "YES" if ref[:7] in (first.result_text or "") else "PARTIAL"
    q5.detail = ("the agent reported the pinned ref as its base"
                 if q5.verdict == "YES" else
                 "unconfirmed from prose; verify by diffing the pushed branch "
                 "against the pinned ref, which is what the harness does anyway")
    q5.evidence = {"result_excerpt": (first.result_text or "")[:240]}

    print("\nprevious run is terminal. sending follow-up ...")
    try:
        handle2 = transport.send(agent_id, (
            f"What token did you write into spike_one.txt? Answer with the "
            f"token only, then create spike_two.txt containing {MARKER_TWO} "
            f"and push."))
        print(f"follow-up accepted: run {handle2.run_id}")
        second = transport.wait(handle2, max_wait_s=max_wait, on_tick=_progress)

        if second.status.lower() == "finished":
            q1.verdict = "YES"
            q1.detail = ("a follow-up on a terminal agent completed normally. "
                         "The in-conversation re-prompt loop works as designed.")
        else:
            q1.verdict = "NO"
            q1.detail = (f"follow-up accepted but ended {second.status}. This "
                         f"is the reported failure mode; fall back to "
                         f"re-spawn-with-context.")
        q1.evidence = {"status": second.status, "branch": second.branch,
                       "duration_ms": second.duration_ms}

        recalled = MARKER_ONE in (second.result_text or "")
        q2.verdict = "YES" if recalled else "NO"
        q2.detail = ("the agent recalled the token from the earlier run, so "
                     "conversation state carries and a re-prompt costs no "
                     "re-explanation."
                     if recalled else
                     "the agent did not recall the earlier token - re-prompts "
                     "must restate context, changing their shape and cost.")
        q2.evidence = {"reply_excerpt": (second.result_text or "")[:240],
                       "looked_for": MARKER_ONE}

    except CursorAPIError as exc:
        q1.verdict = "NO"
        q1.detail = f"follow-up REJECTED: {exc}"
        q1.evidence = {"http_status": exc.status}
        q2.verdict = "ERROR"
        q2.detail = "not reached - the follow-up was rejected"
    except Exception as exc:
        q1.verdict = "ERROR"
        q1.detail = f"{type(exc).__name__}: {exc}"
        q2.verdict = "ERROR"

    try:
        usage = transport._call("GET", f"/agents/{agent_id}/usage")
        print(f"\nspike cost: {usage.get('cost', {}).get('chargedCents', '?')} cents")
    except CursorAPIError:
        pass
    print(f"agent left in place for inspection: "
          f"https://cursor.com/agents/{agent_id}")
    return [q1, q2, q3, q4, q5]


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(prog="spike_cursor")
    parser.add_argument("--repo", required=True)
    parser.add_argument("--model", default=None)
    parser.add_argument("--ref", default="main")
    parser.add_argument("--max-wait", type=int, default=1800)
    parser.add_argument("--out", default="spike_findings.json")
    args = parser.parse_args(argv)

    try:
        load_api_key()
    except CursorAPIError as exc:
        print(str(exc), file=sys.stderr)
        return 2

    findings = run_spike(args.repo, args.model, args.ref, args.max_wait)

    print("\n" + "=" * 70 + "\nSPIKE FINDINGS\n" + "=" * 70)
    for finding in findings:
        finding.show()

    Path(args.out).write_text(
        json.dumps([f.__dict__ for f in findings], indent=2, default=str) + "\n",
        encoding="utf-8")
    print(f"\nwritten to {args.out}")

    print("\n" + "=" * 70 + "\nWHAT THE ANSWERS MEAN\n" + "=" * 70)
    if findings[0].verdict == "YES":
        print("Q1 YES  -> build the in-conversation loop as designed.")
    else:
        print("Q1 !YES -> build re-spawn-with-context: a fresh agent carrying")
        print("           the prior diff, verdict and re-prompt. Gates survive;")
        print("           continuity and token efficiency do not.")
    if findings[2].verdict != "YES":
        print("Q3 !YES -> locate the diff by listing remote refs created since")
        print("           spawn, not by trusting the run result.")
    if findings[3].verdict != "YES":
        print("Q4 !YES -> keep `history` tool-side logging for G-HIND/G-INDEP.")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
