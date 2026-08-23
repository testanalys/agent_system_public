"""The agent harness.

Authority derives from the properties of a diff, not from the identity of
the agent that wrote it or the reasoning it reports. Agents are given a
role, a goal, data and tools — and no knowledge of the rules. The rules
surface only when a change actually trips them, and when they do they
arrive as a re-prompt inside the agent's own session: not "this failed",
but "here is the reasoning step you have not done yet".

Public surface:

    classify.classify(diff, agent_id, skeleton)  -> GREEN | AMBER | RED
    proposal.validate(body, repo)                -> raises with a re-prompt
    triggers.evaluate(repo)                      -> master triggers
    gates.run_all(...)                           -> master gates
"""
from __future__ import annotations

__version__ = "1.0.0"

__all__ = [
    "agentloop", "catalogue", "classify", "cli", "diff", "docket", "gates",
    "history", "history_signals", "keywords", "ledger", "master_loop",
    "notify", "objectstore", "operator", "oracles", "paths", "proposal", "pulls",
    "rebuild", "records", "reprompts", "routing", "runlog", "scorecard",
    "skeleton", "successors", "triggers", "wager",
]
