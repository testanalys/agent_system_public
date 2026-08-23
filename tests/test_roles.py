"""Roles and prompt composition, now that definitions live in the repos.

The load-bearing test here is still `test_no_prompt_leaks_the_rules`.
Agents are deliberately not told that zones, tiers or limits exist,
because an agent told what it is permitted to change optimises for what
is permitted rather than for what is correct - and a held-back change
that was the *right* change is the most valuable artifact this system
makes.

That property used to be maintained by whoever hand-edited the prompt
functions in this package. Definitions are written by agents now, so the
same vocabulary list is enforced twice: here against every composed
prompt, and in production by the `agent_defs` oracle against every
change. One source of truth, so they cannot drift.

Tests that need a real subsystem checkout skip when there is not one -
the container has no sibling repos, and a test that silently passes on
an empty registry is worse than one that says it did not run.
"""
from __future__ import annotations

import os
import re
import sys
from pathlib import Path

import pytest

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT))

import roles                                                     # noqa: E402
from roles import RoleContext, load, render                      # noqa: E402
from roles import definition as defn                             # noqa: E402
from roles.base import Registry, Role                            # noqa: E402
from roles.definition import MASTER, PIT, RETRO   # noqa: E402

from harness.oracle_kit.leak_vocabulary import patterns_for      # noqa: E402

def _subsystem_repos() -> dict[str, Path]:
    """Which checkouts to assert the role rules against.

    Defaults to the bundled example so a fresh clone runs the whole suite
    with no setup. Point it at your own repositories with

        TURNSTILE_TEST_SUBSYSTEMS="discovery=../crawler,pipeline=../pipeline"

    and these tests will hold your real agent definitions to the same rules
    - including the one that matters most, that no prompt names a zone, a
    tier or a limit.
    """
    override = (os.getenv("TURNSTILE_TEST_SUBSYSTEMS") or "").strip()
    if not override:
        return {"example": ROOT / "examples" / "example-subsystem"}
    out: dict[str, Path] = {}
    for pair in override.split(","):
        name, _, path = pair.partition("=")
        if name.strip() and path.strip():
            out[name.strip()] = (ROOT / path.strip()).resolve()
    return out


SUBSYSTEM_REPOS = _subsystem_repos()


def _registry(subsystem: str) -> Registry:
    """The registry for one subsystem, or the only one declared.

    Tests that need *a* real registry name a subsystem for readability.
    When that exact name is not configured - the usual case in a fresh
    clone, where only the bundled example exists - fall back to whatever
    is declared rather than skipping, so the suite still asserts against
    real definitions.
    """
    if subsystem in SUBSYSTEM_REPOS:
        name, repo = subsystem, SUBSYSTEM_REPOS[subsystem]
    elif SUBSYSTEM_REPOS:
        name, repo = next(iter(SUBSYSTEM_REPOS.items()))
    else:
        pytest.skip("no subsystem checkouts declared")
    if not (repo / "agents").is_dir():
        pytest.skip(f"no {name} checkout at {repo}")
    return load(repo, repo_url=f"https://github.com/your-org/{name}",
                subsystem=name)


def _a_pit(registry: Registry) -> Role:
    """Any investigator in this registry. Named roles are not portable."""
    for role in registry.all():
        if role.tier == PIT:
            return role
    pytest.skip("registry declares no pit role")


def _all_roles() -> list[Role]:
    out: list[Role] = []
    for subsystem, repo in SUBSYSTEM_REPOS.items():
        if (repo / "agents").is_dir():
            out.extend(load(repo, subsystem=subsystem).all())
    return out


def _context() -> RoleContext:
    return RoleContext(
        trigger={"metric": "route_yield", "value": 0.38,
                 "target": 0.5, "dims": {"batch": "r-1"},
                 "detail": "recall below gate"},
        brief=[{"id": "route_yield", "last": 0.38, "value": 0.38,
                "mean_7d": 0.42, "mean_28d": 0.49, "n_7d": 4,
                "last_7": [{"label": "2026-08-01", "v": 0.40, "n": 1},
                           {"label": "2026-08-06", "v": 0.38, "n": 1}],
                "target": 0.5}],
        prior_findings=[{"date": "2026-07-19", "agent": "example-route-yield",
                         "summary": "attributed to short episodes",
                         "disposition": "rejected - duration uncorrelated"}],
        signature_history={"closures": [
            {"date": "2026-06-01", "what": "patched the caller",
             "outcome": "recurred"},
            {"date": "2026-07-01", "what": "patched it again",
             "outcome": "recurred"}]},
    )


