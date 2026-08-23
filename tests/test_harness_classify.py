"""End-to-end classifier tests against a real git repository.

These build an actual repo with an actual skeleton and actually commit to
it, because every interesting signal in the classifier reads git. A test
double for git would test the double.
"""
from __future__ import annotations

import json
import subprocess
import sys
from pathlib import Path

import pytest

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))

from harness import classify as classify_mod          # noqa: E402
from harness.diff import SessionDiff                  # noqa: E402
from harness.skeleton import Skeleton, SkeletonError   # noqa: E402

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
        {"id": "R5-instruments", "paths": ["gates.py", "skeleton.json"],
         "reason": "this is the instrument that grades the work"},
    ],
    "zones": [
        {
            "id": "harvest", "paths": ["harvesters/**"],
            "green_for": ["discovery-*", "discovery-retro"],
            "oracle": "python -c \"print('ok')\"",
            "limits": {"files": 2, "net_loc": 150, "new_files_in_zone": 1},
        },
        {
            "id": "prompts", "paths": ["prompts/**"],
            "green_for": ["*-retro"], "deny_self": True,
            "oracle": "python -c \"print('ok')\"",
        },
        {
            "id": "goldens", "paths": ["evals/goldens/**"],
            "green_for": ["retrieval-qa"], "append_only": True,
            "oracle": "python -c \"print('ok')\"",
        },
    ],
}


def _run(repo: Path, *args: str) -> str:
    proc = subprocess.run(["git", *args], cwd=repo, capture_output=True,
                          text=True, check=True)
    return proc.stdout.strip()


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
    _write(root, "harvesters/alpha.py", "def harvest():\n    return []\n")
    _write(root, "prompts/discovery-yield.md", "---\nname: x\n---\nrole\n")
    _write(root, "control/reconciler.py", "def tick():\n    pass\n")
    _write(root, "core/service.py", "def serve():\n    return 1\n")
    _write(root, "gates.py", "THRESHOLD = 0.5\n")
    _write(root, "evals/goldens/set.json", json.dumps([{"q": "a"}, {"q": "b"}]))
    _run(root, "add", "-A")
    _run(root, "commit", "-m", "base")
    _run(root, "checkout", "-b", "work")
    return root


def _classify(repo: Path, agent: str, signature=None, closures=None):
    skeleton = Skeleton.load(repo)
    diff = SessionDiff.collect(repo, "main")
    return classify_mod.classify(diff, agent, skeleton,
                                 signature=signature, closures=closures or [])


def _commit(repo: Path, message: str = "work") -> None:
    _run(repo, "add", "-A")
    _run(repo, "commit", "-m", message)


# ══════════════════════════════════════════════════════════════════ zones

def test_green_inside_own_zone(repo):
    _write(repo, "harvesters/alpha.py", "def harvest():\n    return ['x']\n")
    _commit(repo)
    result = _classify(repo, "discovery-yield")
    assert result.tier == classify_mod.GREEN
    assert result.zones == ["harvest"]
    assert result.oracle


def test_off_plate_is_red_for_a_pit_agent(repo):
    """Same file, same diff — a different agent gets a different answer."""
    _write(repo, "harvesters/alpha.py", "def harvest():\n    return ['x']\n")
    _commit(repo)
    result = _classify(repo, "diarization-qa")
    assert result.tier == classify_mod.RED
    assert result.score >= 100
    assert any("not open to diarization-qa" in s["detail"] for s in result.signals)


def test_core_path_is_red_for_a_pit_agent(repo):
    _write(repo, "core/service.py", "def serve():\n    return 2\n")
    _write(repo, "tests/test_service.py", "def test_serve():\n    assert True\n")
    _commit(repo)
    result = _classify(repo, "discovery-yield")
    assert result.tier == classify_mod.RED
    assert any(s["id"] == "Z1" for s in result.signals)


def test_red_on_control_plane(repo):
    _write(repo, "control/reconciler.py", "def tick():\n    return 1\n")
    _commit(repo)
    result = _classify(repo, "discovery-yield")
    assert result.tier == classify_mod.RED
    assert any(s["id"] == "R1-control" for s in result.signals)


def test_red_on_own_grader(repo):
    """An agent may not edit the exam it sits."""
    _write(repo, "gates.py", "THRESHOLD = 0.1\n")
    _commit(repo)
    result = _classify(repo, "discovery-yield")
    assert result.tier == classify_mod.RED
    assert any(s["id"] == "R5-instruments" for s in result.signals)


def test_deny_self_blocks_own_prompt(repo):
    _write(repo, "prompts/discovery-yield.md", "---\nname: x\n---\nrole goal\n")
    _commit(repo)
    mine = _classify(repo, "discovery-yield")
    assert mine.tier == classify_mod.RED
    assert any("itself" in s["detail"] for s in mine.signals)


def test_retro_may_edit_another_agents_prompt(repo):
    _write(repo, "prompts/discovery-yield.md", "---\nname: x\n---\nrole goal\n")
    _commit(repo)
    result = _classify(repo, "discovery-retro")
    assert result.tier == classify_mod.GREEN


