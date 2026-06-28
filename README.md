# Marshal

[![CI](https://github.com/chiruu12/marshal/actions/workflows/ci.yml/badge.svg)](https://github.com/chiruu12/marshal/actions/workflows/ci.yml)
[![License: MIT](https://img.shields.io/badge/License-MIT-blue.svg)](LICENSE)
[![Python 3.11+](https://img.shields.io/badge/python-3.11%2B-blue.svg)](pyproject.toml)

**The control plane for AI coding agents.** Keep your best reasoning model focused on planning and
review, route execution to cheaper or specialized workers, isolate each task in its own git
worktree, and measure what every routing strategy actually cost.

One driver agent (e.g. Claude Code) plans the work. Marshal spawns and manages a fleet of
*headless* coding agents - **Cursor CLI, OpenCode, Codex, and Claude Code** today (plus an
experimental **Google Antigravity** adapter), more behind a single base class - each running
autonomously in its own isolated git worktree, in parallel. Marshal monitors
them, collects their diffs, tracks per-provider usage, and hands results back for integration.

It plugs into your driver two ways:

- **MCP server** - you declare N backend "clients"; the driver calls a lean tool surface (15 tools):
  `doctor`, `list_clients`, `run_agent`, `run_many`, `spawn`, `cancel_run`, `benchmark`, `report`,
  `get_run`, `collect_run`, `integrate`, `status`, `usage`, `list_workflows`, `run_workflow`.
- **Skills** - orchestration playbooks that teach the driver *what* Marshal can do and *how* to run
  a fleet: `marshal-orchestrate` (decompose → spawn → monitor → collect → integrate),
  `marshal-benchmark` (compare routing strategies on a real task), `marshal-workflow` (author
  and run a declarative recipe), `marshal-review-gate` (gate a merge behind independent reviewer
  consensus), and `marshal-plan-consensus` (converge on an approach before building).

> **Status: V1 core complete · pre-1.0 (APIs may change).** The engine, CLI, and MCP server (15
> tools) work: merge-back (`collect_run` + `integrate`), per-provider cost tracking, capped parallel
> fan-out (`run_many`), non-blocking `spawn`, `cancel_run`, **declarative YAML workflows**, and a
> **measured savings benchmark** (`benchmark`/`report` - run one task through N strategies and
> compare real cost/latency/outcome). OpenCode, Cursor, and Claude Code live-verified (Claude Code
> with native cost); the Codex adapter verified on a fresh usage window (re-verify pending). See
> [`docs/status.md`](docs/status.md).

## Getting started

**Prerequisites:** Python ≥ 3.11, [uv](https://docs.astral.sh/uv/), git, and the CLI for each
backend you'll use (`opencode` / `cursor-agent` / `codex` / `claude` / `agy`) - each authenticated
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

- **One base class, many backends.** Cursor, OpenCode, Codex, Gemini - adding one is a new adapter,
  not a rewrite. Backend choice is a per-call parameter.
- **Parallel by default.** Each agent runs in its own git worktree; your main branch stays clean
  until you explicitly integrate.
- **Per-provider usage tracking.** Token and cost accounting per backend, per client - a `usage`
  command most orchestrators don't have. `marshal doctor` also reports each authenticated backend's
  plan tier where the CLI honestly exposes it (e.g. Cursor's subscription tier + current model).
- **Robust headless execution.** Hard timeouts, no-stdin-deadlock guarantees, and per-backend
  defenses for the real-world hangs and quirks documented in `docs/design.md`.

## What the benchmark gives you

Marshal's headline feature is a **measured** routing comparison, not a guess. Run one task through
several strategies and `report` derives a source-honest table from each run's recorded facts. An
example capture - Marshal benchmarking two OpenCode models on a real drafting task:

| strategy | backend | status | cost | source | duration | out tokens |
|---|---|---|---|---|---|---|
| `kimi` (opencode-go/kimi-k2.6) | opencode | succeeded | **$0.0111** | native | **14.1s** | 377 |
| `glm` (opencode-go/glm-5.2) | opencode | succeeded | $0.0214 | native | 38.0s | 1561 |

**cheapest:** kimi ($0.0111) · **fastest:** kimi (14.1s)

Cost is tagged by **source** and never invented: a strategy whose provider reports no cost shows as
`unavailable` (not `$0`) and is excluded from the `cheapest` ranking. That honesty is the point - you
route on evidence, not vibes. (See [`examples/benchmark-output.md`](examples/benchmark-output.md).)

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