# ══════════════════════════════════════════════════════════ the parser

VALID = """---
name: pipeline-example
description: An example agent for the tests
tier: pit
subsystem: pipeline
signature: P-EXAMPLE
metrics: alpha, beta
---
# Example

## Your goal

Do the example thing.
"""


def test_a_valid_definition_parses():
    item = defn.parse(VALID)
    assert item.name == "pipeline-example"
    assert item.tier == PIT
    assert item.metrics == ("alpha", "beta")
    assert item.capture == ("alpha", "beta")     # defaults to metrics
    assert item.body.startswith("# Example")


@pytest.mark.parametrize("text,expect", [
    ("no frontmatter at all", "no frontmatter"),
    ("---\nname: x\n", "never closed"),
    ("---\ndescription: d\ntier: pit\n---\nbody", "missing: name"),
    ("---\nname: x\ndescription: d\ntier: wizard\n---\nbody", "not one of"),
    ("---\nname: x\ndescription: d\ntier: pit\n---\n", "body is empty"),
    ("---\nname: x\ndescription: d\ntier: pit\nrepo: evil\n---\nb",
     "unknown field"),
    ("---\nname: x\ndescription: d\ntier: pit\nname: y\n---\nb", "set twice"),
    ("---\nname: x\ndescription: d\ntier: pit\n- item\n---\nb",
     "expected 'key: value'"),
])
def test_bad_definitions_are_rejected(text, expect):
    with pytest.raises(defn.DefinitionError, match=expect):
        defn.parse(text)


def test_a_definition_cannot_name_its_own_repo_or_model():
    """Both are reach, so neither is a field an agent can write.

    A definition that named its repo would aim the next spawn somewhere
    it was never granted; one that named its model would promote itself
    onto the expensive tier.
    """
    assert "repo" not in defn.KNOWN
    assert "model" not in defn.KNOWN


def test_the_filename_must_match_the_declared_name(tmp_path):
    path = tmp_path / "something-else.md"
    path.write_text(VALID, encoding="utf-8")
    with pytest.raises(defn.DefinitionError, match="filename"):
        defn.load_file(path)


def test_placeholders_fall_back_when_the_trigger_is_thin():
    body = "Investigating {dims.campaign|this campaign} and {dims.module}."
    assert defn.fill(body, {"campaign": "example-campaign"}).startswith(
        "Investigating example-campaign")
    assert "this campaign" in defn.fill(body, {})
    assert "module" in defn.fill(body, {})


def test_nothing_under_agents_is_ever_executed():
    """The one non-negotiable property of this format.

    Definitions are agent-writable and the loader runs inside the process
    that classifies agents and merges their branches. If any of this
    reached for import machinery, a definition would stop being a prompt
    and become code that runs before any classifier sees the diff.
    """
    for module in (defn, roles.loader):
        source = Path(module.__file__).read_text(encoding="utf-8")
        code = "\n".join(line for line in source.splitlines()
                         if not line.strip().startswith("#"))
        for forbidden in ("import_module", "exec(", "eval(", "__import__",
                          "yaml.load", "pickle"):
            assert forbidden not in code, f"{module.__name__} uses {forbidden}"


# ═══════════════════════════════════════════════════════════ the registry

def test_registry_rejects_duplicate_ids():
    registry = Registry()
    role = Role(id="x", tier=PIT, subsystem="s", summary="", repos=())
    registry.add(role)
    with pytest.raises(ValueError, match="duplicate"):
        registry.add(role)


def test_loading_a_repo_without_agents_is_empty_not_an_error(tmp_path):
    assert len(load(tmp_path)) == 0


