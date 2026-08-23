"""Scored factory: GREEN/RED at 100, habits, agent-specific plates."""
from __future__ import annotations

import json
import subprocess
import sys
from pathlib import Path

import pytest

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))

from harness import classify as classify_mod
from harness.diff import SessionDiff
from harness.skeleton import Skeleton
from harness.scorecard.weights import THRESHOLD

SKELETON = {
    "subsystem": "test-subsystem",
    "protected_branches": ["main"],
    "limits": {
        "files": 3, "net_loc": 80, "gross_churn": 200,
        "new_files_in_zone": 1, "new_files_outside_zone": 0,
        "new_dependencies": 0, "new_config_keys": 0,
        "signature_changes": 0, "branch_tokens": 10,
        "nesting_depth": 1, "dir_population": 15,
        "filename_similarity": 0.8,
    },
    "red": [
        {"id": "R1-control", "paths": ["control/**"],
         "reason": "control-plane logic"},
        {"id": "R5-instruments", "paths": ["gates.py"],
         "reason": "the exam",
         "except_for": ["*-master"]},
    ],
    "zones": [
        {
            "id": "harvest", "paths": ["harvesters/**"],
            "green_for": ["discovery-*", "discovery-retro", "*-master"],
            "oracle": "python -c \"print('ok')\"",
            "limits": {"files": 2, "net_loc": 150, "new_files_in_zone": 1},
        },
        {
            "id": "metric-instruments",
            "paths": ["lifeblood/utils/**", "lifeblood/catalogue.json"],
            "green_for": ["*-master", "*-retro"],
            "touch_cost": 12,
            "oracle": "python -c \"print('ok')\"",
        },
    ],
}


def _run(repo: Path, *args: str) -> str:
    return subprocess.run(
        ["git", *args], cwd=repo, capture_output=True, text=True, check=True,
    ).stdout.strip()


def _write(repo: Path, rel: str, text: str) -> None:
    path = repo / rel
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text(text, encoding="utf-8")


@pytest.fixture
def repo(tmp_path: Path) -> Path:
    root = tmp_path / "testrepo"
    root.mkdir()
    _run(root, "init", "-b", "main")
    _run(root, "config", "user.email", "harness@test")
    _run(root, "config", "user.name", "harness test")
    _write(root, "skeleton.json", json.dumps(SKELETON, indent=2))
    _write(root, "harvesters/alpha.py",
           "def harvest():\n    x = 1\n    return [x]\n")
    _write(root, "control/reconciler.py", "def tick():\n    pass\n")
    _write(root, "gates.py", "THRESHOLD = 0.5\n")
    _write(root, "lifeblood/catalogue.json", "{}\n")
    _run(root, "add", "-A")
    _run(root, "commit", "-m", "base")
    _run(root, "checkout", "-b", "work")
    return root


def _classify(repo: Path, agent: str):
    skeleton = Skeleton.load(repo)
    diff = SessionDiff.collect(repo, "main")
    return classify_mod.classify(diff, agent, skeleton)


def _commit(repo: Path, message: str = "work") -> None:
    _run(repo, "add", "-A")
    _run(repo, "commit", "-m", message)


def test_stub_return_leaving_dead_body_is_a_habit(repo):
    _write(repo, "harvesters/alpha.py",
           "def harvest():\n    return {}\n    x = 1\n    return [x]\n")
    _commit(repo)
    result = _classify(repo, "discovery-yield")
    assert any(s["id"] == "H-STUB" for s in result.signals)
    assert result.score >= 40
    # Size is tiny; stub alone is not 100.
    assert result.tier == classify_mod.GREEN
    assert result.score < THRESHOLD


def test_stub_plus_shape_can_stay_under_red(repo):
    _write(repo, "harvesters/alpha.py",
           "def harvest():\n    return {}\n    x = 1\n    return [x]\n")
    _commit(repo)
    result = _classify(repo, "discovery-yield")
    assert result.band in (classify_mod.GREEN, classify_mod.AMBER, classify_mod.RED)
    assert result.tier == classify_mod.GREEN


def test_control_plane_is_still_one_hundred(repo):
    _write(repo, "control/reconciler.py", "def tick():\n    return 1\n")
    _commit(repo)
    pit = _classify(repo, "discovery-yield")
    master = _classify(repo, "discovery-master")
    assert pit.tier == master.tier == classify_mod.RED
    assert pit.score >= THRESHOLD


def test_instruments_are_red_for_pit_caution_for_master(repo):
    _write(repo, "lifeblood/catalogue.json", '{"metrics": []}\n')
    _commit(repo)
    pit = _classify(repo, "discovery-yield")
    master = _classify(repo, "discovery-master")
    assert pit.tier == classify_mod.RED
    assert master.tier == classify_mod.GREEN
    assert any(s["id"] == "Z-CAUTION" for s in master.signals)
    assert master.score < THRESHOLD


def test_except_for_carves_master_out_of_a_red_rule(repo):
    _write(repo, "gates.py", "THRESHOLD = 0.1\n")
    _commit(repo)
    pit = _classify(repo, "discovery-yield")
    master = _classify(repo, "discovery-master")
    assert pit.tier == classify_mod.RED
    assert any(s["id"] == "R5-instruments" for s in pit.signals)
    assert master.score < THRESHOLD
    assert not any(s["id"] == "R5-instruments" for s in master.signals)
