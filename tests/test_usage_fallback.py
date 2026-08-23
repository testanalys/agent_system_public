"""Master usage-limit create fallback."""
from __future__ import annotations

import sys
from dataclasses import dataclass
from pathlib import Path
from typing import Any

import pytest

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))

from harness.agentloop import SpawnRequest  # noqa: E402
from harness.cursor_rest import CursorAPIError, is_usage_limit_error  # noqa: E402
from harness.master_loop import _UsageFallbackTransport  # noqa: E402
from roles.models import ADVANCED_MODEL, USAGE_FALLBACK_MODEL  # noqa: E402


USAGE_BODY = (
    '{"error":{"code":"usage_limit_exceeded","message":'
    '"You need to increase your hard limit. Background Agent requires '
    'at least $2 remaining until your hard limit."}}'
)


@dataclass
class _FakeHandle:
    agent_id: str
    run_id: str


class _FakeTransport:
    def __init__(self, outcomes: list[Any]):
        self.outcomes = list(outcomes)
        self.creates: list[SpawnRequest] = []

    def create(self, request: SpawnRequest):
        self.creates.append(request)
        outcome = self.outcomes.pop(0)
        if isinstance(outcome, Exception):
            raise outcome
        return outcome


def _request(model: str | None) -> SpawnRequest:
    return SpawnRequest(
        role="discovery-master",
        subsystem="discovery",
        repo_url="https://github.com/example/discovery",
        starting_ref="master",
        prompt="orient",
        model=model,
    )


def test_is_usage_limit_error():
    assert is_usage_limit_error(CursorAPIError(400, USAGE_BODY, "/agents"))
    assert not is_usage_limit_error(
        CursorAPIError(400, '{"error":{"code":"bad_request"}}', "/agents"))


def test_master_create_retries_advanced_on_usage_limit():
    inner = _FakeTransport([
        CursorAPIError(400, USAGE_BODY, "/agents"),
        ("bc-fallback", _FakeHandle("bc-fallback", "run-2")),
    ])
    wrapped = _UsageFallbackTransport(inner)
    agent_id, handle = wrapped.create(_request(ADVANCED_MODEL))
    assert agent_id == "bc-fallback"
    assert handle.run_id == "run-2"
    assert wrapped.fell_back
    assert wrapped.model_used == USAGE_FALLBACK_MODEL
    assert [c.model for c in inner.creates] == [
        ADVANCED_MODEL, USAGE_FALLBACK_MODEL]


def test_master_create_does_not_fallback_for_cheap_model():
    err = CursorAPIError(400, USAGE_BODY, "/agents")
    inner = _FakeTransport([err])
    wrapped = _UsageFallbackTransport(inner)
    with pytest.raises(CursorAPIError):
        wrapped.create(_request("composer-2.5"))
    assert not wrapped.fell_back
    assert len(inner.creates) == 1


def test_master_create_does_not_fallback_for_other_400():
    err = CursorAPIError(400, '{"error":{"code":"invalid_model"}}', "/agents")
    inner = _FakeTransport([err])
    wrapped = _UsageFallbackTransport(inner)
    with pytest.raises(CursorAPIError):
        wrapped.create(_request(ADVANCED_MODEL))
    assert not wrapped.fell_back
