"""Kick mechanical lifeblood capture from the operator dashboard.

Process jobs remain the primary writers. When their history is stale, the
dashboard can fire the same Cloud Run jobs that already know how to collect,
so opening the Lifeblood tab is not hostage to a quiet scheduler window.
"""
from __future__ import annotations

import os
from datetime import datetime, timezone
from typing import Any

PROJECT = os.getenv("GOOGLE_CLOUD_PROJECT") or os.getenv("GCP_PROJECT") or "your-gcp-project"
REGION = os.getenv("GOOGLE_CLOUD_LOCATION") or "us-central1"

# Jobs whose ordinary runs append lifeblood history.
CAPTURE_JOBS = {
    "pipeline": "your-pipeline-reconcile-job",
    # Discovery full harvest is heavy; prefer a capture-only override when set.
    "discovery": os.getenv(
        "TURNSTILE_DISCOVERY_CAPTURE_JOB", "your-discovery-job"
    ),
}

STALE_AFTER_HOURS = float(os.getenv("LIFEBLOOD_STALE_HOURS") or "6")


def hours_since(ts: str | None) -> float | None:
    if not ts:
        return None
    try:
        when = datetime.fromisoformat(str(ts).replace("Z", "+00:00"))
    except ValueError:
        return None
    if when.tzinfo is None:
        when = when.replace(tzinfo=timezone.utc)
    return (datetime.now(timezone.utc) - when).total_seconds() / 3600.0


def is_stale(series: list[dict], *, hours: float = STALE_AFTER_HOURS) -> bool:
    """True when there is no recent ok reading (errors do not count as fresh)."""
    for row in reversed(series or []):
        if row.get("status") != "ok":
            continue
        age = hours_since(row.get("ts"))
        if age is None:
            continue
        return age > hours
    return True


def last_ok(series: list[dict]) -> dict:
    for row in reversed(series or []):
        if row.get("status") == "ok" and row.get("value") is not None:
            return row
    return series[-1] if series else {}


def trigger_capture(subsystem: str) -> dict[str, Any]:
    """Start the subsystem's capture job. Never raises."""
    job = CAPTURE_JOBS.get(subsystem)
    if not job:
        return {"ok": False, "reason": f"no capture job mapped for {subsystem}"}
    try:
        from google.cloud import run_v2
    except ImportError as exc:
        return {"ok": False, "reason": f"google-cloud-run missing: {exc}"}

    name = f"projects/{PROJECT}/locations/{REGION}/jobs/{job}"
    try:
        client = run_v2.JobsClient()
        # Discovery: run capture-only when the job understands the flag.
        overrides = None
        if subsystem == "discovery":
            overrides = run_v2.RunJobRequest.Overrides(
                container_overrides=[
                    run_v2.RunJobRequest.Overrides.ContainerOverride(
                        env=[run_v2.EnvVar(name="LIFEBLOOD_CAPTURE_ONLY", value="1")],
                    )
                ]
            )
        request = run_v2.RunJobRequest(name=name, overrides=overrides)
        op = client.run_job(request=request)
        meta = getattr(op, "metadata", None)
        exec_name = getattr(meta, "name", None) if meta is not None else None
        return {
            "ok": True,
            "job": job,
            "execution": exec_name or "started",
            "subsystem": subsystem,
        }
    except Exception as exc:  # noqa: BLE001
        return {
            "ok": False,
            "job": job,
            "subsystem": subsystem,
            "reason": f"{type(exc).__name__}: {exc}",
        }


def refresh_stale(subs: list[str], *, series_by_sub: dict[str, list[dict]]) -> list[dict]:
    """Fire capture for each subsystem whose sample series look stale."""
    out = []
    for sub in subs:
        sample = series_by_sub.get(sub) or []
        if not is_stale(sample):
            out.append({"subsystem": sub, "skipped": "fresh"})
            continue
        out.append(trigger_capture(sub))
    return out
