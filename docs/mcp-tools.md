# MCP tool reference

The Marshal MCP server (`marshal mcp`) exposes the tools documented below (the normative list is
`@app.tool` in `mcp_server.py`). **New to Marshal? Call `marshal_quickstart` first** — it names the
four-step loop and says which tool to pick when several look alike. Workspace-scoped tools accept an optional `workspace` parameter (defaults to `"default"`);
the global ones — `marshal_quickstart`, `list_workspaces`, `add_workspace` — do not. Run-handle tools (`get_run`, `collect_run`, `cancel_run`, `integrate`, …) resolve the
owning workspace by scanning each repo's ledger, with an optional `workspace` hint to skip the scan.

Results from workspace-scoped tools include a top-level `"workspace"` field naming the repo they came
from.

## Orientation

### `marshal_quickstart`

The canonical loop and the decision boundary between the lookalike tools. No parameters.

**Returns:** `{ what_marshal_is, non_code_runs, the_loop, which_run_tool, which_status_tool, safety, multi_repo }`.

`what_marshal_is` leads with **fleet primitive**: parallel sub-agents in isolated worktrees; a run's
product may be a **DIFF or TEXT** (both first-class). Names write and read-and-reason uses
(implement, research, review, audit, summarise) and that Marshal runs the agents while the driver
decides. `non_code_runs` states that a text-only run is `exited_clean` with value in `text`, and
that `empty` is an outcome (exited 0, neither text nor file changes), not a fault.

Exists because a driver facing ~20 tools has no stated ordering: several do near-identical things
(`run_agent` / `spawn` / `run_many` / `run_workflow`; `status` / `get_run` / `collect_run` /
`get_run_log`) and the blocking-vs-async split is not visible in the names. A driver reads tool
descriptions, not this file — so the orientation lives where it will actually be read.

## Workspace

### `list_workspaces`

List repos this server can target.

| Parameter | Type | Default | Description |
|-----------|------|---------|-------------|
| *(none)* | | | |

**Returns:** `list[dict]` — one row per workspace.

`ready` is a claim about *configuration*, not about the machine: it does not probe whether those
clients' backend CLIs are installed or authenticated. That is `doctor`, which costs subprocesses
this listing deliberately avoids.

| Field | Type | Description |
|-------|------|-------------|
| `name` | string | Workspace name (`default` for `MARSHAL_REPO`). |
| `path` | string | Absolute repo root. |
| `config_path` | string | Path to `fleet.config.yaml`. |
| `configured` | bool | Whether the config **file exists** — nothing more. Not a readiness signal; use `ready`. |
| `client_count` | int | Number of declared clients (0 if missing/broken config). |
| `ready` | bool | Whether this workspace can actually take a run: a config that loads and declares at least one client. This is the field to branch on. |
| `last_activity_at` | string \| null | ISO-8601 UTC of the most recent write to this workspace's run ledger — how you find the repo you were just working in when a dozen are registered. `null` means no runs recorded. Named for what it measures: a record's last write (a run starting, updating, or finishing), not a start time. |
| `ready_reason` | string \| null | Why `ready` is false — `no config file at <path>`, `config does not load: <error>`, or `config declares no clients`. `null` when ready. |
| `default` | bool | True for the default workspace. |

### `add_workspace`

Register a repo in `~/.marshal/workspaces.yaml` (hot-reloaded; no reconnect needed).

**Disabled by default.** The tool stays discoverable, but every call is refused unless the server
process was started with `MARSHAL_ALLOW_MCP_WORKSPACE_REGISTRATION=1` (exact value; captured once
at server build — see `docs/config.md` for the semantics). The refusal happens before any path
validation, registry write, or scaffolding, and its message points at the operator alternative:
`marshal workspace add <name> <path>`, which hot-reloads into the running server the same way.
Enabling the opt-in lets the MCP driver register **any existing directory on the host** as a
target repo — it is not a path allowlist. See `SECURITY.md` before turning it on.

| Parameter | Type | Default | Description |
|-----------|------|---------|-------------|
| `name` | string | *(required)* | Short name (`[A-Za-z0-9._-]+`, not `default`). |
| `path` | string | *(required)* | Absolute path to an existing directory. |
| `scaffold` | bool | `false` | Drop a starter `fleet.config.yaml` if the repo has none. |

