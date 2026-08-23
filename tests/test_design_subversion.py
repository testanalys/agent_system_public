"""Design-subversion habits, narration, conjunctions, and role scale."""
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
from harness.scorecard import habits, narration, conjunctions, weights
from harness.scorecard.weights import THRESHOLD, ROLE_SCALE

SKELETON = {
    "subsystem": "test-subsystem",
    "protected_branches": ["main"],
    "limits": {
        "files": 8, "net_loc": 400, "gross_churn": 800,
        "new_files_in_zone": 2, "new_files_outside_zone": 0,
        "new_dependencies": 0, "new_config_keys": 0,
        "signature_changes": 0, "branch_tokens": 40,
        "nesting_depth": 4, "dir_population": 50,
        "filename_similarity": 0.8,
    },
    "red": [
        {"id": "R1-control", "paths": ["control/**"],
         "reason": "control-plane logic"},
    ],
    "zones": [
        {
            "id": "harvest", "paths": ["harvesters/**"],
            "green_for": ["discovery-*", "discovery-retro", "*-master"],
            "oracle": "python -c \"print('ok')\"",
            "limits": {
                "files": 6, "net_loc": 400, "new_files_in_zone": 2,
                "new_config_keys": 0, "new_dependencies": 0,
                "gross_churn": 800, "branch_tokens": 40, "nesting_depth": 4,
            },
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
           '"""Harvest alpha.\n\nMust validate every row.\n"""\n'
           "def harvest():\n"
           "    x = 1\n"
           "    y = x + 1\n"
           "    return [y]\n")
    _write(root, "harvesters/legacy_helper.py",
           "def legacy_harvest():\n    return [1]\n")
    _write(root, "tests/test_alpha.py",
           "from harvesters.alpha import harvest\n\n"
           "def test_harvest():\n"
           "    assert harvest() == [2]\n"
           "    assert len(harvest()) == 1\n"
           "    assert harvest()[0] > 0\n")
    _write(root, "control/reconciler.py", "def tick():\n    pass\n")
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


def _ids(result) -> set[str]:
    return {s["id"] for s in result.signals}


# ── individual habits ─────────────────────────────────────────────


def test_fake_ok_preferred_over_generic_stub(repo):
    _write(repo, "harvesters/alpha.py",
           "def harvest():\n"
           "    return True\n")
    _commit(repo)
    result = _classify(repo, "discovery-yield")
    assert "H-FAKE-OK" in _ids(result)
    assert "H-STUB" not in _ids(result)
    assert result.tier == classify_mod.GREEN
    assert result.score < THRESHOLD


def test_dual_path_with_legacy_helper(repo):
    _write(repo, "harvesters/alpha.py",
           "import os\n"
           "from harvesters.legacy_helper import legacy_harvest\n\n"
           "def harvest():\n"
           "    if os.environ.get('ENABLE_NEW_HARVEST'):\n"
           "        return [99]\n"
           "    return legacy_harvest()\n")
    _commit(repo)
    result = _classify(repo, "discovery-yield")
    assert "H-DUAL-PATH" in _ids(result)


def test_comment_out_resurrects_code(repo):
    # Keep a real body so H-STUB does not dominate; comments must mirror
    # lines that existed in the base file (x / y / return).
    _write(repo, "harvesters/alpha.py",
           '"""Harvest alpha.\n\nMust validate every row.\n"""\n'
           "def harvest():\n"
           "    # x = 1\n"
           "    # y = x + 1\n"
           "    # return [y]\n"
           "    x = 2\n"
           "    return [x]\n")
    _commit(repo)
    result = _classify(repo, "discovery-yield")
    assert "H-COMMENT-OUT" in _ids(result)


def test_swallow_success_return(repo):
    _write(repo, "harvesters/alpha.py",
           "def harvest():\n"
           "    try:\n"
           "        return [1]\n"
           "    except Exception:\n"
           "        return True\n")
    _commit(repo)
    result = _classify(repo, "discovery-yield")
    assert "H-SWALLOW" in _ids(result)


def test_noqa_on_modified_file(repo):
    _write(repo, "harvesters/alpha.py",
           "def harvest():\n"
           "    x = 1  # noqa: F841\n"
           "    y = x + 1  # type: ignore\n"
           "    return [y]\n")
    _commit(repo)
    result = _classify(repo, "discovery-yield")
    assert "H-NOQA" in _ids(result)


def test_assert_drop_in_tests(repo):
    _write(repo, "tests/test_alpha.py",
           "from harvesters.alpha import harvest\n\n"
           "def test_harvest():\n"
           "    assert harvest() == [2]\n")
    _commit(repo)
    result = _classify(repo, "discovery-yield")
    assert "H-ASSERT-DROP" in _ids(result)


# ── narration ─────────────────────────────────────────────────────


def test_defer_phrase_frame_on_added_comment(repo):
    _write(repo, "harvesters/alpha.py",
           "def harvest():\n"
           "    # will fix this properly later\n"
           "    x = 1\n"
           "    y = x + 1\n"
           "    return [y]\n")
    _commit(repo)
    result = _classify(repo, "discovery-yield")
    assert "N-DEFER" in _ids(result)
    assert result.tier == classify_mod.GREEN


def test_doc_lie_when_body_stubbed(repo):
    _write(repo, "harvesters/alpha.py",
           "def harvest():\n"
           '    """Returns validated harvest rows."""\n'
           "    return {}\n")
    _commit(repo)
    result = _classify(repo, "discovery-yield")
    assert "N-DOC-LIE" in _ids(result)
    assert "H-STUB" in _ids(result)


# ── conjunctions + scenarios ──────────────────────────────────────


def test_stub_plus_defer_reds_for_pit_not_master(repo):
    """Admitted incomplete design: RED for PIT, GREEN for master."""
    _write(repo, "harvesters/alpha.py",
           "def harvest():\n"
           "    # temporary workaround — will fix properly later\n"
           "    return {}\n"
           "    x = 1\n"
           "    return [x]\n")
    _commit(repo)
    pit = _classify(repo, "discovery-yield")
    master = _classify(repo, "discovery-master")
    assert "H-STUB" in _ids(pit)
    assert "N-DEFER" in _ids(pit) or "N-TRIVIAL" in _ids(pit)
    assert "J-STUB-SATD" in _ids(pit)
    assert "N-STRAIN" in _ids(pit)
    assert pit.tier == classify_mod.RED
    assert pit.score >= THRESHOLD
    assert master.tier == classify_mod.GREEN
    assert master.score < THRESHOLD
    assert master.summary["role_scale"] == ROLE_SCALE["master"]
    assert pit.summary["role_scale"] == ROLE_SCALE["pit"]


def test_dual_path_plus_new_env_key_reds_pit(repo):
    _write(repo, "harvesters/alpha.py",
           "import os\n"
           "from harvesters.legacy_helper import legacy_harvest\n\n"
           "def harvest():\n"
           "    if os.environ.get('ENABLE_NEW_HARVEST_V2'):\n"
           "        return [99]\n"
           "    return legacy_harvest()\n")
    _commit(repo)
    pit = _classify(repo, "discovery-yield")
    assert "H-DUAL-PATH" in _ids(pit)
    assert "C7" in _ids(pit)
    assert "J-DUAL-KEY" in _ids(pit)
    assert pit.tier == classify_mod.RED


def test_lone_narration_never_reds(repo):
    _write(repo, "harvesters/alpha.py",
           '"""Harvest alpha.\n\nMust validate every row.\n"""\n'
           "def harvest():\n"
           "    # should work now for the happy path\n"
           "    x = 1\n"
           "    y = x + 1\n"
           "    return [y]\n")
    _commit(repo)
    pit = _classify(repo, "discovery-yield")
    assert "N-CONFIDENT" in _ids(pit)
    assert pit.tier == classify_mod.GREEN
    # Phrase alone (plus role scale) stays a mild amber-or-below smell.
    assert pit.score < THRESHOLD
    assert not any(i.startswith("J-") for i in _ids(pit))


def test_clean_edit_stays_green(repo):
    _write(repo, "harvesters/alpha.py",
           '"""Harvest alpha.\n\nMust validate every row.\n"""\n'
           "def harvest():\n"
           "    x = 2\n"
           "    y = x + 1\n"
           "    return [y]\n")
    _commit(repo)
    pit = _classify(repo, "discovery-yield")
    smell = {i for i in _ids(pit) if i.startswith(("H-", "N-", "J-"))}
    # No design-subversion smells on a clean body tweak.
    assert not smell or smell <= {"H-PRINT"}  # allow nothing unexpected
    assert pit.tier == classify_mod.GREEN


def test_assert_drop_with_stub_reds_pit(repo):
    _write(repo, "harvesters/alpha.py",
           "def harvest():\n"
           "    return {}\n"
           "    x = 1\n"
           "    return [x]\n")
    _write(repo, "tests/test_alpha.py",
           "from harvesters.alpha import harvest\n\n"
           "def test_harvest():\n"
           "    assert harvest() == {}\n")
    _commit(repo)
    pit = _classify(repo, "discovery-yield")
    assert "H-ASSERT-DROP" in _ids(pit)
    assert "H-STUB" in _ids(pit)
    assert "J-ORACLE-DROP" in _ids(pit)
    assert pit.tier == classify_mod.RED


def test_role_scale_multiplies_habit_mass_only():
    hits = [
        {"id": "H-STUB", "points": 40, "detail": "x"},
        {"id": "C1", "points": 12, "detail": "files"},
        {"id": "R1-control", "points": 100, "detail": "protected"},
    ]
    scaled = weights.apply_role_scale(hits, "discovery-yield")
    by = {h["id"]: h for h in scaled}
    assert by["H-STUB"]["points"] == int(round(40 * ROLE_SCALE["pit"]))
    assert by["H-STUB"]["raw_points"] == 40
    assert by["C1"]["points"] == 12
    assert by["R1-control"]["points"] == 100


def test_conjunction_stub_satd_unit():
    hits = [
        {"id": "H-STUB", "points": 40, "paths": ["a.py"]},
        {"id": "N-DEFER", "points": 8, "paths": ["a.py"]},
    ]
    conj = conjunctions.collect(hits)
    assert any(c["id"] == "J-STUB-SATD" for c in conj)
