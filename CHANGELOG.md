# Changelog

All notable changes to this project are documented here.

The format is based on [Keep a Changelog](https://keepachangelog.com/en/1.1.0/), and this project
adheres to [Semantic Versioning](https://semver.org/spec/v2.0.0.html). Marshal is **pre-1.0**: minor
versions may include breaking API changes until 1.0.

## [Unreleased]

### Security
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

[Unreleased]: https://github.com/chiruu12/marshal/compare/v0.0.1...HEAD
[0.0.1]: https://github.com/chiruu12/marshal/releases/tag/v0.0.1
