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
builtin). The PyPI distribution name is `marshal` when that name is free; the documented fallback is
`marshal-orchestrator`.

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

## Releasing

Marshal is pre-1.0 — minor versions may include breaking changes until 1.0. Publishing to PyPI is a
**human-gated** action: the [Release workflow](.github/workflows/release.yml) runs only on a
**published GitHub Release** or a manual `workflow_dispatch` (never on push to a branch). It uses
PyPI Trusted Publishing (OIDC); there is no long-lived PyPI API token in GitHub secrets.

### Cut a version

1. Bump `version` in `pyproject.toml` and `__version__` in `src/marshal_engine/__init__.py` to the
   same value (e.g. `0.1.0`). Also bump `.claude-plugin/plugin.json` and
   `.claude-plugin/marketplace.json` to match.
2. In `CHANGELOG.md`, promote `## [Unreleased]` to a version heading
   (`## [0.1.0] - YYYY-MM-DD`) and leave a fresh empty `## [Unreleased]` section above it.
3. Open a PR with those changes; merge only after the gate is green.

### Verify the artifact before releasing

From a clean checkout of the release commit:

```bash
uv build
unzip -l dist/marshal-*-py3-none-any.whl | grep -E 'marshal_engine/(py\.typed|data/prices\.yaml)'
# Wheel must NOT contain tests/, .marshal/, teams/, or fleet.config.yaml
tar -tzf dist/marshal-*.tar.gz | head   # sdist must include src/, tests/, pyproject.toml, README, LICENSE

TMP=$(mktemp -d)
uv venv "$TMP/venv"
uv pip install --python "$TMP/venv/bin/python" dist/marshal-*-py3-none-any.whl
"$TMP/venv/bin/marshal" --version   # expect: marshal <version>
rm -rf "$TMP"
```

### Publish

1. Confirm the PyPI project name is still available as `marshal`, or change
   `[project].name` to `marshal-orchestrator` before the first publish (import package stays
   `marshal_engine`).
2. Ensure Trusted Publishing is configured on PyPI for this repo’s `release.yml` and the `pypi`
   GitHub Environment (see the comment block at the top of `.github/workflows/release.yml`).
   **Configure that environment’s protection rules** — required reviewers, and deployment branches
   limited to `v*` tags. This is not belt-and-braces: `workflow_dispatch` runs the workflow file as
   it exists on the ref you select, so the in-workflow tag/version guard can be removed on a branch,
   and PyPI checks only the workflow filename and environment name. Environment protection is the
   one control that does not live inside the ref being published.
3. Create and **publish** a GitHub Release for tag `v<version>`. Publishing the Release is the
   human action that triggers the PyPI upload. A manual `workflow_dispatch` works too, but it must
   select the **tag**, not a branch: the workflow refuses to publish from a non-tag ref, and
   refuses when the tag name does not match the built `__version__`. Both refusals are deliberate —
   a PyPI version can never be replaced once uploaded.

## Reporting security issues

Do **not** open a public issue for vulnerabilities. See [`SECURITY.md`](SECURITY.md).
