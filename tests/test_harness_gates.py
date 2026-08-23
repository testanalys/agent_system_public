"""Proposal gates, trade-off lint, triggers and master gates."""
from __future__ import annotations

import json
import subprocess
import sys
from pathlib import Path

import pytest

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))

from harness import docket as docket_mod                    # noqa: E402
from harness import gates, keywords, records                # noqa: E402
from harness.agentloop import AgentEvidence                  # noqa: E402
from harness import proposal as proposal_mod                 # noqa: E402
from harness import triggers as triggers_mod                 # noqa: E402


def _run(repo: Path, *args: str) -> str:
    return subprocess.run(["git", *args], cwd=repo, capture_output=True,
                          text=True, check=True).stdout.strip()


def says(text: str, phrase: str) -> bool:
    """Re-prompts are wrapped for a terminal, so phrases straddle lines."""
    return " ".join(phrase.lower().split()) in " ".join(text.lower().split())


@pytest.fixture
def repo(tmp_path: Path) -> Path:
    root = tmp_path / "repo"
    (root / "lifeblood" / "state").mkdir(parents=True)
    (root / "agent_state").mkdir(parents=True)
    _run(root.parent, "init", str(root))
    _run(root, "config", "user.email", "t@t")
    _run(root, "config", "user.name", "t")
    (root / "lifeblood" / "catalogue.json").write_text(json.dumps({
        "subsystem": "test",
        "metrics": [
            {"id": "funnel_conversion", "name": "Funnel", "family": "V",
             "lens": "SCALE", "cadence": "daily", "unit": "percent",
             "target": 80, "better": "up"},
            {"id": "code_health", "name": "Code health", "family": "S",
             "lens": "DESIGN", "cadence": "daily", "unit": "bytes"},
        ],
    }), encoding="utf-8")
    (root / "evidence.md").write_text("some evidence\n", encoding="utf-8")
    _run(root, "add", "-A")
    _run(root, "commit", "-m", "base")
    return root


def _body(**overrides) -> proposal_mod.ProposalBody:
    data = {
        "change": "Fold the metadata job into the publish step.",
        "why_now": "It has gone missing after a successful upstream run five times.",
        "evidence_refs": ["evidence.md", "metric:funnel_conversion"],
        "tradeoff": ("Removes a job and a stage handoff; publish runs a few "
                     "minutes slower; the chain stops breaking."),
        "expected_effect": {
            "metric": "funnel_conversion", "direction": "up",
            "rationale": (
                "Funnel conversion is the share of admitted work that "
                "becomes findable audio; a stall here means volume never "
                "reaches the product."
            ),
        },
    }
    data.update(overrides)
    return proposal_mod.ProposalBody(**data)


# ═════════════════════════════════════════════════════════════ trade-off

def test_tradeoff_word_cap_is_hard():
    result = keywords.check(" ".join(["complexity", "cost"] * 20))
    assert result["over_word_cap"]


def test_tradeoff_needs_two_axes():
    only_engineering = keywords.check("Removes a module and simplifies the code.")
    assert only_engineering["groups_present"] == ["engineering"]
    assert not only_engineering["enough_coverage"]

    two_axes = keywords.check("Removes a module; recall improves.")
    assert two_axes["enough_coverage"]


def test_missing_group_snippets_are_returned():
    missing = keywords.missing_groups("Removes a module and simplifies the code.")
    ids = {g.id for g in missing}
    assert {"economics", "data_quality", "reach"} <= ids
    assert all(g.snippet.strip() for g in missing)


def test_operator_example_statement_passes():
    statement = ("More complexity in the diarization module and two extra "
                 "handoffs, for a large gain in speaker-label accuracy and "
                 "overall data quality.")
    result = keywords.check(statement)
    assert not result["over_word_cap"]
    assert result["enough_coverage"]


# ════════════════════════════════════════════════════════ proposal gates

def test_valid_proposal_passes(repo):
    proposal_mod.validate(_body(), repo)


def test_missing_fields_reprompt(repo):
    with pytest.raises(proposal_mod.ProposalInvalid) as exc:
        proposal_mod.validate(_body(change="", tradeoff=""), repo)
    assert exc.value.gate == "P0"
    assert says(exc.value.reprompt, "change")


def test_unresolvable_evidence_reprompts(repo):
    with pytest.raises(proposal_mod.ProposalInvalid) as exc:
        proposal_mod.validate(_body(evidence_refs=["nope/missing.py"]), repo)
    assert exc.value.gate == "P1"
    assert says(exc.value.reprompt, "do not resolve to anything")
    assert says(exc.value.reprompt, "nope/missing.py")


