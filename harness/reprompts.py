"""Re-prompt text - the single source of truth for everything an agent
reads when a gate fires.

These are not error messages. A gate is the one moment where the harness
has the agent's full working context and can direct it toward the
analysis it skipped, so every re-prompt does three things:

    1. name the specific trip, with numbers
    2. give the next concrete action, in order of preference
    3. ask a question that makes the agent reconsider the change itself
       rather than reformat the paperwork

The preferred first action for a blocked change is *reduction*, not
splitting. Splitting a change that is too broad usually preserves the
complexity and merely spreads it; removing what is not load-bearing is
what actually shrinks the system.
"""
from __future__ import annotations

import re
import textwrap
from typing import Iterable

from . import keywords
from .scorecard import coaching

# ASCII only, deliberately. A re-prompt that fails to encode is a re-prompt
# the agent never sees, and this text is the entire enforcement mechanism  -
# it has to survive a cp1252 console, a CI log and a Slack paste unchanged.
RULE = "=" * 70


_BULLET = re.compile(r"^\s*(?:[*-]|\d+\.)\s+")
WIDTH = 76


def _wrap(text: str, indent: str = "") -> str:
    """Re-flow prose to terminal width without breaking sentences apart.

    Source strings here are written as readable multi-line literals, so
    consecutive non-blank lines are one paragraph and get joined before
    filling. Bullets and indented blocks each stay on their own, otherwise
    a list collapses into a wall.
    """
    out: list[str] = []
    buffer: list[str] = []
    lead = 0

    def flush() -> None:
        nonlocal buffer, lead
        if not buffer:
            return
        pad = indent + " " * lead
        out.append(textwrap.fill(
            " ".join(part.strip() for part in buffer),
            width=WIDTH, initial_indent=pad,
            subsequent_indent=pad + ("  " if _BULLET.match(buffer[0]) else ""),
        ))
        buffer = []

    for raw in text.strip("\n").split("\n"):
        if not raw.strip():
            flush()
            out.append("")
            continue
        this_lead = len(raw) - len(raw.lstrip(" "))
        if _BULLET.match(raw) or (buffer and this_lead != lead
                                  and not _BULLET.match(buffer[0])):
            flush()
            lead = this_lead
        elif not buffer:
            lead = this_lead
        buffer.append(raw)
    flush()
    return "\n".join(out).rstrip()


def _signal_lines(signals: Iterable[dict]) -> str:
    lines = []
    for sig in signals:
        limit = sig.get("limit")
        value = sig.get("value")
        if limit is not None and value is not None:
            lines.append(f"  * {sig['detail']}  ({value} vs limit {limit})")
        else:
            lines.append(f"  * {sig['detail']}")
    return "\n".join(lines)


# ========================================================== submit verdicts

def green(paths: list[str], oracle: str | None, merged_into: str) -> str:
    body = f"MERGED into {merged_into}.\n\n"
    body += f"  {len(paths)} file(s) changed.\n"
    if oracle:
        body += f"  Acceptance check passed: {oracle}\n"
    body += "\nYour change is live. Continue, or end the session if the work is done."
    return body


