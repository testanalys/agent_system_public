"""The bridge between Cursor's agent harness and ours, driven by a fake.

A fake transport is the right tool here. The questions these tests answer
are all about *our* control flow - does a broad change get re-prompted
into the same conversation, does a blocked-but-silent agent get caught,
does a merge only happen when the acceptance check passed - and none of
them are questions about Cursor. The live behaviour that genuinely cannot
be faked is spiked separately, in `harness/spike_cursor.py`.
"""
from __future__ import annotations

import json
import subprocess
import sys
from pathlib import Path

import pytest

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))

from harness import agentloop                                  # noqa: E402
from harness.agentloop import (AgentEvidence, SpawnRequest,     # noqa: E402
                               adjudicate, checkout_branch,
                               collect_facts, resolve_origin_branch,
                               run_to_completion)
from harness.diff import SessionDiff                            # noqa: E402
from harness.skeleton import Skeleton                           # noqa: E402

SKELETON = {
    "subsystem": "demo", "protected_branches": ["main"],
    "limits": {"files": 3, "net_loc": 80, "gross_churn": 200,
               "new_files_in_zone": 1, "new_files_outside_zone": 0,
               "new_dependencies": 0, "new_config_keys": 0,
               "signature_changes": 0, "branch_tokens": 10,
               "nesting_depth": 1, "dir_population": 15,
               "filename_similarity": 0.8},
    "red": [{"id": "R1-control", "paths": ["control/**"],
             "reason": "control-plane logic"}],
    "zones": [{"id": "harvest", "paths": ["harvesters/**"],
               "green_for": ["discovery-yield"],
               "oracle": "python -c \"print('ok')\"",
               "limits": {"files": 2, "net_loc": 150, "new_files_in_zone": 1}}],
}


def _git(repo: Path, *args: str) -> str:
    return subprocess.run(["git", *args], cwd=repo, capture_output=True,
                          text=True, check=True).stdout.strip()


def _write(repo: Path, rel: str, text: str) -> None:
    path = repo / rel
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text(text, encoding="utf-8")


@pytest.fixture
def repo(tmp_path: Path) -> Path:
    root = tmp_path / "repo"
    root.mkdir()
    _git(root, "init", "-b", "main")
    _git(root, "config", "user.email", "t@t")
    _git(root, "config", "user.name", "t")
    _write(root, "skeleton.json", json.dumps(SKELETON))
    _write(root, "harvesters/alpha.py", "def harvest():\n    return []\n")
    _write(root, "core/service.py", "def serve():\n    return 1\n")
    _git(root, "add", "-A")
    _git(root, "commit", "-m", "base")
    return root


class FakeGit:
    """Branches already exist locally; merge is a real git merge."""

    def __init__(self, repo: Path):
        self.repo = repo
        self.fetched: list[str] = []
        self.merged: list[str] = []

    def fetch(self, branch: str) -> None:
        self.fetched.append(branch)

    def merge_to_base(self, branch: str, base: str) -> str:
        self.merged.append(branch)
        _git(self.repo, "checkout", base)
        _git(self.repo, "merge", "--no-ff", "-m", f"merge {branch}", branch)
        return base

    def collect_diff(self, base: str, branch: str) -> SessionDiff:
        return SessionDiff.collect(self.repo, base, branch)

    def files_under(self, branch: str, prefix: str) -> list[str]:
        return []


class FakeTransport:
    """Replays a scripted sequence of agent runs and records re-prompts."""

    def __init__(self, script: list[AgentEvidence]):
        self.script = list(script)
        self.sent: list[str] = []
        self.created: list[SpawnRequest] = []
        self._index = 0

    def create(self, request: SpawnRequest):
        self.created.append(request)
        return "bc-fake", self._next()

    def wait(self, handle):
        return handle

    def poll(self, agent_id: str, run_id: str):
        return self._pending.get(run_id)

    def send(self, agent_id: str, text: str):
        self.sent.append(text)
        return self._next()

    def _next(self):
        evidence = self.script[min(self._index, len(self.script) - 1)]
        self._index += 1
        return evidence