def test_unknown_metric_reprompts_with_the_catalogue(repo):
    with pytest.raises(proposal_mod.ProposalInvalid) as exc:
        proposal_mod.validate(
            _body(expected_effect={"metric": "vibes", "direction": "up"}), repo)
    assert exc.value.gate == "P2"
    assert says(exc.value.reprompt, "funnel_conversion")


def test_one_sided_tradeoff_reprompts_with_the_missing_axes(repo):
    with pytest.raises(proposal_mod.ProposalInvalid) as exc:
        proposal_mod.validate(
            _body(tradeoff="Simplifies the code and deletes a module."), repo)
    assert exc.value.gate == "P3"
    assert says(exc.value.reprompt, "withdraw")


def test_reused_evidence_prompts_for_a_new_measurement(repo):
    records.save_proposal(records.Proposal(
        id="P-old", subsystem="test", agent="a", role="pit", session="s",
        tier="AMBER", created=records.now(),
        body={"evidence_refs": ["evidence.md", "metric:funnel_conversion"]},
    ), repo=repo)
    with pytest.raises(proposal_mod.ProposalInvalid) as exc:
        proposal_mod.validate(_body(), repo)
    assert says(exc.value.reprompt, "write a new test instead")
    assert says(exc.value.reprompt, "tools/evidence")


def test_a_fresh_script_rescues_reused_evidence(repo):
    records.save_proposal(records.Proposal(
        id="P-old", subsystem="test", agent="a", role="pit", session="s",
        tier="AMBER", created=records.now(),
        body={"evidence_refs": ["evidence.md", "metric:funnel_conversion"]},
    ), repo=repo)
    (repo / "probe.py").write_text("print(1)\n", encoding="utf-8")
    proposal_mod.validate(
        _body(evidence_refs=["evidence.md", "metric:funnel_conversion",
                             "script:probe.py"]),
        repo,
    )


# ══════════════════════════════════════════════════════════════ triggers

def test_one_red_attempt_fires_immediately(repo):
    records.record_attempt(records.Attempt(
        ts=records.now(), session="s1", agent="qa", role="pit", tier="RED",
        attempt=1, files=1, net_loc=5, paths=["control/x.py"],
    ), repo)
    found = triggers_mod.attempt_triggers(repo)
    assert [t.id for t in found] == ["MT1"]
    assert found[0].urgent


def test_three_ambers_fire_even_when_later_reduced_to_green(repo):
    for index in range(3):
        records.record_attempt(records.Attempt(
            ts=records.now(), session=f"s{index}", agent="qa", role="pit",
            tier="AMBER", attempt=1, files=4, net_loc=200,
            paths=[f"core/{index}.py"], outcome="merged",
        ), repo)
    found = triggers_mod.attempt_triggers(repo)
    assert [t.id for t in found] == ["MT2"]
    assert found[0].evidence["reduced_to_green"] == 3


def test_two_ambers_do_not_fire(repo):
    for index in range(2):
        records.record_attempt(records.Attempt(
            ts=records.now(), session=f"s{index}", agent="qa", role="pit",
            tier="AMBER", attempt=1, files=4, net_loc=200, paths=["core/x.py"],
        ), repo)
    assert triggers_mod.attempt_triggers(repo) == []


def test_metric_breaching_for_three_windows_fires(repo):
    history = repo / "lifeblood" / "state" / "funnel_conversion.jsonl"
    with open(history, "w", encoding="utf-8") as handle:
        for value in (85, 70, 65, 60):
            handle.write(json.dumps({
                "id": "funnel_conversion", "ts": records.now(),
                "status": "ok", "value": value, "unit": "percent",
            }) + "\n")
    found = triggers_mod.metric_triggers(repo)
    assert any(t.id == "MT3" for t in found)


def test_healthy_metric_does_not_fire(repo):
    history = repo / "lifeblood" / "state" / "funnel_conversion.jsonl"
    with open(history, "w", encoding="utf-8") as handle:
        for value in (85, 88, 90):
            handle.write(json.dumps({
                "id": "funnel_conversion", "ts": records.now(),
                "status": "ok", "value": value, "unit": "percent",
            }) + "\n")
    assert triggers_mod.metric_triggers(repo) == []


def test_triggers_are_durable_until_a_cycle_clears_them(repo):
    records.set_trigger("MT2", "three ambers", {}, repo)
    assert "MT2" in records.load_triggers(repo)["set"]
    records.clear_triggers(["MT2"], "M-test", repo)
    state = records.load_triggers(repo)
    assert "MT2" not in state["set"]
    assert state["history"][0]["cleared_by"] == "M-test"


