# How to read the numbers

One entry per metric. An agent is shown the metric, its history, and this
note — never a threshold and never a rule.

## route_yield

Verified, downloadable sources produced per harvest session. Zero across
several sessions means the routes stopped resolving, not that there was
nothing to find. Compare against the 28-day figure before concluding the
source is exhausted.

## duplicate_rate

Share of discovered items already in the index. A rising rate is usually
healthy — it means coverage is catching up. A rate near 1.0 with a falling
`route_yield` means the route is returning the same page repeatedly.
