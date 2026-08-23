"""Generic acceptance checks usable by any repo's zones.

A zone earns the right to be merged unreviewed by declaring an oracle  - 
an acceptance test with a floor that proves the change does what it
claims. Domain oracles (does this harvest route actually produce a
verified op; did diarization recall hold on frozen fixtures) live in the
repos that own the domain.

The four here are domain-independent and work today:

    lifeblood_selftest   a changed metric still satisfies the contract
    agent_defs           a changed agent definition parses, does not leak
                         the rules to the agent it defines, and names
                         nothing that does not exist
    append_only          an append-only zone was only appended to
    condition_replay     a changed spawn condition does not fire
                         spuriously when replayed over real run records

Each is runnable as ``python -m harness.oracle_kit.<name>`` and exits
non-zero on failure, which is the whole contract an oracle has to meet.
"""
from __future__ import annotations

__all__ = ["lifeblood_selftest", "agent_defs", "append_only", "condition_replay"]
