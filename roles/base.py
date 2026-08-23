"""What an agent is, as data.

A role is a markdown file in the repo the agent investigates, parsed by
`definition.py` and turned into one of these by `loader.py`. What lives
here is the *shape* of a role and the order its prompt is assembled in -
mechanism, with no subsystem knowledge in it.

It used to be the other way round: seven `compose()` functions in this
package held every prompt in the system, and the master was handed a
checkout of this repository so that `history` was on its path. That put
the one agent whose edits matter most in a position to read the
classifier grading it, and it meant improving a pipeline agent was an
edit to the operating system rather than to the pipeline.

**Least privilege is a property of the role.** A diarization agent needs
to read staging parquet and Cloud Run logs; a harvest agent needs the
internet and a proxy; neither needs the other's access, and nothing needs
write access to production data. Handing every agent one fat environment
would quietly undo the blast-radius reasoning the zone model depends on,
because an agent's real reach is the union of what it can edit and what
its credentials can touch.

**Credentials are not a role's business.** An earlier version declared a
`secrets` tuple and a `cursor_env` name per role. Neither was ever sent:
`cursor_rest.create()` posts `prompt`, `repos`, `autoCreatePR`, `model`
and `envVars`, and a live agent record confirms `env: {"type": "cloud"}`
with no name. So the config described a mechanism that did not exist,
which is worse than no config - it reads as a guarantee.

The real mechanism is simpler and already in place: the Cursor
environment carries an Application Default Credential, and every repo
carries `utils/hydrate_env.py`. An agent hydrates what it needs from
Secret Manager itself, and GCP authorises and logs each read.
"""
from __future__ import annotations

from dataclasses import dataclass, field

from .definition import MASTER, PIT, RETRO, fill


@dataclass(frozen=True)
class Role:
    id: str
    tier: str
    subsystem: str
    summary: str

    repos: tuple[str, ...]
    model: str | None = None

    # The prompt body, as authored in the repo's `agents/` directory.
    body: str = ""

    # Failure signature this role is usually sent to investigate, when it
    # has one. Feeds the recurrence signal that blocks a third same-level fix.
    signature: str | None = None

    # Which metrics this role orients on, and which of them are worth a
    # fresh collect before it spawns. Declared beside the prompt, because
    # the answer changes whenever the subsystem's measurements change.
    metrics: tuple[str, ...] = ()
    capture: tuple[str, ...] = ()

    def env_vars(self) -> dict[str, str]:
        """Who this agent is. Nothing else.

        Credentials are deliberately absent. The Cursor environment
        already carries an Application Default Credential, and each repo
        carries `utils/hydrate_env.py`, so an agent fetches exactly the
        secrets it needs from Secret Manager at the moment it needs them.

        That is better than shipping secrets in `envVars` on three
        counts: nothing sensitive crosses this API or sits in an agent
        record, every read is authorised and logged by GCP rather than by
        us, and widening an agent's reach becomes an IAM change on the
        service account instead of an edit here.
        """
        return {"TURNSTILE_AGENT_ID": self.id,
                "TURNSTILE_AGENT_ROLE": self.tier,
                "TURNSTILE_SUBSYSTEM": self.subsystem}


