# Marshal

Orchestration engine for driving a **fleet of headless coding agents** (Cursor CLI, OpenCode,
Codex, Google Antigravity, Claude Code, Command Code, Goose) from one "driver"
agent (e.g. Claude Code). The driver plans; Marshal spawns and manages the fleet in isolated git
worktrees, in parallel, and reports back - exposed as an **MCP server + Skills**, with
**per-provider usage tracking**.

Marshal is the **infrastructure layer**. A future, separate product (**Chauffeur**) - an end-user
autonomous coding system - will be built on top of Marshal. See `docs/chauffeur-future.md`. Keep
Marshal clean and embeddable.

> **Current status:** full vertical slice built (engine → service → CLI → MCP); suite green.
> **V1 complete**: merge-back, per-provider cost-proof, capped parallel `run_many`, non-blocking
> `spawn`, `cancel_run`, the **measured savings benchmark** (`benchmark`/`report`), **declarative
> YAML workflows**, and driver Skills. MCP tools are documented in `docs/mcp-tools.md` (incl.
> multi-workspace: one server targets several repos, selected per call, registered in
> `~/.marshal/workspaces.yaml` + hot-reloaded). OpenCode + Cursor + Claude Code live-verified
> (Claude Code with native cost). Remaining work is coverage/polish. See `docs/status.md`.

## Directory Structure

