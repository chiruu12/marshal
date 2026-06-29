# Changelog

All notable changes to this project are documented here.

The format is based on [Keep a Changelog](https://keepachangelog.com/en/1.1.0/), and this project
adheres to [Semantic Versioning](https://semver.org/spec/v2.0.0.html). Marshal is **pre-1.0**: minor
versions may include breaking API changes until 1.0.

## [Unreleased]

### Fixed
- **EastRouter cost reader now paginates `/v1/usage`.** A single page (`?limit=1000`) could miss a
  long run's records when the account was busy (a 283s run + a concurrent benchmark pushed them past
  page 1), so its **real** `admin-api` cost silently fell back to `unavailable`. The reader now walks
  pages (assumed newest-first) back to the run's window, with safe termination (short/empty page,
  past-window, a no-progress guard for an API that ignores `offset`, and a page cap) and the same
  honest token-reconciliation guard. Naive `created_at` timestamps are also normalized to UTC.
- **Cost-source + resilience fixes** (from a PR review pass). A real EastRouter `admin-api` cost now
  counts toward the benchmark `cheapest` comparison and gets its own usage-summary bucket (it was
  silently excluded from both, so real-cost runs could lose `cheapest` and the source split didn't
  sum). `cancel_run`'s `cancelled` status is no longer clobbered when the killed run's thread returns
  (the terminal write is conditional on the run still being RUNNING). `list_workspaces` /
  `marshal workspace list` degrade to 0 clients on a malformed per-repo config instead of crashing.
  EastRouter cost attribution normalizes a naive `created_at` to UTC (a swallowed `TypeError` was
  silently dropping real costs). A CI test that assumed a backend CLI (cursor) was installed is now
  environment-independent.
- **Concurrency + merge-back hardening** (from an adversarial audit of the highest-consequence
  paths). The per-run state layer now serializes same-run writes with a per-run lock and writes via a
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