@dataclass
class RoleContext:
    """Everything the harness knows at the moment of spawning."""
    trigger: dict = field(default_factory=dict)
    brief: list[dict] = field(default_factory=list)
    prior_findings: list[dict] = field(default_factory=list)
    signature_history: dict = field(default_factory=dict)
    run_record: dict = field(default_factory=dict)
    extra: dict = field(default_factory=dict)

    # ---------------------------------------------------------- blocks

    def trigger_block(self) -> str:
        if not self.trigger:
            return ""
        lines = ["## What brought you here", ""]
        metric = self.trigger.get("metric")
        if metric:
            lines.append(
                f"{metric} is {self.trigger.get('value')} against a target of "
                f"{self.trigger.get('target')}"
                + (f" ({self.trigger['dims']})" if self.trigger.get("dims") else "")
            )
        if self.trigger.get("detail"):
            lines.append(self.trigger["detail"])
        return "\n".join(lines)

    def brief_block(self) -> str:
        """This run + daily bars + window means. A bare level is close to useless."""
        if not self.brief:
            return ""
        lines = ["## Where the numbers are", "",
                 "| metric | last | mean_7d | mean_28d | n_7d |",
                 "|---|---|---|---|---|"]
        for m in self.brief:
            lines.append(
                f"| {m.get('id')} | {m.get('last', m.get('value'))} "
                f"| {_fmt(m.get('mean_7d'))} | {_fmt(m.get('mean_28d'))} "
                f"| {m.get('n_7d', '-')} |")
        # Compact last_7 series under the table when present
        for m in self.brief:
            bars = m.get("last_7") or []
            shown = [b for b in bars if b.get("n")]
            if not shown:
                continue
            bits = ", ".join(
                f"{b['label'][-5:]}={b['v']}" for b in shown[-7:])
            lines.append(f"last_7 `{m.get('id')}`: {bits}")
        lines.append("")
        lines.append(
            "last is this instance; mean_* average every instance in the "
            "window; last_7 is the day-by-day view. A number inside its "
            "target while the bars are sliding down is more urgent than "
            "one that breached today.")
        return "\n".join(lines)

    def prior_block(self) -> str:
        """What was already found here, and what happened to it."""
        if not self.prior_findings:
            return ""
        lines = ["## What has already been found here", ""]
        for f in self.prior_findings[:3]:
            lines.append(f"- {f.get('date', '?')} {f.get('agent', '?')}: "
                         f"{f.get('summary', '')}")
            if f.get("disposition"):
                lines.append(f"  -> {f['disposition']}")
        lines.append("")
        lines.append(
            "This is a record of what was already tried, not a playbook. "
            "Do not rerun the last approach because it is nearby in "
            "agent_state. If you reach the same conclusion, say so and "
            "stop — recurrence is itself the finding. If you reach a "
            "different one, say what changed your mind.")
        return "\n".join(lines)

    def signature_block(self) -> str:
        history = self.signature_history
        if not history or not history.get("closures"):
            return ""
        closures = history["closures"]
        lines = [f"## This problem has been closed before ({len(closures)}x)", ""]
        for c in closures:
            lines.append(f"- {c.get('date', '?')}: {c.get('what', '')} "
                         f"-> {c.get('outcome', 'did not hold')}")
        lines.append("")
        if len(closures) >= 2:
            lines.append(
                "Two attempts at this level have already failed. A third of "
                "the same shape is not a plan. Either find what makes this "
                "recur, or say plainly that the structure is right and the "
                "recurrence is worth tolerating - with what tolerating it "
                "costs.")
        return "\n".join(lines)


def _fmt(value) -> str:
    if value is None:
        return "-"
    try:
        return f"{float(value):g}"
    except (TypeError, ValueError):
        return str(value)


def _signed(value) -> str:
    if value is None:
        return "-"
    try:
        return f"{float(value):+g}"
    except (TypeError, ValueError):
        return str(value)



from . import metrics as role_metrics


def render(role: Role, context: RoleContext,
           orientation: dict[str, str] | None = None,
           scale_framework: str | None = None) -> str:
    """Assemble the spawn prompt.

    Order is deliberate: who you are, why you were woken, what the data
    says, what is already known, then how to finish. Nothing here names
    a zone, a limit or a tier - an agent told what it is permitted to
    change optimises for what is permitted rather than for what is
    correct, and the blocked change that was the *right* change is the
    most valuable thing this system produces.

    The body arrives from the repo's `agents/` directory rather than
    from a function in this package. Everything after it is still
    composed here, by code, from what the harness measured.
    """
    if not role.body.strip():
        raise ValueError(f"role {role.id} has no prompt body")
    focus = role_metrics.metrics_for_role(role, context.trigger)
    blocks = [
        fill(role.body, context.trigger.get("dims")),
        _scale_block(role, scale_framework),
        context.trigger_block(),
        context.brief_block(),
        role_metrics.orientation_for(focus, orientation),
        context.signature_block(),
        context.prior_block(),
        _closing(role),
    ]
    return "\n\n".join(b.strip() for b in blocks if b and b.strip()) + "\n"