**Returns:** `{ name, path, config_path, scaffolded }`.

**Errors:** refused with the policy message above when the opt-in is not set.

## Diagnose

### `list_clients`

| Parameter | Type | Default | Description |
|-----------|------|---------|-------------|
| `workspace` | string \| null | `null` | Target workspace. |

**Returns:** `{ clients, skipped, driver_context, workspace }`

- `clients`: `[{ name, backend, model, permission, permission_fidelity }]`
- `skipped`: `[{ name, backend, reason }]` — clients declared in the config that are **not usable
  right now**, and why (backend CLI absent, or a backend name that does not exist). Previously
  these were filtered out silently: Marshal warned on stderr, which an MCP driver never sees, so
  the client just vanished from the list with no error.
- `permission_fidelity`: `enforced-denies` \| `boundary-only` \| `unrestricted` — resolved from the client's `(backend, permission)` pair (see `docs/design.md` §5 / `SECURITY.md`). `yolo` is always `unrestricted`; `safe-edit` / `read-only` inherit the backend's safe-edit capability. Distinct from doctor's `permission:<backend>`, which reports backend safe-edit capability only.
- `driver_context`: string \| null — from `fleet.config.yaml` `context.driver`

### `list_models`

| Parameter | Type | Default | Description |
|-----------|------|---------|-------------|
| `workspace` | string \| null | `null` | Target workspace. |

**Returns:** `{ models, backend_models, driver_context, workspace }`

- `models`: `[{ id, backends, cost, quota_type, notes }]` — the optional `models:` catalog (metadata only)
- `backend_models`: `{ backend: [model_id] | null }` — model ids each configured backend's CLI
  reports, populated **only when no `models:` catalog is configured**. `null` for a backend means
  it exposes no way to ask, which is not the same as "it has no models". Kept separate from
  `models` on purpose: the catalog is curated metadata a human wrote, this is whatever a CLI said
  just now — and neither drives routing, which clients own.

### `doctor`

Preflight the selected workspace (toolchain, repo, config, per-backend CLI + auth, and static
`permission:<backend>` fidelity checks). Read-only. `permission:*` is `ok` for `enforced-denies`
and `warn` for `boundary-only` (never a failure); it appears even when the CLI probe fails.

A `quota:<backend>` **warn** appears when recent runs on that backend failed for billing or quota
reasons, with the count and the latest error. It is derived from this workspace's run ledger, not
from a provider API — so it reports what actually happened rather than predicting. **Its absence is
not a quota clearance:** doctor cannot see provider balances, and a backend that is installed,
authenticated, and out of credit still passes every other check.

| Parameter | Type | Default | Description |
|-----------|------|---------|-------------|
| `workspace` | string \| null | `null` | Target workspace. |

**Returns:** `{ checks, fails, warns, ok, workspace }`

- `checks`: `[{ name, status, detail, fix }]` — `status` is `ok`, `warn`, or `fail`
- `ok`: true when `fails == 0`

## Run

### `run_agent`

Delegate a goal to a worker agent in an isolated worktree; **blocks** until finished. Product may
be a diff or text — both first-class (see `collect_run`).

