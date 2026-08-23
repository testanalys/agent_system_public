"""Master loop: wager gate, phase transitions, routing, rebuild path."""
from __future__ import annotations

import json
import sys
from pathlib import Path

import pytest

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT))

from harness import master_loop, notify, rebuild, routing, wager     # noqa: E402
from harness.classify import Classification, GREEN, RED, AMBER      # noqa: E402
from harness.diff import FileChange, SessionDiff                    # noqa: E402
from harness.gates import GateResult                                # noqa: E402
from harness.skeleton import Skeleton                               # noqa: E402


# ═══════════════════════════════════════════════════════════════ wager

def test_wager_hold_needs_almost_nothing():
    w = wager.from_dict({"mode": "hold", "headline": "wait for next pulse"})
    wager.validate(w)
    assert w.holding


def test_wager_repair_requires_surface_and_metric(tmp_path):
    (tmp_path / "lifeblood").mkdir()
    (tmp_path / "lifeblood" / "catalogue.json").write_text(json.dumps({
        "metrics": [{"id": "funnel_conversion"}],
    }), encoding="utf-8")
    with pytest.raises(wager.WagerInvalid) as exc:
        wager.validate(wager.from_dict({
            "mode": "repair", "headline": "fix the stall",
            "why_now": "conversion fell",
            "expected_effect": {
                "metric": "funnel_conversion", "direction": "up",
                "rationale": (
                    "Funnel conversion is the share of admitted work that "
                    "becomes findable audio; a stall here means volume never "
                    "reaches the product."
                ),
            },
        }), tmp_path)
    assert any("surface" in p for p in exc.value.problems)

    wager.validate(wager.from_dict({
        "mode": "repair", "headline": "fix the stall",
        "why_now": "conversion fell",
        "expected_effect": {
            "metric": "funnel_conversion", "direction": "up",
            "rationale": (
                "Funnel conversion is the share of admitted work that "
                "becomes findable audio; a stall here means volume never "
                "reaches the product."
            ),
        },
        "surface": ["core/pipeline/x.py"],
    }), tmp_path)


def test_wager_rejects_unknown_mode():
    with pytest.raises(wager.WagerInvalid):
        wager.validate(wager.from_dict({"mode": "refactor", "headline": "x"}))


# ══════════════════════════════════════════════════════════ next_phase

