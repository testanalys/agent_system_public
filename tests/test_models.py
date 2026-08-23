"""Tier model ids and usage-limit fallback selection."""
from __future__ import annotations

import sys
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))

from roles.models import (  # noqa: E402
    ADVANCED_MODEL,
    CHEAP_TIER_MODELS,
    MASTER_MODEL,
    PIT_MODEL,
    RETRO_MODEL,
    USAGE_FALLBACK_MODEL,
    is_advanced_model,
    model_for_tier,
    usage_fallback_model,
)


def test_cheap_tier_order_pit_and_retro():
    assert CHEAP_TIER_MODELS == ("grok-4.5", "composer-2.5")
    assert PIT_MODEL == "composer-2.5"
    assert RETRO_MODEL == "grok-4.5"
    assert model_for_tier("pit") == PIT_MODEL
    assert model_for_tier("retro") == RETRO_MODEL
    assert model_for_tier("master") == MASTER_MODEL == ADVANCED_MODEL


def test_usage_fallback_only_from_advanced():
    assert USAGE_FALLBACK_MODEL == CHEAP_TIER_MODELS[0]
    assert is_advanced_model(ADVANCED_MODEL)
    assert not is_advanced_model(PIT_MODEL)
    assert usage_fallback_model(ADVANCED_MODEL) == "grok-4.5"
    assert usage_fallback_model(PIT_MODEL) is None
    assert usage_fallback_model(RETRO_MODEL) is None
    assert usage_fallback_model(None) is None
