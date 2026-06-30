# Example: benchmark output

A real `benchmark` + `report` comparison: one goal run through several strategies (configured
clients), each in its own isolated git worktree, with a source-honest cost/latency table. It doubles
as a demo of Marshal's cost honesty — a client whose cost is unknown is reported as `unavailable`,
not `$0`, and `cheapest` ranks **only** clients with a known cost.

**Goal:** implement a `TokenBucket` rate limiter (stdlib-only, with injectable-clock pytest tests),
run across four clients.

| Strategy   | Backend       | Model                          | Status    | Cost          | Source       | Duration | Tokens (in / out) |
|------------|---------------|--------------------------------|-----------|---------------|--------------|----------|-------------------|
| deepseek   | opencode      | opencode-go/deepseek-v4-flash  | succeeded | **$0.0029**   | native       | 81.8 s   | 11,740 / 1,977    |
| claude     | claude-code   | claude-sonnet-4-6              | succeeded | $0.3374       | native       | 121.4 s  | 17 / 6,837        |
| cmdcode    | command-code  | zai-org/GLM-5.2                | succeeded | `unavailable` | unavailable  | 252.6 s  | 0 / 0             |
| codex-glm  | codex         | z-ai/glm-5.1 (via EastRouter)  | succeeded | `unavailable` | unavailable  | 283.0 s  | 231,075 / 7,812   |

```
cheapest: deepseek (opencode)  $0.0029   [cmdcode, codex-glm not ranked: cost unavailable]
fastest:  deepseek (opencode)  81.8 s
```

**Verified outcome (a separate step — `report` gives the table above):** we ran each produced
solution's tests. `deepseek`, `claude`, and `cmdcode` passed 6/6; `codex-glm`'s test file failed to
import (collection error). So the cheapest, fastest client was also a correct one — for ~1/115th of
`claude`'s cost.

What this demonstrates:

- **Measured, not guessed.** Cost, latency, and token counts come from each run's recorded facts.
- **Honest sourcing.** `cmdcode` (a hosted account that reports no tokens/cost) and `codex-glm` (whose
  long EastRouter run fell past a single `/v1/usage` page in this early run) both show `unavailable`
  and are excluded from `cheapest` — never a misleading `$0`. `codex-glm`'s real charge (~$0.16,
  recoverable from the provider's usage API) motivated the `/v1/usage` **pagination** the cost reader
  now does to recover a long run's real `admin-api` cost.
- **More reasoning ≠ better.** `codex-glm` spent **231K input tokens** over-exploring a one-class
  task, ran slowest, and still shipped broken code. Benchmark; don't assume.
- **Same goal, fair comparison.** Every strategy ran the identical goal in its own isolated worktree.

The underlying shape is a `BenchmarkResult` with `strategies: [StrategyResult{client, backend, model,
status, cost_usd, source, duration_ms, input_tokens, output_tokens}]` plus derived `cheapest` and
`fastest` labels (see `src/marshal_engine/fleet.py`).
