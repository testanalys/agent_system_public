"""The master docket and its remit.

Two things are composed here, both by code and neither by an agent:

    the docket  - everything the master must dispose of this cycle
    the remit   - what it is being asked to do, derived from *why* it woke

The remit matters more than it looks. A master woken by three blocked
changes should be asked to read them together before adjudicating any of
them, because three agents hitting the same wall in one cycle usually
means the wall is in the wrong place. A master woken by a degrading trend
that no agent raised should be asked why nothing noticed. Those are
different jobs, and a static prompt cannot tell them apart.
"""
from __future__ import annotations

import json
from dataclasses import asdict, dataclass, field
from datetime import datetime, timezone
from pathlib import Path

from . import catalogue, paths, records

DISPOSITIONS_FILE = "dispositions.json"
VALID_DISPOSITIONS = ("approve", "revise", "reject", "defer")

REMITS: dict[str, dict] = {
    "MT1": {
        "depth": "deep",
        "headline": "An agent proposed a change to the protected surface.",
        "instruction": (
            "Its diff and its argument are attached. Decide whether the "
            "architecture is wrong, not merely whether the change is safe. "
            "Read the full history of this surface before you decide: an "
            "agent reaching for the control plane is either telling you the "
            "design is obstructing correct work, or telling you the agent is "
            "confused, and the history is what distinguishes those."
        ),
    },
    "MT2": {
        "depth": "moderate",
        "headline": "Several changes were held back this cycle.",
        "instruction": (
            "Read them together before adjudicating any of them individually. "
            "Several agents hitting the same wall in one cycle usually means "
            "the wall is in the wrong place - a zone drawn too narrowly, a "
            "limit set wrong, or a seam that puts related work on both sides "
            "of a boundary. Look for the shared cause first. If there is "
            "genuinely none, say so and adjudicate them separately."
        ),
    },
    "MT3": {
        "depth": "deep",
        "headline": "A metric has been degrading and no agent raised it.",
        "instruction": (
            "Two questions, and the second one is the more important. First: "
            "what is causing the degradation. Second: why did nothing notice? "
            "Every condition, gate and agent in this subsystem ran while this "
            "was happening and none of them fired. That is a finding about "
            "the instruments, and it will keep costing us until it is fixed."
        ),
    },
    "MT4": {
        "depth": "deep",
        "headline": "A failure signature keeps returning.",
        "instruction": (
            "Previous fixes and their outcomes are attached. A fix at the same "
            "level as those attempts is not an acceptable answer here - that "
            "level has been tried and it did not hold. Either act one level "
            "up (the component, the architecture, the data model, or the "
            "agent system itself), or record explicitly that the structure is "
            "correct and this recurrence is worth tolerating, with what "
            "tolerating it costs."
        ),
    },
    "MT5": {
        "depth": "narrow-urgent",
        "headline": "The subsystem or its instruments stopped producing.",
        "instruction": (
            "Restore observability first, diagnose second. A system that "
            "cannot see itself cannot be reasoned about, and every other item "
            "on this docket is unreliable until the instruments are back. Do "
            "not begin a broad investigation while blind."
        ),
    },
    "MT6": {
        "depth": "light",
        "headline": "Routine review. Nothing is wrong.",
        "instruction": (
            "Nothing has broken and nothing was held back. Look at what has "
            "accumulated rather than at what failed: trends over the long "
            "window, files that keep being edited, zones that were never "
            "used, config that nothing reads any more. Ending with no plan is "
            "the expected outcome and is recorded as a success."
        ),
    },
    "MT7": {
        "depth": "moderate",
        "headline": "A deferred disposition is due.",
        "instruction": (
            "Something was deferred with a settle-when condition, or sat "
            "deferred past the staleness window. Re-open it. Either the "
            "evidence has arrived and a decision is cheap, or the defer "
            "had no real condition and needs one — or a disposition."
        ),
    },
}


