# Using Marshal

Marshal is a **fleet primitive**: one driver agent spawns and coordinates many sub-agents, each in
its own isolated git worktree, in parallel. You declare named **clients** (each pinning a backend +
model + permission), then call Marshal three ways: as an MCP server, as a CLI, or as a Python
library.

Marshal runs whatever a headless agent CLI can run: implementation, review panels, audits, and
research fan-out. A run that only reads and reasons still returns its work — its final message
lands on the run record as `text` and the run is `exited_clean`; `collect_run` reports which
artifact it was via `produced` (`diff` | `text` | `nothing`) and hands back the message itself for
a text run. `empty` means the run produced neither text nor file changes.

The real gap is **structured** output: results come back as prose you parse
([#97](https://github.com/chiruu12/marshal/issues/97)). Where a backend truncates long final
messages (Cursor does), have the agent write its report to a file — that is why the built-in review
teams do so.

> **Status:** V1 core complete, pre-1.0. The engine, CLI, and MCP server work, including merge-back
> (`collect_run` + `integrate`), capped parallel fan-out (`run_many`), and a measured savings
> benchmark (`benchmark`/`report`). See [`status.md`](status.md). For every config key see
> [`config.md`](config.md); for MCP parameters and return shapes see [`mcp-tools.md`](mcp-tools.md).

## Concepts

| Term | What it is |
|------|------------|
| **driver** | The agent (e.g. Claude Code) that plans the work and calls Marshal. It keeps the expensive reasoning. |
| **backend** | A CLI adapter (cursor, opencode, codex, claude-code, command-code, goose, antigravity). Chosen per call, never global. |
| **client** | A named worker in `fleet.config.yaml` pinning a backend + model + permission. You route tasks to clients by name. |
| **run** | One execution of a client on a task; ends `exited_clean`/`empty`/`failed`/`timed_out`/`cancelled`/`verify_failed`. |
| **worktree** | The isolated git checkout **one run** works in (under `.marshal/worktrees/`). The safety boundary — main is untouched until you integrate. |
| **workspace** | A **whole repo** the server can target. Distinct from *worktree*: a workspace holds many runs, each in its own worktree. One server can target several workspaces (`list_workspaces`, `workspace=`). |
| **integrate** | Merge a run's worktree branch back into the target repo's current branch (the only step that touches it). |
| **workflow** | A declarative YAML recipe that sequences the primitives (fan-out → collect → gated integrate). |

## Install

```bash
uv tool install "MarshalFleet[mcp]"
# or
pipx install "MarshalFleet[mcp]"
```

The `[mcp]` extra is what makes `marshal mcp` work. Without it the server exits before a host can
connect.

For clone-from-source, tracking unreleased work on `main`, backend CLI auth, and MCP wiring, see
**[`../SETUP.md`](../SETUP.md)**. Marshal does **not** install the backend CLIs.

Developing Marshal itself:

```bash
uv sync --extra mcp --extra dev
```

The base package is Pydantic + PyYAML. The `mcp` extra adds the MCP server; `dev` adds the
test/lint toolchain.

## Configure a fleet

From your project repo, scaffold a starter config:

```bash
marshal init   # scaffolds fleet.config.yaml in the current repo
```

The scaffold ships every client commented out — uncomment at least one (or add your own). A
filled-in config looks like:

```yaml
defaults:
  permission: safe-edit        # read-only | safe-edit | yolo
  timeout_s: 600

clients:
  implementer:
    backend: opencode          # opencode | cursor | codex | command-code | claude-code | goose | antigravity
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
  unset - but Marshal does **not** inject it; the CLI's own login is what authenticates. For two
  clients on the same backend with different provider homes (`env:`, e.g. `CODEX_HOME`), see
  [`examples/per_client_env.yaml`](../examples/per_client_env.yaml).
- An OpenCode client with no `model` defaults to `opencode-go/glm-5.2` so runs bill the Go
  subscription, not Fireworks credits. A `fireworks-ai/*` model is rejected outright.
- **`worktree_setup`** (optional, top-level): a command run once in each fresh worktree before the
  agent starts - e.g. `worktree_setup: uv sync --extra dev --extra mcp` to provision the worktree's
  own venv. Accepts a string or an argv list; omit it for repos that need no setup. Marshal scrubs
  the driver's `VIRTUAL_ENV`/`PYTHONHOME` for the command (and for agent runs), so the worktree's
  own environment wins - without it, an agent's `uv run pytest` would resolve the driver's venv and
  test stale code.   A non-zero exit tears the worktree down and fails the run early. By default
  argv[0]'s basename must be allowlisted (typo guard only — `python -c` and similar still pass);
  non-allowlisted basenames and relative path argv[0] need `allow_unsafe_commands`
  (see `docs/config.md`).
- **`retries`** (optional, top-level, default `2`): how many times to re-run a run that failed for a
  **transient** reason - a backend state-DB lock, a rate limit, a 5xx, a dropped connection - with
  exponential backoff. Set `0` to disable. Genuine task failures and timeouts are **never** retried
  (a timeout retry just burns another full window). A retried run records its `attempts` count.
- **`verify`** (optional, top-level): a gate command run in the worktree **after** a run that would
  otherwise be `exited_clean` and actually changed files (e.g. the repo's full test suite). Text-only
  replies are never gated. A non-zero exit marks the run `verify_failed` instead of `exited_clean`; the
  worktree and diff are kept for review, and the command's output tail lands on the run record
  (`verify_output`). Same string-or-argv shape, env hygiene, and allowlist rules as `worktree_setup`.
  Executes worktree content the agent may have modified — see `SECURITY.md`.
- **`allow_unsafe_commands`** (optional, top-level, default `false`): opt-in so `worktree_setup` /
  `verify` may use a non-allowlisted basename (including `sh -c …`) or a relative path argv[0].
  Without it, those forms are rejected at config load (runtime setup/verify keep the same check as
  a backstop). Does not restrict args of allowlisted tools — see `SECURITY.md`.
- **`integrate_run_hooks`** (optional, top-level, default `false`): when `false`, `commit_run` /
  `integrate` use `git --no-verify` so prompting hooks cannot deadlock a headless merge. Set
  `true` only for known non-interactive hooks; prompting hooks can still hang until the git
  timeout. Opted-in hooks may be scripts the agent modified — see `SECURITY.md`. Prefer
  `verify:` / CI when unsure.
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
- **`budgets`** (optional, top-level): $ caps per scope (a backend, a client, or the whole fleet)
  and time window (`session` | `week` | `month`). Default is **soft-warn** (stderr when a scope's
  windowed spend meets/exceeds the cap; the run proceeds). Set `enforce: true` to refuse matching
  over-cap spawns (`BudgetExceeded`) and serialize matching in-flight spawns **within one process**
  — two processes against the same repo (CLI alongside a running MCP server) each read the same
  ledger snapshot and can both admit, so treat it as a strong per-driver guard rather than a
  distributed lock ([#182](https://github.com/chiruu12/marshal/issues/182)). Set at most one of
  windowed spend meets/exceeds the cap; the run proceeds). Set `enforce: true` to hard-refuse matching
  over-cap spawns (`BudgetExceeded`) and serialize matching in-flight spawns across processes
  (CLI + MCP on one repo; `.marshal/budget_gate.json`). Set at most one of
  `backend` / `client` per entry (omit both for a global cap); `limit_usd` must be positive; the
  scope's `cost_usd` comes from the usage ledger, so subscription / unknown-cost backends (which
  report `$0` / `unavailable`) never trigger a $ cap and show `unavailable` spent when the scope
  has runs with no priced cost (an empty scope still shows `$0.0000`; no fake percentage,
  no fabricated "remaining"). See [`config.md`](config.md) for the full census.

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
      enforce: true            # optional hard refuse
  ```

  The MCP `usage` tool's response (and `marshal usage --config fleet.config.yaml --json`) includes
  a `budgets` list with `scope / window / spent_usd / limit_usd / remaining_usd / enforce /
  spent_known` per budget, so the driver can see how much room is left alongside the spend.
  When `spent_known` is false, treat `spent_usd` / `remaining_usd` as unknown.
- **Missing backend CLI → the client is skipped, not fatal.** At startup Marshal probes each
  configured backend's CLI; a client whose CLI is unavailable is **skipped** with a stderr warning
  (and listed under `skipped_clients`) so the rest of the fleet still runs - a missing CLI never fails
  a run mid-flight. `marshal doctor` still reports an unavailable backend as a FAIL so you can see
  what's missing.

### Permission tiers

| Tier | Meaning |
|------|---------|
| `read-only` | Plan/inspect only - no edits. |
| `safe-edit` | Edit and run **inside the worktree**, no prompts. The default. What that mode actually enforces varies by backend — see `permission_fidelity` below and `docs/design.md` §5. |
| `yolo` | Fully unrestricted (OpenCode still denies `question` so headless cannot deadlock). Opt-in only. |

**`permission_fidelity`** is a coarse honesty signal. Two surfaces, keep them distinct:

| Surface | What it describes |
|---------|-------------------|
| `marshal backends` / doctor `permission:<backend>` | The backend's **safe-edit** capability |
| `list_clients` | The resolved `(backend, permission)` pair for that client |

| Value | Meaning |
|-------|---------|
| `enforced-denies` | This tier installs a restriction beyond the worktree (curated denies, workspace sandbox, or plan/read-only mode). Not a true process sandbox. On backends: Cursor, OpenCode, Codex safe-edit. |
| `boundary-only` | No Marshal deny layer; treat the worktree + explicit integrate as the boundary. On backends: Command Code, Goose, Antigravity, Claude Code. |
| `unrestricted` | Client-only: `permission: yolo` — deny/sandbox overlay dropped by design. Never claim `enforced-denies` for these. |

`marshal backends` prints `fidelity=…` (and JSON `permission_fidelity`); `list_clients` includes the
resolved field per client; `marshal doctor` emits `permission:<backend>` for backend safe-edit
(`ok` for enforced-denies, `warn` for boundary-only). Prefer `enforced-denies` **safe-edit**
clients for sensitive work; never treat a `yolo` client as restricted.

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
| `MARSHAL_ALLOW_MCP_WORKSPACE_REGISTRATION` | unset (disabled) | Set to exactly `1` to enable the `add_workspace` MCP tool (see `docs/config.md`). |

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
hint. Add a repo with `marshal workspace add <name> [path]` (operator-run; the default path) - it
appears **without reconnecting** the server. The MCP `add_workspace` tool can do the same but is
**disabled by default**; enable it deliberately with `MARSHAL_ALLOW_MCP_WORKSPACE_REGISTRATION=1`
on the server process (see `docs/config.md` and `SECURITY.md`). Config edits hot-reload the same
way: adding, changing, or
deleting a workspace's `fleet.config.yaml` is picked up on the next tool call. A reload keeps the
workspace's budget continuity: the enforce-budget in-flight guard and the `session` window clock
survive the rebuild (limits themselves come from the newly loaded config), so an unrelated edit
mid-run does not admit a second spawn past an `enforce: true` cap or reset session spend accounting
(see `docs/config.md` for the residual when an enforce budget's own definition changes). Spend and
budget evaluation stay on each repo's own ledger — there is no cross-workspace rollup. With no
file and no `MARSHAL_WORKSPACES`, it's the single-repo server it always was. Runnable library form:
[`examples/multi_workspace.py`](../examples/multi_workspace.py).

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
| `marshal_quickstart()` | **Start here.** The four-step loop, and which tool to pick when several look alike (the run-ish and status-ish groups). |
| `list_workspaces()` | List the repos this server can target (name, path, `ready` + `ready_reason`, client_count). |
| `add_workspace(name, path, scaffold?)` | Register a repo in the central registry; usable immediately (no reconnect). |
| `doctor()` | Preflight the setup (toolchain, repo, config, per-backend CLI availability + auth); read-only. Run it before spawning. |
| `list_clients` | List configured clients (name, backend, model, permission, permission_fidelity) plus `driver_context`. |
| `list_models` | List the optional `models:` catalog (`id`, `backends`, `cost`, `quota_type`, `notes`) plus `driver_context`. |
| `run_agent(client?, goal, task_id?, context_files?, read_paths?, base_branch?, model?, backend?, duration?)` | Delegate a goal to a worker agent in an isolated worktree; returns the run record. Product may be a diff or text (both first-class). Omit `client` for an ad-hoc spawn by `backend` (+ optional `model`). `duration` is a preset name or positive seconds. `base_branch` bases the worktree on a branch other than HEAD (e.g. after `commit_run`). `read_paths` is a read-only escape hatch for files outside the worktree (copied under `.marshal-context/` as immutable files/dirs; secret-shaped descendants, symlinks inside a declared tree, and special files like FIFOs are refused; copies open fail-closed). See [`examples/read_paths.py`](../examples/read_paths.py). |
| `run_many(jobs, max_concurrency?)` | Delegate several `{client?, goal, task_id?, context_files?, read_paths?, workspace?, then?, …}` jobs in parallel, each in its own worktree (each product may be a diff or text); optional per-job `then` runs a follow-up in the same worker as soon as that job's primary finishes (does not wait for sibling jobs). Per-job `workspace` allows mixed-repo batches under one concurrency cap. Returns one `{primary, then?, then_skipped?}` object per input job (input order): `primary` is the job's run; `then` is the follow-up when it ran; `then_skipped` explains why `then` did not (primary failed, no branch, primary's branch has no commits beyond its base, `commit_run` blocked, …). Pipelined review: [`examples/pipelined_review.py`](../examples/pipelined_review.py); mixed workspaces: [`examples/multi_workspace.py`](../examples/multi_workspace.py). |
| `spawn(client?, goal, task_id?, context_files?, read_paths?, base_branch?, model?, backend?, duration?)` | Start a worker agent in the background; returns its RUNNING record at once (before worktree provisioning finishes) - poll `get_run`/`status`; `cancel_run` works during setup. Same delegation primitive as `run_agent` (diff or text product). Same ad-hoc/`model`/`duration`/`base_branch`/`read_paths` rules. |
| `cancel_run(run_id)` | Stop a running agent (process-group `SIGTERM`); returns the updated record. Only signals runs **this process started** — for one started by a process that has since died it stamps the record `cancelled` without ending the agent, and `error` says so. |
| `benchmark(goal, clients, task_id?)` | Run one goal through several clients (strategies) and compare cost/latency/outcome. |
| `report(task_id)` | Re-derive a past benchmark's strategy comparison from the ledger (read-only). |
| `get_run(run_id)` | Fetch one run record (status ∈ `exited_clean`/`empty`/`failed`/`timed_out`/`cancelled`/`verify_failed`). `empty` is an outcome (exited 0, neither text nor file changes), not a fault; for text-only work read `text`. |
| `read_run_file(run_id, path)` | Read one file out of a run's worktree — how one agent's artifact reaches the next. Returns `{content, truncated, size_bytes}`; check `truncated`. Path must be relative to that worktree and stay inside it. |
| `collect_run(run_id)` | What a run produced: diff/changed files and/or final text via `produced` (`diff` \| `text` \| `nothing`; read-only; nothing is merged). Review before integrating a diff; for text runs the value is in `text`. |
| `commit_run(run_id, message?)` | Freeze a finished run's work onto its own branch (your branch untouched) so a dependent run can `spawn` with `base_branch` = that branch. Outcome ∈ `committed`/`clean`/`blocked`/`error`. |
| `integrate(run_id, cleanup?)` | Merge a diff run's branch into the current branch. Outcome ∈ `merged`/`conflict`/`blocked`/`empty` (no file changes to merge — an outcome, not a fault)/`error`. Skip text-only runs. |
| `clean(scope?, run_ids?, older_than_hours?, dry_run?)` | Tear down finished runs' worktrees + branches (ledger + run history kept). Never a running run. `scope` ∈ `merged`/`finished`/`all`. Scope-mode cleans also reap orphaned worktree dirs (`orphans_removed`). Returns `{removed, orphans_removed, skipped, errors, dry_run}`. |
| `status()` | List all runs with status + cost (status ∈ `exited_clean`/`empty`/`failed`/`timed_out`/`cancelled`/`verify_failed`). |
| `usage(window?)` | Per-provider usage summary (totals + by backend/client/model/backend×model, with input/output/cache-read/cache-write token columns and a native/admin-api cost split). `window` ∈ `session` (since the MCP server started) \| `day` (last 24h) \| `week` (7d) \| `month` (30d) \| `all` (default; the full ledger) — same set as `marshal usage --window`. The resolved `window` and `since` are echoed back. When the workspace's config declares `budgets:`, the response also includes a `budgets` list with per-budget `scope / window / spent_usd / limit_usd / remaining_usd / enforce / spent_known` (soft-warn by default; `enforce: true` refuses over-cap spawns and serializes matching in-flight spawns). `spent_known: false` means spend is unknown — do not treat `spent_usd` / `remaining_usd` as measured. |
| `get_run_log(run_id)` | The full raw stdout/stderr persisted for a run (under `<base>/logs/<run_id>.log`), or `null` when no log was written. The 16KB-truncated `text` on the run record is the agent's *final message*; the log preserves the *whole* stream so a driver can inspect what the agent actually did (esp. on a failure). |
| `list_workflows()` | List declarative workflow recipes found in `<repo>/workflows/`. Returns `{workflows, errors, workspace}` — malformed recipe files land in `errors` (filename → message). |
| `run_workflow(name, inputs?)` | Run a workflow recipe; integration is gated off by default. |
| `list_teams()` | List adversarial review teams found in `<repo>/teams/`. Returns `{teams, errors, workspace}` — malformed team files land in `errors` (filename → message). |
| `run_team(name, target, run_id?/base?/head?/paths?/text?)` | Run a panel of independent read-only reviewers over one subject (`run` diff, commit `range`, a `plan`, or an `audit` of the repo). Returns `unified_report` (read first) plus each reviewer's full report; all persisted under `.marshal/reports/<stamp>-<team>-<id>/`. **Computes no verdict** — collecting the objections and deciding is the caller's job. Never integrates. Runnable form: [`examples/adversarial_review.py`](../examples/adversarial_review.py); team templates in [`examples/teams/`](../examples/teams/). |

## Use it as a CLI

```bash
marshal init               # scaffold a starter fleet.config.yaml in the current repo
marshal doctor             # preflight: toolchain, auth, and backend safe-edit permission_fidelity
marshal backends           # list backends, availability, and safe-edit permission_fidelity
marshal models             # list the optional `models:` catalog from fleet.config.yaml
marshal run --goal "…"     # run a task on a client (or ad-hoc by --backend + --model); blocks until done
marshal spawn --goal "…"   # start a task in the background; returns its RUNNING record at once
marshal status             # list runs, newest first (--limit/--status/--task-id/--since-hours/--full)
marshal status             # list fleet runs (raw ledger read - see the note below)
marshal logs <run_id>      # print the persisted stdout/stderr for one run (full, not truncated)
marshal clean              # tear down finished runs' worktrees + branches (--scope/--dry-run/--older-than)
marshal usage              # per-provider usage summary (--window session|day|week|month|all, --json)
marshal workflows          # list + validate workflow recipes against the config
marshal workflow run NAME  # execute a workflow recipe (--input key=value, --max-concurrency)
marshal teams              # list + validate review teams (incl. the fail-closed read-only rule)
marshal team run NAME      # run a review panel (--target run|plan|range|audit, --run-id/--base/--head/--path/--text/--plan-file); prints the unified report; exits non-zero only if a reviewer failed to report
marshal workspace list     # show the workspace registry
marshal workspace add <name> [path]  # register a repo (scaffolds fleet.config.yaml; path defaults to cwd)
marshal workspace remove <name>      # drop a workspace from the registry
marshal mcp                # run the MCP server over stdio
```

`usage`, `status`, `logs`, and `models` accept `--repo` (default: `$MARSHAL_REPO` or cwd) to target a
repo without the MCP workspace registry.

**`marshal status` reads the ledger raw and never changes it.** It builds no Fleet, so it does not
reconcile: a run whose supervising process died can still read `running` here until a process that
holds `.marshal/fleet.lock` next reconciles (the MCP server does this on `status`/`get_run`). This is
deliberate — a short-lived CLI that reconciles is exactly what once stamped live agents `failed`, so
the CLI observes and the lock holder decides. If a `running` row looks stale, check the MCP `status`
or `cancel_run` it; do not assume the agent is alive. Human output shows `unavailable` when a run's
cost provenance is unknown (Cursor, Command Code, etc.); measured costs — including a genuine
`$0.0000` from `native` / `admin-api` — show as dollar amounts. `run`/`spawn` accept `--repo`, `--config`, `--client` (or
ad-hoc `--backend` + `--model`), and `--duration` (preset or seconds).

**Config path matters.** `run`/`spawn` resolve clients from `<repo>/fleet.config.yaml` (or
`$MARSHAL_CONFIG` / `--config`). Default `--repo` is cwd — running outside the project root with no
`--repo`/`MARSHAL_REPO` loads zero clients and warns on stderr. A path that is not a git work tree
fails immediately (doctor-aligned: `not a git work tree`) and does **not** lead with the
missing-config copy hint. Prefer:

```bash
marshal run --repo /path/to/project --client goose-cursor --goal "…"
# or
cd /path/to/project && marshal run --client goose-cursor --goal "…"
```

Ad-hoc (no named client required):

```bash
marshal run --backend goose --model cursor-agent/auto --goal "Reply with exactly: pong"
```

Goose `--model` accepts either a bare model name (Goose's configured `active_provider`) or
`provider/model` (e.g. `cursor-agent/auto`). Both sides of a slash must be non-empty —
`cursor-agent/` and `/auto` are rejected before a worktree is created.

### `marshal usage`

`marshal usage` rolls up the immutable `usage/events.jsonl` ledger into a human-friendly table with
columns `name · runs · succeeded · cost_usd · cost split · input_tokens · output_tokens ·
cache_read_tokens · cache_write_tokens`, printed for `by_backend`, `by_client`, `by_model`, and
`by_backend_model` (the compound `<backend>/<model>` breakdown - useful when one backend runs
multiple models). The token columns make the previously-hidden per-client/model/cache-read/
cache-write spend visible; the cost split collapses the native / admin-api zeros so a row stays
readable.

Use `--window` to scope the rollup - `session`, `day` (last 24h), `week` (7d), `month` (30d), or
`all` (default; the full ledger). CLI and MCP accept the **same** set (shared
`usage_window_since` helper). On MCP, `session` means since the Fleet's `session_start` (server
wake). The CLI has no long-lived Fleet, so `session` honestly means since this invocation
(typically empty / ~$0) — the help text and human-readable output say so plainly; use
`day`/`week`/`month` for rolling spend. With `--json` the existing
`totals / by_backend / by_client / by_model` shape is preserved (the test that pins it still
passes); the response adds `by_backend_model`, the resolved `window`, and the `since` timestamp
used to filter.

Add `--config fleet.config.yaml` to also surface any `budgets:` declared there. The human output
gets a `budgets` table with columns `scope · window · spent · limit · remaining · mode`
(`mode` is `enforce` or `soft-warn`; aligned via `_align_rows`); the JSON output adds a `budgets`
list (including `enforce` and `spent_known`). No `budgets:` configured = no `budgets` section / key (the "no behavior
change" contract for users who don't opt in). Default is soft-warn (stderr when a cap is met; the
run proceeds); `enforce: true` refuses matching over-cap spawns and serializes matching in-flight
spawns (see [`config.md`](config.md)). Budgets are only meaningful for backends that report cost —
subscription / unknown-cost backends report `$0` / `unavailable`, so a $ cap on them never triggers
and reads `unavailable` spent when runs exist with no priced cost (empty ledger still shows
`$0.0000`; no fake percentage, no fabricated "remaining"). The CLI is single-repo by
nature; MCP `usage` is likewise always one workspace (no cross-workspace spend merge).

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
line with the subscription tier + current model after `cursor-agent status` reports authenticated,
or `plan:goose` with the configured provider + model after `goose info --check` succeeds). Doctor
**fails closed** when a `verifies_auth` backend is present but unauthenticated: Cursor
(`status`/`isAuthenticated` — not bare `about`/`model: Auto`), Goose, Claude Code, Command Code
(`status --json`, not config.json alone), OpenCode (`auth list`), and Codex (`login status`).
Antigravity stays path-only (no cheap auth probe). Doctor is preflight only — it does not hard-block
spawn. For every config key see [`config.md`](config.md).

## Use it as a library

```python
from pathlib import Path
from marshal_engine.core.config import load_config
from marshal_engine.interfaces.service import MarshalService

service = MarshalService(Path("."), load_config("fleet.config.yaml"))
record = service.run_agent("implementer", "Add a docstring to hello()")
print(record.status, record.cost_usd, record.worktree)
print(service.usage()["totals"])
```

Full runnable scripts (including `run_many` + `then`, `read_paths`, review teams, and
multi-workspace) live under [`examples/`](../examples/); start with
[`examples/library_quickstart.py`](../examples/library_quickstart.py).

Each run lands in its own git worktree under `.marshal/worktrees/`, with state in
`.marshal/runs/<run_id>.json` (one file per run), usage in `.marshal/usage/`, and the **full raw
stdout/stderr** in `.marshal/logs/<run_id>.log` (so a driver can `marshal logs <run_id>` to
inspect what the agent actually did — esp. on a failure, where the 16KB-truncated `text` on the
run record is rarely enough). Optional `task_id` values (and the derived run directory name)
must be safe path segments — see the worktree isolation bullet in [`SECURITY.md`](../SECURITY.md).

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
| OpenCode | yes | yes (tokens + cost) | `permission_fidelity=enforced-denies`. Force `opencode-go/*` for the Go sub; via EastRouter (`eastrouter/<id>`) the CLI can't price a custom provider, so cost is `unavailable`. Doctor auth via `opencode auth list` (any credential/env). `safe-edit` stamps `OPENCODE_CONFIG_CONTENT` (`question: deny` + curated denies). |
| Cursor | yes | no | `permission_fidelity=enforced-denies`. Tokens/cost only via Team/Enterprise Admin API. Doctor auth via `cursor-agent status` (`isAuthenticated`); `about` only enriches plan/model after auth. `safe-edit` temporarily merges an engine-managed deny list into the worktree's `.cursor/cli.json` alongside `--force` (includes Write denies for the policy file itself); the file's exact prior state is restored before the run returns, so the overlay never shows up in diffs, commits, or integration. A pre-existing malformed `cli.json` fails the run (preserved untouched). |
| Codex | yes | best-effort | `permission_fidelity=enforced-denies`. Doctor auth via `codex login status`. `workspace-write` sandbox for safe-edit; real cost via EastRouter `usage_api` (`admin-api`), else `unavailable`. |
| Command Code | yes | no | `permission_fidelity=boundary-only`. Hosted account; `-p` reports no tokens/cost, so usage is `unavailable` (spend in its dashboard). Doctor auth via `command-code status --json` (config.json alone is not auth). `plan` for read-only; `safe-edit`/`yolo` both `--yolo` (no per-tool deny grammar yet). |
| Antigravity | yes | no | `permission_fidelity=boundary-only`. Worktree writes work (the run's worktree is pre-registered in trustedWorkspaces and passed via `--add-dir`); supports `safe-edit`/`yolo` (no `read-only`). Doctor is path-only (no cheap auth/status probe). PTY wrapper still TODO. **Single Marshal process per host** when using this backend — see note below. |
| Claude Code | yes | yes (tokens + cost) | `permission_fidelity=boundary-only`. Native `acceptEdits` for safe-edit with **no Marshal deny layer**; cost is native (no estimation). Doctor auth via `claude auth status`. |
| Goose | yes | best-effort | `permission_fidelity=boundary-only`. Headless via `GOOSE_MODE=auto` (Marshal sets it). Pin Cursor with model `cursor-agent/auto` (needs `cursor-agent login` and Goose `active_provider: cursor-agent`). Form is `provider/model` or a bare model; empty sides (`cursor-agent/`, `/auto`) fail fast. Doctor probes auth via `goose info --check` (fails closed if not configured / not logged in). Example client name in `fleet.config.example.yaml`: `goose-cursor`. Stream-json tokens when the provider reports them; cost is `native` only when positive — `cost: 0` / tokens-only stay `unavailable`. |

### Antigravity: one Marshal process per host

`agy`'s `trustedWorkspaces` list lives in a **host-global** settings file
(`~/.gemini/antigravity-cli/settings.json`). Marshal file-locks that settings transaction across
processes (so concurrent writers cannot drop each other's grant from an interleaved
read-modify-write), but the in-flight **refcount that decides when to revoke** a Marshal-introduced
entry is **process-local**. If two Marshal processes (e.g. a long-lived MCP server and a CLI) both
register the same cwd, the first to finish can revoke the grant while the other is still running —
`agy` then diverts edits to its scratch dir.

**Run one Marshal process per host when using the Antigravity backend** (MCP server *or* CLI, not
both concurrently), or accept that mid-run revoke risk. Same-cwd collisions across processes are
narrow in practice (each run gets a unique worktree path); a cross-process claim ledger was rejected
because stale claims would leak trust grants forever and widen agy's write scope. See
[`design.md`](design.md) for the same constraint.

See [`design.md`](design.md) for per-backend invocation details and [`status.md`](status.md)
for what's verified.