def test_a_broken_definition_fails_the_whole_load(tmp_path):
    """Strict on purpose: a partial fleet nobody notices is the worse bug."""
    (tmp_path / "agents").mkdir()
    (tmp_path / "agents" / "good.md").write_text(
        VALID.replace("pipeline-example", "good"), encoding="utf-8")
    (tmp_path / "agents" / "bad.md").write_text("junk", encoding="utf-8")
    with pytest.raises(defn.DefinitionError):
        load(tmp_path)
    assert len(load(tmp_path, strict=False)) == 1


def test_underscore_files_are_not_roles(tmp_path):
    (tmp_path / "agents").mkdir()
    (tmp_path / "agents" / "_orientation.md").write_text(
        "## alpha\n\nRead it carefully.\n", encoding="utf-8")
    registry = load(tmp_path)
    assert len(registry) == 0
    assert registry.orientation == {"alpha": "Read it carefully."}


# ══════════════════════════════════════════════════ the real definitions

def test_every_role_composes_a_prompt():
    roles_found = _all_roles()
    if not roles_found:
        pytest.skip("no subsystem checkouts")
    for role in roles_found:
        text = render(role, _context())
        assert len(text) > 400, f"{role.id} produced a stub"


def test_master_prompt_alludes_to_scale_framework():
    for subsystem in sorted(SUBSYSTEM_REPOS):
        registry = _registry(subsystem)
        masters = [r for r in registry.all() if r.tier == MASTER]
        assert masters, subsystem
        text = render(masters[0], _context(), registry.orientation,
                      registry.scale_framework)
        assert "agents/_scale_framework.md" in text, subsystem
        assert "to-do list" in text.lower(), subsystem
        assert "rationale" in text.lower(), subsystem


@pytest.mark.parametrize("subsystem", sorted(SUBSYSTEM_REPOS))
def test_no_prompt_leaks_the_rules(subsystem):
    registry = _registry(subsystem)
    for role in registry.all():
        text = render(role, _context(), registry.orientation,
                      registry.scale_framework)
        for pattern, label in patterns_for(role.tier):
            match = re.search(pattern, text, re.I)
            assert match is None, (
                f"{role.id} leaks {label}: '{match.group(0)}'. Agents are told "
                f"the goal and the data, never the boundary.")


def test_every_subsystem_has_all_three_tiers():
    for subsystem in sorted(SUBSYSTEM_REPOS):
        registry = _registry(subsystem)
        tiers = {r.tier for r in registry.for_subsystem(subsystem)}
        assert tiers == {PIT, RETRO, MASTER}, subsystem


def test_prompts_carry_orientation_not_instructions():
    registry = _registry("pipeline")
    text = render(_a_pit(registry), _context(),
                  registry.orientation)
    assert "0.38" in text                       # the reading that fired it
    assert "mean_7d" in text or "0.42" in text  # window mean, not bare level
    assert "closed before" in text.lower()      # the recurrence history
    # Orientation is prose about how to read a metric. It travels with the
    # brief so no definition has to restate it.
    orientation = registry.orientation.get("route_yield", "")
    if orientation:
        assert orientation.split(".")[0][:40].strip() in text
    assert "attributed to short episodes" in text


def test_recurrence_history_forbids_a_third_same_level_fix():
    registry = _registry("pipeline")
    text = render(_a_pit(registry), _context())
    assert "A third of the same shape is not a plan" in text


def test_each_tier_is_told_how_to_finish():
    """The harness composes the ending, so no definition has to remember."""
    for subsystem in sorted(SUBSYSTEM_REPOS):
        for role in _registry(subsystem).all():
            text = render(role, _context(),
                          _registry(subsystem).orientation,
                          _registry(subsystem).scale_framework)
            if role.tier == MASTER:
                assert "wager" in text.lower(), role.id
                assert "disposition" in text.lower(), role.id
            else:
                assert "commit" in text.lower(), role.id
                assert "ledger notes" in text.lower(), role.id


