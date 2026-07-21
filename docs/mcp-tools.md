# MCP tool reference

The Marshal MCP server (`marshal mcp`) exposes **24 tools** (counted from `@app.tool` in
`mcp_server.py`). Every action/query tool accepts an optional `workspace` parameter (defaults to
`"default"`). Run-handle tools (`get_run`, `collect_run`, `cancel_run`, `integrate`, …) resolve the
owning workspace by scanning each repo's ledger, with an optional `workspace` hint to skip the scan.

Results from workspace-scoped tools include a top-level `"workspace"` field naming the repo they came
from.

## Workspace

### `list_workspaces`

List repos this server can target.

| Parameter | Type | Default | Description |
|-----------|------|---------|-------------|
| *(none)* | | | |

**Returns:** `list[dict]` — one row per workspace:

| Field | Type | Description |
|-------|------|-------------|
| `name` | string | Workspace name (`default` for `MARSHAL_REPO`). |
| `path` | string | Absolute repo root. |
| `config_path` | string | Path to `fleet.config.yaml`. |
| `configured` | bool | Whether the config file exists. |
| `client_count` | int | Number of declared clients (0 if missing/broken config). |
| `default` | bool | True for the default workspace. |

### `add_workspace`

Register a repo in `~/.marshal/workspaces.yaml` (hot-reloaded; no reconnect needed).

| Parameter | Type | Default | Description |
|-----------|------|---------|-------------|
| `name` | string | *(required)* | Short name (`[A-Za-z0-9._-]+`, not `default`). |
| `path` | string | *(required)* | Absolute path to an existing directory. |
| `scaffold` | bool | `false` | Drop a starter `fleet.config.yaml` if the repo has none. |

**Returns:** `{ name, path, config_path, scaffolded }`.

## Diagnose

### `list_clients`

| Parameter | Type | Default | Description |
|-----------|------|---------|-------------|
| `workspace` | string \| null | `null` | Target workspace. |

**Returns:** `{ clients, driver_context, workspace }`

- `clients`: `[{ name, backend, model, permission }]`
- `driver_context`: string \| null — from `fleet.config.yaml` `context.driver`

### `list_models`

| Parameter | Type | Default | Description |
|-----------|------|---------|-------------|
| `workspace` | string \| null | `null` | Target workspace. |

**Returns:** `{ models, driver_context, workspace }`

- `models`: `[{ id, backends, cost, quota_type, notes }]` — the optional `models:` catalog (metadata only)

### `doctor`

Preflight the selected workspace (toolchain, repo, config, per-backend CLI + auth). Read-only.

| Parameter | Type | Default | Description |
|-----------|------|---------|-------------|
| `workspace` | string \| null | `null` | Target workspace. |

**Returns:** `{ checks, fails, warns, ok, workspace }`

- `checks`: `[{ name, status, detail, fix }]` — `status` is `ok`, `warn`, or `fail`
- `ok`: true when `fails == 0`

## Run

### `run_agent`

Run a task in an isolated worktree; **blocks** until finished.

| Parameter | Type | Default | Description |
|-----------|------|---------|-------------|
| `goal` | string | *(required)* | Natural-language task. |
| `client` | string \| null | `null` | Configured client name. Omit for ad-hoc spawn (set `backend`). |
| `task_id` | string \| null | `null` | Grouping id for `report()`. |
| `context_files` | list[string] \| null | `null` | Repo-relative paths injected into the prompt. |
| `base_branch` | string \| null | `null` | Branch to base the worktree on (default: current HEAD). Use after `commit_run` to chain work. |
| `model` | string \| null | `null` | Override the client's resolved model, or the model for an ad-hoc spawn. |
| `backend` | string \| null | `null` | Bare backend for ad-hoc spawn (e.g. `opencode`). Ignored when `client` is set. |
| `duration` | string \| int \| null | `null` | Per-spawn timeout override (preset name or positive seconds). |
| `workspace` | string \| null | `null` | Target workspace. |

