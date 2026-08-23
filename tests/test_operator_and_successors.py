"""Operator cards, INCOMPLETE cooldown, successor chain."""
from __future__ import annotations

import sys
from pathlib import Path

import pytest

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))

from harness import ledger, operator, successors
from reconciler.tick import scan


@pytest.fixture(autouse=True)
def store(tmp_path, monkeypatch):
    monkeypatch.setenv("TURNSTILE_AGENT_STORE", str(tmp_path / "store"))
    return tmp_path / "store"


def test_operator_open_and_hold_are_sibling_folders():
    operator.write_open("discovery", {
        "branch": "cursor/discovery-master-1",
        "headline": "needs a look",
        "agent_id": "bc-1",
    })
    operator.write_hold("discovery", {
        "branch": "cursor/discovery-master-2",
        "headline": "wait for the next pulse",
        "why_now": "yield is still moving",
        "agent_id": "bc-2",
    })
    opened = operator.list_open("discovery")
    held = operator.list_hold("discovery")
    assert len(opened) == 1 and opened[0]["kind"] == "open"
    assert opened[0]["branch"] == "cursor/discovery-master-1"
    assert len(held) == 1 and held[0]["headline"] == "wait for the next pulse"
    assert "operator/open" in opened[0]["_key"]
    assert "operator/hold" in held[0]["_key"]


def test_incomplete_writes_operator_card_with_sub_reason():
    operator.write_incomplete("discovery", {
        "sub_reason": "no_wager_produced",
        "agent_id": "bc-m",
        "branch": "cursor/x",
    })
    rows = operator.list_incomplete("discovery")
    assert len(rows) == 1
    assert rows[0]["kind"] == "incomplete"
    assert rows[0]["sub_reason"] == "no_wager_produced"
    assert rows[0]["headline"] == "INCOMPLETE — no wager produced"
    assert "operator/incomplete" in rows[0]["_key"]


def test_raise_incomplete_notify():
    from harness import notify
    result = notify.raise_incomplete(
        "pipeline",
        sub_reason="turn_cap",
        agent_id="bc-2",
        detail={"reason": "hit turn cap (5)"},
        escalations=["E-1"],
    )
    assert result["ok"]
    assert result["sub_reason"] == "turn_cap"
    assert "turn cap" in result["headline"].lower()
    rows = operator.list_incomplete("pipeline")
    assert rows[0]["escalations_still_open"] == ["E-1"]


def test_incomplete_cooldown_keeps_escalations_and_skips_the_tick():
    entry = ledger.Escalation.raise_for(
        subsystem="discovery", role="discovery-retro",
        agent_id="bc-1", tier="RED", branch="cursor/x", base="master")
    ledger.record(entry)
    ledger.set_cooldown("discovery", minutes=30, reason="no wager after re-prompt")

    launched = []
    report = scan("discovery",
                  launch=lambda s: launched.append(s) or {"execution": "e"},
                  in_flight=lambda s: [])
    assert launched == []
    assert "cooldown" in report["master"]["skipped"]
    assert ledger.open_entries("discovery")[0].id == entry.id


def test_expired_cooldown_does_not_block(monkeypatch):
    from datetime import datetime, timedelta, timezone
    from harness import objectstore

    entry = ledger.Escalation.raise_for(
        subsystem="discovery", role="discovery-retro",
        agent_id="bc-1", tier="RED", branch="cursor/x", base="master")
    ledger.record(entry)
    objectstore.put("agent_layer/escalations/cooldown/discovery.json", {
        "subsystem": "discovery",
        "until": (datetime.now(timezone.utc) - timedelta(minutes=1)
                  ).isoformat(timespec="seconds"),
        "reason": "stale",
    })
    launched = []
    report = scan("discovery",
                  launch=lambda s: launched.append(s) or {"execution": "e"},
                  in_flight=lambda s: [])
    assert launched == ["discovery"]
    assert report["master"]["execution"] == "e"


def test_successor_chain_is_freestyle_then_bake():
    chain = successors.after("discovery-freestyle-rd")
    assert chain is not None
    assert chain.spawn == "discovery-route-bake"
    assert chain.require == "interaction_handoff"
    assert successors.after("discovery-route-bake") is None

    class Git:
        def files_under(self, branch, prefix):
            return ["state/S1/rd_retro_handoff/interaction.jsonl"]

    status = successors.satisfied("interaction_handoff", Git(), "b", "S1")
    assert status["ok"] is True