def amber(signals: list[dict], attempt: int, attempts_allowed: int,
          zone_hint: str | None = None) -> str:
    remaining = attempts_allowed - attempt
    header = f"{RULE}\nNOT MERGED - this change is broader than merges unreviewed.\n{RULE}"

    trips = _signal_lines(signals)

    if remaining > 0:
        action = _wrap("""
FIRST, TRY TO REDUCE IT. Not split it - reduce it. Splitting a broad change
into three smaller ones usually keeps every bit of the complexity and just
spreads it across three commits.

Re-read your own diff and ask, line by line: what here is actually load
bearing for the problem you were sent to solve? Changes grow while you work.
Defensive branches get added for cases that cannot occur. Helpers get
extracted that have one caller. Config gets introduced for a value that never
varies. Ancillary tidying attaches itself to the real fix.

Then ask the harder question: is there a less invasive solution that fixes the
same underlying problem? Touching four files often means the fix is being
applied at the wrong level - that a single change further up would make three
of the four unnecessary. If you can find that change, it is almost always the
better one, and it is usually smaller.

When you have cut it back, commit and push again. The harness re-judges
your branch when the turn ends. You have """ + f"{remaining} re-try" + ("s" if remaining != 1 else "") + """ before the change must be proposed instead.
""")
        action += "\n\n" + _wrap("""
IF IT IS ALREADY MINIMAL, PROPOSE IT. A change that cannot be reduced without
losing the fix is a change that needs a human decision, not a smaller diff.
Commit a proposal JSON under `agent_state/proposals/` on the branch you
already pushed and push again. Your work is preserved either way.
""")
    else:
        action = _wrap("""
YOU HAVE USED YOUR RE-TRIES. This change now needs a human decision rather
than another attempt at the shape.

Commit a proposal JSON under `agent_state/proposals/` on the branch you
already pushed. Your work is preserved in full - the diff travels with
the proposal, so nothing you have built is lost.

In the proposal, say what you tried to remove and could not. A reviewer
learning that the change is irreducible, and why, is more useful than a
reviewer reading the diff cold.
""")

    footer = _wrap("""
ONE THING TO KNOW: this attempt is recorded whether or not you reduce it to a
merge. That is deliberate. A subsystem where agents keep reaching past their
own footing is a subsystem with a design problem, and that signal is worth
more than a clean-looking history. So do not narrow the change to hide it  -
narrow it only if the narrow version is genuinely better.
""")

    parts = [header, "", "What tripped:", trips, "", action, "", footer]
    if zone_hint:
        parts.insert(4, f"\n  (These paths sit in: {zone_hint})")
    return "\n".join(parts)


def no_diff(result_text: str) -> str:
    """The run finished and the branch is empty.

    Deliberately makes no guess about why. It asks for one line on the
    record, which is cheap for the agent and turns "nothing happened"
    into something a later reader can actually audit.
    """
    excerpt = " ".join((result_text or "").split())[:300]
    parts = [
        f"{RULE}\nNO GIT DIFF IS SHOWING FOR THIS BRANCH.\n{RULE}", "",
    ]
    if excerpt:
        parts += ["  You finished with:", _wrap(excerpt, indent="  | "), ""]
    parts.append(_wrap("""
Nothing was committed, so there is nothing for the system to review, and
right now this run is indistinguishable from one that never started.

If your investigation concluded that no change is necessary, record that
in your ledger notes - one line is enough. Say what you checked and why
it did not warrant a change. That turns "nothing was needed" into a
finding somebody can read in three months instead of a gap in the record.

If instead something stopped you - a credential, a table, a file, an
upstream job that had not run - write that down in the same place, with
the exact error rather than a paraphrase. Whoever picks this up cannot
re-run your session. A blocker recorded is a problem the system can fix;
a blocker that evaporates with the session is one it will meet again.

Then commit the note and finish.
"""))
    return "\n".join(parts)


def freestyle_handoff_missing(session_id: str = "") -> str:
    """Freestyle finished without the interaction handoff on its branch."""
    sid = session_id or "<session_id>"
    path = f"state/{sid}/rd_retro_handoff/interaction.jsonl"
    return "\n".join([
        f"{RULE}\nINTERACTION HANDOFF MISSING ON YOUR BRANCH.\n{RULE}",
        "",
        _wrap(f"""
Your job is to browse, prove downloadable routes, and commit the M1
interaction log. Without `{path}` (non-empty) on the branch you pushed,
bake cannot run and this freestyle turn is incomplete.

Commit and push:
  - `{path}`
  - `state/{sid}/rd_retro_handoff.json` (or manifest under the handoff
    dir) with success_urls / route_keys when routes should bake

Do not rewrite harvester modules. Finish the handoff, then stop.
"""),
    ])


