"""Master triggers — when the expensive agent gets woken.

The master is conditionally rendered. A healthy subsystem runs its PIT
and retro agents indefinitely, merges through the green lane, and never
pays for a master cycle at all.

    MT1  a RED attempt                    fire immediately
    MT2  three AMBER attempts             fire
    MT3  a metric breaching for 3 windows fire  (nobody asked; that is the point)
    MT4  a signature recurring 3 times    fire
    MT5  silence: no runs, or stale       fire immediately
    MT6  no master cycle in 21 days       fire
    MT7  a deferred disposition is due    fire

MT3 and MT4 are the backstops. They guarantee a review even when no
agent raises anything and nothing was blocked, which is the failure mode
the measured record shows most clearly: six days of zero throughput that
nothing noticed, because every component reported success at doing
nothing.

AMBER attempts count toward MT2 **whether or not the agent later reduced
the change to a green merge.** The attempt is the signal.
"""
from __future__ import annotations

from dataclasses import dataclass, field
from datetime import datetime, timedelta, timezone
from pathlib import Path

from . import catalogue, docket as docket_mod, records

RED_THRESHOLD = 1
AMBER_THRESHOLD = 3
AMBER_WINDOW_DAYS = 14
METRIC_BREACH_WINDOWS = 3
ADVERSE_TREND_PERIODS = 2
SIGNATURE_THRESHOLD = 3
SILENCE_HOURS = 48
STALE_METRIC_HOURS = 36
MASTER_STALENESS_DAYS = 21
MIN_CYCLE_INTERVAL_HOURS = 24
URGENT = {"MT1", "MT5", "MT7"}


@dataclass
class Trigger:
    id: str
    reason: str
    evidence: dict = field(default_factory=dict)
    urgent: bool = False


def _now() -> datetime:
    return datetime.now(timezone.utc)


def _parse(ts: str | None) -> datetime | None:
    if not ts:
        return None
    try:
        return datetime.fromisoformat(ts)
    except ValueError:
        return None


# ─────────────────────────────────────────────────────────────── MT1 / MT2

def attempt_triggers(repo: Path | None = None) -> list[Trigger]:
    out: list[Trigger] = []
    attempts = records.load_attempts(repo, days=AMBER_WINDOW_DAYS)

    reds = [a for a in attempts if a.get("tier") == "RED"]
    if len(reds) >= RED_THRESHOLD:
        latest = reds[-1]
        out.append(Trigger(
            id="MT1",
            reason=(f"{len(reds)} change(s) reached the protected surface; "
                    f"most recently {latest.get('agent')} on "
                    f"{', '.join(latest.get('paths', [])[:3])}"),
            evidence={"attempts": reds[-4:]},
            urgent=True,
        ))

    ambers = [a for a in attempts if a.get("tier") == "AMBER"]
    if len(ambers) >= AMBER_THRESHOLD:
        agents = sorted({a.get("agent", "?") for a in ambers})
        reduced = sum(1 for a in ambers if a.get("outcome") == "merged")
        out.append(Trigger(
            id="MT2",
            reason=(f"{len(ambers)} changes were held back in "
                    f"{AMBER_WINDOW_DAYS} days across {len(agents)} agent(s)"
                    + (f"; {reduced} were later reduced to a merge" if reduced else "")),
            evidence={"attempts": ambers[-6:], "agents": agents,
                      "reduced_to_green": reduced},
        ))
    return out


# ───────────────────────────────────────────────────────────────────── MT3

def metric_triggers(repo: Path | None = None) -> list[Trigger]:
    """Metrics breaching a declared target, or trending adversely.

    Metrics without a declared target are skipped rather than guessed at.
    A target nobody set is not a breach.
    """
    out: list[Trigger] = []
    for entry in catalogue.catalogue_for(repo).get("metrics", []):
        target = entry.get("target")
        better = entry.get("better")
        if target is None or better not in ("up", "down"):
            continue
        rows = [r for r in catalogue.readings(entry["id"], repo=repo)
                if r.get("status") == "ok" and r.get("value") is not None]
        if len(rows) < METRIC_BREACH_WINDOWS:
            continue

        recent = rows[-METRIC_BREACH_WINDOWS:]
        breached = [
            r for r in recent
            if (r["value"] < target if better == "up" else r["value"] > target)
        ]
        if len(breached) == METRIC_BREACH_WINDOWS:
            out.append(Trigger(
                id="MT3",
                reason=(f"{entry['id']} has been outside target ({target}) for "
                        f"{METRIC_BREACH_WINDOWS} consecutive windows; latest "
                        f"{recent[-1]['value']}"),
                evidence={"metric": entry["id"], "target": target,
                          "better": better,
                          "recent": [r["value"] for r in recent]},
            ))
            continue

        adverse = _adverse_periods(rows, better)
        if adverse >= ADVERSE_TREND_PERIODS:
            out.append(Trigger(
                id="MT3",
                reason=(f"{entry['id']} has moved the wrong way for "
                        f"{adverse} consecutive periods "
                        f"(now {rows[-1]['value']})"),
                evidence={"metric": entry["id"], "adverse_periods": adverse,
                          "recent": [r["value"] for r in rows[-5:]]},
            ))
    return out


def _adverse_periods(rows: list[dict], better: str) -> int:
    values = [r["value"] for r in rows if isinstance(r.get("value"), (int, float))]
    if len(values) < 3:
        return 0
    count = 0
    for previous, current in zip(reversed(values[:-1]), reversed(values[1:])):
        moved_wrong = current < previous if better == "up" else current > previous
        if moved_wrong and current != previous:
            count += 1
        else:
            break
    return count