@dataclass
class DocketItem:
    id: str
    kind: str                      # proposal | signature | metric | liveness
    summary: str
    detail: dict = field(default_factory=dict)
    age_days: int = 0
    paths: list[str] = field(default_factory=list)
    signature: str | None = None


@dataclass
class Docket:
    cycle_id: str
    subsystem: str
    created: str
    triggers: list[dict] = field(default_factory=list)
    items: list[DocketItem] = field(default_factory=list)
    remit: dict = field(default_factory=dict)
    metrics: list[dict] = field(default_factory=list)

    @property
    def oldest_age_days(self) -> int:
        return max((i.age_days for i in self.items), default=0)

    def item_ids(self) -> list[str]:
        return [i.id for i in self.items]

    def to_dict(self) -> dict:
        data = asdict(self)
        data["items"] = [asdict(i) for i in self.items]
        return data


def new_cycle_id() -> str:
    return "M-" + datetime.now(timezone.utc).strftime("%Y-%m-%dT%H%M")


def compose_remit(triggers: list[dict]) -> dict:
    """Merge the remits of every trigger, deepest first."""
    order = ["deep", "narrow-urgent", "moderate", "light"]
    ids = [t["id"] for t in triggers if t["id"] in REMITS]
    if not ids:
        ids = ["MT6"]
    chosen = sorted(ids, key=lambda i: order.index(REMITS[i]["depth"]))
    primary = chosen[0]
    return {
        "primary_trigger": primary,
        "depth": REMITS[primary]["depth"],
        "headline": REMITS[primary]["headline"],
        "instruction": REMITS[primary]["instruction"],
        "also": [
            {"trigger": t, "headline": REMITS[t]["headline"],
             "instruction": REMITS[t]["instruction"]}
            for t in chosen[1:]
        ],
    }


def build(repo: Path | None = None) -> Docket:
    repo = repo or paths.repo_root()
    state = records.load_triggers(repo)
    pending = list(state["set"].values())

    items: list[DocketItem] = []

    for proposal in records.list_proposals(repo, status="open"):
        items.append(DocketItem(
            id=proposal.id,
            kind="proposal",
            summary=(f"[{proposal.tier}] {proposal.agent}: "
                     f"{proposal.body.get('change', '(no description)')[:110]}"),
            detail={
                "tier": proposal.tier,
                "agent": proposal.agent,
                "tradeoff": proposal.body.get("tradeoff", ""),
                "evidence_refs": proposal.body.get("evidence_refs", []),
                "irreducible_because": proposal.body.get("irreducible_because", ""),
                "classifier": [s["detail"] for s in proposal.classify.get("signals", [])],
                "has_diff": bool(records.proposal_patch(proposal.id, repo)),
            },
            age_days=records.proposal_age_days(proposal),
            paths=proposal.classify.get("summary", {}).get("paths", []),
            signature=proposal.signature,
        ))

    for trigger in pending:
        if trigger["id"] == "MT4":
            sig = trigger["evidence"].get("signature")
            items.append(DocketItem(
                id=f"sig:{sig}", kind="signature", summary=trigger["reason"],
                detail=trigger["evidence"], signature=sig,
                age_days=_age(trigger.get("first_seen")),
            ))
        elif trigger["id"] == "MT3":
            metric = trigger["evidence"].get("metric")
            items.append(DocketItem(
                id=f"metric:{metric}", kind="metric", summary=trigger["reason"],
                detail=trigger["evidence"], age_days=_age(trigger.get("first_seen")),
            ))
        elif trigger["id"] in ("MT5", "MT6"):
            items.append(DocketItem(
                id=f"liveness:{trigger['id']}", kind="liveness",
                summary=trigger["reason"], detail=trigger["evidence"],
                age_days=_age(trigger.get("first_seen")),
            ))

    return Docket(
        cycle_id=new_cycle_id(),
        subsystem=catalogue.catalogue_for(repo).get("subsystem", repo.name),
        created=records.now(),
        triggers=pending,
        items=items,
        remit=compose_remit(pending),
        metrics=_metric_brief(repo),
    )


