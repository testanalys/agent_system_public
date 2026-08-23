"""What the agents have actually been doing, as rows you can read.

The operating system already keeps three durable records: the proposal
ledger, the run archive, and per-repo `agent_state/`. All three are
answers to "what happened in this run". None of them answers the
operator's question, which is *what has been happening* - which agents
woke, what they claimed they would do, whether it worked, and what is
sitting waiting for a human right now.

So this is one append-only timeline per subsystem, one row per agent
turn, deliberately flat and deliberately boring. Rows are written by the
harness at the points where something is decided, never by an agent, and
writing is best-effort: a dashboard that can break a merge is worse than
no dashboard.

    dashboard/
        timeline.py   the row, and how one gets written
        render.py     read the rows back and print them

The row schema is fixed in `timeline.Row` before there is much to put in
it, on purpose. Two of its fields - the surface a master said it would
touch, and the surface it actually touched - are not populated yet. They
are declared now so that by the time the master loop can fill them in,
there is already history to compare against.
"""
from __future__ import annotations

from .timeline import (Row, decided, phase, proposal_headline, read, record,
                       routed, spawned, wagered)
from . import validate

__all__ = ["Row", "record", "read", "spawned", "decided", "wagered",
           "phase", "routed", "proposal_headline", "validate"]
