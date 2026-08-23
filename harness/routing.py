"""Merge, rebuild, or wait for the operator — one function.

The threshold is whether the change can be finished by code alone.
Categorical RED (schema, infra, secrets, control plane, supervisor
definitions) cannot: those need an operator. Everything else merges and
triggers a rebuild. `hold` is silence.
"""
from __future__ import annotations

from dataclasses import dataclass, field

from . import classify as classify_mod
from .wager import Wager

MERGE, OPERATOR, NONE = "merge", "operator", "none"


@dataclass(frozen=True)
class Route:
    action: str                      # merge | operator | none
    reason: str
    red_rules: tuple[str, ...] = ()
    notify: bool = False

    def as_dict(self) -> dict:
        return {
            "action": self.action,
            "reason": self.reason,
            "red_rules": list(self.red_rules),
            "notify": self.notify,
        }


def route(wager: Wager | None,
          classification: classify_mod.Classification | None,
          *, branch: str = "") -> Route:
    """Decide what happens to this master branch."""
    if wager is None:
        return Route(NONE, "no wager on the branch", notify=False)
    if wager.holding:
        return Route(NONE, f"hold: {wager.headline}", notify=True)

    if classification is None:
        return Route(OPERATOR,
                     "no classification available; operator must review",
                     notify=True)

    if classification.tier == classify_mod.RED:
        rules = tuple(sorted({
            str(s.get("id") or "")
            for s in classification.signals
            if s.get("id") and (
                int(s.get("points") or 0) >= 100
                or not any("points" in x for x in classification.signals)
            )
        }))
        named = ", ".join(rules) if rules else "protected surface"
        return Route(
            OPERATOR,
            (f"RED ({named}): cannot be finished by merge+rebuild alone. "
             f"Branch `{branch or '?'}` waits for the operator."),
            red_rules=rules,
            notify=True,
        )

    return Route(
        MERGE,
        (f"GREEN (score {getattr(classification, 'score', 0)}): "
         f"merge `{branch or '?'}` onto the production branch. "
         f"{wager.headline}"),
        notify=True,
    )