| Parameter | Type | Default | Description |
|-----------|------|---------|-------------|
| `goal` | string | *(required)* | Natural-language task. |
| `client` | string \| null | `null` | Configured client name. Omit for ad-hoc spawn (set `backend`). |
| `task_id` | string \| null | `null` | Grouping id for `report()`. Must be a safe path segment (`[A-Za-z0-9._-]`, no leading `.`/`-`; see `SECURITY.md`); hostile values fail closed before any worktree is created. |
| `task_kind` | string \| null | `null` | Free-text tag for the kind of work (`refactor`, `bugfix`, `docs`, …). Caller taxonomy — not a closed enum. Same safe-token rules as `task_id`. Stamped on the usage event (`task_kind`) for a future routing layer; never invents a ranking. |
| `context_files` | list[string] \| null | `null` | Repo-relative paths injected into the prompt. Each must be **relative to the repo root** and exist **in the worktree**, which holds tracked files only. Absolute paths and `..` are refused (the worktree is the isolation boundary); a gitignored or untracked path fails the spawn rather than handing the agent a file it cannot open. |
| `read_paths` | list[string] \| null | `null` | Read-only escape hatch for material **outside** the worktree. Absolute paths, or paths relative to the **driver's** repo root, are copied into `<worktree>/.marshal-context/<basename>` (files 0o444, directories 0o555) and git-excluded so they never appear in the run's diff. The worker prompt is told reference material is under `.marshal-context/`. Secret-shaped names (`.env*`, `*.pem`, `id_rsa*`, `id_ed25519*`) and anything under a `.ssh` directory are refused on the declared path **and every descendant** that would be copied. Symlinks **inside** a declared tree are refused (a link either smuggles host content when dereferenced or escapes when preserved); a symlinked declared root is resolved first, then validated/copied from the real path. Only regular files and directories are accepted (FIFOs/sockets/devices are refused so provisioning cannot hang before a run timeout exists). Policy is enforced during the fd-relative copy walk (validation at point of use): every `scandir` entry is re-checked, and each file/directory's `(st_dev, st_ino)` from the classifying `lstat` must match `fstat` of the opened fd, refusing a swap to a different file or directory (identity is a secondary check — a delete-then-recreate can reuse an inode, so the per-entry checks are what contain a swapped tree). Destination `.marshal-context` must be absent or a plain directory (a tracked symlink or non-dir is refused; never `resolve()` through it); per-entry destinations never follow symlinks. The up-front scan only names offenders early — it is not the security boundary. Copies also open fail-closed (`O_RDONLY|O_NOFOLLOW|O_NONBLOCK` + `fstat` + identity for files; `O_NOFOLLOW|O_DIRECTORY` + identity for directory descent). A missing or refused path fails the spawn. Surfaced on the run record so a reviewer can see the run saw more than its worktree. |
| `base_branch` | string \| null | `null` | Branch to base the worktree on (default: current HEAD). Use after `commit_run` to chain work. |
| `model` | string \| null | `null` | Override the client's resolved model, or the model for an ad-hoc spawn. |
| `backend` | string \| null | `null` | Bare backend for ad-hoc spawn (e.g. `opencode`). **Mutually exclusive with `client`** — passing both is an error, not a precedence rule. To use a configured client with a different model, pass `client` + `model`. |
| `duration` | string \| int \| null | `null` | Per-spawn timeout override (preset name or positive seconds). |
| `workspace` | string \| null | `null` | Target workspace. |

