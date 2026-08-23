"""Unit tests for dashboard story helpers (no Streamlit / GCS)."""
from __future__ import annotations

import sys
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT))

from dashboard.story import landing_stats, operator_counts, stringify_rows  # noqa: E402


def test_landing_stats_counts_stalled_and_merges():
    rows = [
        {"tier": "master", "kind": "spawn", "ts": "1"},
        {"tier": "master", "kind": "spawn", "ts": "2"},
        {"tier": "master", "kind": "route", "outcome": "merge", "ts": "3"},
        {"tier": "master", "kind": "route", "outcome": "operator", "ts": "4"},
        {"tier": "pit", "kind": "verdict", "ts": "5", "agent": "x", "headline": "hi"},
    ]
    stats = landing_stats(rows)
    assert stats["master_spawns"] == 2
    assert stats["merges"] == 1
    assert stats["operator_routes"] == 1
    assert stats["stalled"] == 0  # 2 spawns, 2 routes
    assert stats["latest"]["headline"] == "hi"


def test_landing_stats_flags_stalled_cycles():
    rows = [
        {"tier": "master", "kind": "spawn", "ts": "1"},
        {"tier": "master", "kind": "spawn", "ts": "2"},
        {"tier": "master", "kind": "route", "outcome": "merge", "ts": "3"},
    ]
    assert landing_stats(rows)["stalled"] == 1


def test_stringify_rows_casts_mixed_types():
    rows = stringify_rows([{"trigger": "20260816-001253", "files": 3, "x": None}])
    assert rows == [{"trigger": "20260816-001253", "files": "3", "x": ""}]


def test_operator_counts_sums_queues():
    def load_op(kind: str, sub: str):
        return [{"k": kind, "s": sub}] * (2 if kind == "open" else 1)

    def load_esc(sub: str):
        return [{"s": sub}]

    counts = operator_counts(
        ["discovery", "pipeline"],
        load_operator=load_op,
        load_escalations=load_esc,
    )
    assert counts == {"open": 4, "hold": 2, "incomplete": 2, "escalations": 2}
