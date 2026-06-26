# Marshal

Orchestration engine for driving a **fleet of headless coding agents** (Cursor CLI, OpenCode,
Codex, Google Antigravity, Claude Code now; Gemini later) from one "driver" agent (e.g. Claude Code). The driver
plans; Marshal spawns and manages the fleet in isolated git worktrees, in parallel, and reports
back - exposed as an **MCP server + Skills**, with **per-provider usage tracking**.

Marshal is the **infrastructure layer**. A future, separate product (**Chauffeur**) - an end-user
autonomous coding system - will be built on top of Marshal. See `docs/chauffeur-future.md`. Keep
Marshal clean and embeddable.

> **Current status:** full vertical slice built (engine → service → CLI → MCP); suite green.
> **V1 complete**: merge-back, per-provider cost-proof, capped parallel `run_many`, non-blocking
> `spawn`, `cancel_run`, the **measured savings benchmark** (`benchmark`/`report`), **declarative
> YAML workflows**, and driver Skills. 14 MCP tools. OpenCode + Cursor + Claude Code live-verified
> (Claude Code with native cost). Remaining work is coverage/polish. See `docs/status.md`.

## Directory Structure

```
marshal/
├── src/marshal_engine/      # the engine (import package; NOT "marshal" - shadows stdlib builtin)
│   ├── types.py             # TaskSpec, RunOpts, AgentResult, UsageRecord, Capabilities, enums
│   ├── backends/            # one adapter per backend, all derive from base.CodingAgentBackend
│   │   ├── base.py          # the base class (cornerstone) - owns the safe run() loop
│   │   ├── cursor.py        # Cursor CLI (cursor-agent)
│   │   ├── opencode.py      # OpenCode (opencode run / serve)
│   │   ├── codex.py         # OpenAI Codex (codex exec)
│   │   ├── antigravity.py   # Google Antigravity (agy)
│   │   └── claude_code.py   # Claude Code (claude -p) - native cost
│   ├── worktree.py          # git worktree lifecycle (the isolation boundary)
│   ├── usage.py             # per-provider usage: events.jsonl + summary.json
│   ├── state.py             # persistent fleet state (one runs/<run_id>.json per run)
│   ├── fleet.py             # orchestrator: worktree → run backend → record usage → persist
│   ├── registry.py          # construct backends by name
│   ├── config.py            # fleet.config.yaml loader + Fireworks guard
│   ├── workflow.py          # declarative YAML workflows: spec + validation + runner over the service primitives
│   ├── service.py           # MarshalService - the testable core the MCP/CLI call into
│   ├── doctor.py            # `marshal doctor` preflight checks (setup readiness) + Cursor plan tier
│   ├── mcp_server.py        # MCP server (FastMCP): list_clients/run_agent/run_many/spawn/cancel_run/benchmark/report/get_run/collect_run/integrate/status/usage/list_workflows/run_workflow
│   └── cli.py               # `marshal` CLI (doctor/backends/usage/status/workflows/mcp)
├── skills/                  # public driver Skills: marshal-orchestrate, marshal-benchmark, marshal-workflow, marshal-review-gate, marshal-plan-consensus
├── examples/                # runnable library_quickstart.py + a benchmark-output sample
├── SETUP.md                 # clone-to-first-run setup guide
├── docs/                    # design · status · usage · chauffeur-future · sources (docs/internal/ is local-only, gitignored)
└── tests/                   # contract tests per backend + engine/service/mcp tests
# .claude/ is local tooling (gitignored); the public copies of the Marshal Skills live in skills/.
```

## Tech Stack

Python ≥ 3.11, managed with **uv**. **Pydantic** models for value types, config, persisted state,
and the MCP I/O surface (validation + uniform JSON serialization); stdlib for the rest (subprocess,
pathlib). Loose, version-variable **backend CLI stdout is parsed as plain dicts** in the adapters -
strict models there would reject on an unexpected upstream field. MCP server via the `mcp` SDK
(optional extra). Config in YAML. No database - file-based state.

## Development

- Install: `uv sync --extra mcp --extra dev`
- Run CLI: `uv run marshal` (`doctor` · `backends` · `usage` · `status` · `workflows` · `mcp`)
- Test: `uv run pytest`
- Lint: `uv run ruff check src tests && uv run mypy`
- Add deps: `uv add <pkg>` (never edit pyproject.toml deps by hand)

The gate every commit must pass (single-line; `git -C`/`uv --directory` from outside the dir):
`uv --directory . run pytest -q && uv --directory . run ruff check src tests && uv --directory . run mypy`

## Core invariants (do not violate)

- **Every agent run gets an external timeout + kill.** Both Cursor and OpenCode hang in the wild.
- **Headless = no stdin = never use a prompting permission mode** (it deadlocks). Default `safe-edit`.
- **Backend is a per-call parameter**, never a global, never encoded in tool/skill names.
- **`build_invocation` and `map_permission` are pure functions** returning argv - unit-testable
  without spawning processes. Every backend ships contract tests.
- **Tag every usage record with its `source`** (native / admin-api / estimated / scraped /
  unavailable). Never present an estimate as ground truth.
- **Usage/cost is a two-layer split.** The engine stamps *facts* (tokens / cost / duration /
  source) to an immutable ledger (`usage/events.jsonl`); interpretation (cost-per-outcome,
  savings) is *derived on read* in the report layer, never stored. Estimated cost is priced at
  run time (a snapshot), so editing the price table never rewrites history.
- **Worktree isolation** is the safety boundary. Main branch is untouched until explicit integrate.
- The **engine is mechanism**; planning/routing/merge judgment lives in **Skills** (and later
  Chauffeur). Don't put decomposition logic in the engine.

Full architecture, per-backend cheat sheets, permission tables, and the edge-case hardening
checklist are in `docs/design.md`. Read it before implementing a backend.

## Conventions

- Read existing files before creating new ones - match patterns.
- Commit messages: one line, describe WHAT shipped, not how. No process/iteration history.
- Never expose internal process in any public-facing output (commits, PRs, README, docs).
