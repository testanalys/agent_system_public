"""The authority model, and the oracle that guards it.

Who may retune which agent used to be implicit in which Python file a
tier could reach. It is now three declarations in the zone map, so it can
be tested directly - and this file is where the intent of FP016_V1 is
actually pinned down:

    a master engineers the agents that investigate its subsystem
    a master never edits the reviewer above it, or itself
    an agent never edits the file that defines it
    an agent nothing can wake is not allowed to exist

The last one is the quiet failure this feature would otherwise breed. A
master answering signal by creating an investigator is the whole point of
the change; a master creating an investigator nothing triggers looks
identical in the diff and does nothing forever.
"""
from __future__ import annotations

import json
import sys
from pathlib import Path

import pytest

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT))

from harness.classify import classify                            # noqa: E402
from harness.diff import FileChange, SessionDiff                 # noqa: E402
from harness.oracle_kit import agent_defs                        # noqa: E402
from harness.skeleton import Skeleton                            # noqa: E402

SKELETONS = ROOT / "skeletons"


def _definition(name="pipeline-example", tier="pit", metrics="alpha",
                body="# Example\n\n## Your goal\n\nDo the example thing.\n",
                description="An example agent for the tests"):
    lines = [f"name: {name}", f"description: {description}", f"tier: {tier}",
             "subsystem: pipeline"]
    if metrics:
        lines.append(f"metrics: {metrics}")
    return "---\n" + "\n".join(lines) + "\n---\n" + body


@pytest.fixture
def repo(tmp_path):
    """A repo with one spawn condition and a two-metric catalogue."""
    (tmp_path / "agents").mkdir()
    (tmp_path / "agents" / "agent_utils").mkdir(parents=True)
    (tmp_path / "agents" / "agent_utils" / "breaches.py").write_text(
        'CONDITIONS = [("pipeline-example", "alpha < 0.5")]\n', encoding="utf-8")
    (tmp_path / "lifeblood").mkdir()
    (tmp_path / "lifeblood" / "catalogue.json").write_text(json.dumps({
        "subsystem": "pipeline",
        "metrics": [{"id": "alpha"}, {"id": "beta"}],
    }), encoding="utf-8")
    return tmp_path


def _write(repo: Path, text: str, name: str = "pipeline-example.md") -> Path:
    path = repo / "agents" / name
    path.write_text(text, encoding="utf-8")
    return path


# ══════════════════════════════════════════════════════════════ the oracle

def test_a_well_formed_definition_passes(repo):
    assert agent_defs.check(_write(repo, _definition()), repo) == []


def test_an_agent_nothing_wakes_is_a_ghost(repo):
    path = _write(repo, _definition(name="pipeline-ghost"),
                  "pipeline-ghost.md")
    problems = agent_defs.check(path, repo)
    assert any("nothing wakes pipeline-ghost" in p for p in problems)


def test_a_master_or_retro_needs_no_spawn_condition(repo):
    """Masters are raised by the reconciler, retros by the run closing."""
    for tier in ("master", "retro"):
        path = _write(repo, _definition(name=f"pipeline-{tier}", tier=tier,
                                        metrics=""),
                      f"pipeline-{tier}.md")
        assert agent_defs.check(path, repo) == [], tier


def test_a_metric_outside_the_catalogue_is_rejected(repo):
    path = _write(repo, _definition(metrics="alpha, invented_metric"))
    problems = agent_defs.check(path, repo)
    assert any("invented_metric" in p for p in problems)


def test_a_definition_that_names_the_rules_is_rejected(repo):
    body = ("# Example\n\n## Your goal\n\nStay inside your zone and do not "
            "make a change that would be blocked.\n")
    problems = agent_defs.check(_write(repo, _definition(body=body)), repo)
    assert any("leaks" in p for p in problems)


def test_a_master_may_name_the_process_vocabulary(repo):
    body = ("# Example\n\n## Your goal\n\nDispose of the docket and write an "
            "engineering plan if the evidence supports one.\n")
    path = _write(repo, _definition(name="pipeline-master", tier="master",
                                    metrics="", body=body),
                  "pipeline-master.md")
    assert agent_defs.check(path, repo) == []


def test_a_definition_with_no_goal_is_rejected(repo):
    path = _write(repo, _definition(body="# Example\n\nSome prose.\n"))
    assert any("goal" in p for p in agent_defs.check(path, repo))


