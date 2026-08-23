"""brief_for: PIT ≤2 metrics with last / mean_7d / mean_28d / last_7."""
from __future__ import annotations

import json
import sys
from datetime import datetime, timedelta, timezone
from pathlib import Path

import pytest

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT))

from harness import docket                                    # noqa: E402
from roles.base import Role                                   # noqa: E402


def _role(role_id: str, tier: str, *metrics: str) -> Role:
    """A role as the loader would build it from the repo's agents/ dir."""
    return Role(id=role_id, tier=tier, subsystem="discovery", summary="",
                repos=(), metrics=metrics)


@pytest.fixture
def repo(tmp_path, monkeypatch):
    (tmp_path / "lifeblood").mkdir()
    (tmp_path / "lifeblood" / "catalogue.json").write_text(json.dumps({
        "subsystem": "audio-discovery",
        "metrics": [
            {"id": "discovery_yield", "name": "Yield"},
            {"id": "campaign_coverage", "name": "Coverage"},
            {"id": "session_yield", "name": "Session"},
            {"id": "code_health", "name": "Size"},
        ],
    }), encoding="utf-8")
    state = tmp_path / "lifeblood" / "state"
    state.mkdir()
    now = datetime.now(timezone.utc)
    for mid, values in (
        ("discovery_yield", [1, 2, 3]),
        ("campaign_coverage", [0.0, 0.5]),
        ("session_yield", [10]),
        ("code_health", [100]),
    ):
        lines = []
        for i, v in enumerate(values):
            ts = (now - timedelta(days=len(values) - 1 - i)).isoformat(
                timespec="seconds")
            lines.append(json.dumps({
                "id": mid, "ts": ts, "status": "ok", "value": v, "unit": "x",
            }))
        (state / f"{mid}.jsonl").write_text("\n".join(lines) + "\n",
                                            encoding="utf-8")
    monkeypatch.setenv("TURNSTILE_AGENT_STORE", "")  # force local history
    return tmp_path


def test_pit_brief_is_focused(repo):
    rows = docket.brief_for(
        repo, audience="pit",
        role=_role("discovery-campaign-yield", "pit",
                   "campaign_coverage", "discovery_yield"),
        trigger={"metric": "discovery_yield", "value": 3})
    ids = [r["id"] for r in rows]
    assert ids[0] == "discovery_yield"
    assert len(ids) <= 2
    assert "last" in rows[0] and "mean_7d" in rows[0] and "last_7" in rows[0]
    assert "code_health" not in ids


def test_retro_brief_is_wider(repo):
    rows = docket.brief_for(
        repo, audience="retro",
        role=_role("discovery-retro", "retro",
                   "session_yield", "discovery_yield", "campaign_coverage"))
    ids = {r["id"] for r in rows}
    assert "session_yield" in ids
    assert "discovery_yield" in ids


def test_the_master_brief_is_the_whole_catalogue(repo):
    """No role passed, so nothing narrows it — the master sees everything."""
    ids = {r["id"] for r in docket.brief_for(repo, audience="master")}
    assert "code_health" in ids
    assert "session_yield" in ids
