# Example: benchmark output

This shows the shape of a `benchmark` + `report` comparison: one goal run through several
strategies (configured clients), with a source-honest cost/latency/outcome table. It doubles as a
demo of Marshal's cost honesty — a strategy whose cost is unknown is reported as `unavailable`, not
as `$0`, and `cheapest` ranks **only** strategies with a known cost.

> The numbers below are an illustrative capture. Regenerate with a real run and paste the actual
> values before relying on them.

**Goal:** "Add input validation to the config loader and a test for it."

| Strategy        | Backend   | Status | Cost     | Source       | Duration | Tokens (in / out) |
|-----------------|-----------|--------|----------|--------------|----------|-------------------|
| implementer     | opencode  | ok     | $0.0123  | native       | 41.2 s   | 18,400 / 2,100    |
| refactorer      | codex     | ok     | —        | unavailable  | 53.8 s   | 21,900 / 3,050    |

```
cheapest: implementer (opencode)  $0.0123   [codex not ranked: cost unavailable]
fastest:  implementer (opencode)  41.2 s
```

What this demonstrates:

- **Measured, not guessed.** Cost, latency, and token counts come from each run's recorded facts.
- **Honest sourcing.** The codex strategy completed successfully, but its model is not in the price
  table, so its cost is `unavailable` and it is excluded from the `cheapest` ranking rather than
  being assigned a misleading `$0`.
- **Same goal, fair comparison.** Every strategy ran the identical goal in its own isolated
  worktree.

The underlying shape is a `BenchmarkResult` with `strategies: [StrategyResult{client, backend,
model, status, cost_usd, source, duration_ms, input_tokens, output_tokens}]` plus derived
`cheapest` and `fastest` labels (see `src/marshal_engine/fleet.py`).