def test_an_agent_cannot_lint_its_own_definition_through(repo):
    path = _write(repo, _definition())
    problems = agent_defs.check(path, repo, changed_by="pipeline-example")
    assert any("own instructions" in p for p in problems)
    assert agent_defs.check(path, repo, changed_by="pipeline-master") == []


def test_an_unparseable_definition_reports_once(repo):
    path = _write(repo, "not a definition at all")
    problems = agent_defs.check(path, repo)
    assert len(problems) == 1
    assert "frontmatter" in problems[0]


def test_the_cli_exits_non_zero_on_a_bad_definition(repo, capsys):
    path = _write(repo, _definition(name="pipeline-ghost"),
                  "pipeline-ghost.md")
    code = agent_defs.main(["--changed", str(path), "--repo", str(repo)])
    assert code == 1
    assert "FAILED" in capsys.readouterr().out


def test_the_cli_ignores_a_change_with_no_definitions_in_it():
    assert agent_defs.main(["--changed", "core/thing.py"]) == 0


# ══════════════════════════════════════════════════════ the authority model

@pytest.mark.parametrize("subsystem", ["pipeline", "discovery"])
def test_the_zone_map_still_loads(subsystem):
    """A zone granting green with no oracle is a config error, not a warning."""
    Skeleton.load_file(SKELETONS / f"{subsystem}.json").lint()


def _verdict(subsystem: str, path: str, agent: str, added: int = 8) -> str:
    """How this subsystem grades one agent touching one file."""
    skeleton = Skeleton.load_file(SKELETONS / f"{subsystem}.json")
    diff = SessionDiff(
        repo=Path("."), base="base", head="head",
        files=[FileChange(path=path, status="M", added=added, deleted=0)],
    )
    return classify(diff, agent, skeleton).tier


@pytest.mark.parametrize("subsystem,agent,pit", [
    ("pipeline", "pipeline-master", "agents/pipeline-funnel-leak.md"),
    ("discovery", "discovery-master", "agents/discovery-route-decay.md"),
])
def test_a_master_can_engineer_the_investigating_agents(subsystem, agent, pit):
    assert _verdict(subsystem, pit, agent) == "GREEN"


@pytest.mark.parametrize("subsystem,agent", [
    ("pipeline", "pipeline-master"), ("discovery", "discovery-master"),
])
def test_a_master_cannot_rewrite_the_reviewer_or_itself(subsystem, agent):
    """RED is not 'forbidden' - it is 'the operator sees this one'."""
    for name in (f"agents/{subsystem}-master.md",
                 f"agents/{subsystem}-retro.md"):
        assert _verdict(subsystem, name, agent) == "RED", name


def test_a_pit_agent_cannot_edit_its_own_definition():
    """deny_self, which had no path to fire on until the definitions moved."""
    own = "agents/pipeline-funnel-leak.md"
    assert _verdict("pipeline", own, "pipeline-funnel-leak") != "GREEN"


def test_a_pit_agent_cannot_edit_a_sibling_definition():
    sibling = "agents/pipeline-diarization-qa.md"
    assert _verdict("pipeline", sibling, "pipeline-funnel-leak") != "GREEN"


def test_a_retro_proposes_agent_changes_rather_than_making_them():
    """Retro is not in green_for, so its edit escalates to a master."""
    assert _verdict("pipeline", "agents/pipeline-funnel-leak.md",
                    "pipeline-retro") != "GREEN"


def test_a_master_can_wire_up_what_wakes_a_new_agent():
    """Otherwise every new agent it creates would be a ghost by construction."""
    assert _verdict("pipeline", "agents/agent_utils/breaches.py", "pipeline-master") == "GREEN"
    assert _verdict("discovery", "scripts/breaches.py",
                    "discovery-master") == "GREEN"


def test_the_metric_orientation_is_a_data_attribute_a_master_can_tune():
    assert _verdict("pipeline", "agents/_orientation.md",
                    "pipeline-master") == "GREEN"


def test_the_zone_map_itself_is_still_out_of_reach():
    """skeleton.json stays categorical. Metric instruments are a caution plate
    for masters and retros, still 100 for PIT."""
    assert _verdict("pipeline", "skeleton.json", "pipeline-master") == "RED"
    for path in ("lifeblood/catalogue.json", "lifeblood/utils/contract.py"):
        assert _verdict("pipeline", path, "pipeline-master") == "GREEN", path
        assert _verdict("pipeline", path, "pipeline-diarization-qa") == "RED", path