def blocked_without_change(result_text: str) -> str:
    """The agent finished, changed nothing, and its own words say it was stuck.

    This is the case a diff cannot distinguish. "I looked and nothing
    needed changing" and "I could not get access to the thing I needed"
    produce identical branches, and recording the second as the first is
    how a real blocker disappears into a clean-looking run.
    """
    excerpt = " ".join((result_text or "").split())[:400]
    return "\n".join([
        f"{RULE}\nNOTHING CHANGED - and it reads like you were stopped, not "
        f"finished.\n{RULE}", "",
        "  You wrote:", _wrap(excerpt, indent="  | "), "",
        _wrap("""
If you were blocked, that is a finding and it needs to survive this
session. A run that ends "I could not proceed" and leaves nothing behind
is indistinguishable from a run that found nothing wrong, and the
difference matters more than whatever you were originally sent to do.

Say precisely what stopped you:

  * what you needed - a credential, a table, a file, a permission, an
    upstream job that had not run
  * what you tried, and what it returned. The exact error beats a
    paraphrase; whoever reads this next cannot re-run your session
  * whether it blocks only your task or everything downstream of it

Then commit a proposal JSON under `agent_state/proposals/` on your branch
and push. A proposal with no code diff is completely valid here - the
change being asked for is to the system's ability to see, not to its
behaviour, and those are usually the cheapest and most valuable things
this system ever ships.

If you were NOT blocked and genuinely concluded no change was warranted,
say that plainly instead, without the hedging - "I checked X, Y and Z
against the batch; the metric moved for reason R; no code change is
warranted" is a complete and successful outcome.
"""),
    ])


def final_attempt_used(signals: list[dict], tier: str | None) -> str:
    trips = _signal_lines(signals)
    return "\n".join([
        f"{RULE}\nSTILL NOT MERGEABLE - and there are no attempts left.\n{RULE}",
        "", "What tripped:", trips, "",
        _wrap("""
This now needs a human decision rather than another attempt at the shape.

Commit a proposal JSON under `agent_state/proposals/` on the branch you
pushed. Your work is preserved in full - the branch travels with the
proposal, so nothing you built is lost, and a reviewer reading a real
diff learns far more than one reading a description of it.

Say what you tried to remove and could not. A reviewer who learns the
change is irreducible, and why, is being handed the actual finding.
"""),
    ])


def proposal_formulation(tier: str | None, path: str,
                         signals: list[dict] | None = None) -> str:
    """Turn boundary after a substantive block — steer toward a proposal.

    The agent has already done its investigation. This message does not ask
    it to undo work or shrink the diff; it asks it to keep the branch and
    add a proof-of-reasoning file the master can read.
    """
    _ = signals  # reserved for a brief context line later; not a wall of trips
    schema = "\n".join([
        f"Commit a JSON file at `{path}` on the branch you already pushed,",
        "with exactly these fields:",
        "",
        '  "change"               what changes, in plain language',
        '  "why_now"              the evidence you measured or read',
        '  "evidence_refs"        file paths, metric ids, run ids, log lines',
        '  "tradeoff"             <=30 words: cost, benefit, which objective moves',
        '  "expected_effect"      {"metric": "<lifeblood metric id>",',
        '                          "direction": "up" | "down" | "flat",',
        '                          "rationale": "why the product needs this now"}',
        '  "irreducible_because"  what you tried to simplify and could not',
    ])
    return "\n".join([
        _wrap("""
Your investigation pass is complete and the work on your branch is the
signal — keep every code change you made. Do not revert commits or strip
files to make the diff smaller.

Because this change touches more than we merge unattended, we need a short
proof-of-reasoning proposal before it goes to a master reviewer. Use this
turn to refine your thinking: we want first-principles fixes that address
root causes, not patches that accumulate tech debt. If you see a cleaner
shape while writing the proposal, improve the code on the same branch — but
start from what you already built rather than starting over.
"""),
        "",
        schema,
        "",
        _wrap("""
Push when the JSON is committed alongside your code. A withdrawn change
with reasoning recorded is a valid outcome; an empty branch is not.
"""),
    ])


def escalated(signals: list[dict], tier: str | None, path: str) -> str:
    """Fallback when escalation fires but no proposal file landed yet."""
    return proposal_formulation(tier, path, signals)


