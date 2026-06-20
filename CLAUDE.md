# Marshal

Orchestration engine for driving a **fleet of headless coding agents** (Cursor CLI, OpenCode,
Codex, Google Antigravity now; Gemini later) from one "driver" agent (e.g. Claude Code). The driver
plans; Marshal spawns and manages the fleet in isolated git worktrees, in parallel, and reports
back — exposed as an **MCP server + Skills**, with **per-provider usage tracking**.

Marshal is the **infrastructure layer**. A future, separate product (**Chauffeur**) — an end-user
autonomous coding system — will be built on top of Marshal. See `docs/chauffeur-future.md`. Keep
Marshal clean and embeddable.

> **Current status:** full vertical slice built (engine → service → CLI → MCP), 57 tests green.
> OpenCode + Cursor live-verified; merge-back and parallel fan-out are next. See `docs/status.md`.

## Directory Structure

```
marshal/
├── src/marshal_engine/      # the engine (import package; NOT "marshal" — shadows stdlib builtin)
│   ├── types.py             # TaskSpec, RunOpts, AgentResult, UsageRecord, Capabilities, enums
│   ├── backends/            # one adapter per backend, all derive from base.CodingAgentBackend
│   │   ├── base.py          # the base class (cornerstone) — owns the safe run() loop
│   │   ├── cursor.py        # Cursor CLI (cursor-agent)
│   │   ├── opencode.py      # OpenCode (opencode run / serve)
│   │   ├── codex.py         # OpenAI Codex (codex exec)
│   │   └── antigravity.py   # Google Antigravity (agy)
│   ├── worktree.py          # git worktree lifecycle (the isolation boundary)
│   ├── usage.py             # per-provider usage: events.jsonl + summary.json
│   ├── state.py             # persistent fleet state (fleet.json)
│   ├── fleet.py             # orchestrator: worktree → run backend → record usage → persist
│   ├── registry.py          # construct backends by name
│   ├── config.py            # fleet.config.yaml loader + Fireworks guard
│   ├── service.py           # MarshalService — the testable core the MCP/CLI call into
│   ├── mcp_server.py        # MCP server (FastMCP): list_clients/run_agent/get_run/status/usage
│   └── cli.py               # `marshal` CLI (backends/usage/status/mcp)
├── .claude/skills/          # imported skills; Marshal "driver's manual" skills are planned
├── docs/                    # design · vision · status · usage · decisions · chauffeur-future · sources
└── tests/                   # contract tests per backend + engine/service/mcp tests
```

## Tech Stack

Python ≥ 3.11, managed with **uv**. Stdlib-first engine (dataclasses, subprocess, pathlib).
MCP server via the `mcp` SDK (optional extra). Config in YAML. No database — file-based state.

## Development

- Install: `uv sync --extra mcp --extra dev`
- Run CLI: `uv run marshal` (`backends` · `usage` · `status` · `mcp`)
- Test: `uv run pytest`
- Lint: `uv run ruff check src tests && uv run mypy`
- Add deps: `uv add <pkg>` (never edit pyproject.toml deps by hand)

The gate every commit must pass (single-line; `git -C`/`uv --directory` from outside the dir):
`uv --directory . run pytest -q && uv --directory . run ruff check src tests && uv --directory . run mypy`

## Core invariants (do not violate)

- **Every agent run gets an external timeout + kill.** Both Cursor and OpenCode hang in the wild.
- **Headless = no stdin = never use a prompting permission mode** (it deadlocks). Default `safe-edit`.
- **Backend is a per-call parameter**, never a global, never encoded in tool/skill names.
- **`build_invocation` and `map_permission` are pure functions** returning argv — unit-testable
  without spawning processes. Every backend ships contract tests.
- **Tag every usage record with its `source`** (native / admin-api / estimated / scraped /
  unavailable). Never present an estimate as ground truth.
- **Worktree isolation** is the safety boundary. Main branch is untouched until explicit integrate.
- The **engine is mechanism**; planning/routing/merge judgment lives in **Skills** (and later
  Chauffeur). Don't put decomposition logic in the engine.

Full architecture, per-backend cheat sheets, permission tables, and the edge-case hardening
checklist are in `docs/design.md`. Read it before implementing a backend.

## Conventions

- Read existing files before creating new ones — match patterns.
- Commit messages: one line, describe WHAT shipped, not how. No process/iteration history.
- Never expose internal process in any public-facing output (commits, PRs, README, docs).
