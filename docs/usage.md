# Using Marshal

Marshal drives a fleet of headless coding agents from one driver. You declare named
**clients** (each pinning a backend + model + permission), then call Marshal three ways: as an
MCP server, as a CLI, or as a Python library.

> **Status:** V1 core complete, pre-1.0. The engine, CLI, and MCP server work, including merge-back
> (`collect_run` + `integrate`), capped parallel fan-out (`run_many`), and a measured savings
> benchmark (`benchmark`/`report`). See [`status.md`](status.md). For every config key see
> [`config.md`](config.md); for MCP parameters and return shapes see [`mcp-tools.md`](mcp-tools.md).

## Concepts

| Term | What it is |
|------|------------|
| **driver** | The agent (e.g. Claude Code) that plans the work and calls Marshal. It keeps the expensive reasoning. |
| **backend** | A CLI adapter (cursor, opencode, codex, claude-code, command-code, antigravity). Chosen per call, never global. |
| **client** | A named worker in `fleet.config.yaml` pinning a backend + model + permission. You route tasks to clients by name. |
| **run** | One execution of a client on a task; ends `succeeded`/`empty`/`failed`/`timed_out`/`cancelled`/`verify_failed`. |
| **worktree** | The isolated git checkout **one run** works in (under `.marshal/worktrees/`). The safety boundary — main is untouched until you integrate. |
| **workspace** | A **whole repo** the server can target. Distinct from *worktree*: a workspace holds many runs, each in its own worktree. One server can target several workspaces (`list_workspaces`, `workspace=`). |
| **integrate** | Merge a run's worktree branch back into the target repo's current branch (the only step that touches it). |
| **workflow** | A declarative YAML recipe that sequences the primitives (fan-out → collect → gated integrate). |

## Install

New here? Start with **[`../SETUP.md`](../SETUP.md)** for the full clone-to-first-run path:
prerequisites (Python ≥ 3.11, uv, git) and how to install + authenticate the backend CLIs -
Marshal does **not** install them.

```bash
uv sync --extra mcp --extra dev
```

The base package is Pydantic + PyYAML. The `mcp` extra adds the MCP server; `dev` adds the
test/lint toolchain.

## Configure a fleet

Copy the example and edit it:

```bash
cp fleet.config.example.yaml fleet.config.yaml
```

```yaml
defaults:
  permission: safe-edit        # read-only | safe-edit | yolo
  timeout_s: 600

clients:
  implementer:
    backend: opencode          # opencode | cursor | codex | command-code | claude-code | antigravity
    model: opencode-go/glm-5.2 # Go sub - a fireworks-ai/* model here is rejected
    permission: safe-edit
    secret_ref: env:OPENCODE_API_KEY

  reviewer:
    backend: cursor
    permission: read-only
    secret_ref: env:CURSOR_API_KEY

  planner:
    backend: claude-code       # `claude -p` - native cost (total_cost_usd) + tokens
    model: claude-sonnet-4-6   # bump to claude-opus-4-8 for harder tasks
    permission: safe-edit
```

- **Auth is per-CLI**: run each backend's login once (`opencode auth login`, `cursor-agent login`,
  `codex login`). `secret_ref: env:VAR` is an optional preflight check - `marshal doctor` warns if
  unset - but Marshal does **not** inject it; the CLI's own login is what authenticates.
- An OpenCode client with no `model` defaults to `opencode-go/glm-5.2` so runs bill the Go
  subscription, not Fireworks credits. A `fireworks-ai/*` model is rejected outright.
- **`worktree_setup`** (optional, top-level): a command run once in each fresh worktree before the
  agent starts - e.g. `worktree_setup: uv sync --extra dev --extra mcp` to provision the worktree's
  own venv. Accepts a string or an argv list; omit it for repos that need no setup. Marshal scrubs
  the driver's `VIRTUAL_ENV`/`PYTHONHOME` for the command (and for agent runs), so the worktree's
  own environment wins - without it, an agent's `uv run pytest` would resolve the driver's venv and
  test stale code. A non-zero exit tears the worktree down and fails the run early.
