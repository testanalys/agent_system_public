# Turnstile

<!-- Once pushed, uncomment and replace <you> with your GitHub username:
[![tests](https://github.com/<you>/turnstile/actions/workflows/tests.yml/badge.svg)](https://github.com/<you>/turnstile/actions/workflows/tests.yml)
-->

A system-level harness for coding agents. It runs outside the agent's CLI,
waits for the session to finish, reads everything the agent did, and decides
what merges.

Built to run and scale [regest.ai](https://regest.ai).

---

## The problem

Agentic systems that can change the code they operate within, with minimal or
no human in the loop, suffer technical debt from introduced complexity
compounding over time.

The cause is behavioural. Agents have a propensity toward action. That
propensity feeds confirmation bias and crowds out the restrained moves —
inaction, deleting code, simplifying a design, scaling an ambition back. Ask a
good engineer to fix something and the answer is sometimes a smaller system.
Ask an agent and the answer is almost always more code.

This is hard to catch because every individual change is defensible. The
failure only exists in aggregate.

---

## Existing solutions

Current approaches to constraining agent behaviour work inside the agent
harness: the CLI infrastructure wrapped around the model loop. Slash commands,
hooks, MCP servers, subagents, rules files. It is what lets an agent interact
mechanically with its environment.

![The layers of an agent harness](docs/img/harness-layers.svg)

You can constrain from there, and it would be wrong to pretend otherwise. A
hook is a script that fires on a trigger, so you can set a trap: when the agent
reaches for a tool it should not be reaching for, block the call and re-prompt.
That is real, deterministic constraint.

It is just a poor place to *guarantee* it. A tool call is a weak proxy for a
behaviour — what you want to constrain is a disposition, and one tool call
rarely represents one. Traps get walked around, because blocking one route to
an outcome leaves the others open, and the trap set becomes its own
maintenance burden. And all of it lives where the agent lives: hook config,
rules files, skills and subagent definitions sit in the repository the agent
is working in, so an agent with write access to the repo has write access to
its own constraints.

---

## Constraining at the turn boundary

Wait until the session ends and the picture inverts. You hold the full net
diff, every tool call the agent made, and the prose it wrote in its own
docstrings. Behaviour is legible in aggregate in a way it never is at any
single point mid-run, because by then the pattern has finished happening.

The trade is honest. You let the session run to completion, so the tokens are
spent either way. In exchange you stop over-engineering constraint into the
agent layer, and you guard against behaviour rather than a proxy for it.

Use the agent harness to extend what an agent can reach. Use an outer system
harness to constrain it. This repo is that outer layer.

---

## What is in this repo

```
harness/
  classify.py       the only thing that assigns a status
  scorecard/        the rules — shape, history, habits, narration, conjunctions
  skeleton.py       zone maps; a zone without an acceptance check refuses to load
  diff.py           net session diff and its shape signals
  history_signals.py  churn, region churn, recurrence, oscillation
  reprompts.py      what gets written back into the agent's context
  triggers.py       when a supervising agent is warranted
  oracle_kit/       acceptance checks
  cursor_rest.py    agent transport (REST, stdlib only)
  agentloop.py      spawn, adjudicate, re-prompt

roles/              who the agents are: prompt composition and credential scope
skeletons/          the zone maps, one per subsystem
config/             the reference data you edit — repos, branches, identity
examples/           a working subsystem, so a fresh clone runs green
reconciler/         the scheduled tick
dashboard/          operator view: timeline, metrics, escalations
cloud/              one execution, one agent, start to finish
```

Three things carry the design.

`classify.py` is the only function that assigns a status to an agent's work.
Agents never label their own output. It takes a diff and returns a tier, a
score, and the list of signals that fired.

`scorecard/` holds the rules and their prices. Every signal is worth a number.
The numbers live in one file so they can be retuned in one place.

`skeletons/` are the zone maps: which paths an agent may write, which are
frozen, and which acceptance check has to pass before anything merges. They
live here, in a repository the agents never clone.

See [INSTRUCTIONS.md](INSTRUCTIONS.md) to run this against your own system.

---

## What implementing this brings

### Judge the session, not the tool call

One session, scored. The scorecard below is real output from `classify.py` in
this repo.

![What the agent said, and what the harness measured](docs/img/turn-boundary.svg)

Nothing the agent said was false. The yield gap was real, the fix worked, the
tests passed.

It still scored 409 against a threshold of 100, because of what it did on the
way there. It widened the definition of the metric it was being graded on. It
kept the old behaviour alive behind a new environment flag rather than
choosing. It added a file whose name was 0.9 similar to the one beside it. And
its docstring hedged while the code committed to the change as settled.

Every one of those is defensible alone. Together they are a shape, and the
shape is the finding.

No hook sees this. A hook fires on a tool call, and a tool call cannot tell you
that a filename resembles its neighbour, or that prose and code disagree about
how finished something is.

### Design subversion is what is worth catching

Most of the scorecard is not about bugs. Tests catch bugs. It is about the ways
a change can be locally correct and structurally corrosive.

| Family | Catches |
| --- | --- |
| **Shape** (`C*`) | files touched, net lines, new dependencies, new config keys, nesting depth, near-duplicate filenames, directory crowding |
| **History** (`H1`–`H4`) | churn on a file, repeat edits to one region, recurring signatures, restoring something previously removed |
| **Habits** (`H-*`) | stubs, dual paths, commented-out code, swallowed exceptions, dropped assertions, `noqa`, functions that return a hardcoded success |
| **Narration** (`N-*`) | added prose that defers, minimises, over-claims, or contradicts the body it documents |
| **Conjunctions** (`J-*`) | combinations worse than their parts — a stub with a TODO, a dual path with a new flag, a weakened oracle |

Two rules shape how they combine.

**Size alone is never fatal.** A clean, large change scores around 32 and
merges. If it were otherwise, agents would learn to make small bad changes.

**Stacked smells are.** Three mild signals plus a conjunction bonus reaches
100. One rough edge under time pressure is engineering. Four in one session is
a disposition.

The most useful rule turned out to be the simplest: **the metric an agent is
graded on is not a metric it may redefine.** An agent asked to raise a number
will, given the chance, redefine the number. That is not dishonesty. It is the
shortest path, and agents take the shortest path.

Prices are role-scaled. An investigator pays 1.3× on habits; a supervising
agent with a wider remit pays 0.7×. Same rules, different tolerance.

### Zoning

Cities do not grow by consensus and they do not grow at random. Infrastructure
is laid down first — water, rail, power — and everything else adapts around it.
Zoning is not there to stop building. It is there so that building does not
destroy the thing that makes building possible.

A zone map is the same instrument. Some paths are open, some are frozen, and
the frozen ones are frozen because they are what the rest depends on: the
instruments that measure the work, the deploy path, the definitions of the
agents themselves.

Every open zone declares an acceptance check, and a zone without one refuses to
load. Granting merge rights with no way to tell whether the change worked is
the exact failure this system exists to prevent, so it is unrepresentable
rather than discouraged.

Zone maps live in this repository, not in the repository they govern. An agent
working in a checkout cannot widen its own permissions, because the file that
would need editing was never cloned.

### Publishing the exam

Publish the questions and an exam stops measuring competence. It measures
recall of the questions.

No prompt in this system contains a zone name, a path list, a tier or a limit.
An agent gets a role, a goal, data and tools, and works how it judges best.
Rules surface only when a change actually trips one.

An agent told what it may change optimises for what is permitted rather than
for what is correct. You get compliance and lose the signal. A linter enforces
this on every prompt, and a test asserts it against every composed prompt from
the same vocabulary list, so the two cannot drift apart.

### A gate is a re-prompt, not a rejection

Nothing here just says no. Every verdict writes back into the agent's context:
what fired, why this cannot be reshaped into a merge, and what to do with the
work instead.

From the run above:

> No agent merges changes here — not because your change is wrong, but because
> a system that can rewrite its own limits has none.
>
> Do not try to reshape this into something that merges. There is no size of
> diff that makes it merge, and attempting one wastes the session.
>
> [...] A proposal against the protected surface is an argument about the
> architecture, and it will be read as one. So argue it.
>
> If, while writing that, you find the answer is "nothing much" — say so and
> withdraw. That is a real outcome and it is recorded as one, not as a failure.

A blocked change that was the *right* change is the most valuable thing this
system produces: a fully-formed argument about the architecture with a working
diff attached. So the gate asks for the argument rather than discarding the
work.

Making withdrawal a recorded success matters more than it looks. An agent that
can only succeed by shipping will always ship.

### Replication over citation

A research field fails in a specific way. A study is wrong. Nobody re-runs it,
because re-running is expensive and the result is already in the literature.
A decade of work builds on top. The error is not in any paper after the first —
it is in the shared premise nobody went back to check.

We talk about an agent as one worker with a long memory. It is not. Each
context window is a new one. That makes an agent system less like an employee
and more like a field: different people, working in sequence, each building on
what the last one wrote down. And it fails the same way, faster. Agent one
writes a summary. Agent two reads the summary instead of the code. Agent three
reads a summary of the summary.

So there are no context handoffs here, and no agent-written memory files. An
agent gets the code, the raw logs, and its own metrics with their history. To
find out what a previous agent did, it searches the full diffs and the raw
sessions. Those are primary sources. A summary is not.

### Homeostasis

A body does not regulate on absolute values. It regulates on deviation and rate
of change. A doctor reading one blood pressure knows almost nothing; the same
reading against a year of them is a diagnosis.

Every brief carries a metric as a level, a 7-day delta and a 28-day delta.
Never a bare number.

An instantaneous metric produces incident response. An agent that sees `0.31`
fixes today. An agent that sees `0.31, down from 0.62 a month ago, falling
steadily` asks a different question, and it is usually the better one.

The history is durable and lives outside the container that computed it. A
container's filesystem does not survive the run, and local history would make
every delta empty.

<!-- Drop a screenshot at docs/img/dashboard.png, then uncomment the line below.
     Left commented so the README never renders a broken image.
![Operator dashboard](docs/img/dashboard.png)
-->

*In use scaling [regest.ai](https://regest.ai) — a semantic search engine over
the spoken record.*

### The cost shape

Every always-on agent behaviour has a recurring token cost. So a design is only
approved here with an answer to one question: **what does cost scale with?**

"Scales with wall-clock" is a rejected shape. A monitoring agent that watches a
run costs money for every minute the run takes, and most of those minutes are
quiet. Worse, its context grows as it watches, so the bill is dominated by
re-reading what it already knows.

Everything here is trigger-fired. An agent starts because a specific number
crossed a specific threshold, so cost scales with the number of real problems
rather than with elapsed time. A tick costs seconds whether or not agents are
running, and nothing long-lived is hosted.

### Agents get scarcer as they get more powerful

Three tiers, with the sampling rate inversely proportional to the blast radius.

| Tier | Runs | Authority |
| --- | --- | --- |
| **Investigator** | on every breach, many in parallel | one zone, auto-merges behind an acceptance check |
| **Retro** | once per cycle, including quiet ones | reviews the cycle and the investigators; proposes |
| **Supervisor** | only when a trigger fires | wide remit, reads the system's own records, output goes to a human |

The cheap, narrow, frequent agents hold real merge authority in a small space.
The expensive, wide, rare one holds none — it produces arguments, and a person
approves them.

---

## Using this on something else

The mechanism does not know what your system does. What it knows about your
system lives in three places, all of them data rather than code:

- **a zone map** — which paths are open, which are frozen, and what has to pass
  before a change merges
- **role definitions** — one markdown file per agent, parsed by a parser that
  can only produce strings and never imports
- **acceptance checks** — one command per zone that returns non-zero when the
  change did not work

Point those at a different repository and the classifier, the scorecard and the
re-prompt loop work unchanged.

The couplings you would need to replace are the agent transport (Cursor Cloud
Agents over REST), the object store, and the job runner. The scorecard has
none — it needs a git repository and nothing else.

---

## Running it

```bash
pip install -e . && python -m pytest -q
```

No dependencies, standard library only. 239 tests, green on a fresh clone
against the bundled example subsystem.

Then point it at any git repository. Copy `docs/example-skeleton.json` in as
`skeleton.json`, edit the zones, and classify your own last commit:

```bash
harness --agent your-agent classify --base HEAD~1
```

You get the tier, the score, and every signal that fired. Exit code is 0 on
green and non-zero otherwise, so it drops into CI as-is.

Full setup in [INSTRUCTIONS.md](INSTRUCTIONS.md).

---

## What this is, and is not

It is a working system, in production, scaling one real product. It is not a
framework, it is not versioned, and the API will change.

The idea is the portable part.