def red(signals: list[dict], attempt: int) -> str:
    header = f"{RULE}\nNOT MERGED - and this one cannot be narrowed into a merge.\n{RULE}"
    trips = _signal_lines(signals)

    body = _wrap("""
The paths above are part of the system's protected surface: control-plane
logic, infrastructure, schema, the instruments that measure this system's
work, or the definition of an agent's own behaviour. No agent merges changes
here - not because your change is wrong, but because a system that can
rewrite its own limits has none.

Do not try to reshape this into something that merges. There is no size of
diff that makes it merge, and attempting one wastes the session.

Commit a proposal JSON under `agent_state/proposals/` and push.
""")

    ask = _wrap("""
THEN TAKE THE STEP THIS DESERVES. A proposal against the protected surface is
an argument about the architecture, and it will be read as one. So argue it.

  * What does the current design get wrong? Not what your change does  -
    what the existing structure makes impossible, expensive, or fragile.
  * Why does the fix have to live here? If it could live somewhere
    unprotected, that alternative is worth naming and dismissing explicitly.
  * What breaks if this is left alone for another month? Be concrete: which
    runs, which metrics, whose work.

If, while writing that, you find the answer is "nothing much" - say so and
withdraw. Commit a one-line withdrawal note on the branch and push. That is
a real outcome and it is recorded as one, not as a failure.
""")
    return "\n".join([header, "", "What tripped:", trips, "", body, "", ask])


# =========================================================== acceptance

def oracle_failed(zone_id: str, command: str, output: str) -> str:
    header = f"{RULE}\nNOT MERGED - the acceptance check for this area did not pass.\n{RULE}"
    body = _wrap(f"""
Zone '{zone_id}' carries an acceptance check, and changes here merge on the
strength of that check rather than on review. It ran and it failed:

  {command}
""")
    tail = _wrap("""
This is the check that lets you change this area without asking anyone, so it
is worth reading its output carefully rather than working around it.

Three possibilities, in the order they are usually true:

  1. The change does not do what you think it does. Most common. Read the
     failure literally before deciding it is wrong about you.
  2. The change is right but incomplete - it works for the case you were
     looking at and breaks a case the fixtures cover. That is the check
     doing its job.
  3. The check itself is wrong or stale. This happens, and it is a real
     finding. But you cannot fix it yourself: the check is protected,
     precisely because it grades you. If you believe this is the case,
     commit a proposal that makes that the argument - with the evidence
     that the fixture no longer reflects reality.

Fix the check failure, commit and push again, or propose.
""")
    excerpt = "\n".join(output.strip().splitlines()[-25:])
    return "\n".join([header, "", body, "  Output (last 25 lines):",
                      textwrap.indent(excerpt, "  | "), "", tail])


def tests_failed(command: str, output: str) -> str:
    excerpt = "\n".join(output.strip().splitlines()[-25:])
    return "\n".join([
        f"{RULE}\nNOT MERGED - the test suite failed.\n{RULE}", "",
        f"  {command}", "",
        textwrap.indent(excerpt, "  | "), "",
        _wrap("""
Nothing merges over a red suite, including changes that are obviously
correct and unrelated to the failure.

If you believe the failure pre-dates your change, verify it rather than
assuming it: stash your work, run the suite on the base commit, and check.
If it fails there too, say so in your session notes and propose the fix for
the pre-existing failure separately - a broken suite blocks every agent
after you, so finding it is worth reporting.
"""),
    ])


def missing_tests(paths: list[str]) -> str:
    listing = "\n".join(f"  * {p}" for p in paths[:8])
    return "\n".join([
        f"{RULE}\nNOT MERGED - code changed, no test changed.\n{RULE}", "",
        listing, "",
        _wrap("""
Changes outside the designated zones carry a test with them. Not for
ceremony: this system's recurring failures are overwhelmingly things that
worked when they were written and quietly stopped working later, and a test
is the only thing that notices.

Write the test that would have failed before your change and passes after
it. If you cannot construct one, that is informative - it usually means the
behaviour depends on live cloud state, in which case say so explicitly and
propose the change rather than merging it blind.
"""),
    ])


