"""Tests for partial-work escalation when a session dies after AMBER/RED."""
from __future__ import annotations

from cloud import agent_job


def test_substantive_prior_skips_failed_tail():
    history = [
        {"attempt": 1, "tier": "RED", "branch": "agent/a", "evidence": object()},
        {"attempt": 2, "tier": None, "branch": None, "evidence": object()},
    ]
    row = agent_job._substantive_prior(history)
    assert row is not None
    assert row["tier"] == "RED"
    assert row["branch"] == "agent/a"


def test_substantive_prior_none_when_no_branch():
    history = [
        {"attempt": 1, "tier": "RED", "branch": None, "evidence": object()},
        {"attempt": 2, "tier": None, "branch": None, "evidence": object()},
    ]
    assert agent_job._substantive_prior(history) is None
