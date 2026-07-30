# Configuration reference

Marshal reads fleet settings from `fleet.config.yaml` (per repo), a central workspace registry at
`~/.marshal/workspaces.yaml`, and a handful of `MARSHAL_*` environment variables. This document
lists every key the engine and MCP server honor today.

## `fleet.config.yaml`

Copy `fleet.config.example.yaml` to `fleet.config.yaml` in the repo root (or point `MARSHAL_CONFIG`
at another path for the default workspace). Each named **client** pins a backend, permission tier,
and optional model. Keys under `defaults:` are merged into every client before per-client overrides
are applied.

### `defaults`

| Key | Type | Default | What it does | Example |
|-----|------|---------|--------------|---------|
| `permission` | `read-only` \| `safe-edit` \| `yolo` | `safe-edit` | Normalized permission tier passed to the backend adapter. Headless runs must not use a prompting mode. | `permission: read-only` |
| `timeout_s` | int (seconds) | `600` | Hard external timeout for each agent run on clients that do not override it. | `timeout_s: 1200` |

### `clients.<name>`

Each entry under `clients:` is a named client. The YAML key is the client name (used by MCP/CLI).

| Key | Type | Default | What it does | Example |
|-----|------|---------|--------------|---------|
| `backend` | string | *(required)* | Backend id to invoke (`opencode`, `cursor`, `codex`, `claude-code`, `antigravity`, `command-code`, `goose`, …). Goose models use `provider/model` (e.g. `cursor-agent/auto`) or a bare model; empty sides around `/` are rejected. | `backend: goose` |
| `model` | string \| omitted | `null` | Model id passed to the backend. OpenCode with no model defaults to `opencode-go/glm-5.2` at resolve time. OpenCode `fireworks-ai/*` models are rejected at load. Goose rejects malformed `provider/` / `/model` strings before spawn. | `model: claude-sonnet-4-6` |
| `permission` | `read-only` \| `safe-edit` \| `yolo` | from `defaults` | Overrides the fleet default for this client. | `permission: safe-edit` |
| `timeout_s` | int | from `defaults` | Per-client hard timeout (seconds). | `timeout_s: 600` |
| `env` | map of strings | `{}` | Literal environment variables merged into each agent child for this client only. **This is the allowlist escape hatch:** agent children inherit only an operational base set plus that backend's credential vars (see below); use `env:` to pass an extra **non-secret** var the base set omits (provider routing, e.g. `CODEX_HOME`). A leading `~` in values is expanded. **Refused at load:** empty keys; `PATH` (Marshal merges the user's interactive PATH at engine entry — overriding it here would break that recovery); any key whose name contains `KEY`, `TOKEN`, `SECRET`, `PASSWORD`, or `CREDENTIAL` (case-insensitive) — credentials are not configured here. Does not undo Marshal's hygiene (`VIRTUAL_ENV`, `MARSHAL_*` are never inherited; `extra_env` from backend `prepare()` can still set them). There is no blanket "inherit the driver environment" flag. | `env: { CODEX_HOME: ~/.codex-eastrouter }` |
| `secret_ref` | string \| omitted | `null` | **Advisory only.** When set to `env:VAR`, `marshal doctor` warns if `VAR` is unset. Marshal does **not** inject `secret_ref` into the child. Each backend may forward a small fixed credential allowlist from the parent when present (e.g. `claude-code` → `ANTHROPIC_API_KEY`, `cursor` → `CURSOR_API_KEY`, `codex` → `OPENAI_API_KEY`/`CODEX_API_KEY`, `opencode` → `OPENCODE_API_KEY`, `command-code` → `COMMAND_CODE_API_KEY`, `antigravity` → `ANTIGRAVITY_API_KEY`, `goose` → `GOOSE_PROVIDER`/`GOOSE_MODEL`). Prefer CLI login; `marshal doctor` reports `child-env:<backend>` forwarding and warns when a set `secret_ref` var is not on that backend's allowlist. | `secret_ref: env:ANTHROPIC_API_KEY` |
| `usage_api` | string \| omitted | `null` | Optional provider usage API to fetch **real** post-run cost (e.g. `eastrouter`). Unset = price from the local table or `unavailable`. | `usage_api: eastrouter` |