```
marshal/
├── src/marshal_engine/      # the engine (import package; NOT "marshal" - shadows stdlib builtin)
│   ├── core/                # value types and pure logic - no subprocess, no git, no network
│   │   ├── types.py         # TaskSpec, RunOpts, AgentResult, UsageRecord, Capabilities, enums
│   │   ├── ids.py           # fail-closed path-segment id rules (task/run/worktree ids)
│   │   ├── config.py        # fleet.config.yaml loader + Fireworks guard + duration presets
│   │   ├── retry.py         # transient-failure classifier + backoff for run retries
│   │   ├── layout.py        # centralized .marshal directory layout helpers
│   │   └── _version.py      # package version from installed metadata
│   ├── backends/            # one adapter per backend, all derive from base.CodingAgentBackend
│   │   ├── base.py          # the base class (cornerstone) - owns the safe run() loop
│   │   ├── cursor.py        # Cursor CLI (cursor-agent)
│   │   ├── opencode.py      # OpenCode (opencode run / serve)
│   │   ├── codex.py         # OpenAI Codex (codex exec)
│   │   ├── antigravity.py   # Google Antigravity (agy)
│   │   ├── command_code.py  # Command Code CLI - safe-edit maps to --yolo (headless auto-accept blocks writes)
│   │   ├── claude_code.py   # Claude Code (claude -p) - native cost
│   │   └── goose.py         # Goose (goose run) - safe-edit/yolo → GOOSE_MODE=auto (worktree boundary)
│   ├── runtime/             # the execution boundary - processes, git, disk
│   │   ├── worktree.py      # git worktree lifecycle (the isolation boundary)
│   │   ├── env.py           # child env allowlist (operational + known credential names) + user PATH recovery
│   │   ├── logs.py          # durable per-run stdout/stderr persistence
│   │   └── state.py         # persistent fleet state (one runs/<run_id>.json per run)
│   ├── accounting/          # usage facts and cost
│   │   ├── usage.py         # per-provider usage: events.jsonl + summary.json
│   │   ├── eastrouter.py    # read real per-run cost from EastRouter /v1/usage (the ADMIN_API path)
│   │   └── budgets.py       # budget caps (soft-warn default; optional enforce: true)
│   ├── orchestration/       # the fleet loop and everything sequenced on top of it
│   │   ├── fleet.py         # orchestrator: worktree → run backend → record usage → persist
│   │   ├── provisioning.py  # copy declared context_files / read_paths into a worktree, fail-closed (symlink/special-file/TOCTOU refusals)
│   │   ├── structured.py    # output_schema: prompt instruction, JSON extraction, schema validation, redaction
│   │   ├── results.py       # the Pydantic result/request DTOs the service, CLI, and MCP all serialize
│   │   ├── registry.py      # construct backends by name
│   │   ├── workflow.py      # declarative YAML workflows: spec + validation + runner over the service primitives
│   │   └── teams.py         # adversarial review teams: panels of independent READ-ONLY reviewers over one subject (run diff / range / plan / audit) → structured report; never integrates
│   ├── interfaces/          # what the outside world touches
│   │   ├── service.py       # MarshalService - the testable core the MCP/CLI call into (single-repo; tenancy lives in workspaces.py)
│   │   ├── workspaces.py    # MCP-layer multi-repo registry: default + ~/.marshal/workspaces.yaml + env, lazy per-repo service cache (hot-reloaded), run-id addressing, register/scaffold helpers
│   │   ├── doctor.py        # `marshal doctor` preflight checks (setup readiness) + Cursor plan tier; verifies auth (not just CLI-on-PATH) for backends exposing an authed probe
│   │   ├── scaffold.py      # repo-shape-aware fleet.config.yaml scaffold
│   │   ├── mcp_server/      # MCP server (MCPServer) - see docs/mcp-tools.md for the tool reference
│   │   │   ├── server.py    # build_service + build_app: constructs the app, the ToolContext, and registers each group
│   │   │   ├── context.py   # ToolContext - what the tool groups share (registry, offload, ws_call, run_call)
│   │   │   ├── schema.py    # parameter descriptions + the shared Job/ThenJob input models
│   │   │   └── tools_*.py   # one module per tool group: inspect, runs, integrate, recipes, workspaces

│   │   └── cli/             # `marshal` CLI (init/doctor/backends/models/run/spawn/usage/status/logs/workflows/teams/team/workspace/clean/mcp)
│   │       ├── parser.py    # argparse wiring + dispatch; `main` is re-exported from __init__
│   │       ├── inspect.py   # read-only views: backends, models, usage, status, logs
│   │       ├── runs.py      # dispatch work: run, spawn
│   │       ├── recipes.py   # workflows and teams
│   │       ├── admin.py     # init, doctor, workspace, clean
│   │       ├── formatting.py # shared display layer: tables, cost/rate rendering
│   │       └── common.py    # shared arg types, repo resolution, service construction
│   ├── config.py service.py teams.py state.py workspaces.py cli.py
│   │                        # re-export shims ONLY - published import paths kept working; no logic
├── skills/                  # public driver Skills: marshal-orchestrate, marshal-benchmark, marshal-workflow, marshal-review-gate, marshal-plan-consensus, marshal-adversarial-review
├── examples/                # runnable library_quickstart.py, a benchmark-output sample, workflows/ + teams/ starters
├── SETUP.md                 # clone-to-first-run setup guide
├── docs/                    # design · status · usage · config · mcp-tools · model-playbook · chauffeur-future · sources (docs/internal/ is local-only, gitignored)
└── tests/                   # contract tests per backend + engine/service/mcp tests
# .claude/ is local tooling (gitignored); the public copies of the Marshal Skills live in skills/.
```

The experimental Cognee-backed Marshal Recall implementation is preserved on
`feature/marshal-recall-cognee`; it is not part of core Marshal.

## Tech Stack

Python ≥ 3.11, managed with **uv**. **Pydantic** models for value types, config, persisted state,
and the MCP I/O surface (validation + uniform JSON serialization); stdlib for the rest (subprocess,
pathlib). Loose, version-variable **backend CLI stdout is parsed as plain dicts** in the adapters -
strict models there would reject on an unexpected upstream field. MCP server via the `mcp` SDK
(optional extra). Config in YAML. No database - file-based state.

## Development

