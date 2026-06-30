# Contributing to Marshal

Thanks for your interest in Marshal. It is an orchestration engine for driving a fleet of
headless coding agents (Cursor, OpenCode, Codex, Antigravity, Claude Code, Command Code) from one driver agent, exposed as an
MCP server and driver Skills. This guide covers the dev setup, the quality gate, and - most
importantly - how to add a new backend, which is Marshal's core extension point.

Marshal is **pre-1.0**; APIs may change between minor versions until 1.0.

## Dev setup

Marshal uses [uv](https://docs.astral.sh/uv/). Python >= 3.11.

```bash
git clone https://github.com/chiruu12/marshal.git && cd marshal
uv sync --extra mcp --extra dev
```

The import package is `marshal_engine` (a top-level package named `marshal` would shadow the stdlib
builtin). The distribution name is `marshal`.

Useful commands:

```bash
uv run marshal doctor      # preflight: is the setup ready to run agents?
uv run pytest -q           # run the suite
uv run ruff check src tests # lint
uv run mypy                # strict type-check (src)
```

## The gate (every change must pass)

Run this single line before opening a PR. CI runs the same gate on Linux (Python 3.11/3.12/3.13)
plus macOS (3.12).

```bash
uv run pytest -q && uv run ruff check src tests && uv run mypy
```

- `pytest` must be green.
- `ruff check src tests` must report no errors.
- `mypy` runs in **strict** mode over `src` and must be clean.
- **Coverage:** CI also enforces a 90% floor (`--cov-fail-under=90`). Check locally with
  `uv run pytest --cov=marshal_engine --cov-report=term-missing` (the bare `pytest -q` skips coverage
  to stay fast).

## Pull request norms

- Branch off `main`.
- Keep commit messages to **one line describing WHAT shipped**, not how or the iteration history.
- Do not include internal process, planning notes, or cost figures in commits, PR descriptions, or
  docs (public-facing output stays clean).
- Update the relevant docs (`README.md`, `docs/`, `CLAUDE.md`) when behavior changes.
- Add an entry under `## [Unreleased]` in `CHANGELOG.md`.
- If you add a backend, ship contract tests (see below).

## Project layout

```
src/marshal_engine/
  types.py            # TaskSpec, RunOpts, AgentResult, UsageRecord, Capabilities, enums (Pydantic v2)
  backends/
    base.py           # CodingAgentBackend - owns the safe run() loop (do not bypass)
    cursor.py opencode.py codex.py antigravity.py claude_code.py command_code.py
  worktree.py         # git worktree lifecycle (isolation boundary)
  usage.py eastrouter.py  # usage ledger (events.jsonl + summary) + EastRouter real-cost reader
  pricing.py state.py fleet.py registry.py config.py retry.py env.py
  service.py          # MarshalService - the testable core the CLI/MCP call into (single-repo)
  workspaces.py       # MCP-layer multi-repo registry (tenancy; the engine stays single-repo)
  doctor.py cli.py mcp_server.py
skills/               # driver Skills (marshal-orchestrate/-benchmark/-workflow/-review-gate/-plan-consensus)
tests/                # contract tests per backend + engine/service/MCP tests
```

## Core invariants (do not violate)

These are load-bearing safety properties. A PR that breaks one will not be merged.

- **Every agent run gets a hard external timeout + process-group kill.** This lives in
  `backends/base.py::run()`; do not spawn agent processes outside it.
- **Headless = no stdin.** Never use a prompting/interactive permission mode - it deadlocks.
  The default tier is `safe-edit`.
- **Backend is a per-call parameter**, never a global and never encoded in tool or skill names.
- **`build_invocation` and `map_permission` are pure functions** returning argv / flags - unit
  testable without spawning a process.
- **Tag every usage record with its `source`** (native / admin-api / estimated / scraped /
  unavailable). Never present an estimate as ground truth, and never invent `$0` for an unknown cost.
- **Worktree isolation is the safety boundary.** The main branch is untouched until an explicit
  `integrate`.
- **The engine is mechanism.** Planning, routing, and merge judgment live in Skills, not the engine.

## Adding a backend (the main extension point)

A backend is one adapter subclassing `CodingAgentBackend` (`src/marshal_engine/backends/base.py`).
The base class owns the shared, concrete `run()` loop (hard timeout, no stdin, process-group kill
on timeout, partial-usage recovery), so an adapter only declares identity, capabilities, and four
hooks.

1. **Create `src/marshal_engine/backends/<name>.py`** with a subclass that sets:
   - `name` - short stable id (e.g. `"opencode"`).
   - `binary` - the executable to invoke (e.g. `"opencode"`).
   - `capabilities` - a `Capabilities` instance so the orchestrator can degrade gracefully.

2. **Implement the four hooks:**
   - `check_available() -> bool` - probe `binary --version` (pin a minimum where hangs/bugs are
     version-gated) and verify credentials are present.
   - `build_invocation(task, opts) -> list[str]` - **pure**: `(task, opts) -> argv`. No side effects,
     no spawning.
   - `map_permission(mode) -> list[str]` - **pure**: a normalized `PermissionMode` -> this backend's
     native flags. Never map to a prompting/interactive mode.
   - `parse_output(raw_stdout, raw_stderr, exit_code) -> AgentResult` - normalize raw output. Treat a
     non-zero exit or unparseable output as **failure**. Populate usage / session_id / files_changed
     where the backend exposes them. Backend stdout is parsed as a plain dict on purpose; only the
     normalized `AgentResult` / `UsageRecord` are Pydantic models.

3. **Optionally override `extract_usage(result) -> UsageRecord | None`** if usage is not in the run
   output (e.g. Cursor fetches from an admin API; an estimate is priced from the price table). Tag
   the record's `source` accordingly.

4. **Register the factory** in `src/marshal_engine/registry.py` by adding your class to
   `_FACTORIES` keyed by `name`.

5. **Ship contract tests** for the two pure functions. Use an existing backend's tests as the
   pattern (`tests/test_cursor_backend.py`, `tests/test_opencode_backend.py`, etc.): assert the argv
   from `build_invocation` for a representative task/opts, and the flags from `map_permission` for
   each `PermissionMode`. These run without spawning a process.

6. **Price the model** (if it reports cost) by adding it to `data/prices.yaml`, or leave cost
   `unavailable` - never fabricate a number.

Run the gate, then open a PR describing what the new backend supports and its verification state
(see `docs/status.md` for the honesty conventions of the verification matrix).

## Reporting security issues

Do **not** open a public issue for vulnerabilities. See [`SECURITY.md`](SECURITY.md).