def _age(ts: str | None) -> int:
    if not ts:
        return 0
    try:
        return max(0, (datetime.now(timezone.utc)
                       - datetime.fromisoformat(ts)).days)
    except ValueError:
        return 0


def _metric_brief(repo: Path) -> list[dict]:
    """Full-catalogue brief (master). Prefer ``brief_for`` for spawns."""
    return brief_for(repo, audience="master")


def brief_for(repo: Path, *, audience: str = "pit", role=None,
              trigger: dict | None = None) -> list[dict]:
    """Audience-scoped metric brief using last / last_7 / mean_7d / mean_28d.

    PIT: ≤2 metrics (trigger + what the role declares). Retro: the set its
    definition names. Master: the whole catalogue.

    Which metrics a role reads is declared in the repo's ``agents/``
    directory rather than mapped here. That map used to be two constants
    in the harness naming pipeline and discovery metrics by hand, which
    meant retuning what an agent looked at was an edit to the operating
    system - and meant this function had to guess a subsystem from a
    catalogue string to decide which constant applied.
    """
    from roles import metrics as role_metrics

    cat = catalogue.catalogue_for(repo)
    known = {e["id"]: e for e in cat.get("metrics", [])}
    if role is not None and audience in ("pit", "retro"):
        ids = role_metrics.metrics_for_role(role, trigger)
    else:
        ids = [e["id"] for e in cat.get("metrics", [])]

    out = []
    for mid in ids:
        if mid not in known and audience == "pit":
            # Still try history — trigger may name a live metric
            pass
        row = _trend_row(mid, repo, known.get(mid, {}))
        if row:
            out.append(row)
    return out


def _trend_row(metric_id: str, repo: Path, entry: dict) -> dict | None:
    """Build one brief row from durable/local history (same semantics as lifeblood)."""
    from datetime import date, timedelta

    rows = [r for r in catalogue.readings(metric_id, repo=repo)
            if r.get("status") == "ok" and isinstance(r.get("value"), (int, float))]
    if not rows:
        return None

    def _ts(r):
        return datetime.fromisoformat(r["ts"])

    numbered = []
    for r in rows:
        try:
            numbered.append((_ts(r), r))
        except (KeyError, ValueError):
            continue
    if not numbered:
        return None

    _, latest = numbered[-1]
    now = datetime.now(timezone.utc)
    cut7 = now - timedelta(days=7)
    cut28 = now - timedelta(days=28)
    vals7 = [float(r["value"]) for ts, r in numbered if ts >= cut7]
    vals28 = [float(r["value"]) for ts, r in numbered if ts >= cut28]

    start_day = (now - timedelta(days=6)).date()
    buckets: dict[date, list[float]] = {}
    for ts, r in numbered:
        day = ts.date()
        if day < start_day:
            continue
        buckets.setdefault(day, []).append(float(r["value"]))
    last_7 = []
    for i in range(7):
        day = start_day + timedelta(days=i)
        vals = buckets.get(day) or []
        if not vals:
            last_7.append({"label": day.isoformat(), "v": None, "n": 0})
        else:
            last_7.append({
                "label": day.isoformat(),
                "v": round(sum(vals) / len(vals), 4),
                "n": len(vals),
            })

    def _mean(vals):
        return round(sum(vals) / len(vals), 4) if vals else None

    return {
        "id": metric_id,
        "name": entry.get("name", metric_id),
        "last": latest["value"],
        "value": latest["value"],  # back-compat for older readers
        "unit": latest.get("unit"),
        "target": entry.get("target"),
        "mean_7d": _mean(vals7),
        "mean_28d": _mean(vals28),
        "n_7d": len(vals7),
        "n_28d": len(vals28),
        "last_7": last_7,
        # legacy two-point fields unused by new brief_block
        "d7": None,
        "d28": None,
    }