# ══════════════════════════════════════════════════════════ master gates

def _docket_with_proposal(repo: Path) -> docket_mod.Docket:
    return docket_mod.Docket(
        cycle_id="M-test", subsystem="test", created=records.now(),
        triggers=[{"id": "MT1", "reason": "red"}],
        items=[docket_mod.DocketItem(
            id="P-1", kind="proposal", summary="a proposal",
            detail={"evidence_refs": ["python -m ops.check --json"],
                    "has_diff": True},
            age_days=30, paths=["core/diarization/assign.py"],
            signature="S1",
        )],
        remit=docket_mod.compose_remit([{"id": "MT1"}]),
    )


def _ev(*commands) -> AgentEvidence:
    """A finished run whose stream carried these shell commands."""
    return AgentEvidence(
        agent_id="bc-x", run_id="run-x", status="finished",
        tool_calls=[{"name": "shell", "args": {"command": c}} for c in commands],
    )


def test_hindsight_gate_requires_the_history_to_be_opened(repo):
    result = gates.hindsight(_ev(), _docket_with_proposal(repo), repo)
    assert not result.passed
    assert says(result.reprompt, "history query")
    assert says(result.reprompt, "contradict a direction previously taken")


def test_hindsight_gate_passes_once_queried(repo):
    evidence = _ev(
        "history query --path core/diarization --span 90d",
        "history query --signature S1 --span 180d",
    )
    assert gates.hindsight(evidence, _docket_with_proposal(repo), repo).passed


def test_independence_gate_blocks_rubber_stamping(repo):
    result = gates.independence(_ev("python -m ops.check --json"),
                                _docket_with_proposal(repo), repo)
    assert not result.passed
    assert says(result.reprompt, "could have come out the other way")


def test_independence_gate_still_wants_the_diff_read(repo):
    result = gates.independence(
        _ev("python -m ops.check --json", "python -m ops.other_probe --sample 20"),
        _docket_with_proposal(repo), repo)
    assert not result.passed
    assert says(result.reprompt, "you have not read what was actually built")


def test_independence_gate_passes_with_novel_work_and_the_diff_read(repo):
    evidence = _ev("python -m ops.other_probe --sample 20",
                   "history diff --proposal P-1")
    assert gates.independence(evidence, _docket_with_proposal(repo), repo).passed


def test_routine_orientation_is_not_independent_work(repo):
    """git status and ls are not evidence that anything was tested."""
    result = gates.independence(_ev("git status", "ls -la", "cat README.md"),
                                _docket_with_proposal(repo), repo)
    assert not result.passed


def test_disposition_gate_lists_what_is_missing(repo):
    result = gates.dispositions("M-test", _docket_with_proposal(repo), repo)
    assert not result.passed
    assert says(result.reprompt, "P-1")
    assert says(result.reprompt, "defer")


def test_defer_without_named_evidence_is_refused(repo):
    with pytest.raises(ValueError, match="name the evidence"):
        docket_mod.save_disposition("M-test", "P-1", "defer", repo=repo)
    docket_mod.save_disposition("M-test", "P-1", "defer",
                                needs="three consecutive publishes", repo=repo)
    assert gates.dispositions("M-test", _docket_with_proposal(repo), repo).passed


def test_every_plan_carries_its_own_proof(repo):
    good, bad = _body(), _body(tradeoff="Better.")
    result = gates.plan_proof([good, bad], repo)
    assert not result.passed
    assert result.detail["plan_index"] == 2
    assert gates.plan_proof([good], repo).passed


# ═══════════════════════════════════════════════════════════════ remits

def test_remit_depth_follows_the_trigger():
    assert docket_mod.compose_remit([{"id": "MT6"}])["depth"] == "light"
    assert docket_mod.compose_remit([{"id": "MT1"}])["depth"] == "deep"
    merged = docket_mod.compose_remit([{"id": "MT6"}, {"id": "MT4"}])
    assert merged["primary_trigger"] == "MT4"
    assert merged["also"]


def test_amber_remit_asks_for_the_common_cause():
    remit = docket_mod.compose_remit([{"id": "MT2"}])
    assert says(remit["instruction"], "wall is in the wrong place")


def test_trend_remit_asks_why_nothing_noticed():
    assert says(docket_mod.compose_remit([{"id": "MT3"}])["instruction"],
                "why did nothing notice")