def _digest(text: str, limit: int = 900) -> str:
    """First real paragraphs of the operator direction, for allusion."""
    chunks: list[str] = []
    buf: list[str] = []
    for raw in (text or "").splitlines():
        line = raw.strip()
        if line.startswith("#"):
            continue
        if not line:
            if buf:
                chunks.append(" ".join(buf))
                buf = []
            continue
        buf.append(line)
        if len(chunks) >= 3:
            break
    if buf and len(chunks) < 3:
        chunks.append(" ".join(buf))
    digest = " ".join(chunks)
    if len(digest) > limit:
        digest = digest[:limit].rsplit(" ", 1)[0] + "..."
    return digest


def _scale_block(role: Role, scale_framework: str | None) -> str:
    if role.tier != MASTER:
        return ""
    lines = [
        "## How this system is meant to grow",
        "",
        "Read `agents/_scale_framework.md` before you wager. It is the "
        "operator's direction for scale — not a task list, and not last "
        "cycle's diff. Let it change which landmark you pick "
        "(`build_agent`, `new_module`, `reduce`, `repair`, `hold`). "
        "Leave the file as you found it.",
    ]
    digest = _digest(scale_framework or "")
    if digest:
        lines += ["", digest]
    return "\n".join(lines)


def _closing(role: Role) -> str:
    if role.tier == MASTER:
        return (
            "## Finishing this turn\n\n"
            "Give every item on your docket a disposition: approve, revise, "
            "reject, or defer. A defer names the evidence that would settle "
            "it, so a later cycle can pick it up cheaply.\n\n"
            "The docket is why you were woken, not a to-do list. You may "
            "reject every proposed patch and still have a complete cycle — "
            "if the heart of it is a new watcher, a new module, or a wait. "
            "`repair` is one landmark, not the default.\n\n"
            "Then commit a short wager at `agent_state/wager.json` on this "
            "branch — mode, headline, why_now, expected_effect (metric, "
            "direction, rationale), and the paths you expect to touch. "
            "why_now is the evidence. rationale is why the product needs "
            "this now, not which function is messy.\n\n"
            "If `agent_state/wager.json` is already on the branch from a "
            "previous cycle, replace it. That was someone else's bet.\n\n"
            "`hold` is a complete outcome when the evidence does not yet "
            "support acting. Commit the wager and the dispositions either "
            "way; the branch is the record.\n\n"
            "Do not start implementing in this turn. Say what you would do "
            "and why, then stop.")
    return (
        "## Finishing\n\n"
        "Commit and push your work when you are done.\n\n"
        "If you conclude that no change is warranted, write that in your "
        "ledger notes and commit it - one line, saying what you checked and "
        "why it did not warrant a change. A conclusion recorded is a finding; "
        "a conclusion left in your head is a gap in the record.")


class Registry:
    """Every role one repo declares, indexed.

    Built per checkout by `loader.load()` rather than populated at import
    time, because which agents exist is a property of the subsystem.
    """

    def __init__(self) -> None:
        self._roles: dict[str, Role] = {}
        self.orientation: dict[str, str] = {}
        self.scale_framework: str = ""

    def add(self, role: Role) -> Role:
        if role.id in self._roles:
            raise ValueError(f"duplicate role id: {role.id}")
        self._roles[role.id] = role
        return role

    def get(self, role_id: str) -> Role:
        if role_id not in self._roles:
            raise KeyError(f"unknown role: {role_id}. "
                           f"known: {', '.join(sorted(self._roles))}")
        return self._roles[role_id]

    def for_subsystem(self, subsystem: str, tier: str | None = None) -> list[Role]:
        return [r for r in self._roles.values()
                if r.subsystem == subsystem and (tier is None or r.tier == tier)]

    def all(self) -> list[Role]:
        return sorted(self._roles.values(), key=lambda r: (r.subsystem, r.tier, r.id))

    def ids(self) -> list[str]:
        return sorted(self._roles)

    def __len__(self) -> int:
        return len(self._roles)

    def __contains__(self, role_id: object) -> bool:
        return role_id in self._roles