# ========================================================== proposal gates

def proposal_evidence(unresolved: list[dict], reused: list[dict]) -> str:
    lines = [f"{RULE}\nPROPOSAL INCOMPLETE - evidence.\n{RULE}", ""]

    if unresolved:
        lines.append("These references do not resolve to anything:")
        lines += [f"  * {u['ref']}  ({u['reason']})" for u in unresolved]
        lines.append("")
        lines.append(_wrap("""
A reference has to point at something a reader can open: a file path (with
an optional :line), a metric id from the lifeblood catalogue, a run record,
a failure signature id, or a committed script. Fix or drop them.
"""))
        lines.append("")

    if reused:
        listing = "\n".join(
            f"  * {r['ref']}  (already cited by {r['cited_by']})" for r in reused
        )
        lines.append("This evidence has been used before:")
        lines.append(listing)
        lines.append("")
        lines.append(_wrap("""
That is not disqualifying - a fact stays a fact. But if the same evidence
supported an earlier proposal and the problem is still here, the evidence is
not settling the question, and repeating it will not settle it this time
either.

WRITE A NEW TEST INSTEAD. You can build a script that produces a view of
this problem nobody has looked at yet, commit it, and cite it from the
proposal (for example under `tools/evidence/`). The script is committed
alongside the proposal, so a reviewer can re-run it, and so can the next
agent that meets this problem. This is the single most valuable thing you
can add to a stale argument - one new measurement is worth more than
another restatement of the old ones.

What would you measure if you were trying to prove your own explanation
wrong? Measure that.
"""))
    return "\n".join(lines)


def proposal_purpose() -> str:
    """why_now / rationale never left the discrete technical problem."""
    return "\n".join([
        f"{RULE}\nPROPOSAL INCOMPLETE - why the product needs this.\n{RULE}",
        "",
        _wrap("""
The change is described, but not why the product needs this now.

A function being wrong, a selector failing, a job skipping a row — those
are the how. The why is what happens to verified output, coverage,
quality, cost, or a bottleneck those numbers are already showing. If this
lands and nothing a user or a later run would notice has changed, it is
not yet a proposal; it is a patch looking for a reason.

Write `expected_effect.rationale` as that product reason. Point at a
metric the system already collects. Then look once more at whether this
change is the smallest way to move that number, or whether the docket
item in front of you is an instance of a larger capacity gap.
"""),
    ])


def proposal_metric(metric: str, available: list[str]) -> str:
    sample = ", ".join(available[:12]) or "(catalogue is empty)"
    return "\n".join([
        f"{RULE}\nPROPOSAL INCOMPLETE - expected effect.\n{RULE}", "",
        f"  '{metric}' is not collected by this system.", "",
        _wrap(f"""
Nobody will ever know whether this change worked. That is the problem - not
the field being wrong.

Point at something the system already measures:

  {sample}

If the right measurement genuinely does not exist, that is worth saying out
loud, and it changes the proposal: adding the measurement becomes part of
what you are proposing, and is often the more valuable half. A change whose
effect cannot be seen cannot be learned from, and this system has a long
record of exactly that.
"""),
    ])