# ───────────────────────────────────────────────────────────────────── MT4

def signature_triggers(repo: Path | None = None) -> list[Trigger]:
    import json
    registry = repo and (repo / "agent_state" / "signatures.json")
    if not registry or not registry.exists():
        return []
    try:
        data = json.loads(registry.read_text(encoding="utf-8"))
    except json.JSONDecodeError:
        return []

    out: list[Trigger] = []
    for sig in data.get("signatures", []):
        occurrences = sig.get("occurrences", [])
        if sig.get("closed_structurally"):
            continue
        if len(occurrences) >= SIGNATURE_THRESHOLD:
            out.append(Trigger(
                id="MT4",
                reason=(f"signature {sig['id']} ({sig.get('label', '')}) has "
                        f"occurred {len(occurrences)} times without a "
                        f"structural closure"),
                evidence={"signature": sig["id"],
                          "occurrences": occurrences[-5:],
                          "prior_closures": sig.get("closures", [])},
            ))
    return out


# ───────────────────────────────────────────────────────────────── MT5 / MT6

def liveness_triggers(repo: Path | None = None) -> list[Trigger]:
    out: list[Trigger] = []
    now = _now()

    sessions = sorted(
        (records.load_session_record(p.stem, repo) or {}
         for p in (repo / "agent_state" / "sessions").glob("*.json")),
        key=lambda s: s.get("updated") or s.get("started") or "",
    ) if repo and (repo / "agent_state" / "sessions").exists() else []
    last_session = _parse(sessions[-1].get("updated")) if sessions else None
    if last_session and (now - last_session) > timedelta(hours=SILENCE_HOURS):
        hours = int((now - last_session).total_seconds() // 3600)
        out.append(Trigger(
            id="MT5",
            reason=f"no agent session has completed in {hours} hours",
            evidence={"last_session": sessions[-1].get("session")},
            urgent=True,
        ))

    stale = _stale_metrics(repo)
    if stale:
        out.append(Trigger(
            id="MT5",
            reason=("metric collection is stale, so the trend backstops "
                    "cannot fire: " + ", ".join(stale[:5])),
            evidence={"stale_metrics": stale},
            urgent=True,
        ))

    days = records.days_since_master_cycle(repo)
    if days is None or days >= MASTER_STALENESS_DAYS:
        out.append(Trigger(
            id="MT6",
            reason=("no master cycle has ever run for this subsystem"
                    if days is None else
                    f"no master cycle in {days} days"),
            evidence={"days_since": days},
        ))
    return out


def _stale_metrics(repo: Path | None) -> list[str]:
    stale: list[str] = []
    cutoff = _now() - timedelta(hours=STALE_METRIC_HOURS)
    for entry in catalogue.catalogue_for(repo).get("metrics", []):
        if entry.get("cadence") in ("weekly", "on-demand"):
            continue
        rows = catalogue.readings(entry["id"], limit=1, repo=repo)
        if not rows:
            continue
        when = _parse(rows[-1].get("ts"))
        if when and when < cutoff:
            stale.append(entry["id"])
    return stale


# ═══════════════════════════════════════════════════════════════ evaluation

def evaluate(repo: Path | None = None) -> list[Trigger]:
    found: list[Trigger] = []
    found.extend(attempt_triggers(repo))
    found.extend(metric_triggers(repo))
    found.extend(signature_triggers(repo))
    found.extend(liveness_triggers(repo))
    found.extend(deferred_triggers(repo))
    return found


def deferred_triggers(repo: Path | None = None) -> list[Trigger]:
    """MT7 — a defer without a revisit condition silently drops a docket item.

    Re-raise anything deferred whose settle-when evidence is now present,
    or whose defer is older than the master staleness window with no
    condition attached.
    """
    out: list[Trigger] = []
    try:
        deferred = docket_mod.list_deferred(repo)
    except Exception:  # noqa: BLE001
        return out
    for item in deferred:
        age = int(item.get("age_days") or 0)
        settle = (item.get("settle_when") or "").strip()
        if settle:
            # Condition named but not yet evaluable here — still raise if old.
            if age < MASTER_STALENESS_DAYS:
                continue
        out.append(Trigger(
            id="MT7",
            reason=(f"deferred item {item.get('id')} is due"
                    + (f" (settle when: {settle})" if settle else "")),
            evidence=item,
            urgent=True,
        ))
    return out


def persist(found: list[Trigger], repo: Path | None = None) -> None:
    for trigger in found:
        records.set_trigger(trigger.id, trigger.reason, trigger.evidence, repo)


def should_fire(repo: Path | None = None) -> tuple[bool, list[dict], str]:
    """(fire, triggers, why-not). Coalesces everything set into one cycle."""
    state = records.load_triggers(repo)
    pending = list(state["set"].values())
    if not pending:
        return False, [], "no triggers set"

    urgent = [t for t in pending if t["id"] in URGENT]
    last = _parse(records.load_cycle(repo).get("last_master_cycle"))
    if last and not urgent:
        elapsed = _now() - last
        if elapsed < timedelta(hours=MIN_CYCLE_INTERVAL_HOURS):
            remaining = MIN_CYCLE_INTERVAL_HOURS - int(elapsed.total_seconds() // 3600)
            return False, pending, (
                f"{len(pending)} trigger(s) set but the last master cycle was "
                f"{int(elapsed.total_seconds() // 3600)}h ago; waiting "
                f"{remaining}h more (urgent triggers override)"
            )
    return True, pending, ""