def _diff(files=2, added=40, new_files=0) -> SessionDiff:
    changes = [
        FileChange(path=f"a{i}.py", status="A" if i < new_files else "M",
                   added=added // max(files, 1), deleted=0)
        for i in range(files)
    ]
    return SessionDiff(repo=Path("."), base="b", head="h", files=changes)


def test_orient_hold_ends_the_cycle():
    w = wager.from_dict({"mode": "hold", "headline": "wait"})
    assert master_loop.next_phase(master_loop.ORIENT, w, None, None) is None


def test_orient_non_hold_goes_to_implement():
    w = wager.from_dict({
        "mode": "repair", "headline": "fix it", "why_now": "x",
        "expected_effect": {"metric": "a", "direction": "up"},
        "surface": ["a.py"],
    })
    assert master_loop.next_phase(
        master_loop.ORIENT, w, None, None) == master_loop.IMPLEMENT


def test_implement_small_change_with_passing_gates_disposes():
    assert master_loop.next_phase(
        master_loop.IMPLEMENT, None, _diff(2, 40),
        [GateResult("G-HIND", True)]) is None


def test_implement_over_bar_goes_to_reduce():
    assert master_loop.next_phase(
        master_loop.IMPLEMENT, None, _diff(9, 200),
        []) == master_loop.REDUCE


def test_implement_failed_gate_goes_to_prove():
    failed = [GateResult("G-HIND", False, reprompt="look at history")]
    assert master_loop.next_phase(
        master_loop.IMPLEMENT, None, _diff(2, 40),
        failed) == master_loop.PROVE


def test_soft_gate_does_not_force_prove():
    soft = [GateResult("G-FP", False, reprompt="quick check")]
    assert master_loop.next_phase(
        master_loop.IMPLEMENT, None, _diff(2, 40), soft) is None


def test_reduce_then_failed_gate_goes_to_prove():
    failed = [GateResult("G-DISP", False, reprompt="dispose")]
    assert master_loop.next_phase(
        master_loop.REDUCE, None, _diff(9, 200),
        failed) == master_loop.PROVE


def test_implement_smells_go_to_reduce_even_when_small():
    hits = [{"id": "H-STUB", "points": 28,
             "detail": "a.py::f returns a stub"}]
    assert master_loop.next_phase(
        master_loop.IMPLEMENT, None, _diff(2, 40),
        [], hits=hits) == master_loop.REDUCE


def test_wager_requires_a_product_rationale(tmp_path):
    (tmp_path / "lifeblood").mkdir()
    (tmp_path / "lifeblood" / "catalogue.json").write_text(json.dumps({
        "metrics": [{"id": "funnel_conversion"}],
    }), encoding="utf-8")
    with pytest.raises(wager.WagerInvalid) as exc:
        wager.validate(wager.from_dict({
            "mode": "repair", "headline": "fix the helper",
            "why_now": "the function is messy",
            "expected_effect": {
                "metric": "funnel_conversion", "direction": "up",
                "rationale": "refactored the helper for cleanliness",
            },
            "surface": ["core/pipeline/x.py"],
        }), tmp_path)
    assert any("rationale" in p for p in exc.value.problems)


def test_leftover_wager_is_rejected():
    class Git:
        def read_file(self, ref, path):
            return json.dumps({
                "mode": "repair", "headline": "last cycle's bet",
                "why_now": "old evidence",
                "expected_effect": {
                    "metric": "funnel_conversion", "direction": "up",
                    "rationale": (
                        "Funnel conversion is the share of admitted work "
                        "that becomes findable audio."
                    ),
                },
                "surface": ["a.py"],
            })

    with pytest.raises(wager.WagerInvalid) as exc:
        wager.load_from_branch(Git(), "agent/branch", base="origin/master")
    assert any("leftover" in p for p in exc.value.problems)


# ══════════════════════════════════════════════════════════════ routing

def test_hold_writes_an_operator_card():
    w = wager.from_dict({"mode": "hold", "headline": "wait"})
    r = routing.route(w, Classification(tier=GREEN))
    assert r.action == routing.NONE
    assert r.notify is True


def test_green_merges():
    w = wager.from_dict({
        "mode": "build_agent", "headline": "new investigator",
        "why_now": "gap", "expected_effect": {"metric": "a", "direction": "up"},
        "surface": ["agents/x.md"],
    })
    r = routing.route(w, Classification(tier=GREEN), branch="agent/m-1")
    assert r.action == routing.MERGE
    assert r.notify is True


def test_red_waits_for_operator():
    w = wager.from_dict({
        "mode": "new_module", "headline": "schema",
        "why_now": "x", "expected_effect": {"metric": "a", "direction": "up"},
        "surface": ["migrations/1.sql"],
    })
    r = routing.route(w, Classification(
        tier=RED, signals=[{"id": "R2-schema", "detail": "sql"}]),
        branch="agent/m-2")
    assert r.action == routing.OPERATOR
    assert "R2-schema" in r.red_rules
    assert r.notify is True


def test_job_yaml_is_red_for_master():
    """Closed loop depends on infra/env configs never auto-merging."""
    skeleton = Skeleton.load_file(ROOT / "skeletons" / "pipeline.json")
    from harness.classify import classify
    diff = SessionDiff(
        repo=Path("."), base="b", head="h",
        files=[FileChange(path="pipeline/cloud/jobs/sync.yaml", status="M",
                          added=10, deleted=2)],
    )
    result = classify(diff, "pipeline-master", skeleton)
    assert result.tier == RED


def test_supervisor_definition_is_red_for_master():
    skeleton = Skeleton.load_file(ROOT / "skeletons" / "pipeline.json")
    from harness.classify import classify
    diff = SessionDiff(
        repo=Path("."), base="b", head="h",
        files=[FileChange(path="agents/pipeline-retro.md", status="M",
                          added=12, deleted=3)],
    )
    result = classify(diff, "pipeline-master", skeleton)
    assert result.tier == RED
    assert any(s.get("id") == "R7-supervisor-definitions"
               for s in result.signals)


def test_scale_framework_is_red_for_master():
    skeleton = Skeleton.load_file(ROOT / "skeletons" / "discovery.json")
    from harness.classify import classify
    diff = SessionDiff(
        repo=Path("."), base="b", head="h",
        files=[FileChange(path="agents/_scale_framework.md", status="M",
                          added=8, deleted=2)],
    )
    result = classify(diff, "discovery-master", skeleton)
    assert result.tier == RED
    assert any(s.get("id") == "R7-supervisor-definitions"
               for s in result.signals)


def test_campaign_json_is_red_for_every_agent():
    skeleton = Skeleton.load_file(ROOT / "skeletons" / "discovery.json")
    from harness.classify import classify
    diff = SessionDiff(
        repo=Path("."), base="b", head="h",
        files=[FileChange(path="campaigns/example-campaign.json",
                          status="M", added=6, deleted=1)],
    )
    for agent in ("discovery-master", "discovery-campaign-yield"):
        result = classify(diff, agent, skeleton)
        assert result.tier == RED, agent
        assert any(s.get("id") == "R8-campaign-portfolio"
                   for s in result.signals)


def test_pit_agent_and_breach_wiring_can_merge():
    skeleton = Skeleton.load_file(ROOT / "skeletons" / "pipeline.json")
    from harness.classify import classify
    diff = SessionDiff(
        repo=Path("."), base="b", head="h",
        files=[
            FileChange(path="agents/pipeline-funnel-leak.md", status="M",
                       added=20, deleted=4),
            FileChange(path="agents/agent_utils/breaches.py", status="M",
                       added=15, deleted=2),
        ],
    )
    result = classify(diff, "pipeline-master", skeleton)
    assert result.tier == GREEN
    w = wager.from_dict({
        "mode": "build_agent", "headline": "watch the funnel",
        "why_now": "gap", "expected_effect": {"metric": "a", "direction": "up"},
        "surface": ["agents/pipeline-funnel-leak.md", "agents/agent_utils/breaches.py"],
    })
    assert routing.route(w, result, branch="agent/m").action == routing.MERGE


def test_large_master_agent_diff_stays_under_red():
    skeleton = Skeleton.load_file(ROOT / "skeletons" / "pipeline.json")
    from harness.classify import classify
    files = [FileChange(path=f"agents/pipeline-funnel-leak.md", status="M",
                        added=50, deleted=10)]
    for i in range(5):
        files.append(FileChange(path=f"agents/extra-{i}.md", status="A",
                                added=20, deleted=0))
    diff = SessionDiff(repo=Path("."), base="b", head="h", files=files)
    result = classify(diff, "pipeline-master", skeleton)
    assert result.tier == GREEN
    assert result.score < 100
    assert result.signals


# ══════════════════════════════════════════════════════════════ notify

def test_hold_writes_operator_folder_not_slack(tmp_path, monkeypatch):
    monkeypatch.setenv("TURNSTILE_AGENT_STORE", str(tmp_path / "store"))
    w = wager.from_dict({"mode": "hold", "headline": "wait for next pulse"})
    r = routing.route(w, None)
    result = notify.notify_master(w, r, subsystem="pipeline", agent_id="bc-m")
    assert result["ok"]
    assert result.get("hold")
    from harness import operator
    held = operator.list_hold("pipeline")
    assert held and held[0]["headline"] == "wait for next pulse"

    w2 = wager.from_dict({
        "mode": "repair", "headline": "touch infra",
        "why_now": "x", "expected_effect": {"metric": "a", "direction": "up"},
        "surface": ["cloud/jobs/x.yaml"],
    })
    r2 = routing.Route(routing.OPERATOR, "RED (R3-infra)",
                       red_rules=("R3-infra",), notify=True)
    text = notify.master_message(w2, r2, subsystem="pipeline",
                                 branch="agent/x", files=1, classify=RED)
    assert "NEEDS OPERATOR" in text
    assert "R3-infra" in text

    from harness import pulls
    title = pulls.operator_title("pipeline", "touch infra")
    assert title.startswith(pulls.PR_TITLE_PREFIX)
    assert title.startswith("[turnstile-operator]")
    assert "pipeline" in title
    assert "touch infra" in title


def test_rebuild_dry_run_for_pipeline(tmp_path):
    (tmp_path / "pipeline" / "cloud").mkdir(parents=True)
    (tmp_path / "pipeline" / "cloud" / "cloudbuild-pipeline.yaml").write_text("steps: []\n")
    result = rebuild.rebuild("pipeline", tmp_path, dry_run=True)
    assert result["ok"] and result["dry_run"]
    assert result["path"] == "production_branch_push"


# ═══════════════════════════════════════════════════════════ prompts

def test_implement_reprompt_does_not_name_later_phases():
    from harness import reprompts
    text = reprompts.implement({
        "mode": "reduce", "headline": "delete the dual path",
        "surface": ["a.py"], "expected_effect": {"metric": "code_restraint",
                                                  "direction": "up"},
    }).lower()
    assert "do it" in text
    assert "reduce" not in text or "delete the dual path" in text
    # Must not announce the loop.
    assert "phase" not in text
    assert "you will later" not in text
    assert "next you will" not in text


def test_reduce_reprompt_quotes_the_counts():
    from harness import reprompts
    text = reprompts.reduce(9, 240, ["a.py", "b.py"])
    assert "9 files" in text
    assert "+240" in text or "240" in text


def test_reduce_reprompt_names_the_smell_not_the_rule():
    from harness import reprompts
    from harness.oracle_kit.leak_vocabulary import LEAK_PATTERNS
    import re
    text = reprompts.reduce(2, 40, ["a.py"], hits=[{
        "id": "H-STUB", "points": 28,
        "detail": "a.py::harvest returns a stub and leaves the old body",
    }, {
        "id": "H1", "points": 12,
        "detail": "a.py has been modified 5 times in 30 days",
    }])
    assert "stub" in text.lower()
    assert "5 times" in text
    assert "H-STUB" not in text
    assert "H1" not in text
    for pattern, label in LEAK_PATTERNS:
        match = re.search(pattern, text, re.I)
        assert match is None, f"reduce coaching leaks {label}: {match.group(0)}"


def test_implement_build_agent_does_not_shrink_the_bet():
    from harness import reprompts
    text = " ".join(reprompts.implement({
        "mode": "build_agent", "headline": "watch uncovered sources",
        "why_now": "tool count is flat and yield is zero",
        "expected_effect": {
            "metric": "harvest_tool_growth", "direction": "up",
            "rationale": "new watchers compound verified audio every run",
        },
        "surface": ["agents/discovery-source-gap.md"],
    }).lower().split())
    assert "do not shrink" in text
    assert "capability" in text
    assert "phase" not in text
    assert "you will later" not in text


# ════════════════════════════════════════════════════ observation / MT7

def test_master_error_with_origin_branch_does_not_abort(tmp_path, monkeypatch):
    """Cursor ERROR is not a master outcome when git has the branch."""
    import json as json_mod
    import subprocess
    from harness.agentloop import AgentEvidence, SpawnRequest

    monkeypatch.setenv("TURNSTILE_AGENT_STORE", str(tmp_path / "store"))
    repo = tmp_path / "repo"
    repo.mkdir()

    def git(*args: str) -> str:
        return subprocess.run(
            ["git", *args], cwd=repo, capture_output=True,
            text=True, check=True).stdout.strip()

    git("init", "-b", "main")
    git("config", "user.email", "t@t")
    git("config", "user.name", "t")
    (repo / "harvesters").mkdir()
    (repo / "harvesters" / "a.py").write_text("x = 1\n", encoding="utf-8")
    (repo / "skeleton.json").write_text(json_mod.dumps({
        "subsystem": "demo", "protected_branches": ["main"],
        "limits": {"files": 8, "net_loc": 200, "gross_churn": 400,
                   "new_files_in_zone": 4, "new_files_outside_zone": 2,
                   "new_dependencies": 0, "new_config_keys": 0,
                   "signature_changes": 0, "branch_tokens": 10,
                   "nesting_depth": 1, "dir_population": 15,
                   "filename_similarity": 0.8},
        "red": [],
        "zones": [{"id": "harvest", "paths": ["harvesters/**"],
                   "green_for": ["discovery-master"],
                   "oracle": "python -c \"print('ok')\""}],
    }), encoding="utf-8")
    git("add", "-A")
    git("commit", "-m", "base")
    git("checkout", "-b", "agent/m")
    (repo / "harvesters" / "a.py").write_text("x = 2\n", encoding="utf-8")
    git("add", "-A")
    git("commit", "-m", "work")
    git("checkout", "main")

    class Git:
        def __init__(self):
            self.base = "main"

        def fetch(self, branch: str) -> None:
            pass

        def collect_diff(self, base: str, branch: str) -> SessionDiff:
            return SessionDiff.collect(repo, "main", branch)

        def merge_to_base(self, branch: str, base: str) -> str:
            raise AssertionError("should not merge without a wager")

    class Transport:
        def __init__(self):
            self.sent: list[str] = []

        def create(self, request):
            return "bc-m", "handle"

        def wait(self, handle, max_wait_s=1, **kwargs):
            return AgentEvidence(
                agent_id="bc-m", run_id="run-1", status="error",
                branch="agent/m", error_message="worker_gone")

        def send(self, agent_id, text):
            self.sent.append(text)
            return "handle"

    report = master_loop.run(
        SpawnRequest(
            role="discovery-master", subsystem="demo",
            repo_url="file://x", starting_ref="main", prompt="orient"),
        Transport(), repo, Skeleton.load(repo), Git(),
        max_wait_s=1,
    )
    assert report.outcome != "FAILED"
    assert "run ended error" not in (report.detail.get("reason") or "")
    assert report.branch == "agent/m"
    assert report.outcome == "INCOMPLETE"
    assert report.detail.get("sub_reason") == "no_wager_produced"