def proposal_tradeoff(result: dict, statement: str) -> str:
    lines = [f"{RULE}\nPROPOSAL INCOMPLETE - trade-off statement.\n{RULE}", ""]

    if result["empty"]:
        lines.append(_wrap("""
The trade-off statement is empty. It is the one field a human actually reads
before deciding, so it cannot be skipped.
"""))
    elif result["over_word_cap"]:
        lines.append(_wrap(f"""
The trade-off statement is {result['words']} words. The cap is
{keywords.MAX_WORDS}.

The cap is the exercise. Anything that survives thirty words is the part you
actually believe; everything cut was hedging, restatement, or implementation
detail that belongs in the diff. If you cannot get under the cap, that is
usually a sign the change is doing more than one thing - and if it is, it
should probably be two changes.
"""))

    if not result["enough_coverage"]:
        if lines[-1] != "":
            lines.append("")
        lines.append(_wrap("""
It also needs to engage more than one axis. Right now it touches
"""
            + (", ".join(result["groups_present"]) or "none")
            + """, and a trade-off with only one side is not a trade-off.
"""))
        lines.append("")
        for group in keywords.missing_groups(statement):
            lines.append(_wrap(group.snippet, indent="  "))
            lines.append("")

    lines.append(_wrap(f"""
WRITE IT AS A PROPOSITION. In {keywords.MAX_WORDS} words or fewer, answer:
what gets worse, what gets better, and which objective or metric moves.

  "More complexity in the diarization module and two extra handoffs between
   stages, for a large gain in speaker-label accuracy and overall data
   quality."

  "Removes a job, a queue entry and a stage handoff; publish runs a few
   minutes longer; the chain stops breaking between stages."

Note what both examples do: they name a real cost. If yours has no cost
side, look harder - and if there genuinely is none, say "no principle
sacrificed" explicitly so a reader knows you looked.

AND ONE MORE THING. If, while writing this, you conclude the change is not
worth what it costs: withdraw it. Commit a one-line withdrawal note and
push. That is a successful outcome and it is recorded as one.
"""))
    return "\n".join(lines)


# =========================================================== master gates

def hindsight(missing_paths: list[str], missing_signatures: list[str],
              span_needed_days: int, span_seen_days: int) -> str:
    lines = [f"{RULE}\nPLAN NOT ACCEPTED - it has not been reconciled with history.\n{RULE}", ""]
    lines.append(_wrap("""
Changes are made in the hindsight of a technical journey. This plan touches
code with a past, and the past has not been read.
"""))
    lines.append("")
    if missing_paths:
        lines.append("  Not looked at:")
        lines += [f"    history query --path {p} --span 90d" for p in missing_paths[:6]]
    if missing_signatures:
        lines += [f"    history query --signature {s} --span 180d" for s in missing_signatures[:6]]
    if span_seen_days < span_needed_days:
        lines.append(
            f"    (your queries spanned {span_seen_days}d; the oldest item on "
            f"the docket is {span_needed_days}d old)"
        )
    lines.append("")
    lines.append(_wrap("""
Then answer both of these in the plan itself:

  * DOES THIS CONTRADICT A DIRECTION PREVIOUSLY TAKEN HERE? If it does, name
    the decision and justify reversing it. Reversing a past decision is
    allowed and is sometimes exactly right - reversing one silently is not,
    because the next agent inherits a codebase that argues with itself and
    no record of which side won.

  * HAS THIS BEEN ATTEMPTED BEFORE? If so, how does yours differ from the
    attempt that did not hold? "The same fix, done properly this time" is
    not a difference. Say what is structurally different, or plan something
    else - a third attempt at the same level as two failures is the pattern
    this system exists to break.

If the history shows your plan is a repeat, that is not a wasted cycle. It
is the most useful thing you could have found, and it should change the
plan.
"""))
    return "\n".join(lines)


def independence(proposal_id: str, commands_seen: list[str]) -> str:
    listing = "\n".join(f"    {c}" for c in commands_seen[:6]) or "    (none)"
    return "\n".join([
        f"{RULE}\nPLAN NOT ACCEPTED - you are endorsing work you did not test.\n{RULE}", "",
        _wrap(f"""
Every command in this session also appears in {proposal_id}'s own evidence:
"""),
        listing, "",
        _wrap("""
Re-running someone else's evidence confirms the evidence. It does not
confirm the conclusion drawn from it - and the conclusion is what you are
being asked to approve.

Run at least one check of your own that could have come out the other way.
The useful question is not "does the evidence hold up" but "what would I
look at if I suspected this proposal was wrong?" Look at that.

Concretely, the things that most often break a plausible proposal:

  * the effect is real but attributed to the wrong cause - check whether it
    also appears where the proposed cause is absent
  * the sample the proposer looked at is not representative - check the
    cases they did not open
  * the fix works and something else regresses - check the neighbouring
    metric, not the target one
  * it already happened before and did not hold - check the history

If the proposal survives, say so in one line and continue. If it does not,
you have caught something, and that is the more valuable outcome.
"""),
    ])


