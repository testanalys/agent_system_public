# Setting this up against your own system

Everything project-specific has been replaced with a placeholder. This file
lists every one of them, in the order you need them.

You can get value from step 1 alone. Steps 2 and 3 only matter if you want
agents spawned and merged automatically.

---

## Step 1 — Run it, and classify one of your own commits

No configuration needed.

```bash
pip install -e . && python -m pytest -q
```

The suite runs against the bundled example subsystem in
`examples/example-subsystem/`, so a fresh clone is green with no setup.

Now point the classifier at a real repository. Copy the example zone map in
and edit the three sections it explains:

```bash
cp docs/example-skeleton.json /path/to/your-repo/skeleton.json
```

Then, from inside that repository:

```bash
harness --agent your-agent classify --base HEAD~1
```

You get the tier, the score and every signal that fired on your last commit.
Exit code is 0 on green, non-zero otherwise, so it drops into CI as-is.

**This is the part worth trying first.** Run it over your own recent history
and see what the scorecard says about work you already shipped.

---

## Step 2 — Declare your subsystems

A subsystem is one repository, the branch it deploys from, and the zone map
that governs it.

### `config/subsystems.json`

The only file you must edit. Replace the `example` entry:

```json
"subsystems": {
  "discovery": {
    "repo": "your-org/your-repo",
    "branch": "main",
    "skeleton": "skeletons/discovery.json"
  }
}
```

Declare the branch explicitly even when it is `main`. A reconciler that
merges into a branch nothing deploys from reports every merge as a success
and changes nothing in production.

Also set `commit_identity` to something obviously non-human, so a merge the
system made is never mistaken for one a person made.

### `skeletons/<subsystem>.json`

One zone map per subsystem. Start from `docs/example-skeleton.json`, which
documents every field inline. Three sections:

- **`limits`** — the shape budget for one session. Not hard stops; going over
  costs points.
- **`red`** — frozen paths. One hit is categorical. Give every rule a
  `reason`: the reason is quoted back to the agent and is the only
  explanation it gets.
- **`zones`** — where agents may write, and the acceptance check each zone
  requires.

**Every zone needs an oracle.** A zone declared without one refuses to load.
Before you open a zone, run its oracle against a deliberately broken input
and confirm it fails. An oracle that cannot fail is not an oracle.

Zone maps live here, in this repository, not in the repository they govern.
That is deliberate: an agent working in a checkout cannot widen its own
permissions.

### `<your-repo>/agents/*.md`

One markdown file per agent, in the repository the agent works in. Copy the
three in `examples/example-subsystem/agents/` and rewrite the bodies.

Each subsystem needs all three tiers: a `pit` investigator, a `retro`, and a
`master`.

**Do not name the rules in a prompt.** No zone names, no path lists, no
tiers, no limits. An agent told what it may change optimises for what is
permitted rather than for what is correct. This is linted, and
`tests/test_roles.py` asserts it against every composed prompt.

To hold your real definitions to that rule:

```bash
TURNSTILE_TEST_SUBSYSTEMS="discovery=../your-repo" python -m pytest tests/test_roles.py -q
```

---

## Step 3 — Wire the cloud

Only needed to run agents automatically. Everything below is a placeholder
in the current tree.

### Secrets

| Variable | What it is |
|---|---|
| `CURSOR_API_KEY` | Cursor Cloud Agents API key. Read from the environment or any sibling `.env`. |
| `GH_TOKEN` | Repo write access, for fetching agent branches and pushing merges. |

### Environment

| Variable | Default | What it is |
|---|---|---|
| `TURNSTILE_AGENT_STORE` | — | Where run records and escalations live. `gs://your-bucket` or a local directory. |
| `TURNSTILE_WORKSPACE` | `/workspace` | Where the reconciler clones repositories. |
| `TURNSTILE_SUBSYSTEMS` | `config/subsystems.json` | Override the config file location. |
| `TURNSTILE_PLANS_ROOT` | — | Optional long-form plans directory. |
| `TURNSTILE_BASE_BRANCH` | from config | Override the merge target. |
| `TURNSTILE_TEST_COMMAND` | — | Test command run as part of an acceptance check. |

The store accepts a plain directory path, so you can run the whole loop
locally before involving any cloud.

### Placeholders in the deployment files

Search and replace across `cloud/` and `dashboard/`:

| Placeholder | Replace with |
|---|---|
| `your-gcp-project` | your GCP project id |
| `your-artifact-repo` | your Artifact Registry repository name |
| `turnstile@your-gcp-project.iam.gserviceaccount.com` | your service account |
| `gs://your-bucket` | your bucket |
| `us-central1` | your region, if not this one |

The service account needs `secretmanager.versions.access` on both secrets,
and object read/write on the bucket.

These files are GCP-shaped because that is what this was built on. Nothing in
`harness/` depends on GCP — `harness/objectstore.py` takes a bucket URI or a
directory path, and everything else is git and HTTP.

---

## What you would have to replace to run this elsewhere

Three seams, in order of how much work they are:

1. **The object store** (`harness/objectstore.py`) — already abstracts a
   bucket or a directory. Adding S3 or Azure Blob is one class.
2. **The job runner** (`cloud/`) — Cloud Run jobs. Anything that runs a
   container on a schedule works.
3. **The agent transport** (`harness/cursor_rest.py`) — Cursor Cloud Agents
   over REST. This is the real coupling. It needs an API that starts an
   agent against a repo, reports the branch it pushed, and accepts a
   follow-up message on a finished session. The re-prompt loop depends on
   that last one.

`harness/scorecard/` and `harness/classify.py` depend on none of it. They
need a git repository and nothing else.

---

## Known limits

- The role tests fall back to the bundled example when no real checkout is
  configured. That is deliberate, but it means a fresh clone proves the
  mechanism rather than your configuration.
- `dashboard/` reads GCS directly and has not been abstracted the way
  `objectstore.py` has.
- Nothing is gated on token spend. Usage is recorded for visibility only.