### `context`

Fleet-wide layered context strings.

| Key | Type | Default | What it does | Example |
|-----|------|---------|--------------|---------|
| `worker` | string \| omitted | `null` | Prepended to every worker agent's goal (shared operating assumptions). | `worker: "Run uv run pytest -q before finishing."` |
| `driver` | string \| omitted | `null` | Surfaced to the driver via `list_clients` / `list_models` as `driver_context`. | `driver: "Integrate manually after review."` |

### `worktree_setup`

| Type | Default | What it does | Example |
|------|---------|--------------|---------|
| string or argv list \| omitted | `null` | Command run once in each fresh worktree **before** the agent starts (e.g. `uv sync`). Accepts a shell string or YAML list. Marshal scrubs the driver's `VIRTUAL_ENV`. Non-zero exit fails the run early. **Security:** argv[0]'s **basename** must be on the allowlist (`uv`, `npm`, `pnpm`, `yarn`, `bun`, `make`, `cargo`, `go`, `pytest`, `python`/`python3`, `poetry`, `pip`/`pip3`, `ruff`, `mypy`, `tox`, `nox`) unless `allow_unsafe_commands: true`. This is a typo / wrong-binary guard, **not** a sandbox: allowlisted tools still run arbitrary code via their args (e.g. `python -c …`, `uv run sh -c …`, `make -f …`). Non-allowlisted basenames (including `sh`/`bash`) need the opt-in. Relative path argv[0] (e.g. `.venv/bin/python`) is refused without the opt-in — it resolves against the worktree cwd and may be agent-rewritten; bare names and absolute paths are basename-checked. Refusals happen at **config load** (runtime setup/verify keep the same check as a backstop). `marshal doctor` still warns for allowlisted / opted-in setups. | `worktree_setup: uv sync --extra dev` |

### `verify`

| Type | Default | What it does | Example |
|------|---------|--------------|---------|
| string or argv list \| omitted | `null` | Gate command run in the worktree **after** a run that would otherwise be `exited_clean` and changed files (post-agent). Text-only replies are never gated. Non-zero exit → status `verify_failed`; output tail stored on the run record. Same allowlist rules as `worktree_setup`, but timing differs: allowlisted runners execute **agent-modified** project content (`Makefile`, npm scripts, tests, package code) under your identity — allowlist ≠ sandbox. Acceptable when you trust the config and treat agent tasks as code you might run yourself; still review `collect_run` / CI before integrate. See `SECURITY.md`. | `verify: uv run pytest -q` |

### `allow_unsafe_commands`

| Type | Default | What it does | Example |
|------|---------|--------------|---------|
| bool \| omitted | `false` | Opt-in to run `worktree_setup` / `verify` when argv[0]'s basename is **not** on the allowlist (e.g. `sh -c …`) or when argv[0] is a relative path. When false, those forms are **rejected at config load** (runtime setup/verify keep the same check as a backstop). Does not restrict args of allowlisted tools and is not a sandbox. | `allow_unsafe_commands: true` |

### `integrate_run_hooks`

| Type | Default | What it does | Example |
|------|---------|--------------|---------|
| bool \| omitted | `false` | When `false`, `commit_run` / `integrate` pass `git --no-verify` so prompting pre-commit/pre-merge hooks cannot deadlock a headless driver, and so Marshal does not execute possibly **agent-modified** / repo-controlled hook scripts. Set `true` only when hooks are known **non-interactive** *and* you trust their provenance for your threat model. Prompting hooks can hang until the git timeout (`GIT_TERMINAL_PROMPT=0` + closed stdin + timeout still apply). Prefer `verify:` + human/CI review over hooks when unsure. See `SECURITY.md`. | `integrate_run_hooks: true` |