def _delta(rows: list[dict], days: int) -> float | None:
    if len(rows) < 2:
        return None
    cutoff = datetime.now(timezone.utc).timestamp() - days * 86400
    older = None
    for row in rows:
        try:
            if datetime.fromisoformat(row["ts"]).timestamp() <= cutoff:
                older = row
        except (KeyError, ValueError):
            continue
    if older is None:
        older = rows[0]
    try:
        return round(rows[-1]["value"] - older["value"], 4)
    except TypeError:
        return None


# =========================================================== dispositions

def dispositions_path(cycle_id: str, repo: Path | None = None) -> Path:
    return paths.state_dir(repo) / "cycles" / cycle_id / DISPOSITIONS_FILE


def load_dispositions(cycle_id: str, repo: Path | None = None) -> dict:
    path = dispositions_path(cycle_id, repo)
    if not path.exists():
        return {}
    try:
        return json.loads(path.read_text(encoding="utf-8"))
    except json.JSONDecodeError:
        return {}


def save_disposition(cycle_id: str, item_id: str, verdict: str,
                     reason: str = "", needs: str = "",
                     repo: Path | None = None) -> None:
    if verdict not in VALID_DISPOSITIONS:
        raise ValueError(
            f"disposition must be one of {', '.join(VALID_DISPOSITIONS)}"
        )
    if verdict == "defer" and not needs.strip():
        raise ValueError(
            "a defer must name the evidence that would settle it "
            "(--needs). 'Not enough information' without naming the "
            "information is how a problem becomes permanent."
        )
    data = load_dispositions(cycle_id, repo)
    data[item_id] = {
        "verdict": verdict, "reason": reason, "needs": needs,
        "ts": records.now(),
    }
    path = dispositions_path(cycle_id, repo)
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text(json.dumps(data, indent=2) + "\n", encoding="utf-8")


def save_docket(docket: Docket, repo: Path | None = None) -> Path:
    path = paths.state_dir(repo) / "cycles" / docket.cycle_id / "docket.json"
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text(json.dumps(docket.to_dict(), indent=2, default=str) + "\n",
                    encoding="utf-8")
    return path


def load_docket(cycle_id: str, repo: Path | None = None) -> Docket | None:
    path = paths.state_dir(repo) / "cycles" / cycle_id / "docket.json"
    if not path.exists():
        return None
    data = json.loads(path.read_text(encoding="utf-8"))
    items = [DocketItem(**i) for i in data.pop("items", [])]
    return Docket(items=items, **data)


def latest_cycle_id(repo: Path | None = None) -> str | None:
    root = paths.state_dir(repo) / "cycles"
    if not root.exists():
        return None
    cycles = sorted(p.name for p in root.iterdir() if p.is_dir())
    return cycles[-1] if cycles else None


def list_deferred(repo: Path | None = None) -> list[dict]:
    """Every defer still sitting in cycle disposition files.

    Used by MT7 so a deferred item cannot vanish forever. Age is measured
    from the disposition timestamp when present.
    """
    root = paths.state_dir(repo) / "cycles"
    if not root.exists():
        return []
    out: list[dict] = []
    now = datetime.now(timezone.utc)
    for cycle in sorted(p for p in root.iterdir() if p.is_dir()):
        data = load_dispositions(cycle.name, repo)
        for item_id, entry in data.items():
            if (entry or {}).get("verdict") != "defer":
                continue
            age = 0
            try:
                age = max(0, (now - datetime.fromisoformat(entry["ts"])).days)
            except (KeyError, ValueError, TypeError):
                pass
            out.append({
                "id": item_id,
                "cycle_id": cycle.name,
                "settle_when": entry.get("needs") or "",
                "reason": entry.get("reason") or "",
                "age_days": age,
            })
    return out
