"""Scored classification of a git diff.

Routing is two answers: land it (GREEN) or don't (RED). Score 100 is one
categorical miss — a protected file, a PIT agent off its plate, a restore
inside the protection window. Everything else is a fraction of that, so
we can add rules over time and retune the threshold without inventing a
third routing colour.

AMBER, if it appears, is a dashboard band between 40 and 99. It does not
change whether a change merges.
"""
from __future__ import annotations

from .engine import AMBER_FLOOR, THRESHOLD, Scorecard, evaluate

__all__ = ["AMBER_FLOOR", "THRESHOLD", "Scorecard", "evaluate"]
