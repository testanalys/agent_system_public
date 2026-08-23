"""Dashboard timeline: row schema, render, and the PIT→master chain."""
from __future__ import annotations

import json
import sys
from pathlib import Path
from unittest.mock import patch

import pytest

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT))

from dashboard import render, timeline, validate           # noqa: E402


def test_row_divergence_for_master_wager():
    row = timeline.Row(
        subsystem="pipeline", agent="pipeline-master", tier="master",
        kind="route", strategy="repair",
        surface_declared=("agents/x.md", "utils/breaches.py"),
        surface_actual=("agents/x.md", "utils/breaches.py", "core/y.py"),
    )
    assert row.divergence == pytest.approx(1 / 3, abs=0.01)


def test_row_divergence_none_for_pit_verdict():
    row = timeline.Row(
        subsystem="pipeline", agent="pipeline-funnel-leak", tier="pit",
        kind="verdict", surface_actual=("utils/a.py",),
    )
    assert row.divergence is None


def test_render_shows_spawn_proposal_and_master_rows():
    rows = [
        {"ts": "2026-08-11T10:00:00+00:00", "agent": "pipeline-funnel-leak",
         "kind": "spawn", "tier": "pit", "headline": "woken by funnel stall"},
        {"ts": "2026-08-11T10:30:00+00:00", "agent": "pipeline-funnel-leak",
         "kind": "proposal", "tier": "pit",
         "headline": "Split the stall detector from the retry loop"},
        {"ts": "2026-08-11T10:31:00+00:00", "agent": "pipeline-funnel-leak",
         "kind": "verdict", "tier": "pit", "classify": "AMBER",
         "outcome": "ESCALATE", "files": 4},
        {"ts": "2026-08-11T11:00:00+00:00", "agent": "pipeline-master",
         "kind": "spawn", "tier": "master", "headline": "woken by 1 escalations"},
        {"ts": "2026-08-11T11:20:00+00:00", "agent": "pipeline-master",
         "kind": "wager", "tier": "master", "strategy": "repair",
         "headline": "Fix the stall at the source", "outcome": "committed"},
        {"ts": "2026-08-11T11:45:00+00:00", "agent": "pipeline-master",
         "kind": "phase", "tier": "master", "headline": "orient → implement",
         "outcome": "implement"},
        {"ts": "2026-08-11T12:10:00+00:00", "agent": "pipeline-master",
         "kind": "route", "tier": "master", "strategy": "repair",
         "classify": "GREEN", "outcome": "merge", "files": 2,
         "headline": "Fix the stall at the source"},
    ]
    text = render.as_table(rows)
    assert "SPAWN" in text or "spawn" in text.lower()
    assert "pipeline-funnel-leak" in text
    assert "pipeline-master" in text
    assert "Fix the stall" in text


def test_validate_flow_accepts_pit_to_master_chain():
    rows = [
        {"kind": "spawn", "tier": "pit", "agent": "pipeline-funnel-leak"},
        {"kind": "proposal", "tier": "pit", "agent": "pipeline-funnel-leak"},
        {"kind": "verdict", "tier": "pit", "outcome": "ESCALATE",
         "agent": "pipeline-funnel-leak"},
        {"kind": "spawn", "tier": "master", "agent": "pipeline-master"},
        {"kind": "wager", "tier": "master", "agent": "pipeline-master"},
        {"kind": "route", "tier": "master", "agent": "pipeline-master",
         "outcome": "merge"},
    ]
    assert validate.flow(rows) == []


def test_validate_flow_flags_missing_master_wager():
    rows = [
        {"kind": "spawn", "tier": "pit", "agent": "pipeline-funnel-leak"},
        {"kind": "verdict", "tier": "pit", "outcome": "ESCALATE",
         "agent": "pipeline-funnel-leak"},
        {"kind": "spawn", "tier": "master", "agent": "pipeline-master"},
        {"kind": "route", "tier": "master", "agent": "pipeline-master",
         "outcome": "merge"},
    ]
    problems = validate.flow(rows)
    assert any("wager" in p for p in problems)


def test_record_proposal_reads_change_from_branch():
    from cloud import agent_job

    class FakeGit:
        base = "main"

        def read_file(self, branch, path):
            assert branch == "agent/x"
            assert path.endswith(".json")
            return json.dumps({"change": "Reduce the dual retry path\nmore"})

    with patch.object(timeline, "record", wraps=timeline.record) as rec:
        agent_job._record_proposal(
            "pipeline", "pipeline-funnel-leak", "pit",
            FakeGit(), "agent/x", ["agent_state/proposals/escalation.json"])
        assert rec.called
        row = rec.call_args[0][0]
        assert row.kind == "proposal"
        assert "Reduce the dual retry path" in row.headline