- Install: `uv sync --extra mcp --extra dev`
- Run CLI: `uv run marshal` (`init` · `doctor` · `backends` · `models` · `run` · `spawn` · `usage` · `status` · `logs` · `workflows` · `workflow` · `workspace` · `clean` · `mcp`)
- Test: `uv run pytest`
- Lint: `uv run ruff check src tests && uv run mypy`
- Add deps: `uv add <pkg>` (never edit pyproject.toml deps by hand)

The gate every commit must pass (single-line; `git -C`/`uv --directory` from outside the dir):
`uv --directory . run pytest -q && uv --directory . run ruff check src tests && uv --directory . run mypy`

CI additionally enforces a **90% coverage floor** (`--cov-fail-under=90`) and runs the suite on
Linux (py3.11-3.13) + macOS (py3.12, for the POSIX process-group paths). Check coverage locally with
`uv run pytest --cov=marshal_engine --cov-report=term-missing` (the bare `pytest -q` stays fast).

### Development rules

- **Docs + CHANGELOG ride the feature commit.** Ship user-facing doc updates and `[Unreleased]`
  entries in the same PR as the code they describe.
- **Never hardcode counts in prose** (tool counts, client counts, etc.) — link the normative home
  (`docs/mcp-tools.md` for MCP tools, `docs/config.md` for config keys).
- **One normative home per fact:** `docs/design.md` = architecture; `docs/usage.md` = user manual;
  `docs/config.md` = config census; `docs/mcp-tools.md` = MCP tool reference; `CHANGELOG.md` =
  history; `skills/` = driver playbooks.
- **YAGNI gate** — no new field/param/config key without a consumer wired in the same PR.
- **Shared builders** — any operation exposed on 2+ of library/CLI/MCP goes through one shared
  builder/serializer.

## Core invariants (do not violate)

- **Every agent run gets an external timeout + kill.** Both Cursor and OpenCode hang in the wild.
- **Headless = no stdin = never use a prompting permission mode** (it deadlocks). Default `safe-edit`.
- **Backend is a per-call parameter**, never a global, never encoded in tool/skill names.
- **`build_invocation` and `map_permission` are pure functions** returning argv - unit-testable
  without spawning processes. Every backend ships contract tests.
- **Tag every usage record with its `source`** (native / admin-api / unavailable). Never fabricate
  cost when the backend or a provider usage API did not report it.
- **Usage/cost is a two-layer split.** The engine stamps *facts* (tokens / cost / duration /
  source) to an immutable ledger (`usage/events.jsonl`); interpretation (cost-per-outcome,
  savings) is *derived on read* in the report layer, never stored.
- **Worktree isolation** is the safety boundary. Main branch is untouched until explicit integrate.
- The **engine is mechanism**; planning/routing/merge judgment lives in **Skills** (and later
  Chauffeur). Don't put decomposition logic in the engine.
- **Imports point downward through the layers**: `interfaces → orchestration → backends →
  {runtime, accounting} → core`. `runtime` and `accounting` are siblings and must not import each
  other. Enforced by `tests/test_import_layers.py` over the AST import graph, so lazy and
  `TYPE_CHECKING` imports count too. New top-level modules belong in a layer, not the package root.
- **Tenancy (multi-workspace) lives in the MCP layer** (`workspaces.py`), not the engine.
  `MarshalService`/`Fleet` stay single-repo; the registry builds one per repo and keys it on the
  resolved path. Each workspace keeps its own config, worktrees, and ledger - never share run state
  across them. Chauffeur replaces the registry later with real multi-tenancy; the engine is untouched.

Full architecture, per-backend cheat sheets, permission tables, and the edge-case hardening
checklist are in `docs/design.md`. Read it before implementing a backend.

## Conventions

- Read existing files before creating new ones - match patterns.
- Commit messages: one line, describe WHAT shipped, not how. No process/iteration history.
- Never expose internal process in any public-facing output (commits, PRs, README, docs).