- **`retries`** (optional, top-level, default `2`): how many times to re-run a run that failed for a
  **transient** reason - a backend state-DB lock, a rate limit, a 5xx, a dropped connection - with
  exponential backoff. Set `0` to disable. Genuine task failures and timeouts are **never** retried
  (a timeout retry just burns another full window). A retried run records its `attempts` count.
- **`verify`** (optional, top-level): a gate command run in the worktree **after** a run that would
  otherwise be `succeeded` and actually changed files (e.g. the repo's full test suite). Text-only
  replies are never gated. A non-zero exit marks the run `verify_failed` instead of `succeeded`; the
  worktree and diff are kept for review, and the command's output tail lands on the run record
  (`verify_output`). Same string-or-argv shape and env hygiene as `worktree_setup`.
- **`context`** (optional, top-level): fleet-wide layered context strings.
  - `worker` — prepended to every worker agent's goal (shared operating assumptions).
  - `driver` — surfaced back to the driver via `list_clients` / `list_models` as `driver_context`.
- **`models`** (optional, top-level): a catalog the driver reads with `list_models` / `marshal models`.
  Each entry has `id` (provider/model), `backends` it runs on, and short free-form strings for
  `cost` / `quota_type` / `notes`. Pure metadata — does **not** change routing (clients still own
  backend+model). Absent or empty = no catalog to expose.
- **Duration presets** — per-spawn timeout overrides for `run_agent` / `spawn` / `run_many` (and
  `marshal run` / `marshal spawn` with `--duration`). Pass a preset name (`short`=300s,
  `medium`=1200s, `large`=6000s, `long`=24000s) or a positive integer of seconds. The override
  replaces the resolved `timeout_s` on the `RunRequest` for that one call; validation happens up
  front so a typo fails fast before any worktree is created. See also [`config.md`](config.md).
- **`budgets`** (optional, top-level): advisory $ caps per scope (a backend, a client, or the
  whole fleet) and time window (`session` | `week` | `month`). **Soft-warn only — a cap never
  blocks a run.** When a scope's windowed spend meets or exceeds its cap, Marshal prints a stderr
  warning at the start of the next run on that scope. Set at most one of `backend` / `client` per
  entry (omit both for a global cap); `limit_usd` must be positive; the scope's `cost_usd` comes
  from the usage ledger, so subscription / unknown-cost backends (which report `$0`) never
  trigger a $ cap and show `$0.00` spent (no fake percentage, no fabricated "remaining").

  ```yaml
  budgets:
    - client: implementer      # cap the implementer client
      window: week
      limit_usd: 5.00
    - backend: cursor          # cap the cursor backend
      window: session
      limit_usd: 1.00
    - window: month            # global: no backend / client
      limit_usd: 25.00
  ```

  The MCP `usage` tool's response (and `marshal usage --config fleet.config.yaml --json`) includes
  a `budgets` list with `scope / window / spent_usd / limit_usd / remaining_usd` per budget, so
  the driver can see how much room is left alongside the spend.
- **Missing backend CLI → the client is skipped, not fatal.** At startup Marshal probes each
  configured backend's CLI; a client whose CLI is unavailable is **skipped** with a stderr warning
  (and listed under `skipped_clients`) so the rest of the fleet still runs - a missing CLI never fails
  a run mid-flight. `marshal doctor` still reports an unavailable backend as a FAIL so you can see
  what's missing.

### Permission tiers

| Tier | Meaning |
|------|---------|
| `read-only` | Plan/inspect only - no edits. |
| `safe-edit` | Edit and run **inside the worktree**, no prompts. The default. |
| `yolo` | Fully unrestricted. Opt-in only. |

Headless agents have no stdin, so Marshal never uses a prompting mode (it would deadlock).

## Use it as an MCP server

Point your driver at `marshal mcp`. Environment:

| Var | Default | Meaning |
|-----|---------|---------|
| `MARSHAL_REPO` | `.` | The repo agents work in (the **default** workspace). |
| `MARSHAL_CONFIG` | `<repo>/fleet.config.yaml` | The default workspace's fleet config (scoped to `default` only). |
| `MARSHAL_WORKSPACES_FILE` | `~/.marshal/workspaces.yaml` | The central registry of extra workspaces (the recommended way). |
| `MARSHAL_WORKSPACES` | - | Extra workspaces inline: comma/newline-separated `name=/abs/path` entries. |
| `MARSHAL_MAX_CONCURRENT` | 8 when multi-repo | Process-wide cap on concurrent agent runs across all workspaces. |

**Multiple repos from one server.** Declare them in `~/.marshal/workspaces.yaml` (or the inline
`MARSHAL_WORKSPACES` env). The file is the canonical "all config" for the registry:

```yaml
# ~/.marshal/workspaces.yaml
max_concurrent: 8            # optional global cap
workspaces:
  frontend: /abs/path/to/web
  backend:  /abs/path/to/api
```

Each workspace loads its **own** `<repo>/fleet.config.yaml` (clients travel with the repo). Every
tool takes an optional `workspace` param (see `list_workspaces`); the run-handle tools take it as a
hint. Add a repo with `marshal workspace add <name> [path]` or the `add_workspace` tool - it appears
**without reconnecting** the server. Config edits hot-reload the same way: adding, changing, or
deleting a workspace's `fleet.config.yaml` is picked up on the next tool call. With no file and no
`MARSHAL_WORKSPACES`, it's the single-repo server it always was.

Example Claude Code MCP entry. A bare `uv sync` does not put a `marshal` command on your PATH, so
invoke it through uv with the absolute path to your Marshal checkout (or run `uv tool install .`
first to use a bare `"command": "marshal"`). Run `marshal doctor` before wiring this up.

```json
{
  "mcpServers": {
    "marshal": {
      "command": "uv",
      "args": ["--directory", "/abs/path/to/marshal", "run", "marshal", "mcp"],
      "env": {
        "MARSHAL_REPO": "/abs/path/to/your/project",
        "MARSHAL_CONFIG": "/abs/path/to/your/project/fleet.config.yaml"
      }
    }
  }
}
```

Tools exposed to the driver (full parameter and return reference: [`mcp-tools.md`](mcp-tools.md)):

Every action/query tool below takes an optional `workspace` (a name from `list_workspaces`); the
run-handle tools (`get_run`/`collect_run`/`cancel_run`/`integrate`) take it as a hint. Omit it for
the default workspace.

| Tool | Purpose |
|------|---------|
| `list_workspaces()` | List the repos this server can target (name, path, configured, client_count). |
| `add_workspace(name, path, scaffold?)` | Register a repo in the central registry; usable immediately (no reconnect). |
| `doctor()` | Preflight the setup (toolchain, repo, config, per-backend CLI availability + auth); read-only. Run it before spawning. |
| `list_clients` | List configured clients (name, backend, model, permission) plus `driver_context`. |
| `list_models` | List the optional `models:` catalog (`id`, `backends`, `cost`, `quota_type`, `notes`) plus `driver_context`. |
| `run_agent(client?, goal, task_id?, context_files?, base_branch?, model?, backend?, duration?)` | Run a task on a client's backend in an isolated worktree; returns the run record. Omit `client` for an ad-hoc spawn by `backend` (+ optional `model`). `duration` is a preset name or positive seconds. `base_branch` bases the worktree on a branch other than HEAD (e.g. after `commit_run`). |
| `run_many(jobs, max_concurrency?)` | Run several `{client, goal, task_id?, context_files?}` jobs in parallel, each in its own worktree; returns all records. |
| `spawn(client?, goal, task_id?, context_files?, base_branch?, model?, backend?, duration?)` | Start a run in the background; returns its RUNNING record at once - poll `get_run`/`status`. Same ad-hoc/`model`/`duration`/`base_branch` rules as `run_agent`. |
| `cancel_run(run_id)` | Stop a running agent (process-group `SIGTERM`); returns the updated record. |
| `benchmark(goal, clients, task_id?)` | Run one goal through several clients (strategies) and compare cost/latency/outcome. |
| `report(task_id)` | Re-derive a past benchmark's strategy comparison from the ledger (read-only). |
| `get_run(run_id)` | Fetch one run record (status ∈ `succeeded`/`empty`/`failed`/`timed_out`/`cancelled`/`verify_failed`). |
| `collect_run(run_id)` | A run's diff + changed files (read-only; nothing is merged). Review before integrating. |
| `commit_run(run_id, message?)` | Freeze a finished run's work onto its own branch (your branch untouched) so a dependent run can `spawn` with `base_branch` = that branch. Outcome ∈ `committed`/`clean`/`blocked`/`error`. |
| `integrate(run_id, cleanup?)` | Merge a run's branch into the current branch. Outcome ∈ `merged`/`conflict`/`blocked`/`empty`/`error`. |
| `clean(scope?, run_ids?, older_than_hours?, dry_run?)` | Tear down finished runs' worktrees + branches (ledger + run history kept). Never a running run. `scope` ∈ `merged`/`finished`/`all`. Scope-mode cleans also reap orphaned worktree dirs (`orphans_removed`). Returns `{removed, orphans_removed, skipped, errors, dry_run}`. |
| `status()` | List all runs with status + cost (status ∈ `succeeded`/`empty`/`failed`/`timed_out`/`cancelled`/`verify_failed`). |
| `usage(window?)` | Per-provider usage summary (totals + by backend/client/model/backend×model, with input/output/cache-read token columns and a native/admin-api/estimated cost split). `window` ∈ `session` (since the MCP server started) \| `week` (7d) \| `month` (30d) \| `all` (default; the full ledger). The resolved `window` and `since` are echoed back. When the workspace's config declares `budgets:`, the response also includes a `budgets` list with per-budget `scope / window / spent_usd / limit_usd / remaining_usd` (advisory only - caps never block a run). |
| `get_run_log(run_id)` | The full raw stdout/stderr persisted for a run (under `<base>/logs/<run_id>.log`), or `null` when no log was written. The 16KB-truncated `text` on the run record is the agent's *final message*; the log preserves the *whole* stream so a driver can inspect what the agent actually did (esp. on a failure). |
| `list_workflows()` | List declarative workflow recipes found in `<repo>/workflows/`. Returns `{workflows, errors, workspace}` — malformed recipe files land in `errors` (filename → message). |
| `run_workflow(name, inputs?)` | Run a workflow recipe; integration is gated off by default. |
| `memory_query(query)` | Recall a Marshal Recall memory snippet from the workspace's past runs (empty-handed message when memory is disabled or nothing matches). |
| `memory_add(text, tags?)` | Store a freeform note into the workspace's shared memory graph, recallable via `memory_query`. |
| `memory_stats()` | Memory configuration + data paths for a workspace (works without Cognee installed). |

## Use it as a CLI

```bash
marshal doctor             # preflight: check the setup is ready to run agents
marshal backends           # list backends and availability
marshal models             # list the optional `models:` catalog from fleet.config.yaml
marshal run --goal "…"     # run a task on a client (or ad-hoc by --backend + --model); blocks until done
marshal spawn --goal "…"   # start a task in the background; returns its RUNNING record at once
marshal status             # list fleet runs
marshal logs <run_id>      # print the persisted stdout/stderr for one run (full, not truncated)
marshal clean              # tear down finished runs' worktrees + branches (--scope/--dry-run/--older-than)
marshal usage              # per-provider usage summary (--window day|week|month|all, --json)
marshal workflows          # list + validate workflow recipes against the config
marshal workspace list     # show the workspace registry
marshal workspace add <name> [path]  # register a repo (scaffolds fleet.config.yaml; path defaults to cwd)
marshal workspace remove <name>      # drop a workspace from the registry
marshal memory query "…"   # recall from Marshal Recall's memory for this repo
marshal memory add "…"     # store a freeform note (--tags a,b) in this repo's memory
marshal memory stats       # show memory config + data paths (--json)
marshal memory improve     # run memify on this repo's memory dataset
marshal memory forget      # forget this repo's memory dataset (--all wipes every dataset)
marshal mcp                # run the MCP server over stdio
```

`usage`, `status`, `logs`, and `models` accept `--repo` (default: `$MARSHAL_REPO` or cwd) to target a
repo without the MCP workspace registry. `run`/`spawn` accept `--repo`, `--config`, `--client` (or
ad-hoc `--backend` + `--model`), and `--duration` (preset or seconds).

### `marshal usage`

`marshal usage` rolls up the immutable `usage/events.jsonl` ledger into a human-friendly table with
columns `name · runs · succeeded · cost_usd · cost split · input_tokens · output_tokens ·
cache_read_tokens`, printed for `by_backend`, `by_client`, `by_model`, and `by_backend_model` (the
compound `<backend>/<model>` breakdown - useful when one backend runs multiple models). The token
columns make the previously-hidden per-client/model/cache-read spend visible; the cost split
collapses the native / admin-api / estimated zeros so a row stays readable.

Use `--window` to scope to a rolling time window - `day` (last 24h), `week` (7d), `month` (30d), or
`all` (default; the full ledger). The CLI has no server reference, so `day` is the rolling session
equivalent; the MCP `usage` tool's `window="session"` maps to the server's actual `session_start`
timestamp. With `--json` the existing `totals / by_backend / by_client / by_model` shape is
preserved (the test that pins it still passes); the response adds `by_backend_model`, the resolved
`window`, and the `since` timestamp used to filter.

Add `--config fleet.config.yaml` to also surface any advisory `budgets:` declared there. The
human output gets a `budgets` table with columns `scope · window · spent · limit · remaining`
(aligned via `_align_rows`); the JSON output adds a `budgets` list. No `budgets:` configured =
no `budgets` section / key (the "no behavior change" contract for users who don't opt in).
**Budgets are advisory only**: a cap that has been met never blocks a run, just prints a stderr
warning at the start of the next run on that scope. Budgets are also only meaningful for
backends that report cost - subscription / unknown-cost backends report `$0`, so a $ cap on them
never triggers and reads `$0.00` spent (no fake percentage, no fabricated "remaining").

### `marshal logs`

`marshal logs <run_id>` prints the full raw stdout/stderr that an agent emitted on a run - the
whole stream, NOT the 16KB-truncated `text` on the run record. The 16KB cap is fine for the
agent's *final message* (the summary text the run record shows), but a failure is rarely the last
sentence; the log file preserves everything the subprocess said, so a driver can `grep` the
agent's tool calls, error tracebacks, and stderr noise after the fact. The MCP `get_run_log` tool
returns the same content. Logs are best-effort: a write failure (disk full, permission) is
swallowed in `Fleet._execute` so a run is never broken by the logger, and any existing run predating
log storage has no file (the CLI returns non-zero and the MCP tool returns `log=null` in that
case).

`marshal doctor` also reports a backend's plan tier where the CLI exposes it (e.g. a `plan:cursor`
line with the subscription tier + current model). For every config key see [`config.md`](config.md).

## Use it as a library

```python
from pathlib import Path
from marshal_engine.config import load_config
from marshal_engine.service import MarshalService

service = MarshalService(Path("."), load_config("fleet.config.yaml"))
record = service.run_agent("implementer", "Add a docstring to hello()")
print(record.status, record.cost_usd, record.worktree)
print(service.usage()["totals"])
```

Each run lands in its own git worktree under `.marshal/worktrees/`, with state in
`.marshal/runs/<run_id>.json` (one file per run), usage in `.marshal/usage/`, and the **full raw
stdout/stderr** in `.marshal/logs/<run_id>.log` (so a driver can `marshal logs <run_id>` to
inspect what the agent actually did — esp. on a failure, where the 16KB-truncated `text` on the
run record is rarely enough).

## Collect and integrate a run

A run's work stays isolated in its worktree until you explicitly merge it back. Review it first,
then integrate:

```python
collected = service.collect_run(record.run_id)
print(collected.changed_files)        # what the agent touched
print(collected.diff)                 # full diff, including new files

result = service.integrate(record.run_id, cleanup=True)
if result.status == "conflict":
    print("resolve these:", result.conflicts)   # merge was aborted; repo left clean
else:
    print(result.status, "->", result.merged_into)  # "merged" (or "empty" if nothing changed)
```

`collect_run` is read-only. `integrate` commits the worktree's changes onto its
`marshal/<run_id>` branch and merges that into the branch you currently have checked out; a
conflict is reported and the merge aborted so you resolve it deliberately. `cleanup=True` removes
the worktree after a successful merge.

## Run a workflow

When you orchestrate the same shape of work repeatedly - fan a task out to a few clients, collect
their diffs, then merge the good ones - capture it as a **workflow**: a declarative YAML recipe in
`<repo>/workflows/`. Marshal runs it by sequencing the very primitives above (`run_many` /
`run_agent` / `collect_run` / `integrate`) in the declared order. It adds **no new execution path**,
so every run still flows through the safe fleet loop and worktree isolation.

```yaml
# workflows/review.yaml
name: review
description: Review a target across two clients and surface diffs to merge.
inputs: [target]               # values passed at run time; referenced as {target} in goals
phases:
  - name: review
    run: fan_out               # → run_many across the listed clients, one shared task_id
    clients: [reviewer-a, reviewer-b]
    goal: "Review {target} for correctness bugs and missing tests; apply scoped fixes."
  - run: collect               # → collect_run for each preceding run (read-only)
  - run: integrate             # auto: false (default) → lists candidates, merges nothing
```

Phase kinds: `fan_out` (needs `clients` + `goal`), `agent` (a single `client` + `goal`), `collect`,
and `integrate`. A `collect`/`integrate` phase acts on the most recent preceding generative phase by
default, or names an earlier one with `from_phase`. Goal templates may reference only declared
`inputs`.

**Integration is gated off by default.** An `integrate` phase with `auto: false` (the default) never
calls `integrate` - it lists the succeeded runs as candidates, one `next_actions` line each, and the
result status is `awaiting_review`. You read the collected diffs, then `integrate` the good runs
yourself. Set `auto: true` only when you want the workflow to merge succeeded runs unattended.

Discover and validate recipes (every client name is checked against your config, fail-fast):

```bash
marshal workflows           # human-readable; add --json for machine output
```

Then run one from your driver over MCP:

```text
run_workflow("review", {"target": "src/foo.py"})
```

It returns each phase's run ids, the collected diffs, a rolled-up `status`
(`completed` / `awaiting_review` / `error`), and `next_actions`. The `marshal-workflow` Skill is the
driver's playbook for authoring and running them; starter templates live in `examples/workflows/`.

## Where things land

```
.marshal/
├── worktrees/<task>.<backend>.<id>/   # isolated checkout per run (kept until you integrate)
├── runs/<run_id>.json            # one file per run: status + cost (single writer per run)
├── logs/<run_id>.log             # one file per run: full raw stdout/stderr (success or failure)
└── usage/
    ├── events.jsonl              # one line per run
    └── summary.json              # rolled-up totals
```

## Backend notes

| Backend | Edits | Usage in output | Notes |
|---------|-------|-----------------|-------|
| OpenCode | yes | yes (tokens + cost) | Force `opencode-go/*` for the Go sub; via EastRouter (`eastrouter/<id>`) the CLI can't price a custom provider, so cost is `unavailable`. |
| Cursor | yes | no | Tokens/cost only via Team/Enterprise Admin API. |
| Codex | yes | best-effort | `workspace-write` sandbox for safe-edit; real cost via EastRouter `usage_api` (`admin-api`), else estimated/unavailable. |
| Command Code | yes | no | Hosted account; `-p` reports no tokens/cost, so usage is `unavailable` (spend in its dashboard). `plan` for read-only; safe-edit maps to `--yolo` (headless auto-accept blocks writes). |
| Antigravity | yes | no | Worktree writes work (the run's worktree is pre-registered in trustedWorkspaces and passed via `--add-dir`); supports `safe-edit`/`yolo` (no `read-only`). |
| Claude Code | yes | yes (tokens + cost) | `acceptEdits` for safe-edit; cost is native (no estimation). |

See [`design.md`](design.md) for per-backend invocation details and [`status.md`](status.md)
for what's verified.