def _branch(repo: Path, name: str, files: dict[str, str]) -> str:
    _git(repo, "checkout", "-b", name, "main")
    for rel, text in files.items():
        _write(repo, rel, text)
    _git(repo, "add", "-A")
    _git(repo, "commit", "-m", f"work on {name}")
    _git(repo, "checkout", "main")
    return name


def _evidence(**kw) -> AgentEvidence:
    base = dict(agent_id="bc-fake", run_id="run-1", status="finished",
                result_text="done", branch=None,
                tool_calls=[{"name": "shell", "args": {"command": "pytest -q"}}])
    base.update(kw)
    return AgentEvidence(**base)


def _request(repo: Path, role="discovery-yield", **kw) -> SpawnRequest:
    base = dict(role=role, subsystem="demo", repo_url="file://x",
                starting_ref="main", prompt="do the thing")
    base.update(kw)
    return SpawnRequest(**base)


# ══════════════════════════════════════════════════════════ the happy path

def test_green_branch_is_merged(repo):
    _branch(repo, "agent/a", {"harvesters/beta.py": "def h():\n    return ['x']\n"})
    git = FakeGit(repo)
    decision = adjudicate(_evidence(branch="agent/a"), _request(repo), repo,
                          Skeleton.load(repo), git, attempt=1)
    assert decision.outcome == "MERGED"
    assert git.merged == ["agent/a"]
    assert "harvesters/beta.py" in _git(repo, "ls-tree", "-r", "--name-only", "main")


def test_merge_only_happens_after_checks_pass(repo):
    _branch(repo, "agent/a", {"harvesters/beta.py": "def h():\n    return ['x']\n"})
    git = FakeGit(repo)
    decision = adjudicate(
        _evidence(branch="agent/a"), _request(repo), repo,
        Skeleton.load(repo), git, attempt=1,
        run_checks=lambda result, diff: (False, "the acceptance check failed"),
    )
    assert decision.outcome == "REPROMPT"
    assert git.merged == []


# ═══════════════════════════════════════════════════ blocked and re-prompted

def test_broad_change_is_reprompted_into_the_same_conversation(repo):
    _branch(repo, "agent/wide", {
        "core/a.py": "x = 1\n", "core/b.py": "y = 2\n",
        "core/c.py": "z = 3\n", "core/d.py": "w = 4\n"})
    git = FakeGit(repo)
    transport = FakeTransport([_evidence(branch="agent/wide"),
                               _evidence(branch="agent/wide")])
    decision, history = run_to_completion(
        _request(repo), transport, repo, Skeleton.load(repo), git)

    assert len(transport.sent) == 1, "exactly one re-prompt before escalating"
    assert "proof-of-reasoning" in transport.sent[0].lower()
    assert "do not revert" in transport.sent[0].lower()
    assert decision.outcome == "ESCALATE"
    assert git.merged == []


def test_escalation_asks_for_the_argument_in_the_branch(repo):
    """The loop closes on a commit, not on a record written elsewhere.

    An escalated agent is asked for its reasoning as a file on the branch
    it already pushed, against the code it argues for. What the master
    later reads is that commit; the ledger entry only says the branch
    exists.
    """
    _branch(repo, "agent/wide", {"core/a.py": "x=1\n", "core/b.py": "y=2\n",
                                 "core/c.py": "z=3\n", "core/d.py": "w=4\n"})
    transport = FakeTransport([_evidence(branch="agent/wide"),
                               _evidence(branch="agent/wide")])
    decision, _ = run_to_completion(
        _request(repo), transport, repo, Skeleton.load(repo), FakeGit(repo))

    assert decision.outcome == "ESCALATE"
    assert agentloop.PROPOSAL_PATH in decision.reprompt
    for field in ("irreducible_because", "expected_effect", "tradeoff"):
        assert field in decision.reprompt


