# Marshal

[![CI](https://github.com/chiruu12/marshal/actions/workflows/ci.yml/badge.svg)](https://github.com/chiruu12/marshal/actions/workflows/ci.yml)
[![License: MIT](https://img.shields.io/badge/License-MIT-blue.svg)](LICENSE)
[![Python 3.11+](https://img.shields.io/badge/python-3.11%2B-blue.svg)](pyproject.toml)

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

> **Status: V1 core complete · pre-1.0 (APIs may change).** The engine, CLI, and MCP server (11
> tools) work: merge-back (`collect_run` + `integrate`), per-provider cost tracking, capped parallel
> fan-out (`run_many`), non-blocking `spawn`, and a **measured savings benchmark**
> (`benchmark`/`report` — run one task through N strategies and compare real cost/latency/outcome).
> OpenCode and Cursor live-verified; the Codex adapter verified on a fresh usage window (re-verify
> pending). See [`docs/status.md`](docs/status.md).

## Getting started

**Prerequisites:** Python ≥ 3.11, [uv](https://docs.astral.sh/uv/), git, and the CLI for each
backend you'll use (`opencode` / `cursor-agent` / `codex` / `agy`) — each authenticated via its own
login. Marshal does **not** install the backend CLIs.

```bash
git clone https://github.com/chiruu12/marshal.git && cd marshal
uv sync --extra mcp --extra dev
cp fleet.config.example.yaml fleet.config.yaml   # then edit your clients
uv run marshal doctor                            # preflight: is everything ready to run?
```

Then wire `marshal mcp` into your driver and run a task. Full walkthrough: **[`SETUP.md`](SETUP.md)**.

### Install as a Claude Code plugin

Marshal ships as a Claude Code plugin that bundles both driver Skills **and** the MCP server in one
step. From Claude Code:

```
/plugin marketplace add chiruu12/marshal
/plugin install marshal@marshal
```

The plugin runs the MCP server from its own checkout (via `uv`), pointed at whatever project you
have open — so you still need `uv`, a `fleet.config.yaml` in that project, and the backend CLIs
authenticated (`uv run marshal doctor` checks all of this). Until you add a config, the server
starts with zero clients and tells you how to configure one.

Prefer to copy just the Skills? The two driver Skills live in [`skills/`](skills/) — copy
`skills/marshal-orchestrate` and `skills/marshal-benchmark` into your driver's skills directory
(e.g. `.claude/skills/`) and wire the MCP server by hand per **[`SETUP.md`](SETUP.md)**.

## Why Marshal

- **One base class, many backends.** Cursor, OpenCode, Codex, Gemini — adding one is a new adapter,
  not a rewrite. Backend choice is a per-call parameter.
- **Parallel by default.** Each agent runs in its own git worktree; your main branch stays clean
  until you explicitly integrate.
- **Per-provider usage tracking.** Token and cost accounting per backend, per client — a `usage`
  command most orchestrators don't have.
- **Robust headless execution.** Hard timeouts, no-stdin-deadlock guarantees, and per-backend
  defenses for the real-world hangs and quirks documented in `docs/design.md`.

## What the benchmark gives you

Marshal's headline feature is a **measured** routing comparison, not a guess. Run one task through
several strategies and `report` derives a source-honest table from each run's recorded facts. An
example capture — Marshal benchmarking two OpenCode models on a real drafting task:

| strategy | backend | status | cost | source | duration | out tokens |
|---|---|---|---|---|---|---|
| `kimi` (opencode-go/kimi-k2.6) | opencode | succeeded | **$0.0111** | native | **14.1s** | 377 |
| `glm` (opencode-go/glm-5.2) | opencode | succeeded | $0.0214 | native | 38.0s | 1561 |

**cheapest:** kimi ($0.0111) · **fastest:** kimi (14.1s)

Cost is tagged by **source** and never invented: a strategy whose provider reports no cost shows as
`unavailable` (not `$0`) and is excluded from the `cheapest` ranking. That honesty is the point — you
route on evidence, not vibes. (See [`examples/benchmark-output.md`](examples/benchmark-output.md).)

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

- [`SETUP.md`](SETUP.md) — clone-to-first-run setup (prerequisites, install, auth, verify, wire in).
- [`docs/usage.md`](docs/usage.md) — configure a fleet and drive it via MCP, CLI, or library.
- [`docs/status.md`](docs/status.md) — what's built, the backend verification matrix, and the roadmap.
- [`docs/design.md`](docs/design.md) — full architecture, per-backend cheat sheets, permission
  model, usage schema, and the edge-case hardening checklist.
- [`docs/sources.md`](docs/sources.md) — primary sources.

## Contributing & community

- [`CONTRIBUTING.md`](CONTRIBUTING.md) — dev setup, the quality gate, and how to add a backend.
- [`SECURITY.md`](SECURITY.md) — the security model and how to report a vulnerability privately.
- [`CHANGELOG.md`](CHANGELOG.md) · [`CODE_OF_CONDUCT.md`](CODE_OF_CONDUCT.md)

## License

MIT
