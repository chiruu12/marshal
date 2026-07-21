# Marshal

[![CI](https://github.com/chiruu12/marshal/actions/workflows/ci.yml/badge.svg)](https://github.com/chiruu12/marshal/actions/workflows/ci.yml)
[![License: MIT](https://img.shields.io/badge/License-MIT-blue.svg)](LICENSE)
[![Python 3.11+](https://img.shields.io/badge/python-3.11%2B-blue.svg)](pyproject.toml)

**The control plane for AI coding agents.** Keep your best reasoning model focused on planning and
review, route execution to cheaper or specialized workers, isolate each task in its own git
worktree, and measure what every routing strategy actually cost.

One driver agent (e.g. Claude Code) plans the work. Marshal spawns and manages a fleet of
*headless* coding agents - **Cursor CLI, OpenCode, Codex, Command Code, and Claude Code** today (plus
an experimental **Google Antigravity** adapter), more behind a single base class - each running
autonomously in its own isolated git worktree, in parallel. Marshal monitors
them, collects their diffs, tracks per-provider usage, and hands results back for integration.

It plugs into your driver two ways:

- **MCP server** - you declare N backend "clients"; the driver calls a lean tool surface (see
  [`docs/mcp-tools.md`](docs/mcp-tools.md) for the full reference). One server can target several
  repos at once - every tool takes an
  optional `workspace`, repos are registered in `~/.marshal/workspaces.yaml` (or `marshal workspace
  add`), and new ones show up without a reconnect (see [SETUP.md](SETUP.md)).
- **Skills** - orchestration playbooks that teach the driver *what* Marshal can do and *how* to run
  a fleet: `marshal-orchestrate` (decompose → spawn → monitor → collect → integrate),
  `marshal-benchmark` (compare routing strategies on a real task), `marshal-workflow` (author
  and run a declarative recipe), `marshal-review-gate` (gate a merge behind independent reviewer
  consensus), and `marshal-plan-consensus` (converge on an approach before building).

> **Alpha (0.0.1) · pre-1.0, APIs may change.** The engine, CLI, and MCP server work end to
> end: parallel fan-out (`run_many`), non-blocking `spawn` + `cancel_run`, merge-back (`collect_run` +
> `integrate`), **declarative YAML workflows**, **multi-workspace** (one server, many repos), and a
> **measured savings benchmark** (`benchmark`/`report`). OpenCode, Cursor, Claude Code, and Command
> Code are live-verified; Codex too (its **real** per-run cost read back from the provider usage API
> where available). Cost is always tagged by `source` and never faked. See
> [`docs/status.md`](docs/status.md).

## Getting started

**Prerequisites:** Python ≥ 3.11, [uv](https://docs.astral.sh/uv/), git, and the CLI for each
backend you'll use (`opencode` / `cursor-agent` / `codex` / `command-code` / `claude` / `agy`) - each authenticated
via its own login. Marshal does **not** install the backend CLIs.

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
have open - so you still need `uv`, a `fleet.config.yaml` in that project, and the backend CLIs
authenticated (`uv run marshal doctor` checks all of this). Until you add a config, the server
starts with zero clients and tells you how to configure one.

Prefer to copy just the Skills? The driver Skills live in [`skills/`](skills/) - copy the
`skills/marshal-*` directories you want into your driver's skills directory (e.g. `.claude/skills/`)
and wire the MCP server by hand per **[`SETUP.md`](SETUP.md)**.

## Why Marshal

- **One base class, many backends.** Cursor, OpenCode, Codex, Command Code, Gemini - adding one is a new adapter,
  not a rewrite. Backend choice is a per-call parameter.
- **Parallel by default.** Each agent runs in its own git worktree; your main branch stays clean
  until you explicitly integrate.
- **Per-provider usage tracking.** Token accounting for every backend, plus cost tagged by source:
  **native** where the provider reports it (OpenCode, Claude Code), real **`admin-api`** cost for
  Codex routed through EastRouter (read back from its usage API), **estimated** where a model is
  priced, and `unavailable` otherwise (Cursor, Antigravity, Command Code, and OpenCode on an unpriced
  custom provider) - never a fake $0. A `usage`
  command most orchestrators don't have - per-backend/client/model/`backend×model` tables with
  input/output/cache-read token columns and a native/admin-api/estimated cost split, time-windowed
  via `--window day|week|month|all` (CLI) or `window: session|week|month|all` (MCP - `session`
  means "since the server started"). `marshal doctor` also reports each authenticated backend's
  plan tier where the CLI honestly exposes it (e.g. Cursor's subscription tier + current model).
- **Robust headless execution.** Hard timeouts, no-stdin-deadlock guarantees, and per-backend
  defenses for the real-world hangs and quirks documented in `docs/design.md`.

## What the benchmark gives you

Marshal's headline feature is a **measured** routing comparison, not a guess. Run one task through
several strategies and `report` derives a source-honest table from each run's recorded facts. A real
run - implementing a `TokenBucket` rate limiter (stdlib, with injectable-clock tests) across four clients:

| strategy | backend | status | cost | source | duration | in/out tokens |
|---|---|---|---|---|---|---|
| `deepseek` (opencode-go/deepseek-v4-flash) | opencode | succeeded | **$0.0029** | native | **81.8s** | 11.7K / 2.0K |
| `claude` (claude-sonnet-4-6) | claude-code | succeeded | $0.3374 | native | 121.4s | 17 / 6.8K |
| `cmdcode` (zai-org/GLM-5.2) | command-code | succeeded | `unavailable` | unavailable | 252.6s | 0 / 0 |
| `codex-glm` (z-ai/glm-5.1, via EastRouter) | codex | succeeded | `unavailable`\* | unavailable | 283.0s | 231K / 7.8K |

**cheapest:** deepseek ($0.0029) · **fastest:** deepseek (81.8s)

We then ran each produced solution's tests: `deepseek`, `claude`, and `cmdcode` all passed 6/6 - and
**`deepseek` did it cheapest, fastest, and correct, for ~1/115th of `claude`'s cost.** `codex-glm`
burned **231K input tokens** over-exploring a simple task, ran slowest, and shipped code that doesn't
even import. That's the point: **route on measured evidence, not vibes.**

Cost is tagged by **source** and never invented: a client whose cost can't be attributed shows
`unavailable` (not `$0`) and is excluded from the `cheapest` ranking. \*In that early run `codex-glm`'s
16-request, 283s EastRouter session fell past a single `/v1/usage` page, so the ledger honestly
recorded `unavailable` rather than guess. The reader now **paginates** `/v1/usage` to recover a long
run's real `admin-api` cost. (See [`examples/benchmark-output.md`](examples/benchmark-output.md).)

## Workflows

For orchestration you run more than once, write it down. A **workflow** is a declarative YAML recipe
(phases of fan-out, collect, and gated integrate) that Marshal runs by *sequencing the same safe
primitives* a driver would call by hand. It adds no new execution path: every run still flows
through the isolated-worktree fleet loop (timeout, process-group kill, usage ledger), and
**integration is gated off by default** so nothing touches your branch until you review it.

```yaml
# workflows/review.yaml
name: review
description: Review a target across two clients and surface diffs to merge.
inputs: [target]
phases:
  - name: review
    run: fan_out
    clients: [reviewer-a, reviewer-b]
    goal: "Review {target} for correctness bugs and missing tests; apply scoped fixes."
  - run: collect          # gather every candidate's diff (read-only)
  - run: integrate        # auto: false (default) → lists candidates; you merge the good ones
```

Validate recipes against your config with `marshal workflows`, then run one over MCP
(`run_workflow("review", {"target": "src/foo.py"})`). It returns the collected diffs plus a gated
`awaiting_review` status with concrete next-actions; the driver integrates the good runs one at a
time. The `marshal-workflow` Skill is the authoring + running playbook; templates live in
[`examples/workflows/`](examples/workflows/).

## Model catalog and duration presets

- **Model catalog** (`models:` in `fleet.config.yaml`) is a sheet the driver can read with
  `marshal models` (or the `list_models` MCP tool) - one row per `id` (provider/model), with
  `backends` it runs on and short free-form strings for `cost` / `quota_type` / `notes`. The
  catalog is pure data; it does NOT change routing (clients still own backend+model). Absent or
  empty = no catalog to expose; a malformed row raises `ConfigError` at load.
- **Duration presets** are per-spawn timeout overrides for `run_agent` / `spawn` / `run_many` (and
  `marshal run` / `marshal spawn` with `--duration`). Pass a preset name (`short`=300s,
  `medium`=1200s, `large`=6000s, `long`=24000s) or a positive integer of seconds. The override
  replaces the resolved `timeout_s` on the `RunRequest` for that one call; validation happens up
  front so a typo fails fast before any worktree is created.

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
                      ├─ Codex adapter
                      ├─ Command Code adapter  →  each runs headless in its own git worktree
                      ├─ Claude Code adapter
                      └─ Antigravity adapter
   │
   ▼
usage tracking (events.jsonl + summary)
```

Marshal is the **infrastructure layer**. A future end-user product, **Chauffeur**, will be built on
top of it. See `docs/chauffeur-future.md`.

## Documentation

- [`SETUP.md`](SETUP.md) - clone-to-first-run setup (prerequisites, install, auth, verify, wire in).
- [`docs/usage.md`](docs/usage.md) - configure a fleet and drive it via MCP, CLI, or library.
- [`docs/config.md`](docs/config.md) - every `fleet.config.yaml` key and `MARSHAL_*` env var.
- [`docs/mcp-tools.md`](docs/mcp-tools.md) - MCP tool reference (parameters and return shapes).
- [`docs/model-playbook.md`](docs/model-playbook.md) - which model/client to route a task to, by
  task weight (heavy/standard/light), with a copy-paste tiered fleet and cost-honesty notes.
- [`docs/status.md`](docs/status.md) - what's built, the backend verification matrix, and the roadmap.
- [`docs/design.md`](docs/design.md) - full architecture, per-backend cheat sheets, permission
  model, usage schema, and the edge-case hardening checklist.
- [`docs/sources.md`](docs/sources.md) - primary sources.

## Contributing & community

- [`CONTRIBUTING.md`](CONTRIBUTING.md) - dev setup, the quality gate, and how to add a backend.
- [`SECURITY.md`](SECURITY.md) - the security model and how to report a vulnerability privately.
- [`CHANGELOG.md`](CHANGELOG.md) · [`CODE_OF_CONDUCT.md`](CODE_OF_CONDUCT.md)

## License

MIT
