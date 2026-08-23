"""Cursor REST transport helpers — offline, no API key required."""
from __future__ import annotations

import sys
from pathlib import Path
from unittest.mock import MagicMock

import pytest

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))

from harness.cursor_rest import CursorRestTransport, RunHandle, _git_target  # noqa: E402


@pytest.fixture
def transport() -> CursorRestTransport:
    return CursorRestTransport(api_key="test-key")


def test_extract_run_id_from_nested_run(transport):
    assert transport._extract_run_id({
        "run": {"id": "run-nested"},
        "agent": {"id": "bc-1"},
    }) == "run-nested"


def test_extract_run_id_from_latest_on_agent(transport):
    assert transport._extract_run_id({
        "agent": {"latestRunId": "run-latest"},
    }) == "run-latest"


def test_send_resolves_run_id_when_response_is_nested(transport):
    transport._call = MagicMock(return_value={
        "run": {"id": "run-follow"},
        "agent": {"id": "bc-9"},
    })
    handle = transport.send("bc-9", "follow up")
    assert handle.run_id == "run-follow"
    assert handle.agent_id == "bc-9"


def test_send_falls_back_to_agent_latest_run_id(transport):
    transport._call = MagicMock(side_effect=[
        {"agent": {"id": "bc-9"}},  # POST /runs — no run id
        {"latestRunId": "run-from-get"},  # GET /agents/bc-9
    ])
    handle = transport.send("bc-9", "follow up")
    assert handle.run_id == "run-from-get"


def test_poll_requires_run_id_and_uses_agent_lookup(transport):
    transport._call = MagicMock(side_effect=[
        {"latestRunId": "run-2"},  # resolve
        {"status": "FINISHED", "result": "ok"},  # GET run
    ])
    evidence = transport.poll("bc-9", "")
    assert evidence.status == "finished"
    assert evidence.run_id == "run-2"
    assert transport._call.call_args_list[0].args == ("GET", "/agents/bc-9")
    assert transport._call.call_args_list[1].args == (
        "GET", "/agents/bc-9/runs/run-2")


def test_git_target_reads_agent_document_the_ui_uses():
    branch, _ = _git_target({
        "id": "bc-1",
        "target": {"branchName": "cursor/wi-legislature-av-route-decay-1d65",
                   "autoBranch": True},
    })
    assert branch == "cursor/wi-legislature-av-route-decay-1d65"


def test_git_target_reads_nested_agent_and_run_git():
    nested, _ = _git_target({
        "agent": {"target": {"branchName": "cursor/from-nested"}},
    })
    assert nested == "cursor/from-nested"
    from_run, pr = _git_target({
        "git": {"branches": [{"branch": "cursor/from-run",
                              "prUrl": "https://example/pr"}]},
    })
    assert from_run == "cursor/from-run"
    assert pr == "https://example/pr"
    assert _git_target({"status": "ERROR"}) == (None, None)


def test_create_persists_target_branch_name(transport):
    from harness.agentloop import SpawnRequest

    transport._call = MagicMock(return_value={
        "id": "bc-1",
        "latestRunId": "run-1",
        "target": {"branchName": "cursor/assigned-at-create"},
    })
    agent_id, handle = transport.create(SpawnRequest(
        role="discovery-yield", subsystem="discovery",
        repo_url="https://github.com/t/webcrawler",
        starting_ref="abc123", prompt="go"))
    assert agent_id == "bc-1"
    assert handle.branch == "cursor/assigned-at-create"
    assert transport._working_branch["bc-1"] == "cursor/assigned-at-create"


def test_create_persists_nested_agent_target(transport):
    from harness.agentloop import SpawnRequest

    transport._call = MagicMock(return_value={
        "agent": {"id": "bc-2",
                  "target": {"branchName": "cursor/nested-create"}},
        "run": {"id": "run-9"},
    })
    _, handle = transport.create(SpawnRequest(
        role="x", subsystem="discovery",
        repo_url="https://github.com/t/r", starting_ref="a", prompt="p"))
    assert handle.branch == "cursor/nested-create"
    assert handle.run_id == "run-9"


def test_send_keeps_working_branch(transport):
    transport._working_branch["bc-9"] = "cursor/kept"
    transport._call = MagicMock(return_value={"run": {"id": "run-follow"}})
    handle = transport.send("bc-9", "follow up")
    assert handle.branch == "cursor/kept"
    assert handle.run_id == "run-follow"


def test_wait_fills_branch_from_agent_when_run_git_is_empty(transport):
    transport._call = MagicMock(side_effect=[
        {"status": "ERROR", "result": ""},
        {"id": "bc-9", "target": {"branchName": "cursor/from-agent"}},
        {"totalUsage": {}},
    ])
    transport._read_stream = MagicMock(return_value=(
        [], {"code": "worker_gone", "message": "reconnect budget exhausted"}))
    evidence = transport.wait(RunHandle("bc-9", "run-2"), max_wait_s=5)
    assert evidence.status == "error"
    assert evidence.branch == "cursor/from-agent"
    assert evidence.error_code == "worker_gone"
    assert "/agents/bc-9" in [c.args[1] for c in transport._call.call_args_list]


def test_wait_prefers_branch_recorded_at_spawn(transport):
    transport._call = MagicMock(side_effect=[
        {"status": "FINISHED", "result": "ok"},
        {"target": {"branchName": "cursor/from-agent"}},
        {},
    ])
    transport._read_stream = MagicMock(return_value=([], None))
    evidence = transport.wait(
        RunHandle("bc-9", "run-2", "cursor/from-create"), max_wait_s=5)
    assert evidence.branch == "cursor/from-create"
