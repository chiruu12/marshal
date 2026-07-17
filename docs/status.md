# Implementation status & roadmap

A snapshot of what's built, what's verified, and what's next. For the architecture see
[`design.md`](design.md); for how to use it see [`usage.md`](usage.md).

## What's built

The full vertical slice is in place - driver → MCP → service → fleet → backends:

| Module | Responsibility | State |
|--------|----------------|-------|
| `types.py` | Shared Pydantic models + enums | done |
| `backends/base.py` | Abstract backend + safe `run()` (no-stdin, hard timeout) | done |
| `backends/{cursor,opencode,codex,command_code,antigravity,claude_code}.py` | Six adapters off one base class | done |
| `worktree.py` | Git worktree lifecycle (isolation boundary) | done |
| `usage.py` | Per-provider usage (events.jsonl + summary + cost-per-outcome) | done |
| `pricing.py` | Token → cost price table (the `ESTIMATED` path) | done |
| `eastrouter.py` | Read real per-run cost from EastRouter `/v1/usage` (the `ADMIN_API` path) | done |
| `state.py` | Persistent fleet state (one `runs/<run_id>.json` per run) | done |
| `fleet.py` | Orchestrator: worktree → run → price → record → persist | done |
| `registry.py` | Construct backends by name | done |
| `config.py` | `fleet.config.yaml` → clients, Fireworks guard | done |
| `workflow.py` | Declarative YAML workflows - spec, validation, runner over the service primitives | done |
| `workspaces.py` | Multi-repo registry (MCP layer): default + `~/.marshal/workspaces.yaml` + env, lazy per-repo service cache (hot-reloaded), service-free run-id addressing, register/scaffold helpers, shared concurrency gate | done |
| `service.py` | Testable core the MCP/CLI call into (single-repo) | done |
| `cli.py` | `marshal doctor/backends/usage/status/workflows/workspace/clean/mcp` | done |
| `mcp_server.py` | MCP surface over stdio ([`docs/mcp-tools.md`](mcp-tools.md)); each action/query tool takes an optional `workspace` | done |

Quality gate: full unit suite passes; ruff and mypy (strict) clean across all source files. CI
enforces a 90% coverage floor (currently ~92%) and runs on Linux (py3.11-3.13) + macOS (py3.12).

## Backend verification matrix

| Backend | Installed | Read-only | Safe-edit (worktree write) | Native usage |
|---------|-----------|-----------|----------------------------|--------------|
| OpenCode | yes | verified | verified | verified (tokens + cost) |
| Cursor | yes | verified | verified | n/a by design (Admin API only) |
| Claude Code | yes | verified | verified | verified (tokens + cost, native) |
| Codex | yes | - | verified* | tokens only (cost `admin-api`/estimated/unavailable) |
| Command Code | yes | plan mode | verified (`--yolo`; headless auto-accept blocks writes) | none (hosted account → `unavailable`)*** |
| Antigravity | yes | verified (reply) | verified** | none |

\* Codex verified end-to-end via a custom OpenAI-compatible provider (Responses API): worktree
writes land and the JSONL parser extracts text + tokens correctly. A Codex client routed through
EastRouter with `usage_api: eastrouter` has its **real** per-run cost read back from EastRouter's
`/v1/usage` and reported as `admin-api`; without a usage API the cost is `estimated` (model in the
price table) or `unavailable` (token-only, unpriced).
\*\*\* Command Code live-verified headless (model `zai-org/GLM-5.2`). `command-code -p` prints plain
text with no token/cost accounting, so usage is `unavailable` (a hosted account's spend lives in its
own dashboard, never a fabricated $0); `doctor` surfaces its provider + default model.
\*\* Antigravity headless writes now land in the worktree (verified end-to-end 2026-06-27). The
adapter's `prepare()` pre-registers the run's worktree in `~/.gemini/antigravity-cli/settings.json`
`trustedWorkspaces` and passes `--add-dir <cwd>`; without the trust entry, agy diverts edits to its
scratch dir (`--add-dir` alone was insufficient). Still no native usage (text-only output).

## Roadmap

`collect_run`/`integrate` are shipped.

### Phase 1 - cost-proof (shipped)
Per-provider cost is now trustworthy and honest, single-threaded. Shipped: `duration_ms` on every
run; `extract_usage` wired into `Fleet.run`; a YAML price table (`pricing.py` + `data/prices.yaml`) with
`ESTIMATED` tagging and `unpriced`-not-`$0` honesty; cost/duration/source persisted on `RunRecord`;
cost-per-outcome (`$/run`, `$/succeeded`) + a native/estimated split in `usage`; partial-usage
recovery on timeout; and `RunStatus.EMPTY` for clean-but-no-work runs. The engine stamps facts to an
immutable ledger; the report layer derives interpretation on read.

### Phase 2 - solidify (shipped)
Done: the 5 known `collect_run`/`integrate` bugs (quoted-path handling via `-z`; dirty-main integrate
now returns a structured `blocked` status; conflict/blocked retries re-merge instead of reporting
"empty"; detached-HEAD refused; `commit_all --no-verify` pinned by a hook test); a git-spawn timeout
+ stdin guard + `GIT_TERMINAL_PROMPT=0` on `WorktreeManager._git`; globally-unique `run_id`; a run
loop that terminal-stamps `failed` on any exception (no zombie RUNNING); and **process-group kill on
timeout** (`os.killpg`), completing invariant #1 (no orphaned agent grandchildren).

