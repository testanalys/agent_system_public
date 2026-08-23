"""Canonical model ids per tier.

Two price bands. Advanced is for masters only. Cheap is an ordered list:
index 0 is the mid-cheap default (retros + usage-limit fallback from
advanced); the last entry is the cheapest (PIT).

PIT and retro agents do local, bounded work on the cheap band. Masters
adjudicate competing proposals and may implement structural work, so they
start on the advanced model and may fall back to cheap[0] when Cursor
rejects the spawn for usage limits.

This lives in the operating system rather than beside the agent
definitions on purpose. A definition that could name its own model would
let an agent promote itself onto the expensive tier.
"""
from __future__ import annotations

# Ordered: mid-cheap first, cheapest last. Retros take [0]; PIT takes [-1].
# IDs must match Cloud Agents GET /v1/models (not IDE-only slugs like
# cursor-grok-4.5-high). Effort is a separate API param; base id is enough.
CHEAP_TIER_MODELS: tuple[str, ...] = (
    "grok-4.5",
    "composer-2.5",
)

ADVANCED_MODEL = "kimi-k3"

PIT_MODEL = CHEAP_TIER_MODELS[-1]
RETRO_MODEL = CHEAP_TIER_MODELS[0]
MASTER_MODEL = ADVANCED_MODEL

# Primary fallback when an advanced-model spawn hits a usage hard limit.
USAGE_FALLBACK_MODEL = CHEAP_TIER_MODELS[0]

BY_TIER = {
    "pit": PIT_MODEL,
    "retro": RETRO_MODEL,
    "master": MASTER_MODEL,
}


def model_for_tier(tier: str) -> str:
    return BY_TIER.get(tier, PIT_MODEL)


def is_advanced_model(model: str | None) -> bool:
    return bool(model) and model == ADVANCED_MODEL


def usage_fallback_model(model: str | None) -> str | None:
    """Cheap[0] when ``model`` is advanced; otherwise no fallback."""
    if is_advanced_model(model):
        return USAGE_FALLBACK_MODEL
    return None