def test_reduced_second_attempt_merges(repo):
    _branch(repo, "agent/wide", {"core/a.py": "x=1\n", "core/b.py": "y=2\n",
                                 "core/c.py": "z=3\n", "core/d.py": "w=4\n"})
    _branch(repo, "agent/narrow", {"harvesters/beta.py": "def h():\n    return ['x']\n"})
    git = FakeGit(repo)
    transport = FakeTransport([_evidence(branch="agent/wide"),
                               _evidence(branch="agent/narrow")])
    decision, history = run_to_completion(
        _request(repo), transport, repo, Skeleton.load(repo), git)

    assert len(transport.sent) == 1
    assert decision.outcome == "MERGED"
    assert git.merged == ["agent/narrow"]


def test_control_plane_change_is_never_merged(repo):
    _branch(repo, "agent/red", {"control/reconciler.py": "def tick():\n    return 1\n"})
    git = FakeGit(repo)
    decision = adjudicate(_evidence(branch="agent/red"), _request(repo), repo,
                          Skeleton.load(repo), git, attempt=1)
    assert decision.outcome == "REPROMPT"
    assert decision.tier == "RED"
    assert decision.detail.get("kind") == agentloop.REPROMPT_PROPOSAL
    assert agentloop.PROPOSAL_PATH in decision.reprompt
    assert git.merged == []


def test_zone_is_agent_conditional_at_the_run_boundary(repo):
    _branch(repo, "agent/a", {"harvesters/beta.py": "def h():\n    return ['x']\n"})
    git = FakeGit(repo)
    theirs = adjudicate(_evidence(branch="agent/a"),
                        _request(repo, role="diarization-qa"), repo,
                        Skeleton.load(repo), git, attempt=1)
    assert theirs.outcome == "REPROMPT"
    assert git.merged == []


# ════════════════════════════════════════════════ the agent changed nothing

def test_no_diff_always_reprompts_for_a_ledger_note(repo):
    """No inference from prose - an empty diff is never terminal."""
    git = FakeGit(repo)
    decision = adjudicate(
        _evidence(branch=None,
                  result_text="I checked the batch; no code change warranted."),
        _request(repo), repo, Skeleton.load(repo), git, attempt=1)
    assert decision.outcome == "REPROMPT"
    assert "no git diff is showing" in decision.reprompt.lower()
    assert "ledger notes" in decision.reprompt.lower()


def test_no_diff_reprompt_does_not_guess_at_blockers(repo):
    """The same re-prompt regardless of what the agent's prose implies."""
    git = FakeGit(repo)
    concluded = adjudicate(
        _evidence(branch=None, result_text="nothing needed changing"),
        _request(repo), repo, Skeleton.load(repo), git, attempt=1)
    blocked = adjudicate(
        _evidence(branch=None, result_text="I was unable to read the bucket"),
        _request(repo), repo, Skeleton.load(repo), git, attempt=1)
    assert concluded.outcome == blocked.outcome == "REPROMPT"
    # The re-prompt quotes the agent's own closing words back, so the two
    # differ by that excerpt. The guidance after it must be identical -
    # that is what "no inference from prose" means.
    marker = "Nothing was committed"
    assert (concluded.reprompt[concluded.reprompt.index(marker):]
            == blocked.reprompt[blocked.reprompt.index(marker):])


def test_twice_empty_ends_incomplete_for_the_retro_to_handle(repo):
    git = FakeGit(repo)
    decision = adjudicate(_evidence(branch=None, result_text="still nothing"),
                          _request(repo), repo, Skeleton.load(repo), git,
                          attempt=3)
    assert decision.outcome == "INCOMPLETE"
    assert "retro" in decision.detail["reason"]


def test_no_tools_and_no_diff_still_just_reprompts(repo):
    """Even a do-nothing run gets asked for its ledger line first."""
    git = FakeGit(repo)
    decision = adjudicate(_evidence(branch=None, tool_calls=[], result_text="ok"),
                          _request(repo), repo, Skeleton.load(repo), git,
                          attempt=1)
    assert decision.outcome == "REPROMPT"


