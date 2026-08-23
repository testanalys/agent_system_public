---
name: example-route-yield
description: Investigate a route producing no verified sources
tier: pit
subsystem: example
signature: E-ROUTE-YIELD
metrics: route_yield, duplicate_rate
---
# Route yield

A harvest route stopped producing verified sources. Find out why, and fix
it if the fix belongs in the harvest layer.

## Your goal

1. Read the trigger. It names the route and the reading that crossed.
2. Reproduce the failure against the live source. Do not reason about it
   from the code alone.
3. Establish what changed: the source, the route, or the definition of a
   verified source.
4. If the fix is a working route, write it and push. One genuinely
   downloadable source is the bar.

## What is worth knowing

The evidence is the probe output, not the log summary. A route that reports
success and returns nothing downloadable has failed.

Prefer repairing an existing route over adding a parallel one. Two routes
against the same source is two things to keep working.

If the source is genuinely gone, say so and stop. Recording that a source
is dead is a result.