### Phase 3 - parallel + measured benchmark (shipped)
**Per-run state files** (`runs/<run_id>.json`, single writer each, aggregates derived on read) and
**usage derived on read** (append-only `events.jsonl`) for concurrency safety; **capped parallel
`run_many`** via `ThreadPoolExecutor` behind a swappable Fleet API (worktree-create lock + Cursor
launch stagger + per-job failure isolation); and the **measured savings benchmark** - `benchmark`
runs one goal through N clients (strategies) and `report` derives a source-honest cost/latency/
outcome comparison from the ledger ("cheapest" only ranks strategies with a known cost). All
exposed over the service and MCP. This completes the V1 core (engine + cost + benchmark).

### Phase 4 - coverage & productization (started)
Shipped: the **Skills layer** - `skills/marshal-orchestrate` (decompose → spawn → monitor →
collect → integrate), `skills/marshal-benchmark` (compare strategies), `skills/marshal-workflow`
(author + run a declarative recipe), `skills/marshal-review-gate` (gate a merge behind reviewer
consensus), and `skills/marshal-plan-consensus` (converge on an approach before building) driver
playbooks, completing the four surfaces (engine · MCP · Skills · config).
Also shipped: **non-blocking `spawn`** - start a run in the background (persistent pool on the Fleet)
and poll `status`/`get_run`; the run is recorded RUNNING at once and survives the driver turn.
**`cancel_run`** stops a running agent by id (process-group `SIGTERM`).
**Declarative YAML workflows** (`workflow.py`) - a recipe of `fan_out`/`collect`/gated-`integrate`
phases the engine runs by sequencing the existing safe primitives (no new execution path; integrate
gated off by default); surfaced as `list_workflows`/`run_workflow` (MCP), `marshal workflows` (CLI),
and the `marshal-workflow` Skill.
**Cursor plan tier in `doctor`** - surfaces the authenticated CLI's subscription tier + current
model (an honest account fact; individual accounts expose no usage/quota API, so no percentage is
fabricated).
**Claude Code backend** (`backends/claude_code.py`) - `claude -p --output-format json` with
`acceptEdits` for safe-edit; it reports `total_cost_usd` + tokens, so usage is `native` (honest
cost, no estimation). Live-verified end-to-end (2026-06-26): edits land in the worktree and the
native cost flows to the ledger.
**Antigravity headless writes** (`backends/antigravity.py`) - `prepare()` registers the run's
worktree in agy's `trustedWorkspaces` before launch, so headless edits land in the worktree instead
of the scratch dir (live-verified 2026-06-27). This closes the prior known limitation.
**Command Code backend** (`backends/command_code.py`) - `command-code -p` (a hosted coding agent on
its own account) with `plan` for read-only and `--yolo` for safe-edit (headless `auto-accept`
blocks writes); `doctor` surfaces its
provider + default model. `-p` prints plain text with no token/cost accounting, so usage is
`unavailable` (a hosted account's spend lives in its own dashboard, never a fabricated $0).
Live-verified headless (model `zai-org/GLM-5.2`).
**EastRouter real-cost reader** (`eastrouter.py`) - a client with `usage_api: eastrouter` has its
REAL per-run charge read from EastRouter's `/v1/usage` after the run and reported as `admin-api`,
attributed by model + the run's time window with a token-reconciliation guard (it keeps the
estimate/unavailable cost when it can't uniquely attribute). Codex routed through EastRouter uses it;
live-verified with real `admin-api` cost on the ledger.
**Graceful backend skip** (`service.py`) - a client whose backend CLI is unavailable is skipped at
startup (stderr warning, recorded on `skipped_clients`) instead of failing a run mid-flight; the full
backend set still reaches the Fleet, so `doctor` still reports a missing backend as a FAIL.
**Workflow client-skip + failure surfacing** (`workflow.py`) - a `fan_out` phase skips clients whose
backend CLI is unavailable (runs with whatever fleet is present; raises only if all are unavailable)
and surfaces non-succeeded runs as phase notes + `next_actions`. The `WorkflowService` Protocol gained
a read-only `client_available()` probe.
**Field-review fixes** (from a real 3-agent fleet session, 2026-07-17) - the state/config sync gaps:
(1) workspace **config hot-reload** - the registry rebuilds a workspace's service when its
`fleet.config.yaml` appears/changes/vanishes (mtime+size signature), so `list_clients`/`spawn` never
serve a stale client list while `doctor` reads live; (2) **PATH fallback + self-healing clients** -
`user_path()` falls back to well-known user bin dirs (`~/.local/bin`, Homebrew, …) when the
login-shell probe fails, `doctor` probes freshly constructed backends when its snapshot lacks one,
and clients skipped at startup re-probe (and heal) on resolution/`list_clients`; (3) the
**`verify:` gate** - an optional per-workspace command (e.g. the repo's full test suite) that runs
after a would-be-succeeded run with file changes; failure lands as **`verify_failed`** with the
output tail on the record, worktree kept for review; (4) **repo-shape-aware scaffold** - a new
workspace's starter config carries commented `worktree_setup` suggestions detected from the repo
layout (root vs nested `pyproject.toml`/`package.json`/`go.mod`/`Cargo.toml`); (5) **orphan
reaping** - scope-mode `clean` reconciles `.marshal/worktrees` against the ledger and reaps dirs
with no (readable) run record (`orphans_removed`); (6) resolution errors carry actionable hints
(ad-hoc `backend=` escape hatch, `doctor`, `add_workspace`).
Remaining: Antigravity native usage; Cursor admin-API usage; a Gemini
backend; PyPI publish; and eventually **Chauffeur** (see [`chauffeur-future.md`](chauffeur-future.md)).
