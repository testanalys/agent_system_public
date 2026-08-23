"""The one piece of state, and the one thing the reconciler does with it.

An escalation is raised by the execution that owned the blocked agent and
consumed by a later master execution. Nothing else joins those two, so
this index is the only durable state in the layer and the tick is the
only sweep over it.

What these tests protect is mostly the absence of things: no
read-modify-write, no shared file, no second master, and no tick that
takes a whole tier down because one subsystem's storage was unreachable.
"""
from __future__ import annotations

import sys
from pathlib import Path

import pytest

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))

from harness import ledger, objectstore, runlog                  # noqa: E402
from reconciler.tick import scan                                 # noqa: E402


@pytest.fixture(autouse=True)
def store(tmp_path, monkeypatch):
    monkeypatch.setenv("TURNSTILE_AGENT_STORE", str(tmp_path / "store"))
    return tmp_path / "store"


def _raise(subsystem="pipeline", agent="bc-1", tier="AMBER", **kw):
    entry = ledger.Escalation.raise_for(
        subsystem=subsystem, role=f"{subsystem}-diarization-qa",
        agent_id=agent, tier=tier, branch=f"cursor/{agent}", base="main", **kw)
    ledger.record(entry)
    return entry


# ═══════════════════════════════════════════════════════════════ the ledger

def test_an_escalation_survives_the_execution_that_raised_it():
    raised = _raise(proposal_paths=["agent_state/proposals/escalation.json"])
    (found,) = ledger.open_entries("pipeline")
    assert found.id == raised.id
    assert found.branch == "cursor/bc-1"
    assert found.proposal_paths == ["agent_state/proposals/escalation.json"]


def test_subsystems_cannot_see_or_overwrite_each_other():
    """One object per escalation - the shared-file failure mode, gone.

    `pending_agents.json` held every subsystem's agents in one document,
    so a pipeline tick rewrote discovery's entries and a discovery tick
    tried to fetch a pipeline branch that did not exist on its remote.
    """
    _raise("pipeline", "bc-1")
    _raise("discovery", "bc-2")
    assert [e.agent_id for e in ledger.open_entries("pipeline")] == ["bc-1"]
    assert [e.agent_id for e in ledger.open_entries("discovery")] == ["bc-2"]


def test_re_raising_the_same_agent_updates_rather_than_duplicates():
    """A retried execution must not hand the master the same branch twice."""
    _raise(agent="bc-1", tier="AMBER")
    _raise(agent="bc-1", tier="RED")
    (only,) = ledger.open_entries("pipeline")
    assert only.tier == "RED"


def test_closing_keeps_the_record_and_clears_the_docket():
    entry = _raise()
    ledger.close(entry, disposed_by="bc-master")
    assert ledger.open_entries("pipeline") == []
    closed = objectstore.list_keys(ledger.CLOSED_PREFIX)
    assert len(closed) == 1
    assert objectstore.get_json(closed[0])["disposed_by"] == "bc-master"


def test_the_docket_points_at_branches_rather_than_copying_them():
    """The master reads the argument off the branch, against the code.

    A docket that inlined the proposal would be a second copy that starts
    disagreeing with the branch the moment anyone touches it.
    """
    _raise(proposal_paths=["agent_state/proposals/escalation.json"],
           trigger={"metric": "diarization_quality", "value": 0.31,
                    "target": 0.5})
    block = ledger.docket_block(ledger.open_entries("pipeline"))
    assert "cursor/bc-1" in block
    assert "agent_state/proposals/escalation.json" in block
    assert "diarization_quality" in block
    assert "disposition" in block


def test_a_missing_proposal_is_shown_to_the_master_not_hidden():
    """An agent asked for its reasoning that gave none is itself a finding."""
    _raise(proposal_paths=[])
    block = ledger.docket_block(ledger.open_entries("pipeline"))
    assert "did not commit it" in block


def test_an_unconfigured_store_is_reported_not_swallowed(monkeypatch):
    """Durability matters in exactly one place, and it says so."""
    monkeypatch.delenv("TURNSTILE_AGENT_STORE", raising=False)
    entry = ledger.Escalation.raise_for(
        subsystem="pipeline", role="r", agent_id="bc-9", tier="RED",
        branch="cursor/bc-9", base="main")
    assert ledger.record(entry) is None
    assert ledger.open_entries("pipeline") == []


# ══════════════════════════════════════════════════════════════ the archive

def test_the_archive_is_never_on_a_decision_path(monkeypatch):
    """A failed dump costs a record, not an outcome."""
    monkeypatch.delenv("TURNSTILE_AGENT_STORE", raising=False)
    assert runlog.dump("pipeline", "bc-1", {"result_text": "x"}) is None

    monkeypatch.setenv("TURNSTILE_AGENT_STORE", "gs://")   # malformed on purpose
    assert runlog.dump("pipeline", "bc-1", {"result_text": "x"}) is None


def test_an_archived_run_can_be_read_back_for_review():
    runlog.dump("pipeline", "bc-1", {"result_text": "what I concluded",
                                     "shell_commands": ["pytest -q"]})
    found = runlog.find("pipeline", "bc-1")
    assert found["result_text"] == "what I concluded"
    assert found["archived_at"]


# ═════════════════════════════════════════════════════════════════ the tick

def test_a_quiet_subsystem_launches_nothing():
    launched = []
    report = scan("pipeline", launch=lambda s: launched.append(s),
                  in_flight=lambda s: [])
    assert launched == []
    assert report["master"]["skipped"] == "nothing escalated"


def test_escalations_launch_exactly_one_master():
    _raise(agent="bc-1")
    _raise(agent="bc-2", tier="RED")
    launched = []
    report = scan("pipeline",
                  launch=lambda s: launched.append(s) or {"execution": "e-1"},
                  in_flight=lambda s: [])
    assert launched == ["pipeline"]
    assert report["open"] == 2
    assert report["by_tier"] == {"AMBER": 1, "RED": 1}


def test_a_master_already_reading_them_is_not_joined_by_a_second():
    """Two masters would write dispositions for the same branches."""
    _raise()
    launched = []
    report = scan("pipeline", launch=lambda s: launched.append(s),
                  in_flight=lambda s: ["turnstile-agent-abcde"])
    assert launched == []
    assert "already reading" in report["master"]["skipped"]


def test_a_failed_launch_is_not_reported_as_launched():
    from cloud.tick_all import _master_status

    assert "FAILED" in _master_status({
        "launched": False,
        "reason": "PermissionDenied: run.jobs.run",
    })


def test_the_tick_reads_one_subsystem_and_touches_no_checkout():
    """The tick used to clone three repos and call Cursor. Now it lists keys.

    Everything that made a tick fail - a git clone over a flaky link, a
    branch missing from a sibling's remote, a busy conversation - is
    absent because none of it is reachable from here.
    """
    _raise("discovery", "bc-2")
    report = scan("pipeline", in_flight=lambda s: [], dry_run=True)
    assert report["open"] == 0
    assert report["subsystem"] == "pipeline"