def dispositions(missing: list[str], total: int) -> str:
    listing = "\n".join(f"  * {m}" for m in missing)
    return "\n".join([
        f"{RULE}\nCYCLE NOT COMPLETE - {len(missing)} of {total} docket items have no disposition.\n{RULE}", "",
        listing, "",
        _wrap("""
Every item gets one of: approve / revise / reject / defer.

DEFER IS FINE and is often the right answer - most problems do not need to
be solved on the day they are noticed. But a defer must name the evidence
that would settle it, so a later cycle can pick it up cheaply instead of
starting over. Record the disposition on your branch (for example in
`agent_state/dispositions/`) with the evidence condition attached.

"Not enough information" without naming the information is how a problem
becomes permanent. This system has eleven open pain points and nine
recurring failure signatures, and every one of them got there by being
deferred without a condition for coming back.

REJECT IS ALSO FINE, and needs a reason, not a plan. "The cost is real but
smaller than the change" is a complete rejection.
"""),
    ])


def unread_proposal_diff(proposal_id: str) -> str:
    return "\n".join([
        f"{RULE}\nPLAN NOT ACCEPTED - you have not read what was actually built.\n{RULE}", "",
        _wrap(f"""
{proposal_id} carries a working diff, and this session never opened it:

    history diff --proposal {proposal_id}

The diff is the most informative thing in the proposal. A description tells
you what an agent meant to do; the diff tells you what it found necessary
once it was inside the code - which files resisted, what had to be threaded
through, what was harder than expected. That is usually where the real
finding is, and it is frequently different from the finding the agent wrote
down.

Read it, then decide.
"""),
    ])


def first_principles_nudge(missing: list[str]) -> str:
    """Soft check - not a hard fail. Asks the master to glance once more."""
    listing = "\n".join(f"  * {m}" for m in missing)
    return "\n".join([
        f"{RULE}\nQUICK CHECK - before you close this cycle.\n{RULE}", "",
        _wrap("""
Not a rejection. Just checking you thought about a few things that often
change the answer when they are looked at late:
"""),
        "", listing, "",
        _wrap("""
If you already did this off-transcript, say so in one line and continue.
If not, a short pass now is cheaper than discovering it later. Prefer
the smallest change that still addresses the real cause - and say if the
right move is actually a larger simplification.
"""),
    ])


# =========================================================== master turns

def implement(wager: dict) -> str:
    """Ask the master to do what it bet it would do.

    Deliberately does not name what comes after. An agent told a
    simplification pass is coming writes a draft it expects to clean up.
    """
    headline = (wager.get("headline") or "what you committed to").strip()
    mode = (wager.get("mode") or "").strip()
    surface = wager.get("surface") or []
    effect = wager.get("expected_effect") or {}
    metric = effect.get("metric")
    direction = effect.get("direction")
    rationale = (effect.get("rationale") or "").strip()
    why_now = (wager.get("why_now") or "").strip()

    lines = [
        f"{RULE}\nDO IT.\n{RULE}", "",
        _wrap(f"""
You said: {headline}
""" + (f"Strategy: {mode}." if mode else "")),
        "",
    ]
    if why_now:
        lines.append(_wrap(f"Why now: {why_now}"))
        lines.append("")
    if rationale:
        lines.append(_wrap(f"Product reason: {rationale}"))
        lines.append("")
    if surface:
        lines.append("You expected to touch:")
        lines += [f"  * {p}" for p in surface[:12]]
        lines.append("")
    if metric:
        lines.append(_wrap(
            f"You bet {metric} would move {direction or 'as you named'}. "
            f"Leave that number observable."
        ))
        lines.append("")

    if mode in ("build_agent", "new_module"):
        closer = """
Build the capability you named. The docket is why you were woken, not a
to-do list: if the heart of it is a new watcher or a new module, do that.
Do not shrink the bet back into a patch of today's instance. Keep it as
simple as the capability allows — a new file that owns one seam is better
than three helpers beside the old one.

Test what you can reach on this machine and against non-production only.
When the code and any tests are committed and pushed, stop. The branch is
the work.
"""
    elif mode == "reduce":
        closer = """
Take the surplus out. Prefer deleting a class of duplication over adding
a helper that papers over it. The bet was contraction; do not grow the
tree on the way to shrinking it.

Test what you can reach on this machine and against non-production only.
When the code and any tests are committed and pushed, stop. The branch is
the work.
"""
    else:
        closer = """
Implement it on this branch. Prefer the smallest change that realises the
bet — and if while doing it you see the real cause sits one level up, take
that instead and say so. The wager is a landmark, not a cage.

Test what you can reach on this machine and against non-production only.
When the code and any tests are committed and pushed, stop. Do not write
a plan document about the work; the branch is the work.
"""
    lines.append(_wrap(closer))
    return "\n".join(lines)


