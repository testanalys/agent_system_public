"""Freestyle handoff gate + bake trigger helpers."""
from __future__ import annotations

from pathlib import Path

from harness import handoff
from harness.skeleton import Skeleton


class _Git:
    def __init__(self, files: list[str]):
        self._files = files

    def files_under(self, branch: str, prefix: str) -> list[str]:
        return [f for f in self._files if f.startswith(prefix)]


def test_handoff_complete_requires_interaction_jsonl():
    git = _Git([
        "state/S1/rd_retro_handoff/manifest.json",
    ])
    assert handoff.handoff_complete(git, "agent/x", "S1")["ok"] is False

    git = _Git([
        "state/S1/rd_retro_handoff/interaction.jsonl",
        "state/S1/rd_retro_handoff.json",
    ])
    status = handoff.handoff_complete(git, "agent/x", "S1")
    assert status["ok"] is True
    assert status["interaction_paths"]


def test_bake_trigger_names_branch_and_session():
    t = handoff.bake_trigger("S1", "cursor/freestyle-abc", handoff={
        "interaction_paths": ["state/S1/rd_retro_handoff/interaction.jsonl"],
    })
    assert t["dims"]["freestyle_branch"] == "cursor/freestyle-abc"
    assert t["dims"]["session_id"] == "S1"


def test_session_id_from_dims():
    assert handoff.session_id_from(
        {"dims": {"session_id": "20260812-000346"}}, None) == "20260812-000346"


def test_freestyle_not_green_on_harvesters():
    root = Path(__file__).resolve().parents[1]
    sk = Skeleton.load_file(root / "skeletons" / "discovery.json", repo=root)
    harvest = next(z for z in sk.zones if z.id == "harvest-layer")
    assert "discovery-freestyle-rd" not in harvest.green_for
    assert "discovery-route-bake" in harvest.green_for
    handoff_z = next(z for z in sk.zones if z.id == "freestyle-handoff")
    assert "discovery-freestyle-rd" in handoff_z.green_for
