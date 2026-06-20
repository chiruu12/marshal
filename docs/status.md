# Implementation status & roadmap

A snapshot of what's built, what's verified, and what's next. For the architecture see
[`design.md`](design.md); for how to use it see [`usage.md`](usage.md).

## What's built

The full vertical slice is in place — driver → MCP → service → fleet → backends:

| Module | Responsibility | State |
|--------|----------------|-------|
| `types.py` | Shared dataclasses + enums | done |
| `backends/base.py` | Abstract backend + safe `run()` (no-stdin, hard timeout) | done |
| `backends/{cursor,opencode,codex,antigravity}.py` | Four adapters off one base class | done |
| `worktree.py` | Git worktree lifecycle (isolation boundary) | done |
| `usage.py` | Per-provider usage (events.jsonl + summary + cost-per-outcome) | done |
| `pricing.py` | Token → cost price table (the `ESTIMATED` path) | done |
| `state.py` | Persistent fleet state (one `runs/<run_id>.json` per run) | done |
| `fleet.py` | Orchestrator: worktree → run → price → record → persist | done |
| `registry.py` | Construct backends by name | done |
| `config.py` | `fleet.config.yaml` → clients, Fireworks guard | done |
| `service.py` | Testable core the MCP/CLI call into | done |
| `cli.py` | `marshal backends/usage/status/mcp` | done |
| `mcp_server.py` | 10-tool MCP surface over stdio (run/run_many/benchmark/report/collect/integrate/…) | done |

Quality gate: 107 unit tests pass; ruff and mypy (strict) clean across all source files.

## Backend verification matrix

| Backend | Installed | Read-only | Safe-edit (worktree write) | Native usage |
|---------|-----------|-----------|----------------------------|--------------|
| OpenCode | yes | verified | verified | verified (tokens + cost) |
| Cursor | yes | verified | verified | n/a by design (Admin API only) |
| Codex | yes | blocked* | blocked* | best-effort |
| Antigravity | yes | verified (reply) | not yet** | none |

\* Codex account is usage-limited until ~2026-07-18; only the failure path is verified.
\*\* Antigravity headless writes divert to `~/.gemini/antigravity-cli/scratch` under an
untrusted workspace (`--add-dir` does not fix it). Needs a PTY / workspace-trust workaround.

## Roadmap

Reordered 2026-06-20 (see [`decisions.md`](decisions.md)) to lead with the differentiator.
`collect_run`/`integrate` are shipped.

### Phase 1 — cost-proof (shipped)
Per-provider cost is now trustworthy and honest, single-threaded. Shipped (see
[`plans/phase1-cost-proof.md`](plans/phase1-cost-proof.md)): `duration_ms` on every run;
`extract_usage` wired into `Fleet.run`; a YAML price table (`pricing.py` + `data/prices.yaml`) with
`ESTIMATED` tagging and `unpriced`-not-`$0` honesty; cost/duration/source persisted on `RunRecord`;
cost-per-outcome (`$/run`, `$/succeeded`) + a native/estimated split in `usage`; partial-usage
recovery on timeout; and `RunStatus.EMPTY` for clean-but-no-work runs. The engine stamps facts to an
immutable ledger; the report layer derives interpretation on read.

### Phase 2 — solidify (shipped)
Done: the 5 known `collect_run`/`integrate` bugs (quoted-path handling via `-z`; dirty-main integrate
now returns a structured `blocked` status; conflict/blocked retries re-merge instead of reporting
"empty"; detached-HEAD refused; `commit_all --no-verify` pinned by a hook test); a git-spawn timeout
+ stdin guard + `GIT_TERMINAL_PROMPT=0` on `WorktreeManager._git`; globally-unique `run_id`; a run
loop that terminal-stamps `failed` on any exception (no zombie RUNNING); and **process-group kill on
timeout** (`os.killpg`), completing invariant #1 (no orphaned agent grandchildren).

### Phase 3 — parallel + measured benchmark (shipped)
**Per-run state files** (`runs/<run_id>.json`, single writer each, aggregates derived on read) and
**usage derived on read** (append-only `events.jsonl`) for concurrency safety; **capped parallel
`run_many`** via `ThreadPoolExecutor` behind a swappable Fleet API (worktree-create lock + Cursor
launch stagger + per-job failure isolation); and the **measured savings benchmark** — `benchmark`
runs one goal through N clients (strategies) and `report` derives a source-honest cost/latency/
outcome comparison from the ledger ("cheapest" only ranks strategies with a known cost). All
exposed over the service and MCP. This completes the V1 core (engine + cost + benchmark).

### Phase 4 — coverage & productization
Optional non-blocking spawn/poll; the Skills layer (`.claude/skills/marshal-*` driver playbooks);
Antigravity PTY/workspace-trust; Cursor admin-API usage; Codex live re-verify; a Gemini backend;
PyPI publish; and eventually **Chauffeur** (see [`chauffeur-future.md`](chauffeur-future.md)).
