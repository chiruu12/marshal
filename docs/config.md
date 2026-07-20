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
| `backend` | string | *(required)* | Backend id to invoke (`opencode`, `cursor`, `codex`, `claude-code`, `antigravity`, `command-code`, …). | `backend: opencode` |
| `model` | string \| omitted | `null` | Model id passed to the backend. OpenCode with no model defaults to `opencode-go/glm-5.2` at resolve time. OpenCode `fireworks-ai/*` models are rejected at load. | `model: claude-sonnet-4-6` |
| `permission` | `read-only` \| `safe-edit` \| `yolo` | from `defaults` | Overrides the fleet default for this client. | `permission: safe-edit` |
| `timeout_s` | int | from `defaults` | Per-client hard timeout (seconds). | `timeout_s: 600` |
| `secret_ref` | string \| omitted | `null` | **Advisory only.** When set to `env:VAR`, `marshal doctor` warns if `VAR` is unset. Marshal does **not** inject this into the backend process — each CLI authenticates via its own login. | `secret_ref: env:ANTHROPIC_API_KEY` |
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
| string or argv list \| omitted | `null` | Command run once in each fresh worktree **before** the agent starts (e.g. `uv sync`). Accepts a shell string or YAML list. Marshal scrubs the driver's `VIRTUAL_ENV`. Non-zero exit fails the run early. **Security:** this is arbitrary argv as your user — `marshal doctor` warns when set. | `worktree_setup: uv sync --extra dev` |

### `verify`

| Type | Default | What it does | Example |
|------|---------|--------------|---------|
| string or argv list \| omitted | `null` | Gate command run in the worktree **after** a run that would otherwise be `succeeded` and changed files. Text-only replies are never gated. Non-zero exit → status `verify_failed`; output tail stored on the run record. Same trust model as `worktree_setup`. | `verify: uv run pytest -q` |

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
| `cost` | string | `""` | Cost provenance hint (`native`, `admin-api`, `estimated`, `scraped`, `unavailable`). | `cost: native` |
| `quota_type` | string | `""` | Billing shape hint (`metered`, `subscription`, `unavailable`). | `quota_type: subscription` |
| `notes` | string | `""` | Free-form note for the driver. | `notes: Go subscription` |

### `budgets[]`

Optional dollar caps. Checked at run start **before** worktree creation. Default is **soft-warn**
(stderr only). Set `enforce: true` to refuse matching spawns when the windowed spend already meets
the cap (`BudgetExceeded`). Subscription / unknown-cost backends report `$0`, so a budget on them
never triggers.

`enforce: true` also admits **at most one in-flight matching spawn** per budget (scope + window +
limit). Parallel `run_many` / concurrent `spawn` would otherwise all pass the same pre-run ledger
snapshot and overshoot the cap before any usage is recorded. The next matching spawn is refused
until the holder finishes (and records spend). Advisory budgets do not take a concurrency slot.

| Key | Type | Default | What it does | Example |
|-----|------|---------|--------------|---------|
| `backend` | string \| omitted | `null` | Scope the cap to one backend. Set **at most one** of `backend` or `client`; omit both for a fleet-wide cap. | `backend: claude-code` |
| `client` | string \| omitted | `null` | Scope the cap to one configured client name. | `client: planner` |
| `window` | `session` \| `week` \| `month` | *(required)* | Time window for spend aggregation. | `window: week` |
| `limit_usd` | float (> 0) | *(required)* | Dollar cap for the scope and window. | `limit_usd: 25.0` |
| `enforce` | bool | `false` | When `true`, refuse new matching spawns once spend ≥ cap, and serialize matching in-flight spawns. When `false`, print a soft warning only. | `enforce: true` |

## `~/.marshal/workspaces.yaml`

Central registry for multi-repo MCP. Override the path with `MARSHAL_WORKSPACES_FILE`. Workspaces
declared here are merged with the default workspace (`MARSHAL_REPO`) and `MARSHAL_WORKSPACES` env
entries; first declaration of a name/path wins. Each workspace loads its own `<repo>/fleet.config.yaml`
and keeps its own `.marshal` ledger.

| Key | Type | Default | What it does | Example |
|-----|------|---------|--------------|---------|
| `workspaces` | map name → path | `{}` | Named repos the MCP server can target via `workspace=`. Names must match `[A-Za-z0-9._-]+` and cannot be `default`. Paths must be existing directories. | `workspaces: { backend: /abs/path/to/backend }` |
| `max_concurrent` | int (> 0) \| omitted | `null` | Process-wide cap on concurrent agent runs across all workspaces when multi-repo is in play. Overridden by `MARSHAL_MAX_CONCURRENT`. | `max_concurrent: 8` |

Register workspaces with `marshal workspace add` or the `add_workspace` MCP tool; the file is
hot-reloaded for **additions** without reconnecting.

## Environment variables

| Variable | Type | Default | What it does | Example |
|----------|------|---------|--------------|---------|
| `MARSHAL_REPO` | path | cwd | Repo root for the **default** workspace (always named `default`). | `MARSHAL_REPO=/projects/myapp` |
| `MARSHAL_CONFIG` | path | `<MARSHAL_REPO>/fleet.config.yaml` | Fleet config for the **default** workspace only. | `MARSHAL_CONFIG=/projects/myapp/fleet.config.yaml` |
| `MARSHAL_WORKSPACES` | string | unset | Additional workspaces: comma- or newline-separated `name=/abs/path` entries (back-compat with the registry file). | `MARSHAL_WORKSPACES=frontend=/abs/fe,backend=/abs/be` |
| `MARSHAL_WORKSPACES_FILE` | path | `~/.marshal/workspaces.yaml` | Path to the central workspace registry file. | `MARSHAL_WORKSPACES_FILE=/cfg/workspaces.yaml` |
| `MARSHAL_MAX_CONCURRENT` | int (> 0) | unset | Process-wide concurrent-run cap. Takes precedence over the registry file's `max_concurrent`. When multi-repo is active and neither is set, defaults to `8`. A lone default workspace with no registry file stays uncapped. | `MARSHAL_MAX_CONCURRENT=4` |
| `MARSHAL_NO_PATH_FIX` | any (truthy) | unset | When set, skip merging the user's login-shell `PATH` at engine entry. Use in hermetic CI or when PATH is already correct. | `MARSHAL_NO_PATH_FIX=1` |
| `LLM_API_KEY` | string | unset | Preferred secret for Marshal Recall (Cognee). Wins over deprecated inline `memory.llm_api_key` when both are set. | `export LLM_API_KEY=...` |

## Per-spawn duration presets (MCP / CLI)

Not fleet-config keys, but accepted by `run_agent`, `spawn`, and `run_many` jobs as `duration`:

| Preset | Seconds |
|--------|---------|
| `short` | 300 |
| `medium` | 1200 |
| `large` | 6000 |
| `long` | 24000 |

A positive integer (or numeric string) is also accepted as raw seconds.
