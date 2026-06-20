# Marshal

**The control plane for AI coding agents.** Keep your best reasoning model focused on planning and
review, route execution to cheaper or specialized workers, isolate each task in its own git
worktree, and measure what every routing strategy actually cost.

One driver agent (e.g. Claude Code) plans the work. Marshal spawns and manages a fleet of
*headless* coding agents — **Cursor CLI, OpenCode, Codex, and Google Antigravity** today, more
behind a single base class — each running autonomously in its own isolated git worktree, in parallel. Marshal monitors
them, collects their diffs, tracks per-provider usage, and hands results back for integration.

It plugs into your driver two ways:

- **MCP server** — you declare N backend "clients"; the driver calls a lean tool surface. Today:
  `list_clients`, `run_agent`, `run_many`, `spawn`, `benchmark`, `report`, `get_run`, `collect_run`,
  `integrate`, `status`, `usage`. Planned: `cancel_run`.
- **Skills** — orchestration playbooks that teach the driver *what* Marshal can do and *how* to run
  a fleet: `marshal-orchestrate` (decompose → spawn → monitor → collect → integrate) and
  `marshal-benchmark` (compare routing strategies on a real task).

> **Status: early development (private).** The V1 core works — engine, CLI, and MCP server (11
> tools): merge-back (`collect_run` + `integrate`), per-provider cost tracking, capped parallel
> fan-out (`run_many`), non-blocking `spawn`, and a **measured savings benchmark**
> (`benchmark`/`report` — run one task through N strategies and compare real cost/latency/outcome).
> OpenCode and Cursor live-verified. See [`docs/status.md`](docs/status.md).

## Why Marshal

- **One base class, many backends.** Cursor, OpenCode, Codex, Gemini — adding one is a new adapter,
  not a rewrite. Backend choice is a per-call parameter.
- **Parallel by default.** Each agent runs in its own git worktree; your main branch stays clean
  until you explicitly integrate.
- **Per-provider usage tracking.** Token and cost accounting per backend, per client — a `usage`
  command most orchestrators don't have.
- **Robust headless execution.** Hard timeouts, no-stdin-deadlock guarantees, and per-backend
  defenses for the real-world hangs and quirks documented in `docs/design.md`.

## Architecture

```
driver (Claude Code)
   │  plans + decides (via Skills)
   ▼
Marshal MCP server  ──  fleet state (file-based)
   │
   ▼
engine ── base class ─┬─ Cursor adapter
                      ├─ OpenCode adapter
                      ├─ Codex adapter        →  each runs headless in its own git worktree
                      └─ Antigravity adapter
   │
   ▼
usage tracking (events.jsonl + summary)
```

Marshal is the **infrastructure layer**. A future end-user product, **Chauffeur**, will be built on
top of it. See `docs/chauffeur-future.md`.

## Documentation

- [`docs/usage.md`](docs/usage.md) — configure a fleet and drive it via MCP, CLI, or library.
- [`docs/status.md`](docs/status.md) — what's built, the backend verification matrix, and the roadmap.
- [`docs/design.md`](docs/design.md) — full architecture, per-backend cheat sheets, permission
  model, usage schema, and the edge-case hardening checklist.
- [`docs/sources.md`](docs/sources.md) — primary sources.

## License

MIT