def test_no_role_carries_the_operating_system_in_its_checkout():
    """The inverse of the property this repo used to assert.

    Masters were handed a checkout of the agent layer so that `history`
    was on their path, which put the one agent whose edits matter most
    in a position to read the classifier grading it. `history` ships as
    an installed command now, so every tier sees exactly one repo: its
    own subsystem.
    """
    for subsystem in sorted(SUBSYSTEM_REPOS):
        registry = _registry(subsystem)
        for role in registry.all():
            assert len(role.repos) == 1, role.id
            assert "agent_system_harness" not in role.repos[0], role.id
            assert "turnstile" not in role.repos[0], role.id


def test_the_harness_holds_no_subsystem_knowledge():
    """Nothing under roles/ names a metric, a domain or a failure mode."""
    domain_words = re.compile(
        r"diariz|harvest|campaign|funnel|proxy|acquisition|crawler|transcri",
        re.I)
    for path in (ROOT / "roles").glob("*.py"):
        text = path.read_text(encoding="utf-8")
        # Docstrings may name an example; code may not.
        code = re.sub(r'"""[\s\S]*?"""', "", text)
        match = domain_words.search(code)
        assert match is None, (
            f"{path.name} names {match.group(0)!r} in code. Subsystem "
            f"knowledge belongs in that subsystem's agents/ directory.")


# ═══════════════════════════════════════════════════════════ metric routing

def test_pit_brief_stays_focused():
    from roles import metrics as role_metrics
    registry = _registry("discovery")
    role = _a_pit(registry)
    ids = role_metrics.metrics_for_role(role, {"metric": role.metrics[0]})
    assert ids[0] == role.metrics[0]   # the metric that fired leads the brief
    assert len(ids) <= 2               # and the brief never becomes a dashboard


def test_a_docket_does_not_displace_the_primary_metric():
    """`escalations` is an event, not a measurement."""
    from roles import metrics as role_metrics
    role = Role(id="r", tier=RETRO, subsystem="s", summary="", repos=(),
                metrics=("alpha",))
    assert role_metrics.metrics_for_role(role, {"metric": "escalations"}) == [
        "alpha"]


def test_orientation_skips_metrics_with_no_guidance():
    from roles import metrics as role_metrics
    assert role_metrics.orientation_for(["alpha"], {}) == ""
    text = role_metrics.orientation_for(["alpha", "beta"],
                                        {"alpha": "Read it low."})
    assert "Read it low." in text
    assert "beta" not in text


# ═════════════════════════════════════════════════════════════ spawn safety

def test_spawn_env_carries_identity_and_no_secrets():
    """The harness names the agent; it does not hand out credentials."""
    for subsystem in sorted(SUBSYSTEM_REPOS):
        for role in _registry(subsystem).all():
            env = role.env_vars()
            assert env["TURNSTILE_AGENT_ID"] == role.id
            assert env["TURNSTILE_AGENT_ROLE"] == role.tier
            assert env["TURNSTILE_SUBSYSTEM"] == role.subsystem
            assert set(env) == {"TURNSTILE_AGENT_ID", "TURNSTILE_AGENT_ROLE",
                                "TURNSTILE_SUBSYSTEM"}, (
                f"{role.id} ships extra env: {sorted(set(env))}")


def test_no_role_ships_a_credential_in_its_spawn_env():
    for subsystem in sorted(SUBSYSTEM_REPOS):
        for role in _registry(subsystem).all():
            for key in role.env_vars():
                for banned in ("CREDENTIAL", "SECRET", "TOKEN", "KEY"):
                    assert banned not in key.upper(), f"{role.id} ships {key}"


def test_process_vocabulary_is_master_only():
    """A master names dispositions and wagers; an investigator must not."""
    from harness.oracle_kit.leak_vocabulary import (INVESTIGATOR_LEAKS,
                                                    LEAK_PATTERNS)
    assert patterns_for(MASTER) == LEAK_PATTERNS
    assert set(patterns_for(PIT)) > set(LEAK_PATTERNS)
    assert set(patterns_for(RETRO)) > set(LEAK_PATTERNS)
    labels = {label for _, label in INVESTIGATOR_LEAKS}
    assert "the proposal schema by name" in labels
