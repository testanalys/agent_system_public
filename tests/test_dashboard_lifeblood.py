"""Dashboard catalogue helpers for lifeblood tooltips."""
from __future__ import annotations

import sys
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT))

from dashboard.lifeblood import (  # noqa: E402
    campaign_target_rows,
    display_value,
    funnel_stage_rows,
    gcs_folders,
    load_catalogue,
    metric_entry,
    metric_help,
)


def test_gcs_folders_include_legacy_pipeline_prefix():
    assert gcs_folders("discovery") == ("audio-discovery",)
    assert "pipeline" in gcs_folders("pipeline")
    assert "pipeline-dispatch" in gcs_folders("pipeline")


def test_metric_help_uses_catalogue_intent():
    cat = load_catalogue("discovery")
    entry = metric_entry(cat, "session_yield")
    help_text = metric_help(entry, {"status": "ok", "ts": "2026-08-19T12:00:00+00:00"})
    assert "session" in help_text.lower() or "Ops produced" in help_text
    assert "Updated 2026-08-19" in help_text


def test_no_data_reason_is_in_tooltip():
    help_text = metric_help(
        {"id": "token_per_output", "intent": "tokens per op", "name": "Tokens per op"},
        {"status": "no_data", "reason": "CURSOR_API_KEY not found"},
    )
    assert "CURSOR_API_KEY" in help_text


def test_display_value_prefers_numeric_ok():
    assert display_value({"status": "ok", "value": 40}) == 40
    assert display_value({"status": "no_data", "value": None}) == "no_data"
    assert display_value({"status": "ok", "value": None, "dims": {"yield_only": True}}) == "yield_only"


def test_last_ok_skips_error_rows():
    from dashboard.lifeblood import last_ok_or_latest, staleness_note

    series = [
        {"status": "ok", "value": 10, "ts": "2026-08-16T00:00:00+00:00"},
        {"status": "error", "value": None, "ts": "2026-08-19T20:00:00+00:00",
         "reason": "ModuleNotFoundError: No module named 'db'"},
    ]
    show, latest = last_ok_or_latest(series)
    assert show["value"] == 10
    assert latest["status"] == "error"
    note = staleness_note(series, hours=1)
    assert "db" in note or "error" in note



def test_campaign_target_rows_flatten():
    rows = campaign_target_rows({
        "campaigns": {
            "example-campaign": {
                "example-harvester": {
                    "unique_ops": 3, "target_window": 2.0, "met": True,
                }
            }
        }
    })
    assert rows == [{
        "campaign": "example-campaign",
        "tool": "example-harvester",
        "unique_ops": 3,
        "target_window": 2.0,
        "met": True,
    }]


def test_funnel_stage_rows_include_meanings():
    rows = funnel_stage_rows({
        "by_stage": {
            "0": {"n": 10, "meaning": "In the corpus, not admitted to the pipeline"},
            "4": {"n": 2, "meaning": "Indexed (findable)"},
        }
    })
    by = {r["stage"]: r for r in rows}
    assert by["0"]["n"] == 10
    assert "not admitted" in by["0"]["meaning"]
    assert by["4"]["n"] == 2
