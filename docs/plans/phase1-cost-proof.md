# Phase 1 — Cost-proof (shipped)

> The differentiated milestone: make per-provider cost **trustworthy and honest**, single-threaded.
> Decided 2026-06-20 (product + engineering review); shipped the same day. See `../decisions.md`
> for the why. All 8 scope items below landed with tests; the gate stayed green per commit.

## Roadmap context

```
P1 cost-proof (this doc, single-threaded)  ->  P2 solidify  ->  P3 parallel + MEASURED benchmark
```

## Architecture invariant (locked)

Two-layer split. The **engine stamps facts** — tokens, cost, `duration_ms`, source — to an
immutable ledger (`usage/events.jsonl`). The **report layer derives interpretation**
(cost-per-outcome, savings) on read; nothing derived is ever stored. Estimated cost is priced at
run time (a price snapshot), so editing the price table never rewrites history. The engine stays
mechanism; interpretation and policy stay out of it.

## Scope (7 items)

1. `duration_ms` on `AgentResult`, timed in `base.run()` on **every** path (success / fail /
   timeout). Latency is a run property, not a usage property, so it survives `usage is None`.
2. Wire `backend.extract_usage(result)` into `Fleet.run` (today it reads `result.usage` directly;
   the seam is dead code).
3. `pricing.py` + a YAML price table (shipped default + user override) -> `ESTIMATED` cost for
   tokens-but-no-cost backends (Codex), stamped at run time. Token -> cost pricing lives in this
   one module, never in the backends (keeps adapters config-free).
4. Pricing honesty: a model with no price entry shows **"unpriced"**, never `$0.00`.
5. Persist `cost_usd` / `duration_ms` / `source` on `RunRecord`, written **once** from the same
   computed object as the usage event (no drift between `fleet.json` and `events.jsonl`).
6. Extend `usage` with cost-per-outcome (`$/run`, `$/succeeded`), source-honest. See **Success
   signal** below — `$/run` counts every terminal run; `$/succeeded` counts only real successes.
7. Partial-usage recovery on timeout: best-effort parse `exc.stdout`, keep `status=timed_out`;
   swallow recovery errors (a failed recovery must never mask the timeout).
8. **Success signal + `RunStatus.EMPTY`.** A run that exits 0 but did no work (empty final text
   AND no changed files) is not a success — it still burned tokens. `Fleet.run` computes this
   authoritatively (it has both the result and the worktree) and stamps `RunStatus.EMPTY`. `$/run`
   counts EMPTY; `$/succeeded` excludes it; `AgentResult.ok` stays `SUCCEEDED`-only. This pulls the
   Antigravity empty-success fix forward from P2 into P1.

## Honesty rules (must be tested)

- `estimated` is never shown as `native` (source tag).
- `unpriced` is never shown as free (`$0`).
- a timed-out run keeps `timed_out` even when partial usage is recovered.
- a no-op run (`EMPTY`) is never counted as a success in `$/succeeded`.

## Test plan (target: 100% of new paths)

Native cost untouched; `estimated = price x tokens`; missing model -> unpriced (no crash);
malformed/missing price file -> fail-safe + stderr warn; `duration_ms > 0` on success, fail, AND
timeout; timeout keeps status + recovers partial tokens; `extract_usage` called once; cost / source
survive a state reload; cost-per-outcome correct with mixed statuses.

## Deferred (NOT in Phase 1)

- **Savings-vs-baseline / `report` narrative -> P3**, done MEASURED via benchmarking (run the same
  task through multiple models, measure both real costs). A P1 hypothetical rests on an equal-token
  assumption that is usually false -> too speculative to present as proof.
- Cursor admin-API usage (externally gated: Team/Enterprise key).
- The clean per-backend "separate parse-usage from decide-status" refactor (TODO; item 7 is the
  best-effort version).

## Product note

Marshal is **self-installed** beside the user's own Claude Code, against their own subscriptions.
No per-client / multi-tenant logic; we ship the engine + setup guidance and users configure
`fleet.config.yaml` themselves. Any baseline/price setting is one plain value in *their* config.
