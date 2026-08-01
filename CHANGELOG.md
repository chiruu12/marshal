# Changelog

All notable changes to this project are documented here.

The format is based on [Keep a Changelog](https://keepachangelog.com/en/1.1.0/), and this project
adheres to [Semantic Versioning](https://semver.org/spec/v2.0.0.html). Marshal is **pre-1.0**: minor
versions may include breaking API changes until 1.0.

## [Unreleased]

### Fixed
- **`marshal models` now answers from the same place the `list_models` MCP tool does.** It read
  `fleet.config.yaml` directly, so on a repo with no `models:` catalog it reported "no catalog,
  add one" while the MCP tool on that same repo returned a full probed list — the CLI claiming
  absence where there was something to show. Both surfaces now go through
  `MarshalService.list_models`, and `marshal models` renders `backend_models`, distinguishing a
  backend that reported nothing from one whose CLI exposes no way to ask. `marshal models --json`
  gains the `backend_models` key.

### Changed
- **`assets/social-card.png` is a committed designed asset**, no longer drawn by
  `assets/render.py` — re-running the script used to overwrite the real card with its rough
  approximation. `render.py` still renders `logo-mark-32.png` from `logo.svg`.
- **`mcp_server.py` split into an `interfaces/mcp_server/` package** (880 lines → 9 modules, largest
  291). Tool handlers group into `tools_inspect`, `tools_runs`, `tools_integrate`, `tools_recipes`,
  and `tools_workspaces`; `schema` holds the parameter descriptions and shared job models; `server`
  builds the app and registers each group. The helpers the handlers used to capture as closure
  variables (`registry`, `offload`, `ws_call`, `run_call`) are now an explicit `ToolContext`, so a
  tool group states what it needs instead of inheriting a nested scope. `build_app`, `build_service`,
  and `main` are re-exported, so `marshal_engine.interfaces.mcp_server` is unchanged for callers.
  The tool surface is byte-identical: every tool's name, description, and JSON input schema was
  diffed before and after (the tool list itself lives in `docs/mcp-tools.md`).
- **`cli.py` split into an `interfaces/cli/` package** (968 lines → 8 modules, largest 209).
  `parser` owns argument wiring and dispatch; handlers group into `inspect` (read-only views),
  `runs`, `recipes` (workflows/teams), and `admin` (init/doctor/workspace/clean), over a shared
  `formatting` display layer and `common` helpers. `main` is re-exported from the package, so
  `marshal_engine.interfaces.cli:main` and the `marshal` command are unchanged.
- **`fleet.py` split into focused modules** (2773 → 2006 lines). `orchestration/provisioning.py`
  owns fail-closed `context_files` / `read_paths` copying, `orchestration/structured.py` owns the
  `output_schema` prompt/parse/validate/redact path, and `orchestration/results.py` owns the
  result and request DTOs. Behaviour is unchanged - the fleet loop imports and calls exactly what
  it did before. `CollectResult`, `IntegrateResult`, `RunRequest`, and the other DTOs are now
  imported from `marshal_engine.orchestration.results`.
- **`marshal_engine` is now organised into layer packages** instead of 23 flat modules:
  `core` (value types, pure logic), `runtime` (processes, git, disk), `accounting` (usage, cost,
  budgets), `backends`, `orchestration` (the fleet loop), and `interfaces` (service, CLI, MCP,
  workspaces, doctor). Imports point strictly downward, with `runtime` and `accounting` as siblings
  that may not import each other; `tests/test_import_layers.py` enforces the direction over the AST
  import graph and replaces the old hand-maintained deny-list of forbidden edges.
- **Published import paths keep working.** `marshal_engine.config`, `.service`, `.teams`, `.state`,
  `.workspaces`, and `.cli` remain importable as re-export shims that bind the same objects, so the
  documented library API and an already-installed `marshal` console script are unaffected. New code
  should import from the layer path (e.g. `marshal_engine.interfaces.service`); the shims are
  scheduled for removal in a later release. Modules that were never part of the documented API moved
  without a shim.

### Removed
- **YAGNI cleanup of unused public / near-public surface.** Dropped unused helpers and fields with
  no in-repo consumers: `AgentResult.ok`, `UsageRecord.duration_ms` (wall-clock remains on
  `AgentResult` / `RunRecord` / ledger `UsageEvent`), `TaskSpec.role` (routing stays driver/config;
  teams' `RoleReview.role` unchanged), `Capabilities.stream_json` / `sessions` / `server_mode`
  (CLI/doctor still surface `json_output` / `native_usage`), `WorktreeManager.list`, module helper
  `workflow.list_workflows` (use `discover_workflows(...).workflows`; MCP/CLI
  `MarshalService.list_workflows` unchanged), `env.base_env_var_names` (survivor `is_base_env_var`),
  unused `WorkspaceRegistry.run_gate` property (`_run_gate` storage kept), `fleet.BudgetExceeded`
  re-export (import from `marshal_engine.accounting.budgets`), unused
  `fetch_run_cost(..., output_tokens=)`
  parameter, and the never-selected `missing_config="silent"` arm.
### Fixed
- **`clean`'s orphan sweep no longer deletes a worktree mid-create (#181).** Between
  `worktrees.create` and the RUNNING record landing, a concurrent `marshal clean` (CLI beside an
  MCP server) saw a directory with no ledger entry and discarded it. `_start` now writes a durable
  `.creating` claim (pid + start time) before create and clears it only after the record's
  `os.replace` publishes (in a `finally`, so a mid-handoff failure cannot leave a stuck claim);
  publish-then-clear leaves no gap where a sweep sees neither shield. The sweep skips a dir whose
  claim holder is still alive (reported under `skipped`) and still reaps genuine orphans when the
  claim is absent or the holder is dead.
- **`cancel_run` no longer `killpg`s a recycled pid after a mid-cancel reap (#183).** Cancel used to
  copy `pid`/`exited` under the lock, release, then signal — so the execute thread could reap and
  the OS reuse the pid before `killpg`. Cancel now re-checks `exited` under the lock immediately
  before signalling and, when the handle carries a start time, refuses a pid whose live identity
  no longer matches. A failed identity probe on a still-alive pid neither signals nor stamps
  `cancelled` — the record stays `running` with an `error` stating the cancel could not be
  confirmed (claiming `cancelled` would leave a live agent behind a lie).

### Added
- **Usage ledger routing facts (Phase 1).** Each `UsageEvent` may carry `task_kind` (caller
  free-text tag) and `goal_digest` (truncated sha256 of the goal — never the text). Both are
  optional so older `events.jsonl` lines still parse and roll up. `task_kind` is accepted on
  `TaskSpec` and threaded through MCP `run_agent` / `spawn` / `run_many` and CLI `run` / `spawn`
  (`--task-kind`). Judgment about the work arrives after the usage line is written, so successful
  `integrate` stamps `outcome: integrated` on the **run record** instead — the ledger stays
  immutable and one-line-per-run. No ranking, registry file, or new MCP tools in this phase.

## [0.2.1] - 2026-07-31

### Changed
- **Relative-path `argv[0]` in `worktree_setup` / `verify` now needs `allow_unsafe_commands: true`.**
  A relative binary (e.g. `.venv/bin/python`, `./scripts/setup.sh`) resolves against the worktree
  and may have been rewritten by the agent whose work it is about to gate. Bare names and absolute
  paths are unaffected. **Upgrade note:** a config using a relative `argv[0]` fails at load until
  you switch to a bare name or an absolute path, or set the opt-in.
- **Codex stock model id is `gpt-5.6-luna`.** Scaffold stub, static `available_models()`,
  playbook, and docs now pin the ChatGPT-account Codex identifier verified with the live CLI
  (`gpt-5.6` alone is rejected).
- **Agent children inherit an env allowlist, not the driver's full environment (#179).**
  Previously `child_env()` copied all of `os.environ` (scrubbing only `VIRTUAL_ENV` /
  `PYTHONHOME` / `MARSHAL_*`), so every spawned agent saw ambient credentials
  (`ANTHROPIC_API_KEY`, `AWS_*`, `GH_TOKEN`, `EASTROUTER_API_KEY`, …). Children now receive
  operational vars plus that backend's own credential allowlist only; unrelated secrets are
  dropped. Per-client `env:` remains the escape hatch for omitted **non-secret** vars
  (secret-shaped keys and `PATH` still refused). Run logs and the 16KB run-record `text`
  redact known credential *values* before persistence (redact **before** any size cut so a
  value straddling the 16KB / verify-tail / error-tail boundary cannot leave a fragment).
  `structured` string leaves and `error` are scrubbed the same way. `marshal doctor`
  reports `child-env` / `child-env:<backend>` forwarding and warns when a set `secret_ref`
  var is not on that backend's allowlist. **Upgrade:** if a backend fails auth after
  upgrading, check `marshal doctor` — env-based keys that are not on the backend allowlist
  are no longer inherited; prefer CLI login (`opencode auth login`, etc.). See
  `docs/config.md` and `SECURITY.md`.

### Fixed
- **`branch_tip` no longer poisons `base_commit` with a ref name when `git rev-parse` fails.**
  A failed rev-parse echoes its argument on stdout (exit 128); that value was stored as
  `base_commit` and compared equal to the branch tip in the `then` chain, silently skipping
  follow-up review stages with a false "primary produced no diff" while the primary had real
  work. `collect_run` on those chained runs also failed with `ambiguous argument`. `branch_tip`
  now raises `WorktreeError`, rejects non-sha tips, and the `then` chain prefers running the
  follow-up when the tip cannot be resolved. Ledger load strips a non-sha `base_commit` to
  `None` so old poisoned records still parse.
- **`list_clients` no longer reports `permission_fidelity: enforced-denies` for `yolo` clients
  (#178).** Fidelity is resolved from the `(backend, permission)` pair: `yolo` → `unrestricted`,
  while `safe-edit` / `read-only` still inherit the backend's safe-edit capability. Doctor
  `permission:<backend>` and `marshal backends` remain backend safe-edit capability (wording
  clarified so the two surfaces are not conflated).
- **Setup/verify allowlist docs no longer overstate the control (#177).** The gate screens
  argv[0]'s basename (a typo / wrong-binary guard) and does not sandbox args — `python -c`,
  `uv run sh -c`, and `make -f` are accepted without `allow_unsafe_commands`, which the docs
  previously denied. `docs/config.md`, `SECURITY.md`, `docs/usage.md`, `docs/design.md`, and the
  in-code comments now describe the real contract.
- **Install docs point at PyPI.** README, SETUP, and `docs/usage.md` recommend
  `uv tool install "MarshalFleet[mcp]"` / `pipx install "MarshalFleet[mcp]"` now that
  MarshalFleet 0.2.0 is published; the `git+https://` path is documented only for tracking
  unreleased work on `main`.
### Fixed
- **`enforce: true` budgets bind across processes.** `EnforceBudgetGate` was an in-memory
  `threading.Lock` only, so a CLI `marshal run` and an MCP server on the same repo could both
  admit past a documented hard cap. Reservations now live in `.marshal/budget_gate.json` under
  an `fcntl.flock` (same RMW idiom as run-record state); a dead holder's pid is reclaimed so a
  crash cannot lock out future spawns. Lock acquire times out after 5s and refuses the spawn
  (fail-closed). A present but corrupt/unreadable reservation file, or a `held` entry with an
  invalid shape, also refuses (fail-closed) rather than treating unknown state as empty;
  soft-warn/reporting stay lenient. A failed `bind` discards the worktree before any RUNNING
  record is written and releases the slot. Unbound placeholders age out after a short TTL so a
  bind-time flock failure whose release cannot re-take the lock cannot poison the slot; Fleet
  renews that TTL during `git worktree add` and `bind` verifies disk ownership (refuse if a
  peer holds the key; re-acquire only when the key is free) so a slow-but-alive holder cannot
  silently share the cap after reclaim. Release paths (`release`, `release_run`, and `begin`
  partial-failure rollback) delete a disk entry only when its reservation token still matches
  the releaser's — key-only cleanup after a lost bind must not free a peer's slot and reopen
  the enforced cap. Soft-warn budgets stay lock-free.

## [0.2.0] - 2026-07-30

### Added
- **`marshal init` scaffolds a starter `fleet.config.yaml`.** Repo-shape-aware stub via the
  existing `scaffold_fleet_config` helper; does not touch the workspace registry. Refuses to
  overwrite an existing config (exit 1). First-run docs and missing-config hints point here
  instead of `marshal workspace add`.
- **Schema-validated structured output on runs.** `TaskSpec.output_schema` (also on the MCP
  `run_agent`/`run_many`/`spawn` tools) asks the agent for a final message that is one JSON object
  conforming to a JSON Schema; the engine extracts and `jsonschema`-validates it and carries the
  result as `structured` on `AgentResult` / `RunRecord` / `collect_run`. A schema-invalid result is
  an honest `failed` run with a `structured_output:` error after retries - never silent prose
  success, never a transient retry. Backends are untouched: the mechanism is prompt-level and
  works with every adapter.
- **Token usage for Cursor and Antigravity (cost stays `unavailable`).** Cursor stamps
  `cacheReadTokens`/`cacheWriteTokens` alongside input/output from the CLI envelope; Antigravity
  runs `--output-format json` and parses text + tokens (availability now requires agy >= 1.1.8,
  surfaced by `doctor`). `cache_write_tokens` flows through `UsageEvent`/bucket rollups to the
  `marshal usage` human and JSON surfaces. No cost is inferred from tokens.
- Requirements stated up front in the README: Python 3.11+, git, and at least one authenticated
  backend CLI - Marshal drives agents, it does not ship one.

### Changed
- **`spawn` returns a `run_id` before worktree provisioning completes.** The RUNNING record is
  registered after `git worktree add` and returned immediately; `read_paths` / `worktree_setup`
  run in the background task. Setup/provision failures terminal-stamp with phase errors
  (`fleet: setup:` / `fleet: provision:`); `cancel_run` works during setup (killpg when a setup
  pid exists, cooperative before); `clean` never reaps while a background task is in flight;
  `collect_run` / `integrate` / `commit_run` return structured results on setup-failed runs.

### Fixed
- **`clean` / `discard` no longer delete non-Marshal branches.** Worktree path cleanup was
  contained under `.marshal/worktrees/`, but `git branch -D` took `RunRecord.branch` unchecked.
  A poisoned terminal run record with `"branch": "main"` made the next `marshal clean` delete
  the operator's main branch without an `integrate`. Branch deletion now refuses any name
  outside the managed `{prefix}/` namespace (fail-closed); the worktree directory is still
  reclaimed, and `clean` reports the refusal under `errors`. `SECURITY.md` no longer overstates
  worktree isolation as a filesystem sandbox.
- **Workflow path loading is contained to the workspace's `workflows/` directory.**
  `run_workflow` accepted any `.yaml`/`.yml` path (absolute or `../` traversal) and
  `find_workflow` composed bare names without a resolve check, so a caller could feed an
  attacker-authored recipe — including `integrate` with `auto: true` — into the fleet. Both
  now mirror team-file containment: bare names and path forms must resolve inside
  `workflows/`. The CLI still searches `workflows/` and `examples/workflows/` on its own
  discovery path.
- **Cost honesty: never claim a measured $0 the provider did not report.**
  - EastRouter rows with missing/null `amount_usd` are skipped for cost attribution (run stays
    `unavailable`) instead of becoming a fabricated `admin-api` $0; an explicit `0.0` still
    attributes as a real free charge.
  - `_apply_external_cost` only replaces `unavailable` cost — a native-cost backend with
    `usage_api` configured keeps its backend-reported cost and `native` source.
  - Budget status lookup failures set `spent_known=false` (spend unknown) instead of asserting
    a known $0; `spent_known` is included in MCP / `marshal usage --json` payloads so drivers
    can tell when `spent_usd` / `remaining_usd` are not measured.
  - Claude Code treats `total_cost_usd: 0` with tokens as `unavailable` (OpenCode/Goose parity)
    rather than claiming a native free run; tokens are preserved.
- **First-run docs and missing-config hints no longer dead-end.** SETUP/usage install
  commands match the working `git+https://` path (MarshalFleet is not on PyPI yet); README's
  60-second path now scaffolds a config via `marshal init`, says the scaffold ships its clients
  commented out, and spawns a client that exists — the previous `--client composer` matched no
  shipped config; the `doctor`/`spawn`/`status` samples match what the CLI actually prints;
  config errors point at `marshal init` instead of an unpackaged `fleet.config.example.yaml`,
  a non-existent `--scaffold` flag, or multi-repo `workspace add`; goose is listed with the other
  backend prerequisites.
- **`marshal doctor` no longer reports "1 clients".** The config line pluralizes on count.
- **Path-traversal `run_id`s are refused at the state/MCP boundary.** `run_id` was never
  validated where it becomes a filesystem path: `FleetState` composed `runs/<run_id>.json`
  directly, and the workspace registry's run-owner scan stat'ed it against every registered
  repo's ledger — so `../../../<ws>/.marshal/runs/<id>` read one workspace's run record through
  another's id (a cross-workspace tenant escape), and any host `*.json` became an existence
  oracle. `FleetState`, `RunLogStore`, and the registry now fail closed on the same safe-charset
  rules as `task_id`, before any path is composed.
- **Retry classifier no longer treats bare status-code mentions as transient.** A stray `429` /
  `502` / `503` / `504` in agent output used to trigger backoff retries; codes now need HTTP /
  provider framing (`http 429`, `status 503`, `error code: 429`, …) or a word-bounded code next to
  a known reason phrase. Phrase markers (`rate limit`, `overloaded`, …) still match. `Popen`
  `OSError`s (not only `FileNotFoundError`) return an actionable `AgentResult` instead of crashing
  `run()`.
- **`marshal workflows` / `marshal workflow run` find bundled example recipes.** The CLI now
  searches both `workflows/` and `examples/workflows/`; a repo-local recipe of the same stem
  shadows the bundled example. Service/MCP resolution stays `<repo>/workflows/` only.
- **Budget-gate reservations and torn ledger records no longer brick the fleet.**
  - Reservation slots reserved so far are released when a later matching enforce budget is already
    held — a multi-budget conflict no longer permanently locks out spawns.
  - Reporting paths skip malformed/torn JSONL lines (stderr warn + count); enforced budgets stay
    fail-closed with an actionable repair message instead of undercounting spend past a hard cap.
  - The gate lock no longer spans a full ledger scan — spend is computed before the lock, then
    revalidated from the appended tail (O(new events)) under the lock.
- **Setup, teardown, and integrate reporting are failure-atomic.**
  - Provisioning exceptions discard the worktree/branch before re-raising (no stranded state).
  - `setup()` matches `verify()`'s `(OSError, SubprocessError)` catch and tears down.
  - A failed `git worktree add -b` best-effort deletes only the branch it created, so a same-id
    retry can succeed.
  - `merged_diff_files` raises on git failure (no silent `changed_files=[]`).
  - `cleanup=True` remove failures stamp a cleanup warning on the run after the terminal status.
  - `clean` age-gates orphaned `*.tmp` under runs/logs; unreachable duplicate returns removed.
- **Run-record and Antigravity settings writes serialize across processes.** `FleetState`
  `update` / `update_if` take an `fcntl.flock` on `runs/<id>.json.lock` around the
  read-modify-write (alongside the per-run thread lock), so a CLI and MCP server on one repo cannot
  drop sibling fields. Antigravity `trustedWorkspaces` prepare/release flock a `settings.lock`
  sidecar for the host-global settings transaction. The in-flight trust refcount remains
  process-local — one Marshal process per host when using Antigravity (documented in usage/design).
- **`claude plugin validate --strict` failed.** `marketplace.json` had no description, which the
  strict validator treats as an error and which blocks submission to the community plugin registry.
- **The MCP server reported an empty version.** `serverInfo.version` was blank in every initialize
  handshake, so a client could not answer "which Marshal am I talking to?" - the first question you
  ask when a tool misbehaves. It now reports the package version.
- **Install instructions now describe an install that works.** The README pointed at
  `uv tool install "MarshalFleet[mcp]"`, which fails because the package is not on PyPI yet. It now
  leads with the Claude Code plugin and a verified `git+https://` install, and says plainly that
  PyPI is pending.
- **Claude Code plugin manifests no longer drift from the package version.** `plugin.json` and
  `marketplace.json` advertised `0.0.1` against a `0.1.0` package, and both listed six of the seven
  backends. Tests now fail on either drift.
- **The status page's remaining-work list matches reality again.** `docs/status.md` still listed a
  Gemini backend (removed in #123) and "PyPI publish" as wholly outstanding (the Trusted Publishing
  infrastructure shipped in #86/#121 and was exercised by the v0.1.0 release, #134); it now says the
  publish itself is what remains.

## [0.1.0] - 2026-07-29

### Fixed
- **`marshal --version` reported the wrong number.** `__version__` was a hardcoded literal that had
  drifted from `[project].version`, so the CLI claimed `0.0.1` from a `0.1.0` build. It now reads
  installed package metadata, and a test fails if a literal is reintroduced.

### Changed
- **Brand assets refined.** The wordmark lockup ships on a transparent background so it renders
  correctly on light and dark surfaces; the crown geometry in `logo.svg` / `logo-dark.svg` /
  `logo-mono.svg` matches it.
- **Every backend answers `available_models()`.** Six of seven adapters inherited the base
  `None`, so `list_models` was half-blind. Cursor / OpenCode / Command Code / Antigravity probe
  their CLI catalogues (bounded timeout, static playbook fallback); Codex / Claude Code / Goose
  return curated static ids from `docs/model-playbook.md`. Never `None`, never empty.
- **Goose `_compose_prompt` matches the base instance-method contract** (was a `@staticmethod`
  with a divergent signature).
- **Antigravity documents its auth gap explicitly** (`account_info` → `None`,
  `verifies_auth` → `False`): `agy` has no cheap auth/status/whoami probe, so doctor stays
  path-only rather than inventing a fake green.
- **CLI cost display no longer implies unmetered runs were free.** `marshal status` and `marshal
  usage` human output render `unavailable` when a run's cost provenance is unknown (or missing),
  instead of showing `$0.0000`. Measured zero-cost runs (`source: native` / `admin-api` / etc.)
  still display `$0.0000`.

### Added
- **`tests/test_backend_contract.py`** — parametrised over `registry.backend_names()` so a new
  adapter is covered automatically (pure `build_invocation`, `map_permission`, never-raise
  `parse_output`, non-empty `available_models`, usage-source vocabulary).

### Removed
- **Estimated cost pricing.** Marshal no longer derives dollar figures from a local price table
  (`pricing.py`, `data/prices.yaml`). Token-only runs now report `source: unavailable` with
  `cost_usd: 0.0` (token counts preserved). Real cost still comes from backend-native reporting
  or a provider usage API (`admin-api`, e.g. EastRouter). Historical ledger lines tagged
  `estimated` still load and still count toward spend rollups; the `cost_estimated` bucket field
  remains in summaries, and legacy spend is still attributed to it so the provenance split keeps
  summing to `cost_usd`.
- **BREAKING (library API): `Fleet(prices=...)`.** The `prices` constructor parameter is gone along
  with the price table it took. Embedders passing it will get a `TypeError` on an unexpected
  keyword - drop the argument; there is no replacement, because there is nothing left to price
  with. `UsageSource.ESTIMATED` and `UsageSource.SCRAPED` are also removed from the enum (stored
  ledger strings are unaffected).

### Changed
- **MCP tool / skill framing finishes the #98 fleet-primitive shift.** Residual diff-centric
  wording on `run_agent`, `spawn`, `run_many`, `collect_run`, `get_run`, `integrate`, `status`,
  and `marshal_quickstart` now treats delegation as the primitive and DIFF or TEXT as first-class
  products. `empty` is described as an outcome (exited 0, neither text nor file changes), not a
  fault. `skills/marshal-orchestrate` decomposes read-and-reason work alongside write work and no
  longer treats `empty` as failure. Docs (`docs/mcp-tools.md`, `docs/usage.md`) match.
- **Codex defaults to stock OpenAI.** The scaffold stub and model playbook now lead with plain
  `backend: codex` / `model: gpt-5.5` and `codex login` or `OPENAI_API_KEY`; EastRouter
  `usage_api` remains documented as an optional add-on for real admin-api cost.

### Added
- **Runnable examples for shipped capabilities.** [`examples/pipelined_review.py`](examples/pipelined_review.py)
  (`run_many` + per-job `then`), [`examples/read_paths.py`](examples/read_paths.py),
  [`examples/adversarial_review.py`](examples/adversarial_review.py),
  [`examples/multi_workspace.py`](examples/multi_workspace.py), and
  [`examples/per_client_env.yaml`](examples/per_client_env.yaml); indexed from
  [`examples/README.md`](examples/README.md), [`docs/usage.md`](docs/usage.md), and the README docs list.
- **Per-client `env` in `fleet.config.yaml`.** Each client may set literal environment variables
  (e.g. `CODEX_HOME`) merged into that client's agent children only, so one Marshal server can drive
  the same backend against different provider setups. Secret-shaped keys, empty keys, and `PATH` are
  refused at load; a leading `~` in values is expanded. See `docs/config.md`.
- **Per-job `then` follow-up on `run_many` (#103).** A job may carry optional `then: {client?, backend?, model?, goal, duration?, …}` — the same field set as a job. As soon as that job's primary reaches a terminal state, the follow-up starts in the **same worker** (not a second barrier). The follow-up worktree is based on the primary's branch after `commit_run`-style freezing so the agent sees the primary's diff. Skipped (with `then_skipped` on the result) when the primary failed, has no branch, or the primary's branch has no commits beyond its base (covers text-only primaries; still runs when the agent self-committed and left a clean tree). Freeze failures (`commit_run` blocked/error) are reported separately in `then_skipped`. `max_concurrency` caps workers (chains), not individual runs within a chain. MCP `run_many`, `MarshalService.run_many`, and the registry fan-out all expose this.
- **Brand assets (`assets/`).** Hand-authored SVG logo and supporting files: `logo.svg` (mark on
  transparent, `#FF5714`), `logo-dark.svg` (same geometry, same colour — contrast verified),
  `logo-mono.svg` (`currentColor` for doc embedding), `wordmark.svg` (mark + logotype in system
  geometric sans), and `architecture.svg` (flat dispatch diagram: driver → MCP server → fleet →
  N isolated worktrees → integrate). See `assets/README.md` for palette values and intended use.

### Changed
- **BREAKING: `run_many` return shape (#103).** Previously `run_many` returned `list[RunRecord]` (one flat run record per input job — the primary). It now returns `list[RunManyJobResult]`, one `{primary, then?, then_skipped?}` object per input job. Callers that treated list elements as `RunRecord` must read `.primary` (and optionally `.then` / `.then_skipped` for the follow-up). Same shape on MCP `run_many`, `MarshalService.run_many`, CLI batch output, and the registry fan-out.
- **Stop tracking `.commandcode/`.** Local Command Code tooling state (same class of ignore as
  `.claude/`); files stay on disk, leave the public index.
- **README is a launch landing page.** Benchmark proof above the fold; install via `uv tool install
  MarshalFleet` / `pipx`; 60-second quickstart; backend matrix (all seven adapters); comparison
  table. Explanatory material moved to [`docs/usage.md`](docs/usage.md), [`docs/model-playbook.md`](docs/model-playbook.md),
  and new [`docs/nerds.md`](docs/nerds.md). Clone-from-source path lives in [`SETUP.md`](SETUP.md).
- **The MCP server targets `mcp` 2.0 (#119).** 2.0.0 removed `mcp.server.fastmcp`; the replacement
  is `mcp.server.mcpserver.MCPServer`. The two names are **disjoint** — no release ships both, and
  1.29.0 emits no deprecation warning on the way out — so there is no pin that satisfies both APIs
  and no way to support both without a fork in the import. We take 2.0 rather than pinning `mcp<2`:
  Marshal is a published distribution, and a `<2` cap would make it the package blocking anyone
  else's resolve for the same work we would have to do later anyway.

  Server-side the port really is the rename — all tools register unchanged. The cost landed in the
  **test surface**, where 2.0 renamed `Tool.inputSchema` to `input_schema` and turned `call_tool`'s
  tuple into a `CallToolResult`. Those helpers now read `.structured_content` by attribute instead
  of unpacking a tuple, which is the shape-independent form they should have used regardless.

### Added
- **`read_paths` — declared read-only escape hatch for files outside a worktree (#105).**
  An agent in an isolated worktree cannot see paths that are not in that checkout;
  `context_files` only injects repo-relative tracked paths. Drivers were manually copying
  reference material in. `read_paths` on `TaskSpec` / `run_agent` / `spawn` / `run_many` jobs
  (and the MCP tools) accepts absolute paths or paths relative to the **driver's** repo root;
  each is copied into `<worktree>/.marshal-context/<basename>` (files 0o444, directories 0o555)
  and appended to the worktree's `.git/info/exclude` so the copies never appear in the run's
  diff or `changed_files`. Teardown restores owner-write on directories before
  `git worktree remove` / `rmtree` so immutable dirs cannot strand a worktree. Secret-shaped
  names (`.env*`, `*.pem`, `id_rsa*`, `id_ed25519*`) and anything under `.ssh` are refused on
  the declared path **and every descendant** that would be copied; symlinks inside a declared
  tree are refused (a symlinked declared root is resolved first, then validated from the real
  path); only regular files and directories are accepted (FIFOs/sockets/devices refused so
  provisioning cannot hang before a run timeout exists). Policy is enforced during the
  fd-relative copy walk (validation at point of use): every `scandir` entry is re-checked for
  secret-shaped names / `.ssh`, symlinks, and non-file/dir types; each file and directory's
  `(st_dev, st_ino)` from the classifying `lstat` must match `fstat` of the opened fd, refusing
  a swap to a different file or directory (identity is a secondary check — a delete-then-recreate
  can be handed back the same inode, so the per-entry checks are what contain a swapped tree).
  Destination `.marshal-context` must be absent or a plain directory (a tracked symlink or
  non-dir is refused; never `resolve()` through it); per-entry destinations never follow
  symlinks (`O_CREAT|O_EXCL` / refuse existing dest symlinks). The up-front tree scan only
  names offenders early before worktree work — it is not the security boundary. Copies also
  open fail-closed (`O_RDONLY|O_NOFOLLOW|O_NONBLOCK` + `fstat` + identity for files;
  `O_NOFOLLOW|O_DIRECTORY` + identity for directory descent). A missing or refused path fails
  the spawn (worktree torn down). The declared list is recorded on `RunRecord` so a reviewer
  can see the run saw more than its worktree. The worker prompt is told read-only reference
  material is under `.marshal-context/`.

- **Adversarial review teams (`teams.py`).** A *team* is a declarative panel of independent,
  read-only reviewers — each role pinned to the client best at its lens — that review one subject
  and each write a report. Teams live in `<repo>/teams/*.yaml` and are surfaced as the `list_teams`
  / `run_team` MCP tools (see [`docs/mcp-tools.md`](docs/mcp-tools.md)), the `marshal teams` /
  `marshal team run` CLI commands, and the `marshal-adversarial-review` Skill; two starter teams
  ship in `examples/teams/`.
  - Four subjects: a run's diff, a commit `range`, a `plan` (free text), or an `audit` of the repo.
    A `range` review can be scoped with `paths` — without it a large diff is truncated at the tail,
    and since git orders paths alphabetically that cuts exactly the code worth reviewing.
  - **The engine computes no verdict.** It parses no reviewer prose and reports no pass/fail: a
    decision derived from text the reviewed material can influence is not trustworthy, and judgment
    belongs to the driver. You get `unified_report` (the panel's shape with every review inline,
    read this first) plus each reviewer's full report; collecting the objections and deciding is
    the caller's job.
  - Reports persist to `.marshal/reports/<stamp>-<team>-<id>/` — `<role>.md` per reviewer plus
    `README.md`.
  - **Fail-closed read-only:** a role naming a client that is not `permission: read-only` is a
    config error raised *before* any reviewer spawns. Marshal will not route a role to a writable
    client; note that `read-only` is OS-enforced only where the backend provides a sandbox, so the
    dependable boundary remains the worktree plus explicit integrate.
  - **Independent, and a shrunken panel is visible.** All roles go out in one `run_many` call under
    a shared `task_id`, so they cannot observe each other and the review prices as one unit. A role
    that failed, timed out, or whose backend was missing is listed in `incomplete_roles` with its
    report absent — a missing lens, never silent approval.
  - **Reviewed material is treated as hostile data.** The subject is delimited by a per-run nonce
    (a markdown fence it could close would let content escape into the strongest prompt position)
    and labelled untrusted. Refs reaching `diff_range` are validated: a `base` of `--output=<path>`
    would otherwise make a read-only diff write an arbitrary file *and* empty stdout, leaving the
    panel to review nothing. Empty subjects are refused, and a team file must live in the
    workspace's own `teams/` directory.
  - Team lookup is contained for **every** name form: a bare name is still a path fragment, so
    `run_team("../evil")` is refused, not just an explicit out-of-tree `.yaml` path.
  - A run that is `running`, `queued`, or `cancelled` cannot be reviewed - its worktree is not a
    stable snapshot (cancellation stamps the status right after signalling, so the agent may still
    be exiting and writing). A terminal-but-unsuccessful run can be, and its status is carried into
    every reviewer's prompt and the report so it is never mistaken for finished work.
  - Like `workflow.py`, the runner **adds no new execution path**: it issues only `collect_run` /
    `diff_range` / `run_many`, so every reviewer still flows through `Fleet.run`. It never
    integrates.
  - `marshal doctor` validates every declared team (unknown clients, the read-only rule) as a
    WARN-level preflight, and the scaffolded `fleet.config.yaml` now suggests commented read-only
    reviewer clients — without one, the first `run_team` a new user tries fails validation.

- **`status` can be filtered and paged, and is compact by default (#72).** It returned every run
  ever recorded, whole — one observed reply was ~395k characters, mostly agent prose the caller had
  not asked for — so its only consumer, a context-bounded agent, stopped calling it and issued N
  `get_run` calls instead. Both the MCP tool and `marshal status` now take `limit` (newest first),
  `status`, `task_id`, and `since_hours`, and omit `text`/`verify_output` unless asked, replacing
  them with `has_text` / `has_verify_output` so an omitted field is never misread as an empty one.
  The reply reports `matched` alongside `returned`: a capped list says so rather than looking like
  the whole ledger.
- **`agent_alive` on the run record (#71).** A driver reading `running` could not tell "still
  working" from "finished, outcome not yet written" — the field report that prompted this had the
  driver conclude a run failed when it had succeeded, and say so. `status`/`get_run` now derive
  whether the agent process is alive at the moment of the read. `null` means *unknown*, never dead:
  the run is terminal, no pid is recorded, or its identity could not be verified. It is computed on
  read and deliberately never persisted — a stored liveness is stale the instant it lands, which is
  the very failure being fixed. It also removes the reason to shell out to `kill -0`, which is not
  sound anyway: pids are reused, so a live pid is not proof the agent lives.
- **PyPI publication prep.** The release workflow publishes via Trusted Publishing (OIDC) only on a
  published GitHub Release or a manual `workflow_dispatch` — never on a branch push, and with no API
  token anywhere. Packaging metadata and hatch sdist excludes are tightened so the wheel carries
  `marshal_engine` plus `py.typed` and `data/prices.yaml` and nothing else (no `tests/`, `.marshal/`,
  repo `teams/`, `fleet.config.yaml`, or `docs/internal/`). Every action in the release job is
  pinned by commit SHA rather than a mutable tag — the job holds `id-token: write`, so any step in
  it can reach the publishing credential — and the run refuses to publish unless it is on a tag
  whose name matches the built version, which also catches a release cut without a version bump.
  The build backend is version-pinned (it is resolved at build time, not from `uv.lock`, so an
  unconstrained one makes the artifact non-reproducible), and a concurrency group stops two runs
  racing the same irreplaceable upload. Documented plainly: `workflow_dispatch` runs the workflow
  file **as it exists on the selected ref**, so the in-workflow guards can be edited away on a
  branch and PyPI validates only the workflow filename and environment — GitHub Environment
  protection is the one control that does not live inside the ref being published, and is required
  rather than recommended. The workflow targets the `PYPI` environment, matched to the one actually
  configured on the repo (required reviewer, plus a deployment policy limited to `v*` **tags** with
  no branch policies). A contract test pins that name: environment names are case-sensitive and a
  mismatch fails *silently* — the run resolves to a different, non-existent environment, so its
  reviewers and tag policy do not apply and its secrets are out of scope. The guard reads the tag through `env:` rather than interpolating
  `github.ref_name` into the script — a git tag may legally contain shell metacharacters, and a
  crafted one would otherwise execute commands inside the `id-token: write` job, before the very
  check meant to stop it. It compares against the version of the **built wheel**, not
  `marshal_engine.__version__`: hatchling builds from `[project].version`, so checking the source
  constant would verify a value the artifact need not carry, and a drift between the two would pass
  the guard while PyPI received a version the tag never claimed.
- **Conflicting routing is refused instead of silently resolved (#101).** Passing both `client` and
  `backend` names two different answers to "what runs this", and the loser was dropped without a
  word — so a run executed on a backend the caller never asked for and nothing in the result said
  so. It now raises, naming both values and the two valid shapes. `client` + `model` is deliberately
  NOT covered: that is a coherent request (this client's backend, that model) and stays a supported
  override. The MCP `backend` description said "ignored if `client` is also set"; documenting a
  silent override does not make it safe.
- **`list_workspaces` says whether a workspace is actually usable (#99).** `configured` meant only
  "a config file exists at this path", and every reader took it for "ready" — a workspace with an
  empty or unparseable config looked identical to a working one, so a driver ran against it, got
  nothing, and fell back to an ad-hoc spawn. `ready` now answers the question people were asking,
  and `ready_reason` says why when it is false: "no config file", "config does not load: <error>",
  and "config declares no clients" need different fixes, and collapsing them to a `0` just moved
  the guessing onto the reader. `configured` keeps its old meaning and is documented as the weak
  claim it always was. `marshal workspace list` prints the reason inline. Note `ready` is a claim
  about configuration, not the machine — it does not probe backend CLIs; that is `doctor`.
- **`doctor` surfaces recent billing/quota failures (#95).** It answered "is the CLI installed and
  logged in?" and presented that as readiness — so a backend that was installed, authed, and out of
  credit passed green, and the driver learned otherwise by spending a run. Two field reports hit
  this independently on the same day (an "Insufficient balance" death at 3.5s, and an exhausted
  premium quota discovered by burning runs). A `quota:<backend>` warn now reports how many recent
  runs failed on billing/quota grounds and quotes the latest error, derived from the run ledger we
  already keep — no provider API, and it reports what happened rather than predicting. Its absence
  is deliberately **not** a clearance: doctor cannot read provider balances, and saying quota looks
  fine because it was never checked is the same overclaim the field reports were about. Rate
  limiting is deliberately excluded from the classifier: a 429 means *slow down*, not *pay*, the
  retry policy already backs off and retries it, and sending an operator to top up over throttling
  is the wrong remedy.

- **A `context_files` path that is not in the worktree fails the spawn (#73).** A worktree holds
  tracked files, so a gitignored path — `tmp/`, a build dir, a scratch report — exists in the
  driver's checkout and simply is not there. The agent was handed a path it could not open; in the
  reported case it said so, worked from the surrounding prose, and produced something adequate *by
  luck*, with neither side able to tell it had solved a different problem. The spawn is now refused,
  naming the missing paths, and the worktree is torn down rather than left behind. Failing is
  deliberate over silently copying the file in: copying puts untracked content into a checkout whose
  purpose is to mirror the repo, and `.env` is gitignored too — "copy whatever the caller named" is
  a way to hand secrets to an agent. Containment is checked first and matters more: `Path(wt) /
  "/etc/passwd"` is `/etc/passwd` (an absolute path discards the base) and `../` walks out the same
  way, so an existence-only check would have passed both and pointed the agent at host files. An
  absolute or traversing `context_files` entry is now refused.
- **`marshal_quickstart` MCP tool: a stated "start here" (#102).** A driver facing ~20 tools had no
  ordering and no decision boundary between the near-duplicates — `run_agent` / `spawn` /
  `run_many` / `run_workflow` and `status` / `get_run` / `collect_run` / `get_run_log` — and learned
  "spawn is the long-job one" only by reading every description. The tool returns the four-step loop
  (`doctor` → `spawn` → `collect_run` → `integrate`), says plainly which run tool blocks and which
  does not, and states up front that a run's status is about the process exiting, not about the work
  being right. It is a tool rather than a docs link because a driver reads tool descriptions.
  `docs/mcp-tools.md` also stops hardcoding a tool count (it was already stale by one) and no longer
  claims *every* tool takes a `workspace` — the global tools do not. A test checks the quickstart's
  claims against the real registered signatures, because an orientation tool that overclaims is the
  same defect as `succeeded` and `configured`, just in prose: two drafts asserted that `integrate`
  is the only thing that reaches your branch, when a workflow with an `auto: true` integrate phase
  does too.
- **`list_workspaces` reports recency (#104).** With fifteen registered repos the list was
  unnavigable by name alone — `provo` from `domo` from `lore` meant opening each. `last_activity_at`
  is the most recent write to that workspace's run ledger, which is how anyone actually finds the
  repo they were just working in. It is a directory **stat**, not a ledger parse: `describe()`
  builds no services and reads no run records, and a full parse per row would turn a cheap listing
  into real work. Named for what it measures — a record's last write, not a run's start time —
  rather than the `last_run_at` the report asked for, since the two are not the same thing.

- **`list_clients` says which clients it dropped, and why (#74).** A client whose backend CLI was
  unavailable was filtered out with no error and no reason - the reporter noticed only incidentally.
  Marshal already knew and warned on stderr, but an MCP driver never sees stderr, so from its side
  the client silently vanished. The listing now carries `skipped: [{name, backend, reason}]`, and an
  unknown backend name reads differently from an installed-but-absent CLI, because those have
  different fixes.

- **`clean --dry-run` says which worktrees hold unmerged work (#76).** The reporter had 84
  worktrees and cleaned none of them: *"I couldn't tell which held unmerged work that wasn't mine."*
  The filters they asked for (`scope`, `older_than_hours`, `dry_run`) already existed and were never
  the blocker - the missing thing was the safety signal, and no amount of filtering substitutes for
  it. A dry run now reports `unmerged_commits` per candidate. `null` means **cannot tell** (no
  branch, or git could not be asked), never zero: a driver reading absence as "nothing to lose"
  would delete work, which is the opposite of what the truth justifies. Note the runs this matters
  most for - succeeded but never integrated - are deliberately outside the default `finished` scope,
  so they need `scope="all"` to appear at all.
- **`integrate` takes a commit `message` (#75).** The Fleet accepted one all along; the service and
  the MCP tool both dropped it, so no caller could reach it and every integrate landed as
  `marshal: integrate <run_id>` - a message about the tooling rather than the change. The reporter
  reset and recommitted after every single one, roughly fifteen times. `commit_run` had taken a
  `message` from the start, which made this an inconsistency in our own surface rather than a
  missing capability. The driver reviewed the diff, so the driver is who should write the message.

- **`list_models` proxies the backend's own list when no catalog is configured (#78).** It returned
  `{"models": []}`, so a driver left Marshal and ran `cursor-agent models` in a shell to find out
  what it could route at - we did exactly the same thing ourselves the same day, to confirm two
  model ids before pointing a fleet at them. Backends can now report what their CLI says they run
  (implemented for Cursor, verified against the real output: 193 ids). `null` for a backend means it
  cannot be asked, not that it runs nothing. It stays in a separate `backend_models` field and never
  feeds routing: the catalog is curated metadata a human wrote, a probe is whatever a CLI said just
  now, and flattening the two would let a probe drift into looking like configuration.

- **`read_run_file` — one agent's artifact can reach the next (#80).** A run that produces a report
  had no way to hand it on: `collect_run` returns the whole diff (wrong granularity) and
  `context_files` refuses paths outside the target worktree, correctly, since that guard is what
  keeps a run inside its boundary. Reading one named file closes the gap. It copies nothing and
  starts nothing, so the driver stays the one deciding what the next agent sees — which is where the
  judgement belongs, because the driver is what reviewed the output. The win is **fidelity** more
  than time: the next agent reads what the first actually wrote instead of a paraphrase. Same
  containment as `context_files`, and `truncated` is explicit — silently returning a prefix would
  let a driver act on part of a report believing it was whole. The read is bounded to what it
  returns rather than slurping the file first — the caller picks the path, so an agent-produced
  artifact of any size would otherwise land in the MCP server's memory; `size_bytes` still reports
  the true size, from `stat()`. A `clean` landing mid-read reports the documented cleaned-worktree
  error rather than a raw `OSError` or a misleading "not a file" - one state must not produce two
  diagnostics depending on which microsecond the caller arrived in. To build ON a run's code rather than read its conclusions,
  `commit_run` + `base_branch` chaining remains the answer.
- **Marshal describes itself as what it is (#98).** Asked to fan out 12-14 agents for research, a
  driver **did not reach for Marshal** and explained why: the description was framed entirely around
  producing diffs, so its own self-description routed the work elsewhere. It never got as far as the
  feature gaps. The MCP server description, `marshal_quickstart`, the README and `docs/usage.md` now
  say fleet primitive first and name code delegation as the best-developed path rather than the only
  one. They also correct what a non-code run actually does, which the old framing got wrong: a run
  that only reads and reasons **still returns its work** - its final message is on the record as
  `text` and the run is `exited_clean`. `collect_run` reports which artifact it was via `produced`;
  `empty` means the run produced neither text nor changes. The real gap is *structured*
  output. Separately, where a backend truncates long final messages (Cursor does), have the agent
  write its report to a file - that, not a missing text path, is why the review teams do so.
- **`collect_run` reports a text artifact instead of looking empty (#97, partial).** It is the tool
  a driver reaches for first to answer "what did this run produce", and for a research or review run
  the honest answer is prose. The engine already treated text alone as `succeeded`, but collect
  returned an empty diff and stopped - so a run that had genuinely said something read as one that
  did nothing, which is what pushed drivers to make agents write files they did not need to. It now
  carries `produced` (`diff` | `text` | `nothing`) and, for a text run, the final message. Callers
  branch on a field rather than inferring intent from which container is empty. Structured output -
  the remaining half of #97 - is deliberately not attempted here: prose you can find beats prose you
  cannot, and a schema is a separate decision.
- **The PyPI distribution is `MarshalFleet`.** `[project].name` must equal the name the Trusted
  Publisher is registered under or the upload is rejected, and the registered project is
  `MarshalFleet` (PyPI normalises it to `marshalfleet`). Three names now, each doing one job: the
  **distribution** is `MarshalFleet` (what `pip install` takes), the **import package** stays
  `marshal_engine` (a top-level `marshal` would shadow the stdlib module), and the **console
  script** stays `marshal` (what you type). Verified end to end: the wheel builds as
  `marshalfleet-0.0.1`, installs into a clean venv, and `marshal --version` still works - as does
  the release guard that parses the version out of the wheel filename.
- **A run records what provisioned its worktree (#77).** Agents reported "1308 passed" where the
  workspace showed "1351 passed, 0 skipped" - a bare `uv sync` had left the project's extras
  uninstalled - and it took **three occurrences** before a driver with full context recognised the
  pattern. In the reporter's words, *"a number that means something different in two worlds, with
  nothing marking it, is a trap the tool sets."* We hit the same class ourselves the same day: six
  `test_cli.py` failures that occur only inside a worktree. `worktree_setup` on the run record names
  the command the environment came from, and `null` says the worktree was a bare checkout - the
  sharpest form of the delta. Marshal does not own that config, but it does own whether the
  difference is visible on the result.

### Changed
- **`succeeded` is now `exited_clean`.** Every field review said the same thing about that word: it
  claims more than it checks. The run's process exited 0; whether the work is *correct* is a
  separate question only a diff review or the `verify:` gate answers. Both the `integrate`
  docstring and `CLAUDE.md` had to shout that caveat — and as one reviewer put it, *when a
  description has to shout a caveat, the API shape is wrong.* The new name states exactly what was
  observed, so the warning stops being load-bearing.

  **Nothing on disk is rewritten.** A stored status is a fact about what happened, so history is
  reinterpreted on read, never edited: both `RunRecord` and `UsageEvent` accept the old spelling and
  return the new one. The **usage ledger** matters as much as the run records here — it is
  append-only, so every event predating the rename still says `succeeded`, and a reader that only
  knew the new word would silently stop counting those runs and quietly change every historical
  cost-per-succeeded figure. One shared alias table (`types.canonical_status`) serves both, so the
  two stores cannot drift apart.

  **Deliberately unchanged:** the usage ledger's `succeeded` / `cost_per_succeeded` *metric* names.
  They count runs that exited clean, and renaming them would break the `usage` JSON surface for a
  cosmetic gain — the complaint was about a per-run **status** overclaiming, not about a counter.
  The residual mismatch (status `exited_clean`, metric `succeeded`) is a known trade, not an
  oversight.

### Documentation
- **Document the run-lifecycle state that shipped without it.** `pid_start_time` and `base_commit`
  are now in the run-record reference with the reason each exists; `.marshal/fleet.lock` is
  described in the layout section; `collect_run`'s committed fields state that they compare against
  the run's own base, not the current branch; and both surfaces now say that `failed` is overloaded
  (agent failure vs orphaned at startup) and how to tell them apart. The `cancel_run` reference
  still described the pre-handle identity check and has been corrected.
- **Marshal ↔ Chauffeur freeze line (#49).** Document the mechanism-vs-judgment boundary:
  engine inventory (worktrees, run loop, ledger, primitives), what stays in Skills/Chauffeur,
  grandfathered sequencers (`workflow.py`, `teams.py`), the three-question admission test, and
  what Chauffeur is expected to replace. Normative detail in `docs/chauffeur-future.md`; inventory
  table in `docs/design.md` §12.
- **Release process for PyPI.** `CONTRIBUTING.md` documents version bumps, promoting CHANGELOG
  `[Unreleased]`, artifact smoke-checks (`uv build` + throwaway venv `marshal --version`), and the
  one-time PyPI Trusted Publisher setup (`marshal`, fallback `marshal-orchestrator`).
- **Cross-workspace usage/budget contract + budget enforce honesty (#44).** Document that
  multi-workspace MCP shares concurrency only — ledgers, budgets, `EnforceBudgetGate`, and
  session clocks stay per-workspace (no registry spend/budget merge; intentional non-goal). Rewrite
  design.md §6 for soft-warn default vs `enforce: true` (fail-closed lookups, gate serialization)
  and `$0`/`unavailable` cost-coverage caveats; sync mcp-tools / SECURITY / usage / MCP `usage`
  docstring drift that still claimed budgets never block.

### Fixed
- **Startup reconciliation no longer reaps runs another process just started.** Observed in
  production, not theorised: two live agents were stamped `failed` ("orphaned at startup") seconds
  after spawning, one of them still running when its record claimed it had died. A run is persisted
  RUNNING a moment before its pid is stamped, so a short-lived process reconciling in that window
  finds a record with no pid and nothing to protect it — the in-process registry only covers runs
  the *same* process started, and the lock only helps once its holder is alive and current. A
  non-terminal record that has no pid yet and is younger than the reap grace period is now left
  alone. The grace is deliberately narrow in both directions: a record that already carries a pid is
  decided immediately (its liveness is knowable, so waiting would only keep a dead run reported as
  RUNNING), and a record skipped for being young is re-examined on the next `status`/`get_run`
  rather than only at the next Fleet construction — otherwise a genuine orphan that happened to be
  young at startup would read RUNNING for the whole life of a long-running server. Multi-workspace
  MCP `status` reads ledgers directly rather than through the service, so it finishes a pending
  reconciliation for any workspace whose Fleet already exists in the process (it still never builds
  one to do so — where no Fleet was built, nothing reaped). A record skipped because its agent was
  still alive is queued for re-check the same way: "alive right now" is a snapshot, and without this
  a run whose agent outlived its supervisor and then exited stayed `running` until the server
  restarted. Two cases remain undecidable by design and are documented rather than guessed at:
  `marshal status` is a raw ledger read that never reconciles (a short-lived CLI mutating run state
  is the original bug), and a record carrying neither a pid nor a parseable `started_at` has no
  evidence either way — it stays visible and honest until `cancel_run`.
- **A live agent that outlived its supervisor is visible instead of silently lost (#87).** Marshal
  cannot signal a process it did not start, so `cancel_run` on such a run only flips the ledger —
  but it used to clear the `pid` while doing so, deleting the operator's only handle on a process
  that was still writing, behind a record claiming the run was over. The pid is kept, the `error`
  says the agent is still running and gives the `kill` command, and `clean` refuses to remove that
  worktree while the process lives. Identity here fails **closed** (pid *and* recorded start time
  must match): reaping assumes ambiguity means "still ours" so it never kills a live run, but
  pointing a human at an unverified pid could send them after a recycled one — and "verified" there
  means a real start-time comparison, not merely the absence of a contradiction, so a pid whose
  probe is unavailable never counts as confirmed. `clean` takes the
  opposite bias on purpose: refusing to remove a worktree that *might* still have a writer only
  leaves a directory behind, while removing one that does destroys work in progress — so it spares
  the worktree of any run whose pid is still alive, verified or not. `SECURITY.md` claimed
  reconciliation stamps such runs terminal — it does not, and never did.
- **`fleet.lock` identity matches run-record identity (#88).** The lock stored a bare pid while run
  records had already learned that a pid is not an identity. If a holder died and the OS handed its
  pid to any unrelated long-lived process, every later Fleet saw a live supervisor, declined the
  claim, and therefore never reaped — stale runs read RUNNING until that unrelated process happened
  to exit. The lock now records the holder's start time too and verifies the pair. A lock written by
  an older version has no start time, and is treated as held while alive, so upgrading never causes
  a takeover it should not make.
- **A cancel ends the retry loop (#89).** The loop never consulted the cancel state, and SIGTERM
  can surface as a transport-shaped error — so a cancelled run classified it as transient, slept,
  and spawned a whole new attempt (backend setup and all) that the pending cancel then killed on
  arrival. That briefly put a second writer in the worktree *after* the record read `cancelled`.
  The state is checked on both sides of the backoff: the sleep is the widest window in the loop, so
  a cancel is likeliest to arrive exactly there, and checking only before it let the loop wake and
  spawn a fresh agent — writing and billing — against an already-cancelled record.
- **Cancel tests now exercise the path they claim to (#90).** Three tests were written against the
  previous identity-checked cancel and never updated: with no in-process handle registered they
  never reached `killpg` at all, so the kill race and its `ProcessLookupError` branch were covered
  in name only, and one still asserted against `pid_start_time` stubs that cancel no longer reads.
- **A reap is decided and committed atomically.** The scan read each record without a lock while
  the write only re-checked "still not finished", so a pid stamped in that gap — the run's own
  process finally reporting in — was overwritten anyway. The whole decision now lives in one
  predicate that runs again inside `update_if`, under the run's own lock, so a reap can never be
  authorised by one test and committed against another.
- **Reconciliation can no longer be lost.** It is no longer gated on a flag fixed at construction:
  such a flag can only ever be cleared, so an orphan created *after* it cleared was never looked at
  again. A denied `fleet.lock` claim is now retried on the next read rather than remembered as
  permanent — ownership can be refused merely because a short-lived CLI held the guard for that
  instant, which previously left a long-running server that never reconciled again.
- **A pid is never written onto a terminal record.** After such a reap, the pid callback stamped a
  live pid onto the `failed` record, producing a record that claimed a running process for a run it
  said was dead. The write is now conditional on the run still being non-terminal.
- **`cancel_run` signals only a live child of the current process.** Signalling goes through an
  in-process handle tracking the child from spawn until it is reaped — the OS cannot recycle a
  child's pid before its parent reaps it, so within that window the pid is unambiguous. A cancel
  arriving before the pid is known is applied the moment it is; a cancel after the child is reaped
  does not signal; publishing a pid clears the handle's exit flag so a cancel during a retry still
  reaches the retry's agent. A run owned by another (or dead) process is stamped `cancelled`
  without a signal and records why.
- **Reap orphaned RUNNING runs at Fleet startup.** A persisted `running`/`queued` record left when
  the supervising MCP server or CLI process died is terminal-stamped `failed` (outcome unknown),
  its `pid` cleared, and `error` records the reap — so `cancel_run` can never `killpg` a reused OS
  pid. Skipped when another live Fleet holds `fleet.lock` or the agent subprocess is still alive.
- **`integrate` warns on base-branch drift.** `RunRecord` now persists the branch a run was
  spawned from; `integrate` sets `base_branch_drift` and names both branches in `message` when the
  merge target differs (the merge still proceeds).
- **`collect_run` surfaces agent self-commits.** Committed work on the run branch (since the
  merge-base with the current branch) is returned in `committed_changed_files`, `committed_diff`,
  and `commit_count`; uncommitted work stays in `changed_files` / `diff`. Fixes the review blind
  spot where Cursor/Claude Code agents commit before exit and the working tree looks empty.
- **Antigravity `trustedWorkspaces` scoped to the run (#48).** `prepare()` no longer leaves
  durable trust entries in the host-global agy settings file: `run()` removes the run's worktree
  path on completion (best-effort; warns on stderr if cleanup cannot read/write the file). A
  malformed or unreadable settings file is preserved and fails the run closed instead of being
  replaced with `{}`. Removal is reference-counted in-process and the count is claimed under the
  same lock that writes the entry, so overlapping Antigravity runs cannot revoke each other's grant
  early, and Marshal only ever removes a path it introduced. A run whose teardown never executed —
  a hard kill, or one later reaped as an orphan — still leaves its path trusted until `clean`
  removes the worktree; reaping makes the record read terminal without doing that cleanup.
- **A worktree removed mid-review no longer escapes as a raw exception.** Collecting a team's
  review subject races `clean`: the worktree can vanish at three different points, and each raises
  a different type - `ValueError` (already gone at resolution), `WorktreeError` (gone after
  resolution, git failed), and `FileNotFoundError` (gone before the git process started, so
  spawning with a deleted cwd raises from subprocess). All three now become the same actionable
  "no longer reviewable" error instead of crossing the MCP boundary raw.
- **Cursor silently truncated long runs.** `--output-format json` returns one final object whose
  `result` holds only the last few hundred characters on a long run (measured: 11,417 output
  tokens generated, 266 characters returned). The adapter now uses `--output-format stream-json`
  and reconstructs the text by concatenating assistant events; when the terminal `result` is
  shorter, the stream wins. A timeout-killed run returns its partial text instead of nothing.
- **Child env scrubs `MARSHAL_*` session variables.** `child_env()` now strips every `MARSHAL_*`
  variable inherited from the driver/MCP process (not just `VIRTUAL_ENV`/`PYTHONHOME`), so worker
  agents' test suites and `marshal` CLI invocations resolve the worktree instead of the driver's
  repo/config. Callers can still pass `MARSHAL_*` values via `extra`.
- **CLI and MCP `usage` windows reconciled.** Both surfaces accept the same set
  (`session|day|week|month|all`) via a shared `usage_window_since` helper. CLI gains `session`
  (honestly "since this invocation" — help + human output state the caveat; no long-lived Fleet);
  MCP gains `day` (last 24h). Existing options kept so callers do not break.
- **Worktree setup allowlist refuses before `git worktree add` (#45).** Non-allowlisted
  `worktree_setup` / `verify` without `allow_unsafe_commands` raise at config load and
  `WorktreeManager` construction, so a static misconfig never creates-then-tears-down worktrees.
  Runtime `setup()` / `verify()` checks remain as a backstop. Doctor surfaces the error as a
  `config` FAIL.
- **Cursor doctor no longer false-greens when logged out (#43).** Auth is gated on
  `cursor-agent status --format json` (`isAuthenticated === true`); `about` only enriches
  plan/model after auth. Bare logged-out `about` (`model: "Auto"`, null tier/email) is no longer
  treated as authenticated.

### Added
- **Fail-closed doctor auth probes for remaining backends (#43).** Claude Code
  (`claude auth status`), Command Code (`command-code status --json`; config.json alone is not
  auth), OpenCode (`opencode auth list`), and Codex (`codex login status`) set `verifies_auth` so
  present-but-unauthenticated CLIs FAIL in `marshal doctor`. Antigravity stays path-only (no cheap
  dedicated auth probe; documented). Doctor remains preflight — spawn is not hard-gated. Headless
  Cursor `--approve-mcps` remains a parked residual (MCP hang hazard, separate from auth).

### Security
- **Explicit worktree / task_id validation (#46).** Driver-supplied `task_id` and worktree
  directory names are fail-closed (charset `[A-Za-z0-9._-]`, no leading `.`/`-`, length caps)
  with resolved-path containment in `create` / `remove` / `discard` (strict descendant of
  `base_dir`; equality refused). Hostile ids raise before any `git worktree` op — no longer
  relying on git ref-format accident. Explicit empty `task_id` fails closed (not replaced by a
  generated id). Normative detail: `SECURITY.md`.
- **Document post-agent verify / integrate_run_hooks exec hazard (#42).** `SECURITY.md`,
  design/config/usage, and `marshal doctor` warnings now state that `verify` and opted-in
  `integrate_run_hooks` may execute agent-modified worktree content/hooks under the operator
  identity (allowlist ≠ sandbox; `worktree_setup` remains pre-agent). Defaults and runtime
  behavior unchanged.

### Added
- **Safe-edit permission fidelity (#40).** Capabilities now declare `permission_fidelity`
  (`enforced-denies` for Cursor/OpenCode/Codex; `boundary-only` for Command Code/Goose/Antigravity/
  Claude Code; default `boundary-only` so unknown adapters fail honest). Surfaced on `list_clients`,
  `marshal backends` (`fidelity=` / JSON), and `doctor` (`permission:<backend>`: ok vs warn). Cursor
  safe-edit also denies Write to `.cursor/cli.json` (root + nested); OpenCode safe-edit bash denies
  extend to curated `git config` / redirection / `tee` / `sed` cases into `.env`/`.git`. Docs,
  `SECURITY.md`, and the orchestrate Skill describe the honesty contract (worktree remains the
  boundary; neither deny list is a sandbox; Claude `acceptEdits` has no Marshal deny layer).

### Removed
- **The Gemini CLI backend** (added and removed within this same unreleased cycle, so it never
  shipped in a release — the `Added` entry is gone rather than leaving a net-zero pair for readers
  to reconcile). Google is steering users toward **Antigravity**, which Marshal already
  adapts - so Google models stay reachable and a second Google adapter would be maintenance for a
  CLI its own vendor is moving off. The adapter was never live-verified (`gemini` was absent on PATH
  throughout; every argv and JSON claim came from reading docs), and an adversarial audit found two
  of those claims wrong - one a safety issue where `read-only` mapped to a mode that auto-approves
  its own exit and escalates to YOLO, so a run declared read-only could write and exit 0. Both were
  fixed, but verifying the rest meant installing a CLI we are now dropping. Unaffected: the
  Antigravity adapter keeps its `~/.gemini/antigravity-cli/` settings path and `gemini-3.x` model
  ids - those are Antigravity's, not the Gemini CLI's.

- **Marshal Recall extracted from core.** The experimental Cognee-backed memory feature is preserved
  on the `feature/marshal-recall-cognee` branch for future reference.

### Fixed
- **Goose `cost: 0` no longer claims `source=native` (#41).** Stream-json / bulk JSON usage is
  stamped native only when reported cost is positive (OpenCode parity). Zero or missing cost keeps
  tokens as `unavailable` so Fleet can estimate instead of locking a fake free run via the native
  short-circuit.
- **MCP workspace registration fails closed by default (#39).** The `add_workspace` MCP tool now
  refuses every call - before any path validation, registry write, or scaffolding - unless the
  server was started with `MARSHAL_ALLOW_MCP_WORKSPACE_REGISTRATION=1` (exact value, captured once
  at server build). A prompt-injected driver can no longer expand the set of repos Marshal may
  modify on a default install. The refusal message names the operator alternative
  (`marshal workspace add <name> <path>`, hot-reloaded into the running server) and the opt-in.
  CLI, registry-file, and env-var registration are unchanged. `SECURITY.md` now documents MCP
  driver authority (ad-hoc backend choice, `integrate`, gated `add_workspace`).
- **Cursor safe-edit deny overlay no longer pollutes run results (#37).** The `.cursor/cli.json`
  merge is now a transaction owned by `CursorBackend.run()`: the file's exact prior state
  (existence, bytes, mode) is snapshotted before the run and restored before Fleet observes the
  worktree. A no-op Cursor safe-edit run is honestly `EMPTY` (and skips verify) instead of a false
  `SUCCEEDED`, and `commit_run`/`integrate` can no longer land Marshal's transient deny policy on
  the user's branch. An existing malformed, unreadable, non-object, symlink, or non-regular
  `cli.json` (or a symlinked `.cursor/` directory) now fails the run closed - preserved
  byte-for-byte, agent never launched - instead of being silently replaced. Restore
  re-validates paths before unlink/replace so a mid-run swap of `.cursor/` for a symlink
  cannot redirect cleanup outside the worktree; a restoration failure fails the run rather
  than returning success with policy residue. The denies remain a curated list, not a
  sandbox (deny fidelity hardening is #40).
- **Config hot-reload no longer forks budget state (#36).** Rebuilding a workspace's service on a
  `fleet.config.yaml` edit (or an `add_workspace` re-registration) now reuses a durable per-repo
  runtime — the same `EnforceBudgetGate` and `session_start` — so an unrelated edit mid-run keeps
  `enforce: true` concurrency and `window: session` accounting intact. Budget limits/scopes still
  hot-reload from the new config; changing an enforce budget's own definition mid-flight can
  re-key its concurrency slot.

### Added
- **Cross-workspace `run_many` (#22 / M4).** MCP `run_many` jobs accept optional per-job
  `workspace`; mixed batches share one concurrency cap via `WorkspaceRegistry.run_many` while each
  workspace keeps its own config, worktrees, and ledger. Call-level `workspace` remains the default
  for jobs that omit it. Docs + `marshal-orchestrate` Skill updated.
- **Optional integrate hooks (#25 / H2).** `integrate_run_hooks: true` omits `git --no-verify` on
  `commit_run` / `integrate` so non-interactive pre-commit/pre-merge hooks run. Default remains
  `--no-verify` for headless reliability; doctor and `SECURITY.md` document the deadlock risk of
  prompting hooks.
- **Setup/verify allowlist + opt-in (#21 / H1).** `worktree_setup` / `verify` refuse non-allowlisted
  binary basenames unless `allow_unsafe_commands: true` (shells always need the opt-in). Allowlisted
  tools (`uv`, `npm`, `pnpm`, …) still run as your user — not a sandbox. Doctor messaging,
  `docs/config.md`, and `SECURITY.md` updated.
- **Goose doctor auth/configure probe (#24).** `GooseBackend.account_info()` runs
  `goose info -v --check` and `verifies_auth()` is true, so `marshal doctor` fails closed when the
  Goose CLI is on PATH but provider auth/configure is missing (including Cursor-backed
  `cursor-agent` login failures). Surfaces `plan:goose` with provider + model when the check
  succeeds. Hint text still points at `goose configure` / `cursor-agent login`.
- **Permission config layer (v0, C1/H4 / #17).** Cursor `safe-edit` `prepare()` merges a curated
  deny list into the worktree's `.cursor/cli.json` (`Shell(rm)`, `.env` read/write, `.git`
  writes) alongside `--force`. OpenCode `prepare()` stamps `OPENCODE_CONFIG_CONTENT` with
  `question: deny` plus curated bash/edit/read/`external_directory` denies for `safe-edit`
  (`yolo` still gets `question: deny` only so headless cannot deadlock). Contract tests cover
  config emission. Command Code / Goose / Antigravity PTY remain deferred (documented in
  `SECURITY.md` and `docs/design.md` §5).
- **Goose backend** (`backends/goose.py`) + `marshal workflow run` CLI — merged from local main
  (`44c48eb`); contract tests included. Goose `safe-edit`/`yolo` map to `GOOSE_MODE=auto` for
  headless runs (CLI ≥ 1.43).
- **Optional hard budget caps** — `budgets[].enforce: true` refuses matching spawns when windowed
  spend already meets the cap (`BudgetExceeded`); default remains soft-warn.
- **Doctor hygiene advisories** — warns on `worktree_setup`/`verify` (config-driven subprocesses;
  allowlist / `allow_unsafe_commands` gate), advisory-only budgets, and `git --no-verify` on
  integrate/commit.
- **Docs-sync invariant test** (`tests/test_docs_sync.py`) — MCP tools, CLI subcommands, and
  `fleet.config.example.yaml` must stay aligned with the code surface.
- **Ad-hoc backend spawn and per-run `model` override** on `run_agent`/`spawn`/`marshal run`/`marshal
  spawn` — pass `backend` (+ optional `model`) with no `client`, or override a configured client's
  model for one call.
- **Model catalog + duration presets** — optional `models:` block in `fleet.config.yaml` surfaced
  via `list_models` / `marshal models`; per-spawn `duration` presets (`short`/`medium`/`large`/`long`
  or positive seconds) on MCP and CLI run entrypoints.
- **Durable per-run logs** — full stdout/stderr under `.marshal/logs/<run_id>.log`, with
  `get_run_log` (MCP) and `marshal logs` (CLI).
- **Advisory `budgets:`** — soft-warn dollar caps per backend/client/fleet window; spend surfaced
  in `usage` / `marshal usage --config`.
- **Workspace config hot-reload** — the registry rebuilds a workspace's service when its
  `fleet.config.yaml` appears/changes/vanishes (mtime+size signature).
- **`verify:` post-run gate** — optional per-workspace command after a would-be-succeeded run with
  file changes; failure lands as `verify_failed` with output tail on the run record.
- **Repo-shape-aware scaffold** — `add_workspace`/`marshal workspace add` drops starter config with
  commented `worktree_setup` suggestions detected from the repo layout.
- **Orphaned-worktree reaping** — scope-mode `clean` reconciles `.marshal/worktrees` against the
  ledger and reaps dirs with no readable run record (`orphans_removed`).
- **Actionable resolution-error hints** — ad-hoc `backend=` escape hatch, `doctor`, `add_workspace`.
- **Reference docs** — `docs/config.md` (every config key) and `docs/mcp-tools.md` (MCP tool census).

### Changed
- **`docs/design.md` §5 permission table includes Goose** (#23). Column documents
  `GOOSE_MODE=chat` (read-only) / `GOOSE_MODE=auto` (safe-edit and yolo, process-equivalent);
  honesty note names Goose + Antigravity alongside Command Code. Aligned with `docs/usage.md` /
  `SECURITY.md`.
- **CLI `run`/`spawn` preflight git-ness** before the missing-config advisory (#19). A non-git
  `--repo` / `MARSHAL_REPO` fails immediately with doctor-aligned wording (`not a git work tree`)
  instead of leading with “copy fleet.config.example.yaml”. Valid git repos without
  `fleet.config.yaml` still get the missing-config warning.
- **Goose `provider/model` validation** (#20). Malformed forms with an empty provider or model
  around `/` (e.g. `cursor-agent/`, `/auto`) raise a clear `ValueError` during argv preflight
  before worktree create. Valid `cursor-agent/auto` and bare model names are unchanged.
- **Client-resolution errors name the config path** — missing `fleet.config.yaml` (wrong
  `--repo`/cwd), empty clients, and skipped backends no longer collapse into a bare
  `known: (none configured)`. CLI `run`/`spawn` warn on stderr when the config file is absent
  (same posture as MCP), while ad-hoc `--backend` still works with zero clients.
- **CLI `run`/`spawn` catch `WorktreeError`** (wrong `--repo` / non-git path on ad-hoc
  `--backend`) with a clean stderr message and exit code 1 instead of a traceback.
- **Memory prefers `LLM_API_KEY` env** over deprecated inline `memory.llm_api_key` in YAML (env
  wins when both are set).
- **`enforce: true` budgets serialize matching in-flight spawns** (one concurrent holder per
  budget) so `run_many` / parallel `spawn` cannot TOCTOU past the ledger snapshot.
- **CLI `run`/`spawn` catch `BudgetExceeded`** with a clean stderr message and exit code 1
  (all backends / ad-hoc providers).
- **Goose adapter updated for CLI ≥ 1.43** — `--output-format stream-json`, `-t` prompt,
  `--no-session`; headless permission via `GOOSE_MODE` (`auto` / `chat`) instead of removed
  `--yes` / `--plan` / `--json`. Parser accepts stream-json and bulk json; auth errors embedded
  in assistant text are treated as FAILED. Model `provider/model` (e.g. `cursor-agent/auto`)
  maps to Goose `--provider` + `--model` for Cursor Agent–backed runs. Live-verified
  `goose-cursor` / ad-hoc `cursor-agent/auto` (2026-07-20).
- **`docs/status.md` module table** refreshed (budgets, layout, logs, scaffold, retry, env, doctor,
  goose, memory); Goose row marked live-verified.
- **`run_many` preserves client `usage_api`** and runs permission preflight before worktree creation.
- **Backend adapter boilerplate consolidated** into the base class; OpenCode export reconciliation
  moved to a post-success finalize hook.
- **`base_branch` on MCP `spawn`/`run_agent`** for dependent chaining; `files_touched` removed.
- **Unified service construction** (`build_service_for`) and workflow recipe errors surfaced over
  MCP (`list_workflows` returns `{workflows, errors, workspace}`).
- **Centralized `.marshal` layout** (`layout.py`) and CLI `--repo` path resolution for
  `usage`/`status`/`logs`/`models`/….
- **Budgets extracted** to `budgets.py`.
- **`doctor` PATH fallback + self-healing skipped clients** — `user_path()` unions well-known user
  bin dirs when the login-shell probe fails; clients skipped at startup re-probe on
  resolution/`list_clients`.

### Added
- **`marshal usage` time windows + per-breakdown token table.** A new `UsageTracker.summary(since,
  until)` window (compared in UTC over each event's `ts`), surfaced via `MarshalService.usage(...)`
  and a new MCP `usage(window: session|week|month|all)` parameter (`session` = since the Fleet's
  `session_start` stamped at process start). The CLI gets `--window day|week|month|all` (rolling
  windows, since the CLI has no server reference). The human `marshal usage` output now prints
  aligned `by_backend`, `by_client`, `by_model`, and the new compound `by_backend_model` tables
  with `name · runs · succeeded · cost_usd · cost split · input_tokens · output_tokens ·
  cache_read_tokens` columns - the per-client/model/cache-read spend the previous output silently
  dropped. `--json` keeps the existing `totals / by_backend / by_client / by_model` shape (the
  test that pins it still passes) and adds `by_backend_model`, `window`, and the resolved `since`.
- **`commit_run` - freeze a run's work onto its own branch for dependent chaining.** A new MCP tool +
  `Fleet.commit_run(run_id)` commits a finished run's (otherwise uncommitted) work onto its
  `marshal/<run_id>` branch **without touching your branch**, so a dependent run can `spawn` with
  `base_branch` = that branch and build on the actual output. Previously, basing a run on a prior
  run's branch saw only the spawn base (the agent left its work uncommitted). Returns
  `committed`/`clean`/`blocked`/`error`; refuses a still-running run. (An adversarial design review
  chose this explicit, driver-invoked primitive over auto-committing every run inside the engine -
  it keeps `collect_run` honest/read-only and integration the only step that moves history into your
  branch.)
- **`marshal clean` - one-shot teardown of finished runs' worktrees + branches.** New CLI command +
  MCP tool + `Fleet.clean(...)`. Reclaims the disk-heavy worktrees and their branches in one call
  while keeping the immutable usage ledger **and** the run-state records (status/cost history stay
  queryable). Never touches a running run. Scopes: `merged` (integrated only), `finished` (default -
  also failed/timed_out/cancelled/empty, but **protects un-integrated `succeeded` work**), `all`.
  Supports `--older-than`, explicit run ids, and `--dry-run`.

### Changed
- **`doctor` now verifies authentication, not just CLI presence.** For a backend that exposes an
  authenticated-only probe (Cursor's `about`), a CLI that is installed but **logged out** - which
  still answers `--version` - is now reported as `CLI present but not authenticated` (with the login
  command) instead of a green `available` that then dies one second into a real run. Backends without
  a cheap authed probe are unchanged (CLI presence reported; auth not claimed).

### Fixed
- **`prepare()` now runs before argv/env snapshot in `CodingAgentBackend.run()`.** Env stamps from
  `prepare()` (e.g. OpenCode `OPENCODE_CONFIG_CONTENT`, Goose `GOOSE_MODE`) were previously built
  into `child_env` *before* `prepare()` ran, so managed permission config never reached the child.
- **Goose surfaces non-JSON failure text on `run.error` (#18).** Provider/config failures printed
  as plain text on stdout (e.g. `Unknown provider`) are now extracted by `GooseBackend.parse_output`;
  shared `_failure_reason` also falls back to a stdout tail when stderr is empty.
- **MCP server + CLI + `MarshalService` + `Fleet` now recover the user's PATH before spawning
  backends.** An MCP host (Claude Code, Cursor, etc.) typically spawns Marshal with a stripped
  PATH that lacks the user's zshrc-managed directories (Homebrew, `~/.local/bin`, npm-global), so
  user-installed CLIs (`opencode`, `cursor-agent`, ...) looked missing to `shutil.which` and
  `marshal doctor` falsely FAILed them, AND the spawned agent subprocess inherited the same
  broken PATH and died with "binary not found". All four entry points (`mcp_server.main`,
  `cli.main`, `MarshalService.__init__`, `Fleet.__init__`) now derive the user's interactive
  PATH from `$SHELL -ilc 'echo $PATH'` and union it into `os.environ['PATH']` (in place,
  additive only, idempotent, cached). Opt out with `MARSHAL_NO_PATH_FIX=1` for hermetic CI
  environments where the user PATH is wrong.
- **OpenCode backend now reconciles the final report from `opencode export`.** Opencode's
  `--format json` stream can drop the final `text` part on long replies (the agent's full final
  report is missing from stdout, observed with the GLM-5.2 / kimi models — the user had to
  finish the thread manually to recover the result), and can also drop the final `step-finish`
  (so cost/tokens drift to zero). On a successful run the backend now shells out once to
  `opencode export <session_id>` (~100-500ms, reads the same on-disk session the CLI itself
  wrote) and uses its authoritative `info.tokens`/`info.cost` and full `messages[].parts[].text`
  to override whatever the live stream gave us. Failed runs, runs without a `sessionID`, and
  exports that fail (no binary, old CLI without `export`, corrupt session) all fall back to the
  live stream — never crash a run over recovery. Opt out per-instance with
  `backend.reconcile_from_export = False` (hermetic tests / power users).
- **EastRouter cost reader now paginates `/v1/usage`.** A single page (`?limit=1000`) could miss a
  long run's records when the account was busy (a 283s run + a concurrent benchmark pushed them past
  page 1), so its **real** `admin-api` cost silently fell back to `unavailable`. The reader now walks
  pages (assumed newest-first) back to the run's window, with safe termination (short/empty page,
  past-window, a no-progress guard for an API that ignores `offset`, and a page cap) and the same
  honest token-reconciliation guard. Naive `created_at` timestamps are also normalized to UTC.
- **Cost-source + resilience fixes.** A real EastRouter `admin-api` cost now
  counts toward the benchmark `cheapest` comparison and gets its own usage-summary bucket (it was
  silently excluded from both, so real-cost runs could lose `cheapest` and the source split didn't
  sum). `cancel_run`'s `cancelled` status is no longer clobbered when the killed run's thread returns
  (the terminal write is conditional on the run still being RUNNING). `list_workspaces` /
  `marshal workspace list` degrade to 0 clients on a malformed per-repo config instead of crashing.
  EastRouter cost attribution normalizes a naive `created_at` to UTC (a swallowed `TypeError` was
  silently dropping real costs). A CI test that assumed a backend CLI (cursor) was installed is now
  environment-independent.
- **Concurrency + merge-back hardening.** The per-run state layer now serializes same-run writes with a per-run lock and writes via a
  *unique* temp file, so a `cancel_run` racing the executing run can no longer crash on `os.replace`
  or lose an update; cancel uses a conditional `update_if` that never overwrites a terminal status.
  `integrate` refuses a still-running run (never commits half-written files), serializes concurrent
  integrates (no `index.lock` race / mid-merge repo), reports the **full** set of files a branch
  lands (self-committed *and* uncommitted, previously under-reported), and treats a
  `has_unmerged_commits` git error as `error` rather than a false `empty` that silently drops work.
  An `on_pid` callback failure no longer leaks the spawned process, a `spawn` onto a shut-down pool
  stamps the run `failed` instead of leaving a RUNNING zombie, and `FleetState.list()` skips a
  binary/foreign ledger file instead of crashing. Each fix has a regression test in
  `tests/test_edge_cases.py`.
- **Antigravity headless writes now land in the worktree** (were diverting to agy's scratch dir).
  Headless `agy` can't establish workspace trust without a TTY, so it wrote edits into
  `~/.gemini/antigravity-cli/scratch` instead of `cwd`. A new `CodingAgentBackend.prepare(opts)` hook
  (run by `base.run()` just before spawn) lets the Antigravity adapter pre-register the run's worktree
  in agy's `trustedWorkspaces` (merge-preserving, atomic, idempotent, prunes dead paths, safe for
  parallel runs); the run also passes `--add-dir <cwd>`. Live-verified end-to-end. `--add-dir` alone
  was insufficient (the prior known limitation).

### Added
- **Marshal Recall (persistent fleet memory)** - a Cognee-backed memory layer so fleet runs carry
  learnings across runs and tools instead of starting cold. After each run Marshal remembers the
  task, repo, client, status, and diff summary; before the next run it recalls relevant past
  learnings and injects them into the worker goal. Memory is partitioned by repo (dataset), tagged by
  client/status/task, and scoped per task group. Any MCP-capable session can write a freeform note
  (`marshal memory add`) that a later run recalls. Off by default; enable via a `memory:` block in
  `fleet.config.yaml`. CLI: `marshal memory query|add|stats|improve|forget`; MCP: `memory_query`,
  `memory_add`, `memory_stats`. Install with `pip install 'marshal[memory,fastembed]'`. Also
  available standalone as [second-self](https://github.com/chiruu12/second-self). See
  [`docs/marshal-recall.md`](docs/marshal-recall.md).
- **Multi-workspace MCP server** - one running server can now target several repos, selected per
  call, instead of being bound to the single `MARSHAL_REPO` it launched against. Workspaces are
  declared in a central registry (`~/.marshal/workspaces.yaml`, override with
  `MARSHAL_WORKSPACES_FILE`; or the `MARSHAL_WORKSPACES` env), each loading its **own**
  `fleet.config.yaml` with its own isolated `.marshal` worktrees + ledger. Every action/query tool
  takes an optional `workspace` param; the run-handle tools (`get_run`/`collect_run`/`cancel_run`/
  `integrate`) resolve a run's owning workspace by a cheap, service-free ledger scan. New
  `list_workspaces` and `add_workspace` MCP tools and a `marshal workspace add/list/remove` CLI - the
  registry **hot-reloads**, so a repo added via `add_workspace` or `marshal workspace add` is usable
  without reconnecting the server. A process-wide concurrency cap (`MARSHAL_MAX_CONCURRENT`, default
  8 when multi-repo) bounds total agent runs across all workspaces. Tenancy lives in the MCP layer;
  the engine (`MarshalService`/`Fleet`) stays single-repo. The MCP surface is now **17 tools**. Fully
  backward compatible - with no registry file and no `workspace` arg, behavior is identical to the
  single-repo server.
- **Transient-failure retries** - a run that fails for a transient infra/transport reason (backend
  state-DB lock, rate limit, 5xx, dropped connection) is now re-run with exponential backoff,
  configurable via a top-level `retries` key (default 2; `0` disables). Genuine task failures and
  timeouts are never retried. The classifier is deliberately conservative (a false positive wastes a
  whole run), and each run records its `attempts` count on the `RunRecord`.
- **Worktree environment isolation + `worktree_setup`** - every spawned child (agents and the new
  setup command) now runs with the driver's `VIRTUAL_ENV`/`PYTHONHOME` scrubbed, so an agent's
  `uv run pytest` resolves the *worktree's* environment instead of silently testing the driver's
  installed code. A new optional top-level config key `worktree_setup` (string or argv list) runs
  once in each fresh worktree right after `git worktree add` - e.g. `uv sync --extra dev --extra mcp`
  to provision the worktree's own venv. A non-zero exit tears the worktree down and fails the run
  early rather than handing the agent a broken environment. Provisioning runs **outside** the
  worktree-create lock (only the millisecond `git worktree add` is serialized), so a parallel
  fan-out (`run_many`) provisions worktrees concurrently instead of one `uv sync` at a time.
- **Model & client routing playbook** (`docs/model-playbook.md`) - how to pick which model/client to
  route a task to by task weight (heavy/standard/light), a per-backend model menu, a copy-paste
  tiered fleet config, routing heuristics, and cost-honesty notes (native/estimated/unavailable).
  Linked from the README and the `marshal-orchestrate` Skill. Codex is documented on `gpt-5.5`; the
  shipped price table no longer pins `gpt-5-codex`, so Codex cost reads `unavailable` until you price
  its model (never a fake `$0`).
- **`doctor` over MCP** - the preflight (toolchain, repo, config, per-backend CLI availability +
  auth) is now an MCP tool, not just a CLI command, so a driver can verify a backend is ready
  *before* spawning instead of discovering it from a failed run. Read-only; returns per-check
  results plus a fails/warns roll-up. The MCP surface is now 15 tools.
- **Claude Code backend** (`claude -p`) - a fifth worker adapter. It reports `total_cost_usd` +
  tokens, so its usage is `native` (honest cost, no estimation); `acceptEdits` maps safe-edit,
  `plan`/`bypassPermissions` map read-only/yolo. Live-verified end-to-end: edits land in the
  worktree and the native cost reaches the ledger. The MCP surface is unchanged - backend is a
  per-call parameter, so every existing tool drives it via a config client.
- **`context_files` on `run_agent` / `run_many` / `spawn`** - a driver can now point a worker at the
  specific repo files it should see (injected into the worker's prompt), scoping its context instead
  of leaking the planner's whole session. Exposed through the service and the MCP tools; every
  backend already consumed the field.
- **Consensus driver Skills** - `marshal-review-gate` (gate a merge behind an independent,
  multi-reviewer quorum and a fixed truth table) and `marshal-plan-consensus` (converge biased,
  independent solver plans into one approach via an independent judge before building). Both are
  pure driver playbooks over the existing MCP tools - they add no new execution path.
- **Architectural-invariant tests** - lock the engine's core invariants in source (default
  safe-edit + always-timed runs, capability/permission agreement, no prompting flag, backend never
  encoded in a public name, usage-source honesty, the `run()` timeout/kill loop) plus a Skill
  entrypoint contract and a CI/release workflow contract (least-privilege tokens, pinned actions,
  frozen installs), so a regression trips a test instead of shipping.
- **`--json` on inspection CLI commands** - `marshal backends`, `status`, `usage`, `workflows`,
  and `doctor` accept `--json` for machine-readable output.
- **Declarative YAML workflows** - author a reusable orchestration recipe (phases of
  `fan_out` → `collect` → gated `integrate`) and run it as one unit. The engine executes a
  workflow by *sequencing existing safe primitives* (`run_many` / `run_agent` / `collect_run` /
  `integrate`); it adds no new execution path, so every run still flows through the safe fleet loop
  (timeout, process-group kill, worktree, usage ledger). Integration is **gated off by default**
  (`auto: false`) - a workflow surfaces candidate runs and next-actions, and the driver merges the
  good ones deliberately. New MCP tools `list_workflows` and `run_workflow`, a `marshal workflows`
  CLI command that lists and validates recipes against the live config, a `marshal-workflow` driver
  Skill, and `examples/workflows/{review,compare}.yaml` templates.
- **`cancel_run`** - stop a running agent by run id (process-group `SIGTERM`); exposed as an MCP
  tool and service method.
- **Cursor plan tier in `doctor`** - when the Cursor CLI is available and authenticated, `marshal
  doctor` reports its subscription tier and current model (an honest account fact, not a fabricated
  quota percentage).

### Changed
- **MCP tools are now non-blocking and self-describing.** Each tool runs async and offloads its
  (possibly long-running) work to a worker thread, so a blocking `run_agent` / `run_many` /
  `benchmark` / `run_workflow` no longer holds the server's event loop - the driver can poll
  `status` / `get_run` and `cancel_run` an in-flight run, not only ones started with `spawn`. Tool
  parameters now carry per-parameter descriptions in the schema, and `run_many` takes a typed job
  shape (`{client, goal, task_id?, context_files?}`) instead of an untyped object.
- **CI: coverage floor + macOS.** CI now enforces a **90%** coverage gate (`--cov-fail-under=90`;
  currently ~92%) and runs the suite on **macOS** (py3.12) in addition to Linux (py3.11-3.13), so
  the POSIX process-group paths (`killpg`/`start_new_session`/worktrees) are exercised on the dev
  platform. Both are locked by the workflow contract tests; the coverage gate is opt-in via a flag so
  a bare local `pytest -q` stays fast.

## [0.0.1]

First tagged release: the V1 vertical slice - engine -> service -> CLI -> MCP.

### Added
- **Engine** for driving headless coding agents in isolated git worktrees, off one base class
  (`CodingAgentBackend`) with a shared safe run loop: hard external timeout, no stdin, and a
  process-group kill on timeout.
- **Backend adapters:** Cursor, OpenCode, and Codex, plus an experimental Google Antigravity adapter
  (reply-verified; headless writes currently divert to a scratch dir rather than the worktree).
- **MCP server** exposing an 11-tool surface: `list_clients`, `run_agent`, `run_many`, `spawn`,
  `benchmark`, `report`, `get_run`, `collect_run`, `integrate`, `status`, `usage`.
- **Merge-back workflow:** `collect_run` (read-only diff review) and `integrate` (explicit merge into
  the current branch); the main branch is untouched until integrate.
- **Per-provider usage tracking:** an append-only ledger (`usage/events.jsonl`) of facts (tokens /
  cost / duration / source) with interpretation derived on read. Cost is tagged by source
  (native / estimated / unavailable) and never fabricated as `$0`.
- **Capped parallel `run_many`** and **non-blocking `spawn`** for background runs.
- **Measured savings benchmark:** `benchmark` runs one goal through N strategies and `report`
  derives a source-honest cost / latency / outcome comparison; "cheapest" ranks only strategies with
  a known cost.
- **`marshal doctor`** preflight CLI command, plus `backends`, `status`, `usage`, and `mcp`.
- **Driver Skills:** `marshal-orchestrate` and `marshal-benchmark`.
- **Claude Code plugin:** `.claude-plugin/` manifests so `/plugin marketplace add chiruu12/marshal`
  installs both Skills and the MCP server in one step. The server runs from the plugin checkout via
  `uv` and starts with zero clients (logging how to configure one) when no `fleet.config.yaml` is
  present, so a fresh install never crashes on connect.
- **Config** via `fleet.config.yaml` (clients = named backend instances) with an example template.

[Unreleased]: https://github.com/chiruu12/marshal/compare/v0.2.1...HEAD
[0.2.1]: https://github.com/chiruu12/marshal/compare/v0.2.0...v0.2.1
[0.2.0]: https://github.com/chiruu12/marshal/compare/v0.1.0...v0.2.0
[0.1.0]: https://github.com/chiruu12/marshal/compare/v0.0.1...v0.1.0
[0.0.1]: https://github.com/chiruu12/marshal/releases/tag/v0.0.1
