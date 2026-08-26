# MCP tool reference

The Marshal MCP server (`marshal mcp`) exposes the tools documented below (the normative list is
`@app.tool` in `mcp_server.py`). **New to Marshal? Call `marshal_quickstart` first** — it names the
canonical loop and says which tool to pick when several look alike. Workspace-scoped tools accept an optional `workspace` parameter (defaults to `"default"`);
the global ones — `marshal_quickstart`, `list_workspaces`, `add_workspace` — do not. Run-handle tools (`get_run`, `collect_run`, `cancel_run`, `integrate`, …) resolve the
owning workspace by scanning each repo's ledger, with an optional `workspace` hint to skip the scan.

Results from workspace-scoped tools include a top-level `"workspace"` field naming the repo they came
from.

## Orientation

### `marshal_quickstart`

The canonical loop and the decision boundary between the lookalike tools. No parameters.

**Returns:** `{ what_marshal_is, non_code_runs, the_loop, which_run_tool, which_read_tool, safety, multi_repo }`.

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

- `clients`: `[{ name, backend, model, permission, permission_fidelity, billing_notice }]`
- `billing_notice`: string \| null — set when this client's resolved model bills a
  **separately-metered provider** rather than the subscription its backend normally uses (today:
  an OpenCode client on a `fireworks-ai/*` model). `null` on every other client, so a notice means
  something. Carried on the listing rather than only stderr for the same reason `skipped` is.
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
- `backend_models`: `{ backend: { models: [model_id], source } }` — what each configured backend
  can say about the models it runs, populated **only when no `models:` catalog is configured**.
  `source` is one of:
  - `probed` — the backend's CLI answered just now. This is live evidence.
  - `static` — a curated list from `docs/model-playbook.md`, used because the CLI could not be
    asked (not installed, probe failed, output shape changed) or has no model-list command at all.
    It may name a model the account cannot actually run; do not treat it as a capability check.
  - `unavailable` — nothing to report.

  Kept separate from `models` on purpose: the catalog is curated metadata a human wrote, this is
  whatever a CLI said just now — and neither drives routing, which clients own.

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
| `artifacts_from` | list[string] \| null | `null` | Run ids whose harvested artifacts this run may read, copied read-only into `<worktree>/.marshal-context/artifacts/<run_id>/` and git-excluded. This is how a multi-round loop hands work forward: a run writes its report to `.marshal-artifacts/` in its own worktree, Marshal harvests it to `.marshal/artifacts/<run_id>/` when the run ends, and a later run names that run here — no pasting findings into the next prompt. Say in the `goal` that the file is there; the copy is not announced to the agent by itself. Naming a run with **no** stored artifacts fails the spawn rather than silently handing the agent nothing (the run record's `artifacts` field lists what a run produced). Duplicated ids are refused. Every component of the destination path is checked, not just its root: each must be absent or a plain directory, a tracked symlink at any of them is refused rather than followed, and the resolved mount is re-checked to be inside the worktree. The same refusal guards `.marshal-artifacts/` on the write side. |
| `base_branch` | string \| null | `null` | Branch to base the worktree on (default: current HEAD). Use after `commit_run` to chain work. |
| `model` | string \| null | `null` | Override the client's resolved model, or the model for an ad-hoc spawn. |
| `backend` | string \| null | `null` | Bare backend for ad-hoc spawn (e.g. `opencode`). **Mutually exclusive with `client`** — passing both is an error, not a precedence rule. To use a configured client with a different model, pass `client` + `model`. |
| `duration` | string \| int \| null | `null` | Per-spawn timeout override (preset name or positive seconds). |
| `workspace` | string \| null | `null` | Target workspace. |

**Returns:** `RunRecord` + `workspace` (see [Run record](#run-record)).

### `spawn`

Same parameters as `run_agent`. Same delegation primitive (product may be a diff or text). Returns
immediately with a `RUNNING` record — right after `git worktree add`, **before** `read_paths` /
`setup_cmd` provisioning (which can take up to `setup_timeout_s`) and before the agent starts — so
the record is pollable and cancellable during setup. Poll `get_run` / `status`; cancel with
`cancel_run`, which SIGTERMs the setup process group once its pid is published, or stamps
`cancelled` and skips the agent when it arrives earlier (a cancel during pure-Python provisioning
such as `read_paths` copies is cooperative — it lands at the next checkpoint). A setup/provisioning
failure lands `failed` with a phase-named error (`fleet: setup:` / `fleet: provision:`) and the
half-made worktree torn down; `collect_run` / `integrate` on such a run surface that error instead
of a diff.

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
| `jobs` | list[Job] | *(required)* | Each job: `{ client?, goal, task_id?, task_kind?, context_files?, read_paths?, artifacts_from?, model?, backend?, duration?, workspace?, then? }`. Omit `client` and set `backend` for ad-hoc spawns. Per-job `workspace` overrides the call-level default. `then` uses the same field set (no nested `then` or `workspace`). |
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

- `workflows`: `[{ name, description, inputs, auto_integrates, phases: [{ name, run, auto, from_phase }] }]`
- `auto_integrates`: true when any `run: integrate` phase has `auto: true` — that recipe merges into
  your current branch with no review step. `auto` on any other phase kind is inert (the runner reads
  it only when integrating), so it is reported per phase but never raises `auto_integrates`.
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

**Reviewing a pull request.** Pass `pr=<number>` with `target='range'`. A PR *is* a commit range,
so there is deliberately no `pr` target kind and no engine change: `pr` resolves the endpoints and
every existing `target: range` team reviews a PR unchanged.

- The diff is taken **from the merge base** (`base...head`, three-dot). Reviewing `base..head`
  against a moving branch would show the panel everything `main` gained since the PR opened, and
  the reviewers would spend their lenses on code the PR's author never wrote.
- The head is used as a **commit SHA, never a branch name**. A PR can come from a fork whose branch
  the author chose, so that name is attacker-controlled data; a branch called `--output=...` handed
  to git as a revision is an arbitrary file write. Marshal reads `headRefOid` instead, so the
  hostile string never becomes an argument.
- Both endpoints are **refreshed before the diff, and the resolution fails closed if either cannot
  be**. The base is the fully-qualified remote-tracking ref (`refs/remotes/origin/main`), fetched
  with an explicit refspec so a narrowed `remote.*.fetch` mapping cannot leave it stale; the head is
  checked against what the fetch actually retrieved, so a force-push mid-resolve is refused rather
  than reviewed. Every one of those failures would otherwise produce a plausible, wrong diff that
  nothing in the output marks as wrong.
- The reply echoes `pull_request` with the title, URL and `stale` (true for a closed or merged PR).
  Check it names the PR you meant: a panel reporting on the wrong PR reads exactly like one
  reporting on the right PR.

`gh` must be on PATH and authenticated; a missing or unauthenticated CLI fails **before** any
reviewer spawns, since a panel is expensive.

| Parameter | Type | Default | Description |
|-----------|------|---------|-------------|
| `name` | string | *(required)* | Review team name (from `list_teams`). |
| `target` | `"run"` \| `"plan"` \| `"range"` \| `"audit"` | *(required)* | What is reviewed; must match the team's declared `target`. |
| `run_id` | string \| null | `null` | Run whose diff to review (target `run`). |
| `base` | string \| null | `null` | Base ref (target `range`). Validated — a ref that git would read as an option is refused. |
| `head` | string \| null | `null` | Head ref (target `range`; default `HEAD`). |
| `pr` | int \| null | `null` | GitHub PR number to review (target `range`). Fills in `base`/`head`, so pass it **instead of** them — supplying both is refused rather than silently preferring one. Needs `gh` on PATH. |
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
| `reviews` | list | Per-role `{ role, client, run_id, status, completed, review, review_truncated, review_full_len, report_path, note }`. `completed` describes the *run* (exited clean and produced a whole report that answers the contract), never the review's merits. Two things are **not** completed, and both keep their raw text on `review`: a run that exits 0 having written only narration, naming none of the contract's sections; and one whose report was cut off by the record's text cap - `review_truncated` is true, `review_full_len` is what it would have been, and the whole report is in `get_run_log`. Both land in `incomplete_roles`. |
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
nothing is merged). Branch on `produced` (`diff` | `text` | `nothing` | `unavailable`).

`unavailable` is **not** `nothing`: it means the work could not be read (worktree torn down, a
git operation failed mid-collect), which is no evidence the run produced none. Do not
`set_outcome(rejected)` on it — `routing` would hold that rejection against a client that may well
have succeeded. Read `unavailable_reason`, and re-run or inspect rather than judging the work.

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
| `commit_count` | integer \| null | Number of commits on the run branch not reachable from that base. **`null` when the count was never taken** (no branch, or the work could not be read) — never read it as zero. |
| `produced` | string | `diff` (files changed) \| `text` (no files, but the agent replied) \| `nothing` (the run genuinely produced neither) \| `unavailable` (the work could not be read — see `unavailable_reason`). **Branch on this** rather than inferring intent from which container is empty — guessing from emptiness is what made research runs read as failures. |
| `unavailable_reason` | string \| null | Why the work could not be read. Set only when `produced == "unavailable"`. |
| `text` | string | The agent's final message — populated when `produced == "text"`, and on the `unavailable` path where the record's message may be the only surviving account of the run. When there is a diff, the diff is the artifact and repeating the message would bloat every reply. |
| `text_truncated` | bool | True when `text` was cut on write. **Check this before treating `text` as a finished product** — a truncated report stops mid-sentence and reads as complete. Full stream: `get_run_log`. |
| `text_full_len` | integer \| null | Pre-truncation character count; `null` when nothing was cut. |

### `status`

| Parameter | Type | Default | Description |
|-----------|------|---------|-------------|
| `workspace` | string \| null | `null` | Scope to one workspace; omit to list **all** workspaces. |
| `limit` | int (1–500) | `50` | Max runs, newest first. |
| `status` | string \| null | `null` | Only runs with this exact status. |
| `task_id` | string \| null | `null` | Only runs with this `task_id`. |
| `since_hours` | float \| null | `null` | Only runs started within N hours. A run whose start time is unreadable is **kept** — a missing timestamp is not evidence it falls outside the window. |
| `view` | `"poll"` \| `"compact"` \| `"full"` | `"poll"` | How much of each run to return (see below). |

**Returns:** `{ runs, returned, matched, truncated, view }`, newest first.

**Three views, narrowest by default.** Polling is the highest-frequency call a driver makes, so
the default carries only what a poll asks — is it done, and was it any good:

| View | Fields |
|------|--------|
| `poll` (default) | `run_id`, `task_id`, `backend`, `client`, `status`, `agent_alive`, `cost_usd`, `source`, `duration_ms`, `outcome`, `ended_at` |
| `compact` | the whole record — `worktree`, `branch`, `base_commit`, token counts, `read_paths`, `artifacts`, `pid` … — minus the unbounded text |
| `full` | everything, including `text` and `verify_output` |

Each view is a superset of the one above it. `text` and `verify_output` are unbounded and dominate
a listing's size (one observed reply was ~395k characters), so every view below `full` replaces
them with `has_text` / `has_verify_output` flags — a caller must be able to tell "this run produced
no message" from "this view omitted it". Use `get_run` for one run's full text.

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

**Returns:** `{ run_id, path, content, truncated, size_bytes, status, error }`.

**Check `status` before `content`.** Only `ok` carries content. The rest say *why* there is none,
and they call for opposite reactions: `gone` (the worktree was cleaned — the run finished, so
re-running it is wasted), `not_found` (the worktree is right there and the agent never wrote that
path — possibly worth another run), `refused` (the path escapes the worktree), `unreadable` (the
file exists but could not be read). These used to arrive as one indistinguishable `ValueError`.
An unknown `run_id` still raises — that is a bad identifier, not a state the run is in.

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

### `wait_for_runs`

Block until every named run reaches a terminal state, or until `timeout_s` (default 60, max 600).
The close of the `spawn` loop: without it a driver polls `status` on a cadence it has to guess,
spending a tool call and a model turn on each tick to learn "not yet" — too tight burns tokens, too
loose adds dead wall-clock to every run.

MCP has no server-initiated push, so "notify me when done" can only be a blocking wait. This is
still a poll loop; the point is that it runs server-side, where a tick costs a few file reads rather
than a turn of context.

Runs come back in the `poll` shape by default — the same three shapes `status` uses, selected with
`view` (`poll` | `compact` | `full`). It previously dumped whole records, which with a fan-out's
worth of runs meant up to 16k of `text` each. The trimmed views replace `text` / `verify_output`
with `has_text` / `has_verify_output`, so an omitted field is never misread as an empty one; reach
for `get_run` or `collect_run` when you want one run's actual text.

Returns `{settled, pending, unknown, timed_out, waited_ms, view}`. Every requested id appears in exactly
one of the three lists.

- **`settled` means finished, never succeeded.** `failed`, `timed_out`, `cancelled`, `verify_failed`
  and `empty` all land there. Terminality is the same predicate `status` and `routing` use — there
  is deliberately no second definition of "done" that could disagree with the record you then read.
  Branch on each record's `status` exactly as you would after a poll.
- **Expiry is not an error.** It returns normally with `timed_out: true` and the unfinished runs in
  `pending`; re-call with just those ids. This is what makes the tool safe under a short client-side
  request timeout: the call is cut off, the driver keeps its progress and asks again, so the worst
  case degrades to a coarser poll instead of a broken tool.
- **`unknown`** is ids with no record in any workspace. Nothing will ever create them, so they are
  reported immediately and never waited on — they do not hold the call open.
- Run ids may **span workspaces**. Each is resolved to its owning repo once, up front (a run cannot
  change workspaces), and they share one deadline. Returned records are workspace-tagged.
- It **reports; it does not act** — no implicit `collect_run`, no implicit `integrate`. Review what
  settled before merging anything.
- A run whose supervisor was killed keeps a `running` record forever and will wait out the full
  timeout — terminality describes the record, not the process, which is why every wait is bounded.
  `cancel_run` on it releases the wait on the next tick.

### `cancel_run`

SIGTERM the agent process group — but only for a **live child of the server process handling the
call**. Signalling goes through an in-process handle that tracks the child from spawn until it is
reaped; the OS cannot recycle a child's pid before its parent reaps it, so within that window the
pid unambiguously belongs to the agent.

- A cancel arriving **before** the pid is known is applied the moment it is, so the agent is stopped
  rather than left running behind an already-terminal record.
- During `spawn`'s provisioning window the same rules cover the `setup_cmd` process group: a cancel
  SIGTERMs it once its pid is published, or stamps `cancelled` and the agent never launches.
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
| `status` | `"committed"` \| `"clean"` \| `"blocked"` \| `"error"` | `blocked` means the run is not safe to freeze yet: still running, or its record reads terminal while its agent process is provably alive (a cancel issued by another Marshal process stamps a status it cannot signal). Recoverable — wait or stop the process, then retry. |
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
| `status` | `"merged"` \| `"conflict"` \| `"blocked"` \| `"empty"` \| `"error"` | `blocked` means nothing was merged and the state is fixable: the target checkout is dirty/colliding or detached, the run is still in progress, or its record reads terminal while its agent is provably alive (see `commit_run`). Retry after resolving. |
| `branch` | string \| null | Source branch. |
| `merged_into` | string \| null | Target branch. |
| `changed_files` | list[string] | |
| `conflicts` | list[string] | Conflicting paths. When the run's base commit was rewritten away these are *not* the cause — see `message`. |
| `commit` | string \| null | Merge commit hash. |
| `message` | string | Detail on failure; base-branch drift warning when `base_branch_drift` is true. On `conflict`, reports when the run's base commit is reachable from **no branch or tag** — a base that is no longer in history makes every file read as changed on both sides, so the `conflicts` list points away from the real cause. It states that observation and offers the likely causes (history rewritten mid-run; a deleted base branch; a `base_branch` naming a commit that was never on one) rather than asserting one, since `base_branch` accepts any commit-ish. The measured claim and the remedy hold in every one of those cases. Silent unless the base is unreachable from the target **and** reached by no surviving ref, so a run deliberately spawned with `base_branch` onto a live branch is not mislabelled. |
| `base_branch_drift` | bool | `true` when the merge target differs from the run's recorded `base_branch` (merge still proceeds). |

### `routing`

| Parameter | Type | Default | Description |
|-----------|------|---------|-------------|
| `task_kind` | string \| null | `null` | Only this kind of work (the free-text tag passed at spawn). Omit for all kinds. |
| `window` | `session` \| `day` \| `week` \| `month` \| `all` | `all` | Window over the usage ledger. Routing wants history, so the default is `all`. `session` is meaningful here (the server is long-lived) but is **not** offered by `marshal routing` — a one-shot CLI process has no session, so the window would start at report time and always return nothing. |
| `workspace` | string \| null | `null` | Target workspace. |

**Returns:** `{ cells, recommended_by_task_kind, recommended, recommended_task_kind, total_runs,
total_judged, events_without_record, task_kind_filter, caveat, window, workspace }`

Derived on read by joining the usage ledger to recorded run outcomes; nothing is stored. Each
cell is one `(task_kind, client)` pair:

- `integration_rate`: integrated ÷ **judged** runs. `null` when nothing has been judged — that is
  *unknown*, not 0%. `n_judged`, `n_unjudged` and `n_no_record` are all reported so a rate is never
  read without its denominator.
- `mean_cost_per_integrated`: `null`, **never 0**, when no integrated run reported a measured cost
  (`native` / `admin-api`). Compare against `measured_cost_all_usd`, which includes money spent on
  runs you rejected — cost-per-integrated alone flatters a client that burns four rejects per keeper.
- `rank` / `cost_ranked`: rank is **within the cell's `task_kind`**, never global — a client
  measured on `docs` has never done your `refactor` work, so the two are not comparable. Within a
  kind: integration rate, then duration, then measured cost, then alphabetical. A cell with nothing
  judged is **unranked** (`rank: null`) but still returned with its counts. A cell with unmeasured
  cost neither wins nor loses the cost tiebreak.
- `recommended_by_task_kind`: `task_kind → best client for it`. `recommended` is the single
  headline answer and is set **only when one `task_kind` is in view** (usually because you passed
  the filter); with several kinds there is no one answer, so it is `null` and this map is what you
  read instead.
- `evidence` / `notes`: the claim with its sample size, and everything that would make the headline
  misleading on its own (small sample, unmeasured cost, pruned records).
- `caveat`: set when no run has been judged at all — the ledger is unevaluated, not empty.

**This is only as honest as your `set_outcome` habit.** If you record integrations and never
rejections, every rate reads 100%.

### `set_outcome`

| Parameter | Type | Default | Description |
|-----------|------|---------|-------------|
| `run_id` | string | — | The run to judge. |
| `outcome` | `integrated` \| `rejected` \| `abandoned` | — | Your judgment about the **work**. |
| `note` | string \| null | `null` | Short reason, kept with the record. Truncated at 2000 chars. |
| `workspace` | string \| null | `null` | Workspace hint; the run's owning workspace is resolved from `run_id`. |

**Returns:** `{ run_id, status, outcome, previous, note, message, workspace }`

- `status`: `recorded` (the verdict was written) \| `unchanged` (same verdict already recorded —
  fully idempotent, nothing written) \| `conflict` (the run is already `integrated`; nothing written)
- `outcome`: what the record says **now** — not what you asked for. A caller that ignores `status`
  still cannot misread the stored verdict.
- `previous`: the verdict before this call, or `null` if it had none.

This is judgment about the work, distinct from the run's process `status`: `exited_clean` says a
process exited 0, not that the diff was any good.

**Record your rejections.** Declining to integrate leaves no trace, so a run you reviewed and threw
away is indistinguishable from one nobody has looked at. Every `routing` rate is a ratio over
*judged* runs, so without rejections it reads 100% for every client and means nothing.

`integrated` is **sticky**: a merge commit is a mechanical fact, not an opinion, so it is never
overwritten and the attempt comes back as `conflict` rather than an error (a driver can branch on
it). The same reasoning runs the other way — you cannot *assert* it. Recording `integrated` on a
run whose `merged_into` is unset is refused, because stickiness is justified only by the merge
existing, and a permanent verdict for a merge that never happened could never be corrected.

A run that has not finished (`queued` / `running`) cannot be judged at all: a verdict on work that
has not happened is a guess, and rates are computed over verdicts.

There is deliberately no `force` — undoing an integration is a new fact about a different commit,
not a correction of the old one. Known limitation: an integration later reverted still counts as
integrated.

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
| `cost_usd` | float \| null | Recorded cost. **`null` = nothing was measured** (read `source`); `0.0` is a measured zero. |
| `input_tokens` | int \| null | **`null` = the backend reported no usage at all**; a number is a real count, and `0` a measured zero. Same rule as `cost_usd` — and these are the fallback ranking metric precisely when cost is `null`, so treating `null` as `0` ranks the *unmeasurable* backend as the most efficient one. |
| `output_tokens` | int \| null | As `input_tokens`. |
| `duration_ms` | int \| null | Wall-clock around the backend invocation. **`null` = the run never reached a backend** (e.g. it failed in provisioning), which is not the same as finishing instantly. |
| `source` | string \| null | Cost provenance (`native`, `admin-api`, `unavailable`, …). |
| `text` | string | Agent's final message, capped on write — read `text_truncated` before treating it as whole. |
| `text_truncated` | bool | True when the cap fired. A truncated report stops mid-sentence and otherwise reads as complete; get the full stream from `get_run_log`. |
| `text_full_len` | integer \| null | Pre-truncation character count; `null` when nothing was cut. |
| `started_at` | string \| null | ISO-8601. |
| `ended_at` | string \| null | ISO-8601. |
| `error` | string \| null | Failure detail. |
| `merged_into` | string \| null | Branch after integrate. |
| `outcome` | string \| null | Judgment about the work, **distinct from** process `status`: `integrated` / `rejected` / `abandoned`. Successful `integrate` stamps `integrated` here (late judgment — the usage event is not rewritten). Absence means no judgment yet; never infer `rejected` from a clean-but-unintegrated run. |
| `commit` | string \| null | Branch tip after `commit_run`. |
| `pid` | int \| null | Agent subprocess pid (while running). |
| `pid_start_time` | string \| null | OS-reported start time of `pid`. A pid alone is not an identity — the OS reuses pids — so startup reconciliation verifies the pair before deciding a recorded run is still alive. |
| `supervisor_pid` | int \| null | Pid of the Marshal process that took this run on and is expected to write its outcome. It is not a claim about who did: nothing clears it at terminal, so a run another process reaped or cancelled still names its original supervisor (`error` says what happened). A different question from `pid`, which names the agent subprocess: after the agent exits its supervisor still has pricing, a usage-API backfill, the `verify:` gate and artifact harvest to do, and the record reads `running` throughout. Startup reconciliation asks about the supervisor, so that a healthy mid-finalization run is not declared abandoned. `null` whenever no identity could be established — a record written before the field existed, or a host where the start-time probe (`ps`) is unavailable; reaping falls back to the agent-pid rule in both cases. |
| `supervisor_start_time` | string \| null | OS-reported start time of `supervisor_pid`, making the pid an identity the way `pid_start_time` does for the agent. Rendered under a pinned locale and timezone, because the process that writes it is not the one that checks it. Set together with `supervisor_pid` or not at all — a pid without a verifiable start time would be trusted on liveness alone. |
| `agent_alive` | bool \| null | **Derived when you read the record, never stored.** Is the agent process alive *right now*: distinguishes "still working" from "finished, outcome not yet written" without shelling out to `kill -0` (which a driver should not do anyway — pids are reused, so a live pid is not proof the agent lives). `null` means unknown, not dead: the run is terminal (the question is moot), no pid is recorded, or its identity could not be verified. Never persisted — a stored liveness is stale the instant it lands. |
| `worktree_setup` | string \| null | The command that provisioned this run's worktree, or `null` when none was configured — meaning a **bare checkout**: no venv, no extras, no gitignored data directories. **Read this before comparing any number the agent reports against your own checkout.** A test count from a worktree provisioned by a bare `uv sync` is not the same measurement as one from a workspace with extras installed, and the two look identical written down. |
| `read_paths` | list[string] | Declared outside-worktree paths this run was allowed to read (copied under `.marshal-context/`). Empty means the run saw only its worktree. |
| `artifacts` | list[string] | Files this run wrote to `.marshal-artifacts/`, harvested to `.marshal/artifacts/<run_id>/` and still readable after the worktree is cleaned. Names only — pass this run's id as another run's `artifacts_from` to hand them forward. Empty means the run produced no artifact, which is the normal case unless the goal asked for one. |
| `attempts` | int | Backend invocations (> 1 means transient retries). |
| `verify_passed` | bool \| null | `null` = no gate ran; `false` with `verify_failed` status. |
| `verify_output` | string | Tail of verify command output. |

Only `exited_clean` runs with a diff are integration candidates. For text-only `exited_clean` work,
read `text` (or `collect_run` with `produced == "text"`) — there is nothing to integrate. `empty`
means the process exited 0 with neither text nor file changes — an outcome, not a fault; do not
integrate. `verify_failed` had file changes but the repo's `verify:` gate rejected them — review the
diff and `verify_output` before deciding.