def test_crashed_run_is_a_failure(repo):
    git = FakeGit(repo)
    for status in ("error", "cancelled", "expired"):
        decision = adjudicate(_evidence(status=status, branch=None),
                              _request(repo), repo, Skeleton.load(repo), git,
                              attempt=1)
        assert decision.outcome == "FAILED", status


def test_error_with_pushed_branch_is_still_classified(repo):
    """Cursor ERROR + branch on origin must not skip the merge path."""
    _branch(repo, "agent/err-ok",
            {"harvesters/beta.py": "def h():\n    return ['x']\n"})
    git = FakeGit(repo)
    decision = adjudicate(
        _evidence(status="error", branch="agent/err-ok",
                  result_text="", error_code="worker_gone",
                  error_message="reconnect budget exhausted"),
        _request(repo), repo, Skeleton.load(repo), git, attempt=1)
    assert decision.outcome == "MERGED"
    assert git.merged == ["agent/err-ok"]


def test_error_with_missing_branch_is_incomplete(repo):
    git = FakeGit(repo)

    def boom(branch: str) -> None:
        raise RuntimeError(f"no such ref {branch}")

    git.fetch = boom  # type: ignore[method-assign]
    decision = adjudicate(
        _evidence(status="error", branch="cursor/phantom-branch",
                  error_message="stream died"),
        _request(repo), repo, Skeleton.load(repo), git, attempt=1)
    assert decision.outcome == "INCOMPLETE"
    assert "not on origin" in decision.detail["reason"]


# ═══════════════════════════════════════════════════════ evidence extraction

def test_shell_commands_are_recoverable_for_the_gates():
    evidence = _evidence(tool_calls=[
        {"name": "shell", "args": {"command": "history query --span 90d"}},
        {"name": "read_file", "args": {"path": "a.py"}},
        {"name": "shell", "args": {"cmd": "python -m ops.probe"}},
    ])
    assert evidence.shell_commands() == ["history query --span 90d",
                                         "python -m ops.probe"]
    assert evidence.tools_named() == {"shell", "read_file"}


# ═══════════════════════════════════════════════════ one execution, one agent

def test_the_loop_holds_the_conversation_for_the_whole_run(repo):
    """No handoff between attempts, so no window for a busy conversation.

    The re-prompt is sent by the same call that received the run it is
    responding to. There is no pending entry, nothing else polls this
    agent, and no second process can arrive mid-run - which is what
    `agent_busy` used to be.
    """
    _branch(repo, "agent/wide", {"core/a.py": "x=1\n", "core/b.py": "y=2\n",
                                 "core/c.py": "z=3\n", "core/d.py": "w=4\n"})
    _branch(repo, "agent/narrow",
            {"harvesters/beta.py": "def h():\n    return ['x']\n"})
    transport = FakeTransport([_evidence(branch="agent/wide"),
                               _evidence(branch="agent/narrow")])
    git = FakeGit(repo)
    decision, history = run_to_completion(
        _request(repo), transport, repo, Skeleton.load(repo), git)

    assert [row["attempt"] for row in history] == [1, 2]
    assert [row["outcome"] for row in history] == ["REPROMPT", "MERGED"]
    assert decision.outcome == "MERGED"
    assert git.merged == ["agent/narrow"]


def test_nothing_is_written_outside_the_repo_being_worked_on(repo):
    """No shared pending file, in this repo or anywhere else.

    The file this asserts the absence of is the one that made a discovery
    tick fetch a pipeline agent's branch, fail, and abort the tier.
    """
    _branch(repo, "agent/a", {"harvesters/beta.py": "def h():\n    return ['x']\n"})
    transport = FakeTransport([_evidence(branch="agent/a")])
    run_to_completion(_request(repo), transport, repo, Skeleton.load(repo),
                      FakeGit(repo))
    assert not (repo / "agent_state" / "pending_agents.json").exists()


