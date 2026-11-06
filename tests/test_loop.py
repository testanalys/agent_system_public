"""The closed loop: signals, wake rules, replay, and the scripted demo."""
from __future__ import annotations

import sys
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT))

from loop import replay, wake                                   # noqa: E402
from loop.signals import PRODUCT, Signal, SignalLog              # noqa: E402


def _issue(title, sev="high", tick=1, component="ingest"):
    return Signal("dq_issue", component, title, "review", sev, tick)


def test_fingerprint_ignores_word_order_and_case():
    assert _issue("Duplicate segments after retry").id == _issue("after retry duplicate SEGMENTS").id


def test_fixed_signal_reopens_when_raised_again():
    log = SignalLog()
    row = log.raise_(_issue("dupes"))
    log.close([row.id], "abc123")
    again = log.raise_(_issue("dupes", tick=2))
    assert again is row and row.status == "reopened" and row.occurrences == 2
    assert "abc123" in row.history[-1]


def test_dq_wakes_on_pressure_and_not_twice_on_same_evidence():
    log = SignalLog()
    for t in ("a", "b", "c"):
        log.raise_(_issue(t, "high"))
    first = wake.dq(log, None, tick=1)
    assert first.start
    assert not wake.dq(log, first.state, tick=1).start


def test_docket_rank_is_deterministic():
    log = SignalLog()
    log.raise_(_issue("low one", "low"))
    log.raise_(_issue("crit", "critical"))
    log.raise_(_issue("med", "medium"))
    assert [s.severity for s in wake.rank(log.live(PRODUCT))] == ["critical", "medium", "low"]


def test_replay_refuses_to_loosen_the_fence():
    rec = replay.Record("b", "repair-x", "RED", categorical=True, merged=False,
                        rescore=lambda sk: "GREEN")
    assert not replay.run([rec], proposed=None).passed


def test_replay_reports_false_positives_as_benefit():
    rec = replay.Record("b", "repair-x", "RED", categorical=False, merged=False,
                        rescore=lambda sk: "GREEN")
    report = replay.run([rec], proposed=None)
    assert report.passed and report.benefit == ["b"]


def test_demo_runs_end_to_end(capsys):
    sys.path.insert(0, str(ROOT / "demo"))
    import simulate
    assert simulate.main(["--no-color"]) == 0
    out = capsys.readouterr().out
    assert "The gate just improved itself" in out
    assert "REOPENED" in out
    assert "dedupe-2" in out and "MERGED" in out