**Returns:** `RunRecord` + `workspace` (see [Run record](#run-record)).

### `spawn`

Same parameters as `run_agent`. Same delegation primitive (product may be a diff or text). Returns
immediately with a `RUNNING` record; poll `get_run` / `status`, cancel with `cancel_run`.

### `run_many`

Delegate several goals in parallel, each to its own worktree. Jobs may target **different registered
workspaces** via an optional per-job `workspace`; the call-level `workspace` is the default for jobs
that omit it. Each job's product may be a diff or text. Optional per-job **`then`** runs a follow-up
in the **same worker** as soon as that job's primary reaches a terminal state — it does **not** wait
for sibling jobs (unlike a barrier). Mixed batches share one `max_concurrency` cap (and the
process-wide `run_gate` when multi-repo is active). Each workspace keeps its own config, worktrees,
and usage ledger — there is no cross-workspace ledger merge. Budgets, `EnforceBudgetGate`, and
session clocks are also **per-workspace**; concurrency is the only shared limiter.

| Parameter | Type | Default | Description |
|-----------|------|---------|-------------|
| `jobs` | list[Job] | *(required)* | Each job: `{ client?, goal, task_id?, task_kind?, context_files?, read_paths?, model?, backend?, duration?, workspace?, then? }`. Omit `client` and set `backend` for ad-hoc spawns. Per-job `workspace` overrides the call-level default. `then` uses the same field set (no nested `then` or `workspace`). |
| `max_concurrency` | int | `4` | Max **workers** (chains) running at once across the whole batch (all workspaces). Each worker runs one job's primary, then its optional `then` back-to-back, so at most `max_concurrency` agent processes run concurrently. |
| `workspace` | string \| null | `null` | Default workspace for jobs that omit per-job `workspace`. |

**Returns:** `list[RunManyJobResult + workspace]` — one object per input job, in input order. Each
object is tagged with the workspace the primary ran in:

| Field | Type | Description |
|-------|------|-------------|
| `primary` | RunRecord | The job's primary run. |
| `then` | RunRecord \| null | The follow-up run, when it ran. Absent when skipped. |
| `then_skipped` | string \| null | Why `then` did not run (primary failed, no branch, primary's branch has no commits beyond its base, `commit_run` blocked, …). |
| `workspace` | string | Workspace the chain ran in. |

**Errors:** unknown per-job / call-level workspace names fail fast before any agent starts (same as
other workspace-scoped tools). Invalid job specs (unknown client, bad `duration`, bad `then`, …)
likewise fail fast before the batch begins.

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

### `list_teams`

| Parameter | Type | Default | Description |
|-----------|------|---------|-------------|
| `workspace` | string \| null | `null` | Target workspace. |

**Returns:** `{ teams, errors, workspace }`

- `teams`: `[{ name, description, target, roles: [{ name, client }], decision }]`
- `errors`: `{ "<filename>": "<message>" }` — malformed team files

### `run_team`

Runs a panel of **independent, read-only** reviewers over one subject. Each role holds one lens,
reviews the same subject in parallel isolation, and writes a report. Every role's client must be
configured `permission: read-only` — a team that names a writable client is a config error raised
before any reviewer spawns.

**This tool computes no verdict.** There is no pass/fail, no tally, and no parsing of reviewer
prose: a decision derived from text the reviewed material can influence is not a decision worth
trusting, and judgment belongs to the caller. Read `unified_report` first, then the individual
reports. This tool never integrates.

| Parameter | Type | Default | Description |
|-----------|------|---------|-------------|
| `name` | string | *(required)* | Review team name (from `list_teams`). |
| `target` | `"run"` \| `"plan"` \| `"range"` \| `"audit"` | *(required)* | What is reviewed; must match the team's declared `target`. |
| `run_id` | string \| null | `null` | Run whose diff to review (target `run`). |
| `base` | string \| null | `null` | Base ref (target `range`). Validated — a ref that git would read as an option is refused. |
| `head` | string \| null | `null` | Head ref (target `range`; default `HEAD`). |
| `text` | string \| null | `null` | The plan to review (target `plan`). |
| `paths` | list[string] \| null | `null` | Limit a `range` diff to these paths. Use it on a large change: the subject is truncated at the **tail**, and git orders paths alphabetically, so `src/` and `tests/` are exactly what gets cut. Paths that are empty, start with `-`, or contain newlines are refused. |
| `workspace` | string \| null | `null` | Target workspace. |

**Returns:** `TeamReview` + `workspace`:

| Field | Type | Description |
|-------|------|-------------|
| `name` | string | Team name. |
| `team_run_id` | string | Unique id for this review. |
| `subject` / `subject_summary` | object / string | What was reviewed. |
| `truncated` | bool | True when the subject exceeded the reviewer size cap and was cut (reviews cover only the visible part). |
| `reviews` | list | Per-role `{ role, client, run_id, status, completed, review, report_path, note }`. `review` is the reviewer's full report text; `completed` describes the *process* (ran clean and produced text), never the content. |
| `unified_report` | string | The report to read first: the panel's shape, who reviewed from which lens, every review inline, and who did not report. States no verdict. |
| `unified_report_path` | string \| null | `README.md` inside the report directory. |
| `report_dir` | string \| null | `.marshal/reports/<stamp>-<team>-<id>/`, holding one `<role>.md` per reviewer plus `README.md`. |
| `incomplete_roles` | list[string] | Roles that produced no report (failed, timed out, backend missing). A missing lens — **not** silent approval. |
| `next_actions` | list[string] | Suggested follow-ups. |

## Inspect

### `get_run`

Fetch one run record by id. Status is one of: `exited_clean` | `empty` (exited 0 with neither text
nor file changes — an outcome, not a fault; nothing to integrate) | `failed` | `timed_out` |
`cancelled` | `verify_failed` (had file changes but the workspace's `verify:` gate rejected them —
review the diff and `verify_output` before deciding). Only `exited_clean` runs with a diff are
integration candidates; for text-only work read `text` (or `collect_run`).

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

Collect what a run produced: diff/changed files and/or final text via `produced` (read-only;
nothing is merged). Branch on `produced` (`diff` | `text` | `nothing`).

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
| `changed_files` | list[string] | Paths with uncommitted changes in the worktree. |
| `diff` | string | Unified diff of uncommitted work (working tree vs HEAD). |
| `committed_changed_files` | list[string] | Files changed in commits on the run branch since the run's **own** base (`base_commit`, falling back to `base_branch`, then — for records predating both — the current branch, or the checked-out commit `HEAD` when the repo is in detached HEAD) — deliberately not the currently checked-out branch, which may have moved since the run started. |
| `committed_diff` | string | Unified diff of those commits (`base...branch`). |
| `commit_count` | integer | Number of commits on the run branch not reachable from that base. |
| `produced` | string | `diff` (files changed) \| `text` (no files, but the agent replied) \| `nothing` (neither). **Branch on this** rather than inferring intent from which container is empty — guessing from emptiness is what made research runs read as failures. |
| `text` | string | The agent's final message — populated **only** when `produced == "text"`. When there is a diff, the diff is the artifact and repeating the message would bloat every reply. |

### `status`

| Parameter | Type | Default | Description |
|-----------|------|---------|-------------|
| `workspace` | string \| null | `null` | Scope to one workspace; omit to list **all** workspaces. |
| `limit` | int (1–500) | `50` | Max runs, newest first. |
| `status` | string \| null | `null` | Only runs with this exact status. |
| `task_id` | string \| null | `null` | Only runs with this `task_id`. |
| `since_hours` | float \| null | `null` | Only runs started within N hours. A run whose start time is unreadable is **kept** — a missing timestamp is not evidence it falls outside the window. |
| `full` | bool | `false` | Include `text` and `verify_output`. |

**Returns:** `{ runs, returned, matched, truncated, compact }`, newest first.

**Compact by default.** `text` and `verify_output` are unbounded and dominate a listing's size
(one observed reply was ~395k characters), so they are replaced by `has_text` /
`has_verify_output` flags — a caller must be able to tell "this run produced no message" from
"this view omitted it". Use `get_run` for one run's full text, or `full=true`.

**Never silently capped.** `matched` is the number of runs that passed the filters and `returned`
is how many came back, with `truncated` saying whether anything was dropped. A driver that read a
capped list as the whole ledger would draw exactly the wrong conclusion.

Omitting `workspace` aggregates run *records* across workspaces for visibility; it is **not** a
merged usage/budget view (see `usage`).

### `read_run_file`

Read one file out of a run's worktree — how one agent's output reaches the next.

| Parameter | Type | Default | Description |
|-----------|------|---------|-------------|
| `run_id` | string | *(required)* | The run whose worktree to read from. |
| `path` | string | *(required)* | Path **relative to that run's worktree root**. Absolute paths and `..` are refused — `Path(wt) / "/etc/passwd"` is `/etc/passwd`, so the containment check is the same one `context_files` applies. |
| `workspace` | string \| null | `null` | Workspace hint. |

**Returns:** `{ run_id, path, content, truncated, size_bytes }`.

**Check `truncated`.** Large files are clipped and `size_bytes` reports the real size; acting on a
prefix while believing it is whole is the mistake this flag exists to prevent.

**Which handover do you want?**

- **Read an artifact** (a report, findings, a generated spec) → `read_run_file`, then put the
  content in the next run's `goal`. The next agent reads what the first actually wrote, rather than
  the driver's paraphrase of it.
- **Build on the code** → `commit_run` then `spawn(base_branch=<that run's branch>)`. The next
  worktree is cut from the work itself.

This is a read: it copies nothing and starts nothing, so the driver stays the one deciding what the
next agent sees — which is where that judgement belongs, since the driver is what reviewed the
output.

### `cancel_run`

SIGTERM the agent process group — but only for a **live child of the server process handling the
call**. Signalling goes through an in-process handle that tracks the child from spawn until it is
reaped; the OS cannot recycle a child's pid before its parent reaps it, so within that window the
pid unambiguously belongs to the agent.

- A cancel arriving **before** the pid is known is applied the moment it is, so the agent is stopped
  rather than left running behind an already-terminal record.
- A cancel **after** the child is reaped does not signal at all.
- A run started by a **different (or dead) process** is stamped `cancelled` without a signal, with
  the reason on `error`. The `pid` is kept when something is still alive at that number (so `clean`
  spares the worktree) and cleared only when the process is gone. Guessing at a pid this process
  does not own risks SIGTERM to an unrelated process group. Such an orphaned agent must be ended by
  hand.
- When this process owns the run but **cannot verify** the child's identity (start-time probe fails
  while the pid is still alive), cancel neither signals nor stamps `cancelled`: the record stays
  `running` with an `error` stating the cancel could not be confirmed. Signalling blindly risks
  `killpg` on a recycled pid; claiming `cancelled` would leave a live agent behind a lie.

| Parameter | Type | Default | Description |
|-----------|------|---------|-------------|
| `run_id` | string | *(required)* | Run id. |
| `workspace` | string \| null | `null` | Workspace hint. |

**Returns:** updated `RunRecord` + `workspace`. On the unconfirmed-identity path above, `status`
remains `running` and `error` carries the uncertainty.

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

Merge a run's worktree branch into the workspace's current branch. Review what it produced with
`collect_run` first — `exited_clean` means the process exited 0, not that the work is correct.
Integrate one diff run at a time; skip text-only runs. Outcome `empty` means no file changes to
merge (an outcome, not a fault).

| Parameter | Type | Default | Description |
|-----------|------|---------|-------------|
| `run_id` | string | *(required)* | Run id. |
| `message` | string \| null | `null` | Commit message for the run's work, in the target repo's own convention. Omitted, it falls back to `marshal: integrate <run_id>` — which names the tooling, not the change. |
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
| `message` | string | Detail on failure; base-branch drift warning when `base_branch_drift` is true. |
| `base_branch_drift` | bool | `true` when the merge target differs from the run's recorded `base_branch` (merge still proceeds). |

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

Per-provider usage summary for **one workspace only** — there is no aggregate / multi-workspace
mode. Budgets in the payload come from that workspace's `fleet.config.yaml` alone. Contrast with
`status`, which may list run *records* across workspaces; that list is **not** a spend rollup.

| Parameter | Type | Default | Description |
|-----------|------|---------|-------------|
| `window` | `"session"` \| `"day"` \| `"week"` \| `"month"` \| `"all"` | `"all"` | Time window (`session` = since MCP server started / Fleet `session_start`; `day` = last 24h). Same set as `marshal usage --window`. |
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
| `budgets` | list \| omitted | Present when that workspace's `fleet.config.yaml` declares `budgets:`: `[{ scope, window, spent_usd, limit_usd, remaining_usd, enforce, spent_known }]`. Soft-warn by default; `enforce: true` refuses over-cap spawns on **that** workspace. `spent_known` is `false` when spend could not be determined (lookup failure, or scope has runs but no priced cost source) — treat `spent_usd` / `remaining_usd` as unknown in that case. |
| `workspace` | string | |

Each **Bucket**: `{ runs, succeeded, cost_usd, cost_native, cost_admin_api, cost_estimated, input_tokens, output_tokens, cache_read_tokens, cache_write_tokens, cost_per_run, cost_per_succeeded }`. `cost_estimated` is a zero tombstone (legacy ledger compatibility).

**Usage event** (one append-only line in that workspace's `usage/events.jsonl` per run): process
facts (`run_id`, `backend`, `client`, `model`, tokens, `cost_usd`, `duration_ms`, `status`,
`source`) plus optional routing facts — `task_kind` (caller tag) and `goal_digest` (truncated
sha256 of the goal text; **never the goal itself**). Both are optional so older lines still parse.
Judgment about the work is not on this line: it arrives later, so successful `integrate` stamps
`outcome: integrated` on the **run record** rather than rewriting the usage line — see
[Run record](#run-record).

## Run record

`RunRecord` fields returned by `run_agent`, `spawn`, `get_run`, `status`, and `cancel_run`:

| Field | Type | Description |
|-------|------|-------------|
| `run_id` | string | Unique id. |
| `task_id` | string | Grouping id. |
| `backend` | string | Backend that ran. |
| `status` | string | `queued` \| `running` \| `exited_clean` (the process exited 0 — **not** a claim that the work is correct; review what it produced) \| `empty` (exited 0 with neither text nor file changes — an outcome, not a fault; nothing to integrate) \| `failed` \| `timed_out` \| `cancelled` \| `verify_failed` — a `failed` with `error` mentioning *orphaned at startup* means the supervising process died before the run finished (not an agent failure); `pid` is cleared |
| `client` | string \| null | Client name (null for ad-hoc spawns). |
| `model` | string \| null | Model used. |
| `worktree` | string \| null | Worktree path. |
| `branch` | string \| null | Worktree branch. |
| `base_branch` | string \| null | Branch the worktree was cut from at spawn time. |
| `base_commit` | string \| null | The commit that branch pointed at when the run was spawned, read from the created worktree. A branch name is mutable; this is what the run actually branched from, and what `collect_run` compares against. |
| `cost_usd` | float | Recorded cost. |
| `input_tokens` | int | |
| `output_tokens` | int | |
| `duration_ms` | int | |
| `source` | string \| null | Cost provenance (`native`, `admin-api`, `unavailable`, …). |
| `text` | string | Agent's final message (truncated). |
| `started_at` | string \| null | ISO-8601. |
| `ended_at` | string \| null | ISO-8601. |
| `error` | string \| null | Failure detail. |
| `merged_into` | string \| null | Branch after integrate. |
| `outcome` | string \| null | Judgment about the work, **distinct from** process `status`: `integrated` / `rejected` / `abandoned`. Successful `integrate` stamps `integrated` here (late judgment — the usage event is not rewritten). Absence means no judgment yet; never infer `rejected` from a clean-but-unintegrated run. |
| `commit` | string \| null | Branch tip after `commit_run`. |
| `pid` | int \| null | Agent subprocess pid (while running). |
| `pid_start_time` | string \| null | OS-reported start time of `pid`. A pid alone is not an identity — the OS reuses pids — so startup reconciliation verifies the pair before deciding a recorded run is still alive. |
| `agent_alive` | bool \| null | **Derived when you read the record, never stored.** Is the agent process alive *right now*: distinguishes "still working" from "finished, outcome not yet written" without shelling out to `kill -0` (which a driver should not do anyway — pids are reused, so a live pid is not proof the agent lives). `null` means unknown, not dead: the run is terminal (the question is moot), no pid is recorded, or its identity could not be verified. Never persisted — a stored liveness is stale the instant it lands. |
| `worktree_setup` | string \| null | The command that provisioned this run's worktree, or `null` when none was configured — meaning a **bare checkout**: no venv, no extras, no gitignored data directories. **Read this before comparing any number the agent reports against your own checkout.** A test count from a worktree provisioned by a bare `uv sync` is not the same measurement as one from a workspace with extras installed, and the two look identical written down. |
| `read_paths` | list[string] | Declared outside-worktree paths this run was allowed to read (copied under `.marshal-context/`). Empty means the run saw only its worktree. |
| `attempts` | int | Backend invocations (> 1 means transient retries). |
| `verify_passed` | bool \| null | `null` = no gate ran; `false` with `verify_failed` status. |
| `verify_output` | string | Tail of verify command output. |

Only `exited_clean` runs with a diff are integration candidates. For text-only `exited_clean` work,
read `text` (or `collect_run` with `produced == "text"`) — there is nothing to integrate. `empty`
means the process exited 0 with neither text nor file changes — an outcome, not a fault; do not
integrate. `verify_failed` had file changes but the repo's `verify:` gate rejected them — review the
diff and `verify_output` before deciding.