**Returns:** `RunRecord` + `workspace` (see [Run record](#run-record)).

### `spawn`

Same parameters as `run_agent`. Returns immediately with a `RUNNING` record; poll `get_run` /
`status`, cancel with `cancel_run`.

### `run_many`

Run several jobs in parallel, each in its own worktree. Jobs may target **different registered
workspaces** via an optional per-job `workspace`; the call-level `workspace` is the default for jobs
that omit it. Mixed batches share one `max_concurrency` cap (and the process-wide `run_gate` when
multi-repo is active). Each workspace keeps its own config, worktrees, and usage ledger — there is
no cross-workspace ledger merge.

| Parameter | Type | Default | Description |
|-----------|------|---------|-------------|
| `jobs` | list[Job] | *(required)* | Each job: `{ client?, goal, task_id?, context_files?, model?, backend?, duration?, workspace? }`. Omit `client` and set `backend` for ad-hoc spawns. Per-job `workspace` overrides the call-level default. |
| `max_concurrency` | int | `4` | Max jobs running at once across the whole batch (all workspaces). |
| `workspace` | string \| null | `null` | Default workspace for jobs that omit per-job `workspace`. |

**Returns:** `list[RunRecord + workspace]` — each record tagged with the workspace it actually ran in.

**Errors:** unknown per-job / call-level workspace names fail fast before any agent starts (same as
other workspace-scoped tools). Invalid job specs (unknown client, bad `duration`, …) likewise fail
fast before the batch begins.

### `benchmark`

Race the same goal through several configured clients.

| Parameter | Type | Default | Description |
|-----------|------|---------|-------------|
| `goal` | string | *(required)* | Task to run on each client. |
| `clients` | list[string] | *(required)* | Client names to compare. |
| `task_id` | string \| null | `null` | Grouping id (auto-generated if omitted). |
| `max_concurrency` | int | `4` | Max clients running at once. |
| `workspace` | string \| null | `null` | Target workspace. |

**Returns:** `BenchmarkResult` + `workspace`:

| Field | Type | Description |
|-------|------|-------------|
| `task_id` | string | Shared grouping key. |
| `goal` | string | The goal that was run. |
| `strategies` | list | Per-client rows: `{ run_id, client, backend, model, status, cost_usd, source, duration_ms, input_tokens, output_tokens }` |
| `cheapest` | string \| null | Winning client among succeeded runs with known cost. |
| `fastest` | string \| null | Winning client among succeeded runs with duration > 0. |

### `list_workflows`

| Parameter | Type | Default | Description |
|-----------|------|---------|-------------|
| `workspace` | string \| null | `null` | Target workspace. |

**Returns:** `{ workflows, errors, workspace }`

- `workflows`: `[{ name, description, inputs, phases: [{ name, run }] }]`
- `errors`: `{ "<filename>": "<message>" }` — malformed recipe files

### `run_workflow`

| Parameter | Type | Default | Description |
|-----------|------|---------|-------------|
| `name` | string | *(required)* | Workflow recipe name (from `list_workflows`). |
| `inputs` | dict \| null | `null` | Inputs the recipe declares. |
| `workspace` | string \| null | `null` | Target workspace. |

**Returns:** `WorkflowResult` + `workspace`:

| Field | Type | Description |
|-------|------|-------------|
| `name` | string | Workflow name. |
| `workflow_run_id` | string | Unique id for this execution. |
| `inputs` | dict | Resolved inputs. |
| `phases` | list | Per-phase `{ name, run, run_ids, records, collected, integrations, skipped, notes }` |
| `status` | `"completed"` \| `"awaiting_review"` \| `"error"` | |
| `next_actions` | list[string] | Suggested follow-ups (e.g. runs to review/integrate). |

## Inspect

### `get_run`

| Parameter | Type | Default | Description |
|-----------|------|---------|-------------|
| `run_id` | string | *(required)* | Run id from `run_agent` / `spawn` / `run_many`. |
| `workspace` | string \| null | `null` | Hint to skip ledger scan. |

**Returns:** `RunRecord` + `workspace`, or `null` if not found.

### `get_run_log`

Full persisted stdout/stderr for a run (not the truncated `text` on the record).

| Parameter | Type | Default | Description |
|-----------|------|---------|-------------|
| `run_id` | string | *(required)* | Run id. |
| `workspace` | string \| null | `null` | Workspace hint. |

**Returns:** `{ run_id, log, workspace }` — `log` is string \| null.

### `collect_run`

Read-only diff collection; nothing is merged.

| Parameter | Type | Default | Description |
|-----------|------|---------|-------------|
| `run_id` | string | *(required)* | Run id. |
| `workspace` | string \| null | `null` | Workspace hint. |

**Returns:** `CollectResult` + `workspace`:

| Field | Type | Description |
|-------|------|-------------|
| `run_id` | string | |
| `branch` | string \| null | Run's worktree branch. |
| `worktree` | string \| null | Worktree path. |
| `changed_files` | list[string] | |
| `diff` | string | Unified diff. |

### `status`

| Parameter | Type | Default | Description |
|-----------|------|---------|-------------|
| `workspace` | string \| null | `null` | Scope to one workspace; omit to list **all** workspaces. |

**Returns:** `list[RunRecord + workspace]`.

### `cancel_run`

SIGTERM the agent process group.

| Parameter | Type | Default | Description |
|-----------|------|---------|-------------|
| `run_id` | string | *(required)* | Run id. |
| `workspace` | string \| null | `null` | Workspace hint. |

**Returns:** updated `RunRecord` + `workspace`.

## Integrate

### `commit_run`

Freeze a finished run's work as a commit on its **own** branch (driver branch untouched). Use before
chaining: `commit_run(A)` then `spawn(B, base_branch=A's branch)`.

| Parameter | Type | Default | Description |
|-----------|------|---------|-------------|
| `run_id` | string | *(required)* | Run id. |
| `message` | string \| null | `null` | Commit message (default: `marshal: <run_id>`). |
| `workspace` | string \| null | `null` | Workspace hint. |

**Returns:** `CommitResult` + `workspace`:

| Field | Type | Description |
|-------|------|-------------|
| `run_id` | string | |
| `status` | `"committed"` \| `"clean"` \| `"blocked"` \| `"error"` | |
| `branch` | string \| null | Branch to base dependent runs on. |
| `commit` | string \| null | Branch tip after commit. |
| `message` | string | Error detail when `status` is `error`. |

### `integrate`

Merge a run's worktree branch into the workspace's current branch. Review with `collect_run` first.

| Parameter | Type | Default | Description |
|-----------|------|---------|-------------|
| `run_id` | string | *(required)* | Run id. |
| `cleanup` | bool | `false` | Remove the worktree after a successful merge. |
| `workspace` | string \| null | `null` | Workspace hint. |

**Returns:** `IntegrateResult` + `workspace`:

| Field | Type | Description |
|-------|------|-------------|
| `run_id` | string | |
| `status` | `"merged"` \| `"conflict"` \| `"blocked"` \| `"empty"` \| `"error"` | |
| `branch` | string \| null | Source branch. |
| `merged_into` | string \| null | Target branch. |
| `changed_files` | list[string] | |
| `conflicts` | list[string] | |
| `commit` | string \| null | Merge commit hash. |
| `message` | string | Detail on failure. |

### `clean`

Tear down finished runs' worktrees and branches. The usage ledger and run-state JSON files are kept.

| Parameter | Type | Default | Description |
|-----------|------|---------|-------------|
| `scope` | `"merged"` \| `"finished"` \| `"all"` | `"finished"` | `merged` = integrated only; `finished` = merged + failed/timed_out/cancelled/empty/verify_failed; `all` = every terminal run (destructive). |
| `run_ids` | list[string] \| null | `null` | Clean exactly these ids (ignores `older_than_hours`). |
| `older_than_hours` | float \| null | `null` | Only clean runs ended at least this many hours ago. |
| `dry_run` | bool | `false` | Preview without removing anything. |
| `workspace` | string \| null | `null` | Target workspace. |

**Returns:** `CleanResult` + `workspace`:

| Field | Type | Description |
|-------|------|-------------|
| `removed` | list[string] | Run ids whose worktrees/branches were removed. |
| `orphans_removed` | list[string] | Worktree dirs with no readable run record (scope-mode only). |
| `skipped` | list | `[{ run_id, reason }]` — e.g. still running. |
| `errors` | list | `[{ run_id, error }]` |
| `dry_run` | bool | Echo of the request flag. |

## Measure

### `report`

Derive a strategy comparison for a past benchmark `task_id` from the ledger (read-only).

| Parameter | Type | Default | Description |
|-----------|------|---------|-------------|
| `task_id` | string | *(required)* | The benchmark grouping key. |
| `workspace` | string \| null | `null` | Workspace the benchmark ran in. |

**Returns:** `BenchmarkResult` + `workspace` (same shape as `benchmark`).

### `usage`

Per-provider usage summary for one workspace.

| Parameter | Type | Default | Description |
|-----------|------|---------|-------------|
| `window` | `"session"` \| `"week"` \| `"month"` \| `"all"` | `"all"` | Time window (`session` = since MCP server started). |
| `workspace` | string \| null | `null` | Target workspace. |

**Returns:**

| Field | Type | Description |
|-------|------|-------------|
| `window` | string | Resolved window name. |
| `since` | string \| null | ISO-8601 start (null for `all`). |
| `totals` | Bucket | Grand totals. |
| `by_backend` | dict | Per-backend buckets. |
| `by_client` | dict | Per-client buckets. |
| `by_model` | dict | Per-model buckets. |
| `by_backend_model` | dict | Keys like `opencode/<model>`. |
| `budgets` | list \| omitted | Present when `fleet.config.yaml` declares `budgets:`: `[{ scope, window, spent_usd, limit_usd, remaining_usd, enforce }]`. |
| `workspace` | string | |

Each **Bucket**: `{ runs, succeeded, cost_usd, cost_native, cost_admin_api, cost_estimated, input_tokens, output_tokens, cache_read_tokens, cost_per_run, cost_per_succeeded }`.

## Run record

`RunRecord` fields returned by `run_agent`, `spawn`, `get_run`, `status`, and `cancel_run`:

| Field | Type | Description |
|-------|------|-------------|
| `run_id` | string | Unique id. |
| `task_id` | string | Grouping id. |
| `backend` | string | Backend that ran. |
| `status` | string | `queued` \| `running` \| `succeeded` \| `empty` \| `failed` \| `timed_out` \| `cancelled` \| `verify_failed` |
| `client` | string \| null | Client name (null for ad-hoc spawns). |
| `model` | string \| null | Model used. |
| `worktree` | string \| null | Worktree path. |
| `branch` | string \| null | Worktree branch. |
| `cost_usd` | float | Recorded cost. |
| `input_tokens` | int | |
| `output_tokens` | int | |
| `duration_ms` | int | |
| `source` | string \| null | Cost provenance (`native`, `admin-api`, `estimated`, …). |
| `text` | string | Agent's final message (truncated). |
| `started_at` | string \| null | ISO-8601. |
| `ended_at` | string \| null | ISO-8601. |
| `error` | string \| null | Failure detail. |
| `merged_into` | string \| null | Branch after integrate. |
| `commit` | string \| null | Branch tip after `commit_run`. |
| `pid` | int \| null | Agent subprocess pid (while running). |
| `attempts` | int | Backend invocations (> 1 means transient retries). |
| `verify_passed` | bool \| null | `null` = no gate ran; `false` with `verify_failed` status. |
| `verify_output` | string | Tail of verify command output. |

Only `succeeded` runs are integration candidates. `empty` ran clean but produced no work — do not
integrate. `verify_failed` produced work but the repo's `verify:` gate rejected it — review the
diff and `verify_output` before deciding.