def test_attempts_are_recorded_even_when_later_merged(repo):
    """The blocked attempt is the escalation signal; reducing must not erase it."""
    _branch(repo, "agent/wide", {"core/a.py": "x=1\n", "core/b.py": "y=2\n",
                                 "core/c.py": "z=3\n", "core/d.py": "w=4\n"})
    _branch(repo, "agent/narrow", {"harvesters/beta.py": "def h():\n    return ['x']\n"})
    transport = FakeTransport([_evidence(branch="agent/wide"),
                               _evidence(branch="agent/narrow")])
    run_to_completion(_request(repo), transport, repo, Skeleton.load(repo),
                      FakeGit(repo))

    from harness import records
    rows = records.load_attempts(repo)
    assert [r["tier"] for r in rows] == ["RED", "GREEN"]
    assert [r["outcome"] for r in rows] == ["blocked", "merged"]


def test_a_failing_fetch_ends_one_agent_and_nothing_else(repo):
    """A branch that cannot be fetched is this agent's problem alone."""
    class MissingRefGit(FakeGit):
        def fetch(self, branch: str) -> None:
            raise RuntimeError(
                f"git fetch origin {branch}:refs/agent/{branch} --force: "
                f"fatal: couldn't find remote ref {branch}")

    transport = FakeTransport([_evidence(branch="agent/ghost")])
    decision, history = run_to_completion(
        _request(repo), transport, repo,
        Skeleton.load(repo), MissingRefGit(repo))
    assert decision.outcome == "INCOMPLETE"
    assert "not on origin" in (decision.detail.get("reason") or "")
    assert len(history) == 1


THIS_MORNING = (
    "cd /workspace && git checkout -- state/ && git status --short && "
    "git checkout -b cursor/master-wager-senate-capacity-20260821 && "
    "git add agent_state/wager.json agent_state/dispositions.json"
)


class PresentGit:
    """Origin has some refs and not others."""

    def __init__(self, present: set[str]):
        self.present = set(present)
        self.fetched: list[str] = []

    def fetch(self, branch: str) -> None:
        self.fetched.append(branch)
        if branch not in self.present:
            raise RuntimeError(f"couldn't find remote ref {branch}")

    def collect_diff(self, base: str, branch: str) -> SessionDiff:
        return SessionDiff(repo=Path("."), base=base, head=branch, files=[])

    def merge_to_base(self, branch: str, base: str) -> str:
        return base


def test_checkout_branch_reads_last_checkout_b():
    assert checkout_branch([THIS_MORNING]) == (
        "cursor/master-wager-senate-capacity-20260821")
    assert checkout_branch(["git checkout -- state/"]) is None
    assert checkout_branch([
        "git checkout -b cursor/first",
        "git switch -c cursor/second",
    ]) == "cursor/second"


def test_resolve_uses_claimed_name_when_origin_has_it():
    git = PresentGit({"cursor/planned"})
    assert resolve_origin_branch(
        git, "cursor/planned", [THIS_MORNING]) == "cursor/planned"
    assert git.fetched == ["cursor/planned"]


def test_resolve_falls_back_to_checkout_when_claimed_is_missing():
    git = PresentGit({"cursor/master-wager-senate-capacity-20260821"})
    assert resolve_origin_branch(
        git, "cursor/escalated-docket-review-2b1f", [THIS_MORNING]
    ) == "cursor/master-wager-senate-capacity-20260821"
    assert git.fetched == [
        "cursor/escalated-docket-review-2b1f",
        "cursor/master-wager-senate-capacity-20260821",
    ]


def test_collect_facts_rewrites_branch_to_the_origin_ref(repo):
    evidence = _evidence(
        branch="cursor/escalated-docket-review-2b1f",
        tool_calls=[{"name": "shell", "args": {"command": THIS_MORNING}}],
    )
    facts = collect_facts(
        evidence, PresentGit({"cursor/master-wager-senate-capacity-20260821"}),
        "main")
    assert facts.on_origin
    assert facts.branch == "cursor/master-wager-senate-capacity-20260821"
    assert evidence.branch == facts.branch
