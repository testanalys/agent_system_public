# Scale direction

What the operator is optimising for, in order. A proposal that improves
something low on this list at the cost of something high on it is a
proposal to reject.

1. **No technical debt.** The biggest project killer.
2. **Simplicity.** Fewest components, simplest interdependencies.
3. **Cost.** Infrastructure and tokens both. A design is approved with an
   answer to: what does its cost scale with?
4. **Volume.** Throughput, within the constraints above.
5. **Quality.** Correctness and fidelity of the output.

A change that adds a component must say which of these it serves and what
it costs on the others.
