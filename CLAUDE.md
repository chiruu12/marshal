# Marshal

Orchestration engine for driving a **fleet of headless coding agents** (Cursor CLI, OpenCode,
Codex now; Gemini later) from one "driver" agent (e.g. Claude Code). The driver plans; Marshal
spawns and manages the fleet in isolated git worktrees, in parallel, and reports back — exposed as
an **MCP server + Skills**, with **per-provider usage tracking**.

Marshal is the **infrastructure layer**. A future, separate product (**Chauffeur**) — an end-user
autonomous coding system — will be built on top of Marshal. See `docs/chauffeur-future.md`. Keep
Marshal clean and embeddable.

## Directory Structure

```
marshal/
├── src/marshal_engine/      # the engine (import package; NOT "marshal" — shadows stdlib builtin)
│   ├── types.py             # TaskSpec, RunOpts, AgentResult, UsageRecord, Capabilities, enums
│   ├── backends/            # one adapter per backend, all derive from base.CodingAgentBackend
│   │   ├── base.py          # the base class (cornerstone)
│   │   ├── cursor.py        # (planned)
│   │   ├── opencode.py      # (planned)
│   │   └── codex.py         # (planned)
│   ├── worktree.py          # git worktree lifecycle (planned)
│   ├── runner.py            # process spawning + timeouts (planned)
│   ├── usage.py             # events.jsonl + summary.json + price table (planned)
│   ├── fleet.py             # persistent fleet state (planned)
│   ├── mcp/                 # MCP server: lean tool surface (planned)
│   └── cli.py               # `marshal` CLI entry point
├── .claude/skills/          # orchestration playbooks + Marshal "driver's manual" skills
├── docs/                    # design.md (full architecture), sources.md, chauffeur-future.md
└── tests/                   # contract tests per backend
```

## Tech Stack

Python ≥ 3.11, managed with **uv**. Stdlib-first engine (dataclasses, subprocess, pathlib).
MCP server via the `mcp` SDK (optional extra). Config in YAML. No database — file-based state.

## Development

- Install: `uv sync`
- Run CLI: `uv run marshal`
- Test: `uv run pytest`
- Lint: `uv run ruff check . && uv run mypy src`
- Add deps: `uv add <pkg>` (never edit pyproject.toml deps by hand)

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