### `retries`

| Type | Default | What it does | Example |
|------|---------|--------------|---------|
| int (≥ 0) | `2` | How many times to re-run on **transient** failures (rate limit, 5xx, connection error) with backoff. `0` disables. Genuine task failures and timeouts are never retried. | `retries: 0` |

### `models[]`

Optional catalog the driver reads via `list_models` / `marshal models`. Pure metadata — does **not** change routing.

| Key | Type | Default | What it does | Example |
|-----|------|---------|--------------|---------|
| `id` | string | *(required)* | Provider/model id (same shape as a client's `model`). | `id: opencode-go/glm-5.2` |
| `backends` | list of strings | *(required, non-empty)* | Backends that can run this model. | `backends: [opencode]` |
| `cost` | string | `""` | Cost provenance hint (`native`, `admin-api`, `unavailable`). | `cost: native` |
| `quota_type` | string | `""` | Billing shape hint (`metered`, `subscription`, `unavailable`). | `quota_type: subscription` |
| `notes` | string | `""` | Free-form note for the driver. | `notes: Go subscription` |

### `budgets[]`

Optional dollar caps **per workspace** (that repo's `fleet.config.yaml` + `.marshal` ledger — no
cross-workspace merge; see [`design.md`](design.md) §6 and [`mcp-tools.md`](mcp-tools.md)).
Checked at run start **before** worktree creation. Default is **soft-warn** (stderr only). Set
`enforce: true` to refuse matching spawns when the windowed spend already meets the cap
(`BudgetExceeded`). Subscription / unknown-cost backends report `$0`, so a budget on them never
triggers.

`enforce: true` also admits **at most one in-flight matching spawn** per budget (scope + window +
limit), across processes (CLI + MCP on the same repo) as well as threads. Parallel `run_many` /
concurrent `spawn` would otherwise all pass the same pre-run ledger snapshot and overshoot the
cap before any usage is recorded. Reservations live in `.marshal/budget_gate.json` under an
`fcntl.flock`; a dead holder's pid is reclaimed so a crash cannot lock out future spawns. Lock
acquire times out after 5s and refuses the spawn (fail-closed). The next matching spawn is
refused until the holder finishes (and records spend). Advisory budgets do not take a
concurrency slot and stay lock-free.

> **Scope: one process.** The concurrency slot is held in memory, so it binds spawns made by a
> single Marshal process. Two processes against the same repo (a `marshal run` CLI invocation
> alongside a running MCP server, or two Fleets) each evaluate the same ledger snapshot and can
> both admit, so the cap can be overshot by roughly the number of processes. Treat `enforce` as a
> strong guard within one driver, not a distributed lock. Tracked in
> [#182](https://github.com/chiruu12/marshal/issues/182).

Editing `fleet.config.yaml` hot-reloads budget **specs** (limits, scopes, `enforce`) on the next
call, but never forks budget **state**: the in-flight guard and the `session` window clock are kept
per workspace across the reload, so an **unrelated** config edit mid-run cannot admit a concurrent
spawn past an enforced cap or reset session spend accounting. Changing an enforce budget's own
`limit_usd` / scope / `enforce` shape mid-flight can still re-key the concurrency slot (an in-flight
run under the old key does not block a spawn admitted under the new key).

| Key | Type | Default | What it does | Example |
|-----|------|---------|--------------|---------|
| `backend` | string \| omitted | `null` | Scope the cap to one backend. Set **at most one** of `backend` or `client`; omit both for a fleet-wide cap. | `backend: claude-code` |
| `client` | string \| omitted | `null` | Scope the cap to one configured client name. | `client: planner` |
| `window` | `session` \| `week` \| `month` | *(required)* | Time window for spend aggregation. | `window: week` |
| `limit_usd` | float (> 0) | *(required)* | Dollar cap for the scope and window. | `limit_usd: 25.0` |
| `enforce` | bool | `false` | When `true`, refuse new matching spawns once spend ≥ cap, and serialize matching in-flight spawns **within one process** (see the scope note above). When `false`, print a soft warning only. | `enforce: true` |

## `<repo>/teams/*.yaml`

Adversarial review panels for `run_team` / `marshal team run`. One file per team; the filename stem
is the default team name. Discovered from `<repo>/teams/` only — a team file outside that directory
is refused, because a team's `rubric` is prompt text delivered to fleet agents. Starter panels to
copy live in `examples/teams/`. `marshal doctor` validates every declared team (WARN, never FAIL —
teams are optional).

| Key | Type | Default | Meaning | Example |
|-----|------|---------|---------|---------|
| `name` | string | *(filename stem)* | Team name. `[A-Za-z0-9._-]`, no leading `.`/`-`, ≤ 40 chars — it becomes part of the run's `task_id` and the report directory. | `name: hard-gate` |
| `description` | string | `""` | What this panel is for; shown by `list_teams`. | |
| `target` | `run` \| `plan` \| `range` \| `audit` | `run` | What the panel reviews. The subject passed at call time must match. | `target: range` |
| `roles[]` | list | *(required, ≥ 2)* | The reviewer lenses. One role is a single opinion, so two is the minimum. | |
| `roles[].name` | string | *(required)* | Lens name; becomes `<role>.md` in the report directory. Same charset rule as `name`. | `name: correctness` |
| `roles[].client` | string | *(required)* | A client from `clients`. **Must be `permission: read-only`** — a team naming a writable client is a config error raised before any reviewer spawns. Point each lens at a different backend. | `client: codex-readonly` |
| `roles[].rubric` | string | *(required)* | The one lens this role holds, and what counts as evidence. May not be empty. | |

There is deliberately **no decision/quorum key**: the engine parses no reviewer prose and computes
no verdict. See [`design.md`](design.md) for why.

## `<repo>/.marshal/reports/`

One directory per panel run (`<stamp>-<team>-<team_run_id>/`) holding `<role>.md` per reviewer plus
`README.md`, the unified report. Written by `run_team`; never read back by the engine. Under
`.marshal/`, so it is covered by the same gitignore as the rest of Marshal's runtime state.

## `~/.marshal/workspaces.yaml`

Central registry for multi-repo MCP. Override the path with `MARSHAL_WORKSPACES_FILE`. Workspaces
declared here are merged with the default workspace (`MARSHAL_REPO`) and `MARSHAL_WORKSPACES` env
entries; first declaration of a name/path wins. Each workspace loads its own `<repo>/fleet.config.yaml`
and keeps its own `.marshal` ledger.

| Key | Type | Default | What it does | Example |
|-----|------|---------|--------------|---------|
| `workspaces` | map name → path | `{}` | Named repos the MCP server can target via `workspace=`. Names must match `[A-Za-z0-9._-]+` and cannot be `default`. Paths must be existing directories. | `workspaces: { backend: /abs/path/to/backend }` |
| `max_concurrent` | int (> 0) \| omitted | `null` | Process-wide cap on concurrent agent runs across all workspaces when multi-repo is in play. Overridden by `MARSHAL_MAX_CONCURRENT`. | `max_concurrent: 8` |

Register workspaces with `marshal workspace add` (the recommended, operator-run path); the file is
hot-reloaded for **additions** without reconnecting. The MCP `add_workspace` tool is an explicit
server opt-in: it refuses unless the server was started with
`MARSHAL_ALLOW_MCP_WORKSPACE_REGISTRATION=1` (see below).

## Child process environment (agents + worktree setup)

Spawned agent processes do **not** inherit the driver's full `os.environ`. Marshal builds a
child env from:

1. **Operational base** — `PATH`, `HOME`/`USER`/`SHELL`, `TERM`, temp dirs, `LANG`/`LC_*`,
   `TZ`, proxy vars, `SSL_CERT_*` / CA bundle vars, `XDG_*`, and on macOS `__CF*` / `__PYVENV*`
   (plus a small set of Windows identity/path roots). Loader-hijack and credential-shaped
   names are excluded.
2. **That backend's credential vars only** — see `secret_ref` row above; a cursor run never
   sees `ANTHROPIC_API_KEY`.
3. **Per-client `env:` literals** — the escape hatch for an omitted non-secret var.
4. **Backend `prepare()` / `extra_env`** — e.g. `GOOSE_MODE`, `OPENCODE_CONFIG_CONTENT`.

`VIRTUAL_ENV`, `PYTHONHOME`, and every `MARSHAL_*` session var are always dropped from the
parent (unless deliberately re-set via `extra_env`). Worktree `setup` / `verify` use the same
operational base with **no** backend credentials.

**Upgrade note:** if a backend suddenly fails to authenticate after upgrading, run
`marshal doctor` and check `child-env:<backend>`. Env-based provider keys that are not on that
backend's allowlist (e.g. using `ANTHROPIC_API_KEY` with OpenCode instead of
`opencode auth login`) are no longer ambiently inherited — use the CLI login, or a credential
var that backend forwards.

## Environment variables

| Variable | Type | Default | What it does | Example |
|----------|------|---------|--------------|---------|
| `MARSHAL_REPO` | path | cwd | Repo root for the **default** workspace (always named `default`). | `MARSHAL_REPO=/projects/myapp` |
| `MARSHAL_CONFIG` | path | `<MARSHAL_REPO>/fleet.config.yaml` | Fleet config for the **default** workspace only. | `MARSHAL_CONFIG=/projects/myapp/fleet.config.yaml` |
| `MARSHAL_WORKSPACES` | string | unset | Additional workspaces: comma- or newline-separated `name=/abs/path` entries (back-compat with the registry file). | `MARSHAL_WORKSPACES=frontend=/abs/fe,backend=/abs/be` |
| `MARSHAL_WORKSPACES_FILE` | path | `~/.marshal/workspaces.yaml` | Path to the central workspace registry file. | `MARSHAL_WORKSPACES_FILE=/cfg/workspaces.yaml` |
| `MARSHAL_MAX_CONCURRENT` | int (> 0) | unset | Process-wide concurrent-run cap. Takes precedence over the registry file's `max_concurrent`. When multi-repo is active and neither is set, defaults to `8`. A lone default workspace with no registry file stays uncapped. | `MARSHAL_MAX_CONCURRENT=4` |
| `MARSHAL_ALLOW_MCP_WORKSPACE_REGISTRATION` | exactly `"1"` | unset (disabled) | Enables the MCP `add_workspace` tool. The value must be **exactly `1`** — unset, empty, `0`, `false`, `true`, or anything else keeps the tool disabled (fail-closed; no generic truthiness). Captured once when the MCP app is built, so mutating the environment mid-session cannot widen authority. Affects **only** the MCP tool: `marshal workspace add`, registry-file edits, and the `MARSHAL_WORKSPACES`/`MARSHAL_REPO` env vars are unaffected. Enabling it delegates registration of any existing host directory to the MCP driver — see `SECURITY.md`. | `MARSHAL_ALLOW_MCP_WORKSPACE_REGISTRATION=1` |
| `MARSHAL_NO_PATH_FIX` | any (truthy) | unset | When set, skip merging the user's login-shell `PATH` at engine entry. Use in hermetic CI or when PATH is already correct. | `MARSHAL_NO_PATH_FIX=1` |

## Per-spawn duration presets (MCP / CLI)

Not fleet-config keys, but accepted by `run_agent`, `spawn`, and `run_many` jobs as `duration`:

| Preset | Seconds |
|--------|---------|
| `short` | 300 |
| `medium` | 1200 |
| `large` | 6000 |
| `long` | 24000 |

A positive integer (or numeric string) is also accepted as raw seconds.