def reduce(files: int, added: int, paths: list[str] | None = None,
           hits: list[dict] | None = None) -> str:
    """A sense-check when the change grew, or when the diff smells unfinished."""
    listing = ""
    if paths:
        listing = "\n".join(f"  * {p}" for p in paths[:14])
    parts = [
        f"{RULE}\nLOOK AGAIN AT WHAT YOU BUILT.\n{RULE}", "",
        _wrap(f"""
This branch now touches {files} files ({added:+} lines). Before it goes
any further, re-read your own diff and ask what can come back out:
"""),
        "",
        _wrap("""
  * What can be deleted and the change still works?
  * What is nearly the same as something already in the tree, and belongs
    inside that thing rather than beside it?
  * Which of these files needs to exist at all for the bet you made?

Prefer removing to relocating. A helper extracted for one caller, a
config key for a value that never varies, a defensive branch for a case
that cannot occur - those are the usual surplus.

If nothing should go, say so in one line on the branch and stop. Finding
nothing to remove is a valid answer; inventing a deletion is not.
"""),
        "",
        listing,
    ]
    extra = coaching.for_reduce(hits)
    if extra:
        parts.extend(["", extra])
    return "\n".join(parts)


def wager_missing(detail: str = "") -> str:
    """ORIENT turn produced no usable wager."""
    extra = f"\n\n  {detail}" if detail else ""
    return "\n".join([
        f"{RULE}\nNO WAGER ON THE BRANCH.\n{RULE}", "",
        _wrap(f"""
Commit `agent_state/wager.json` with mode, headline, why_now,
expected_effect (metric, direction, rationale), and surface — or mode
`hold` if acting now would be premature.{extra}

why_now is the evidence you measured. rationale is why the product needs
this now: more verified output, coverage of a campaign, quality or cost
holding while volume grows, or a bottleneck those numbers already show.
A function being wrong is the how, not the why.

Mode is the landmark, not the task. The docket will pull you toward
repairing whatever was put in front of you. `build_agent` and
`new_module` are live options when existing tools cannot produce new
output by being fixed. Read `agents/_scale_framework.md` before you lock
the mode.

If a wager file is already on this branch from a previous cycle, replace
it. That was someone else's bet.

Do not implement yet. The wager is this turn's work.
"""),
    ])


def wager_invalid(problems: list[str]) -> str:
    listing = "\n".join(f"  * {p}" for p in problems)
    return "\n".join([
        f"{RULE}\nWAGER INCOMPLETE.\n{RULE}", "",
        listing, "",
        _wrap("""
Fix the wager on this branch and push again. Still do not implement —
commit the corrected bet first.

The rationale has to name a product reason, not only a code seam. If the
honest reason is 'this function is messy', that is not yet a wager; look
at whether yield, coverage, quality, or cost actually needs this now, and
whether the landmark should be a new capability instead of a repair.
"""),
    ])
