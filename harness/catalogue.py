"""Lifeblood catalogue access.

The catalogue is the list of things this system measures. It matters here
for one reason: a proposal must name an existing metric as its expected
effect, so that somebody can later tell whether the change worked. A plan
whose effect cannot be observed cannot be learned from.
"""
from __future__ import annotations

import json
import os
from functools import lru_cache
from pathlib import Path

from . import paths

CATALOGUE_RELATIVE = Path("lifeblood") / "catalogue.json"


@lru_cache(maxsize=8)
def _load(repo_str: str) -> dict:
    path = Path(repo_str) / CATALOGUE_RELATIVE
    if not path.exists():
        return {}
    try:
        return json.loads(path.read_text(encoding="utf-8"))
    except json.JSONDecodeError:
        return {}


def catalogue_for(repo: Path | None = None) -> dict:
    return _load(str((repo or paths.repo_root()).resolve()))


def metric_ids(repo: Path | None = None) -> list[str]:
    """Metric ids from this repo, falling back to every sibling repo.

    A proposal in one repo can legitimately predict an effect on a metric
    collected in another — a discovery change moving the pipeline funnel,
    for instance — so the lookup is workspace-wide.
    """
    ids: set[str] = set()
    for entry in catalogue_for(repo).get("metrics", []):
        ids.add(entry["id"])
    if not ids:
        for sibling in paths.code_repos():
            for entry in _load(str(sibling)).get("metrics", []):
                ids.add(entry["id"])
    return sorted(ids)


def all_metrics() -> list[dict]:
    out: list[dict] = []
    seen: set[str] = set()
    for sibling in paths.code_repos():
        data = _load(str(sibling))
        for entry in data.get("metrics", []):
            if entry["id"] in seen:
                continue
            seen.add(entry["id"])
            out.append({**entry, "repo": sibling.name,
                        "subsystem": data.get("subsystem", "")})
    return sorted(out, key=lambda m: (m.get("family", ""), m["id"]))


def known_metric(metric_id: str, repo: Path | None = None) -> bool:
    if not metric_id:
        return False
    return metric_id in set(metric_ids(repo)) | {m["id"] for m in all_metrics()}


def history_path(metric_id: str, repo: Path | None = None) -> Path | None:
    for candidate in ([repo] if repo else []) + paths.code_repos():
        if candidate is None:
            continue
        path = Path(candidate) / "lifeblood" / "state" / f"{metric_id}.jsonl"
        if path.exists():
            return path
    return None


def _remote_lines(metric_id: str, repo: Path | None) -> list[str]:
    """History from the durable store, keyed by the repo's own subsystem.

    A metric id alone is not unique - `code_health` is collected in all
    three repos - so the subsystem prefix is load-bearing. It is read from
    the repo's catalogue rather than mapped here, so there is no table to
    keep in sync with the data repos.
    """
    uri = (os.getenv("TURNSTILE_AGENT_STORE") or "").strip()
    if not uri.startswith("gs://"):
        return []
    subsystem = catalogue_for(repo).get("subsystem")
    if not subsystem:
        return []
    try:
        from google.cloud import storage

        bucket = storage.Client().bucket(uri[5:].split("/", 1)[0])
        blob = bucket.blob(f"lifeblood/{subsystem}/{metric_id}.jsonl")
        return blob.download_as_text().splitlines()
    except Exception:                          # noqa: BLE001 - fall back local
        return []


def readings(metric_id: str, limit: int = 200, repo: Path | None = None) -> list[dict]:
    """Every recorded reading for a metric, newest last.

    The durable store wins when it has anything: a checkout's
    `lifeblood/state/` is one machine's local convenience, and briefing an
    agent on trend deltas computed from it would be briefing it on a
    different history than the job measured.
    """
    lines = _remote_lines(metric_id, repo)
    if not lines:
        path = history_path(metric_id, repo)
        if not path:
            return []
        lines = path.read_text(encoding="utf-8").splitlines()

    rows = []
    for line in lines:
        line = line.strip()
        if not line:
            continue
        try:
            rows.append(json.loads(line))
        except json.JSONDecodeError:
            continue
    return rows[-limit:]