def test_append_only_zone_rejects_edits(repo):
    _write(repo, "evals/goldens/set.json",
           json.dumps([{"q": "changed"}, {"q": "b"}]))
    _commit(repo)
    result = _classify(repo, "retrieval-qa")
    assert result.tier == classify_mod.RED
    assert any(s["id"] == "R9" for s in result.signals)


def test_append_only_zone_allows_new_files(repo):
    _write(repo, "evals/goldens/extra.json", json.dumps([{"q": "c"}]))
    _commit(repo)
    result = _classify(repo, "retrieval-qa")
    assert result.tier == classify_mod.GREEN


# ══════════════════════════════════════════════════════════════════ shape

def test_too_many_files_scores_but_does_not_trip_red_alone(repo):
    for name in ("a", "b", "c", "d"):
        _write(repo, f"harvesters/{name}.py", f"def h_{name}():\n    return []\n")
    _commit(repo)
    result = _classify(repo, "discovery-yield")
    assert result.tier == classify_mod.GREEN
    assert result.score < 100
    assert any(s["id"] == "C1" for s in result.signals)


def test_new_dependency_is_off_plate_for_a_pit_agent(repo):
    _write(repo, "requirements.txt", "requests>=2.0\n")
    _commit(repo)
    result = _classify(repo, "discovery-yield")
    assert result.tier == classify_mod.RED
    assert any(s["id"] == "Z1" for s in result.signals)
    assert any(s["id"] == "C6" for s in result.signals)


def test_new_env_var_scores_but_does_not_trip_red_alone(repo):
    _write(repo, "harvesters/alpha.py",
           "import os\n\n\ndef harvest():\n"
           "    return os.environ.get('NEW_HARVEST_MODE')\n")
    _commit(repo)
    result = _classify(repo, "discovery-yield")
    assert result.tier == classify_mod.GREEN
    assert any(s["id"] == "C7" for s in result.signals)


def test_crowded_directory_blocks_the_sixteenth_file(repo):
    for index in range(16):
        _write(repo, f"opsdir/script_{index}.py", "x = 1\n")
    _commit(repo, "populate")
    _write(repo, "opsdir/one_more_fix.py", "y = 2\n")
    _commit(repo, "one more")
    result = _classify(repo, "discovery-yield")
    assert any(s["id"] == "C12" for s in result.signals)


def test_net_zero_refinement_does_not_count(repo):
    """Iterating inside a session is free; only the net diff is measured."""
    _write(repo, "harvesters/alpha.py", "def harvest():\n" + "    x = 1\n" * 90)
    _commit(repo, "overshoot")
    _write(repo, "harvesters/alpha.py", "def harvest():\n    return ['x']\n")
    _commit(repo, "trim back")
    result = _classify(repo, "discovery-yield")
    assert result.tier == classify_mod.GREEN
    assert result.summary["net_loc"] <= 2


# ════════════════════════════════════════════════════════════════ history

def test_third_closure_of_a_signature_cannot_be_green(repo):
    _write(repo, "harvesters/alpha.py", "def harvest():\n    return ['x']\n")
    _commit(repo)
    closures = [
        {"date": "2026-06-01", "what": "patched the caller"},
        {"date": "2026-07-01", "what": "patched it again"},
    ]
    result = _classify(repo, "discovery-yield", signature="S1", closures=closures)
    assert result.tier == classify_mod.RED
    assert any(s["id"] == "H3" for s in result.signals)


def test_file_churn_scores_but_does_not_trip_red_alone(repo):
    """A file three previous sessions already patched is not a free target."""
    _run(repo, "checkout", "main")
    for index in range(3):
        _write(repo, "harvesters/alpha.py", f"def harvest():\n    return [{index}]\n")
        _commit(repo, f"prior patch {index}")
    _run(repo, "checkout", "-B", "work2")
    _write(repo, "harvesters/alpha.py", "def harvest():\n    return ['final']\n")
    _commit(repo, "the fourth attempt")
    result = _classify(repo, "discovery-yield")
    assert result.tier == classify_mod.GREEN
    assert result.score < 100
    assert any(s["id"] in ("H1", "H2") for s in result.signals)


def test_a_sessions_own_commits_are_not_its_history(repo):
    """Iterating on one file across several commits in one session is free."""
    for index in range(4):
        _write(repo, "harvesters/alpha.py", f"def harvest():\n    return [{index}]\n")
        _commit(repo, f"iteration {index}")
    result = _classify(repo, "discovery-yield")
    assert result.tier == classify_mod.GREEN


# ═════════════════════════════════════════════════════════════════ config

def test_zone_without_oracle_is_a_config_error(tmp_path):
    root = tmp_path / "broken"
    root.mkdir()
    _run(root, "init", "-b", "main")
    broken = {**SKELETON, "zones": [
        {"id": "x", "paths": ["x/**"], "green_for": ["some-agent"]}
    ]}
    _write(root, "skeleton.json", json.dumps(broken))
    with pytest.raises(SkeletonError, match="no oracle"):
        Skeleton.load(root)


def test_empty_diff_is_green(repo):
    result = _classify(repo, "discovery-yield")
    assert result.tier == classify_mod.GREEN
    assert result.summary["files"] == 0
