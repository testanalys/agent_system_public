"""The closed loop: signals in, agents out, merges back into production.

    signals.py   the data points the running system produces on its own
    classes.py   the four agent classes, defined by what they are asked to produce
    wake.py      deterministic: which master starts, and what it is handed
    gate.py      the merge gate: classify, check, merge and rebuild, or record
    replay.py    how a change to the gate itself is judged

Nothing in this package calls a model. Every decision here is code, so
it can be tested, replayed and argued with.
"""
