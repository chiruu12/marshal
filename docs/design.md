# Marshal - Foundational Design

> **Marshal** is the infrastructure layer: one "driver" agent (Claude Code) plans work, then
> Marshal spawns and manages a **fleet of headless coding agents** (Cursor CLI, OpenCode, Codex,
> Command Code, Google Antigravity, Claude Code now; Gemini later), each in an isolated git worktree, in parallel - exposed to the driver as an
> **MCP server + Skills**, with **per-provider usage tracking**. To *marshal* = to gather and
> organize a force - exactly what this does to a fleet of agents.
>
> **Chauffeur** (future, separate product) is the end-user autonomous coding system built *on top
> of* Marshal - planning, routing, self-driving workflows, agent-management UI. Out of scope for
> now; see `docs/chauffeur-future.md`.

Status: design locked. Language: **Python + uv**. **Pydantic v2** models for value types, config,
persisted state, and the MCP I/O surface; stdlib for the rest (subprocess, pathlib). Backend CLI
stdout is parsed as plain dicts on purpose. See the package layout in the README and `docs/status.md`.

---

## 0. Locked decisions

- **Execution model:** background **fleet** - N agents in parallel, each in its own git worktree; driver monitors → collects → merges → verifies.
- **Backends:** one **base class**, one **adapter per backend**. Cursor + OpenCode + Codex + Command Code + Antigravity + Claude Code now. Gemini later = new adapter only.
- **Runtime:** local CLIs (shell out). OpenCode additionally exposes an HTTP server (see §4) - optional fast path.
- **Surface:** MCP server (user-configured, N clients) + Skills (orchestration playbooks). Backend is a **per-client/per-call parameter**, never global, never encoded in tool names. Skills double as the **driver's manual** - they teach the harness (Claude Code or any host) *what* Marshal can do and *how* to drive it (decompose → spawn → monitor → integrate).
- **Differentiator:** **per-provider usage tracking** + a `usage` command. Nearly every competitor omits this.
- **Packaging:** Python package (`uv`), distribute via `uvx`. Private first → public when polished.
- **Naming:** product/repo/CLI/MCP id = `marshal`. The Python **import package must NOT be `marshal`** (it shadows the stdlib `marshal` builtin and won't import) → import package `marshal_engine`, CLI entry point `marshal`. PyPI distribution `marshal` if free, else `marshal-orchestrator`.
- **Two tiers:** Marshal = infra (this repo). Chauffeur = future end-user product built on Marshal. Keep Marshal a clean, embeddable library/engine so Chauffeur (and others) can build on it.

---

## 1. The spine: state must outlive the driver

Claude Code is **stateless across turns** - it forgets the fleet between messages, but background
agents outlive a turn. So fleet state lives in the **long-lived MCP server**, persisted to disk.

- **MCP tools** = mechanism (imperative verbs).
- **Skills** = policy (decomposition, prompt-writing, merge judgment).
- **Engine (Python lib)** = the mechanism the MCP server calls.

Don't put decomposition logic in the MCP server, and don't put process management in a Skill.

---

## 2. Backend base class (litellm-style, pure-function adapters)

Convergent pattern from AWS CAO, ORCH, and litellm: one abstract base; each backend implements a
common contract; the orchestrator treats all backends uniformly. Keep `build_invocation` and
`map_permission` **pure functions returning argv** - fully unit-testable without spawning processes.

```python
class CodingAgentBackend(ABC):
    name: str            # "cursor" | "opencode" | "codex" | "gemini"
    binary: str          # "cursor-agent" | "opencode" | "codex" | "gemini"

    class Capabilities:          # feature flags → orchestrator degrades gracefully
        json_output: bool
        stream_json: bool
        sessions: bool           # resume/continue
        server_mode: bool        # e.g. opencode serve
        native_usage: bool       # emits tokens/cost in output
        permission_modes: set[str]   # {"read-only","safe-edit","yolo"}

    # four abstract hooks every backend implements:
    @abstractmethod
    def check_available(self) -> bool: ...           # which-binary + auth probe + version assert

    @abstractmethod
    def build_invocation(self, task, opts) -> list[str]: ...   # (task, perms, model, session, cwd) -> argv

    @abstractmethod
    def map_permission(self, mode) -> list[str]: ...           # read-only|safe-edit|yolo -> native flags

    @abstractmethod
    def parse_output(self, raw_stdout, raw_stderr, exit_code) -> AgentResult: ...
        # normalize -> {text, session_id, usage:{in,out,cache,cost}, files_changed, status}

    # optional overridable hooks (have defaults):
    def extract_usage(self, result) -> UsageRecord | None: ...   # default: result.usage; override to fetch/estimate
    def prepare(self, opts) -> None: ...                         # default no-op; per-run setup before spawn
    def account_info(self) -> dict[str, str] | None: ...         # default None; cheap account metadata (plan tier)

    # run() lives on the base: build_invocation -> spawn in worktree (timeout!) -> capture -> parse_output
```

Rules: code against **capability flags**, not assumptions. Persist `session_id` yourself.
Add a **version probe** in `check_available` + **contract tests per backend** (their flags/JSON drift fast).

---

## 3. Per-backend cheat sheet (verified from docs, June 2026)

| | **Cursor (`cursor-agent`)** | **OpenCode (`opencode`)** | **Codex** | **Claude Code** |
|---|---|---|---|---|
| Headless run | `cursor-agent -p "..."` | `opencode run "..."` | `codex ...` | `claude --print` |
| JSON | `--output-format json\|stream-json` | `--format json` (NDJSON event stream) | json | `--output-format json\|stream-json` |
| Final text | `result.result` field | concat all `text` events' `part.text` | - | json field |
| Tokens/cost in output | **NONE** (see §6) | `step_finish.cost` + `.tokens.{input,output,reasoning,cache.read,cache.write}` | - | `total_cost_usd` + `usage{...}` |
| File changes | `writeToolCall.result` events / diff worktree | inside `edit`/`write` tool outputs; or `GET /session/:id/diff` | - | - |
| Session resume | `--resume <id>` / `--continue` (persist `session_id` from JSON) | `-s <id>` / `-c` / `--fork` | - | `session_id` returned |
| Model select | `--model` / `--list-models` (default **Auto**) | `-m provider/model` / `opencode models` | - | - |
| Working dir | **no `--cwd`**; `--workspace <path>`; `-w/--worktree [name]`, `--worktree-base` | `--dir <path>` (config walks up to git root) | - | - |
| Server mode | no | **`opencode serve`** (OpenAPI on 127.0.0.1:4096) + `opencode acp` | no | no |

---

## 4. OpenCode server mode (a real advantage)

`opencode serve` → headless HTTP server, **OpenAPI 3.1 at `/doc`**, default `127.0.0.1:4096`.
Auth via `OPENCODE_SERVER_PASSWORD`. Key endpoints: `POST /session`, `POST /session/:id/message`
(blocking) or `POST /session/:id/prompt_async`, `GET /session/:id/diff` (authoritative diff),
`GET /event` (SSE). SDK: `@opencode-ai/sdk`.

Model it as an optional `server_mode` capability: keep a **warm `serve` process** and attach to it
for lower latency, with subprocess `opencode run` as fallback (cmuxlayer-style fast/slow path).

---

## 5. Normalized permission model (3 tiers → native flags)

The single most reusable artifact (from shinpr/sub-agents-mcp). Headless = **no interactive
approvals, ever** - "sub-agents have no stdin, so any approval prompt deadlocks the run."

| Tier | Cursor | OpenCode | Codex | Claude Code | Gemini |
|---|---|---|---|---|---|
| **read-only** | `--mode plan` (or no `--force` + allowlist) | agent `plan` / `permission` read+deny edit/bash | `-s read-only` | `--permission-mode plan` | `--approval-mode plan` |
| **safe-edit** (default) | `--force` (worktree is the boundary) | `--dangerously-skip-permissions` | `-s workspace-write` | `--permission-mode acceptEdits` | `--approval-mode auto_edit` |
| **yolo** (opt-in) | `--force`/`--yolo` (no deny) | `--dangerously-skip-permissions` | workspace-write, no approval | bypass | bypass |

Key per-backend detail:
- **Today, safe-edit is process-equivalent to yolo for Cursor and OpenCode.** The engine emits no deny/allow config: Cursor's safe-edit is a bare `--force` and OpenCode's is `--dangerously-skip-permissions`. The **git worktree is the sole enforced boundary**; the scoped deny-list/allowlist grammar below is a **config layer that is not yet implemented**.
- **Cursor (future config layer):** `--force`/`--yolo` = "allow everything **not explicitly denied**" - so the intended safe pattern is `--force` + a curated `deny` list (`Shell(rm)`, `Write(**/.env)`, `Write(**/.git/**)`). Permission grammar lives in `~/.cursor/cli-config.json` / `.cursor/cli.json`: `Shell(git)`, `Read(glob)`, `Write(src/**)`, `WebFetch(*.github.com)`, `Mcp(server:tool)`. **Deny beats allow.** Redirections (`>`,`|`) can't be allowlisted inline. Also needs `--trust` (headless workspace trust) and `--approve-mcps` for MCP.
- **OpenCode (future config layer):** `permission` keys: `read, edit, glob, grep, bash, task, skill, lsp, question, webfetch, websearch, external_directory, doom_loop`; values `allow|ask|deny`; **last matching rule wins**. **CRITICAL for server mode:** `serve`+`attach` **hangs if any permission is `ask`** → set all to `allow` + `question: deny` in a dedicated `opencode.json`. `--dangerously-skip-permissions` does NOT cover the `question` tool.
- **Worktree isolation is the dominant safety primitive** across all serious tools (ORCH, Crystal, Orca). Main branch untouched until explicit merge. Worktrees share host FS/network → fine for trusted local use; for untrusted code use containers later (agentbox/scion).

---

## 6. Usage tracking (the differentiator) - and the Cursor asymmetry

**Major finding: backends are NOT symmetric on usage.**

- **OpenCode - easy.** Per-step `cost`+`tokens` in the stream; `opencode stats --days --models`; on-disk store at `~/.local/share/opencode/storage/` (note: message files store `cost: 0` → recompute from tokens via price table). Caveat: stream may **drop the final `step_finish`** → read final accounting from on-disk store / `opencode export`, not the stream.
- **Cursor - hard.** **No tokens, no cost in CLI output at all.** Programmatic usage only via the **Admin API** (`api.cursor.com`, HTTP Basic `-u KEY:`) - **Team/Enterprise only**. `POST /teams/filtered-usage-events` returns per-event tokens+cost with an **`isHeadless`** flag and **`serviceAccountId`**. Pattern: give each worker its own **service-account key**, attribute via `serviceAccountId`. Pro/individual accounts → dashboard only, no API.
- **Codex/Gemini - likely no JSON usage** → fall back to terminal screen-scrape (cmuxlayer `read_screen` parses tokens/context% off output).
- **EastRouter (real `admin-api` cost) - implemented.** A client may set `usage_api: eastrouter` to have its REAL per-run cost read from EastRouter's `/v1/usage` after the run (`eastrouter.py`), reported as `admin-api` rather than an estimate - EastRouter's price swings with prompt caching, so a static table would mislead. Attribution is by model + the run's `[start, end]` time window with a token-reconciliation guard; an unattributable run (e.g. two clients on the same EastRouter model concurrently) keeps its estimate/unavailable cost instead of asserting a wrong one. Codex routed through EastRouter uses this; OpenCode pointed at EastRouter (`eastrouter/<id>`) can't be priced by the CLI and stays `unavailable`.

**Local schema (no DB; file-based like ORCH's `.orchestry/`):**

`usage/events.jsonl` (append-only, one line per run):
```json
{"ts":"...","run_id":"...","client":"reviewer","backend":"cursor","model":"...",
 "worktree":"feat-x","tokens":{"input":1234,"output":567,"cache_read":0},
 "cost_usd":0.041,"duration_ms":8200,"status":"success",
 "source":"native|admin-api|estimated|unavailable"}
```
`usage/summary.json` (cumulative rollup, updated each run): `by_client`, `by_backend`, `by_model`, `totals`.

Apply a local `(backend, model) → price` table for backends that report tokens but not cost.
**Tag every record `source`** so estimated/scraped costs are auditable and never presented as ground truth.
Surface a `usage` MCP tool / `<name> usage` CLI with `--since` and `--by client|backend|model`.

---

## 7. MCP surface + config (N user-configured clients)

**Single config file**, named clients each pinning a backend; **secrets by reference only**:

```yaml
# fleet.config.yaml  (see fleet.config.example.yaml)
defaults:
  permission: safe-edit
  timeout_s: 600
clients:
  reviewer:    { backend: cursor,   permission: read-only }
  implementer: { backend: opencode, model: opencode-go/glm-5.2, permission: safe-edit }
  refactorer:  { backend: codex,    permission: safe-edit }
```

Runtime state - worktrees, per-run JSON, usage - lands under `.marshal/`. Auth is per-CLI login;
an optional `secret_ref: env:VAR` is an advisory preflight check only (not injected).

**Worktree environment isolation.** The driver usually runs inside its own activated venv, so
`os.environ` carries `VIRTUAL_ENV`/`PYTHONHOME` pointing at the *driver's* interpreter. Every
spawned child (agents and the worktree-setup command) has those scrubbed (`env.child_env`), so the
worktree's own `.venv` wins - otherwise an agent's `uv run pytest` silently resolves the driver's
install and tests stale code. A fresh worktree has no `.venv` (it's gitignored), so the optional
top-level `worktree_setup` command (e.g. `uv sync --extra dev --extra mcp`) provisions one right
after `git worktree add`; a non-zero exit tears the worktree down and fails the run early.

**Graceful backend skip.** `MarshalService.__init__` probes each configured backend's CLI at
startup; a client whose backend is unavailable is **skipped** (stderr warning, recorded on
`skipped_clients`) rather than failing a run mid-flight. The **full** backend set still goes to the
Fleet, so `doctor` (which probes every configured backend) still reports a missing one as a FAIL.

**Lean tool surface** (backend is a param, NOT in tool names - avoids the 2N-tool explosion).
Shipped today (15): `doctor`, `list_clients`, `run_agent`, `run_many`, `spawn`, `cancel_run`,
`benchmark`, `report`, `get_run`, `collect_run`, `integrate`, `status`, `usage`, `list_workflows`,
`run_workflow`. Current state is tracked in `docs/status.md`.

Mirror to **driver Skills** (the `marshal-*` Skills in `skills/`) so the
fleet works in both MCP and Skills hosts.
Security from day one: **localhost-only bind, reject non-loopback, validate `Host` header** (DNS-rebind).

### Declarative workflows (a recipe is a sequence of primitives, not a new execution path)

A **workflow** (`workflow.py`) is a human-authored YAML recipe - phases of `fan_out` / `agent` /
`collect` / `integrate` - that the engine runs by issuing exactly the calls a driver would make by
hand (`run_many` / `run_agent` / `collect_run` / `integrate`) in declared order. **Safety property:
the runner adds no new execution path.** Every run still flows through `Fleet.run` (external timeout
+ process-group kill + worktree + usage ledger); the runner never spawns a process, touches git, or
writes run state. Spec validation is pure (client names checked against the config, goal templates
restricted to bare `{input}` placeholders, sources resolved) so a typo'd recipe fails before any
agent runs. A `fan_out` phase first drops any client whose backend CLI is unavailable (a read-only
`client_available` probe - the fifth method on the `WorkflowService` Protocol) and runs with whatever
fleet remains, raising only if **all** are unavailable; non-succeeded runs surface as phase notes +
`next_actions`. **Integration is gated off by default** (`auto: false`): a workflow surfaces succeeded
runs as candidates with `next_actions`, and the driver merges the good ones after review - `succeeded`
is not `correct`. The judgment (which recipe, when to merge) stays in the `marshal-workflow` Skill;
the engine only sequences. Discover/validate with `marshal workflows`; run via `run_workflow`.

---

## 8. Edge-case hardening checklist (MUST defend - from real GitHub/forum issues)

1. **External timeout + kill on EVERY run.** Both Cursor (`-p` hang, version-gated) and OpenCode (hangs on API error/429 with no exit code; hangs after tool calls) hang. Treat absence of stdout as a hang.
2. **No-stdin deadlock is the #1 footgun.** Never default to a prompting permission mode. Default `safe-edit` (non-prompting). OpenCode: set `question: deny`.
3. **OpenCode stream drops final `step_finish`** → read final cost/tokens from on-disk store / `export`, not the stream.
4. **OpenCode `serve`+`attach` hangs if any permission is `ask`** → all `allow` + `question: deny` in a dedicated `opencode.json`.
5. **OpenCode rate-limit = immediate exit, no auto-retry** → implement orchestrator backoff/retry.
6. **Cursor: pin & assert version** at startup (hang/race/terminal-release fixes are version-gated). Parse stdout JSON **only on exit 0**; on failure there's no JSON, only stderr.
7. **Cursor wants a TTY** → run under pseudo-tty (`script -q /dev/null`) or `--print`, stdin from `/dev/null`, **clean shell** (a heavy `.zshrc` causes completion-detection hangs).
8. **Cursor concurrent launches:** stagger ~100ms + use worktrees (file-lock race, fixed but stagger anyway).
9. **Cursor workspace trust:** `--trust` / pre-seed trusted config - esp. required for MCP in headless.
10. **Worktree lifecycle:** spec creation, naming, owner-tracking, orphan detection, `git worktree prune` on crash. Track which run owns which worktree in the usage log.
11. **Concurrency caps:** each CLI is 150-400 MB RAM → cap parallel runs per fleet and per client or a fan-out OOMs the host.
12. **Secrets by reference** (`env:VAR`/file), validate presence at load, fail fast with a clear message. Never inline.

---

## 9. Open questions / verify empirically (no docs gap closure)

- OpenCode: stdin piping into `run` (undocumented); `opencode stats --json` (was a feature request); exact `sst → anomalyco` repo-move story (confirmed via redirect, no official announcement found). The canonical repo now redirects to **`github.com/anomalyco/opencode`**; npm still `opencode-ai`.
- OpenCode subscription clarity: the ~$10/mo tier is **OpenCode Go** ($5 first month then $10; caps $12/5h, $30/wk, $60/mo; models GLM-5.1, Kimi K2.6, MiniMax M2.7; provider prefix `opencode/`). **Zen** is separate pay-as-you-go gateway.
- Cursor: exact `sandbox.mode` × `--force` interaction (docs ambiguous); whether `--resume <id>` is reliable fully-headless; resume-after-compression blank-chat bug.
- Cursor usage without a Team/Enterprise plan → decide: require service-account keys, or estimate from a local price table (but Cursor doesn't even emit tokens → estimation needs the Admin API or is impossible for Pro). **This is a product decision to surface.**

---

## 10. Build roadmap

- **Phase 0 - repo:** lay down `pyproject.toml` (uv), the package skeleton, and `docs/`.
- **Phase 1 - engine:** base class + `CursorBackend` + `OpenCodeBackend` + `CodexBackend` (pure `build_invocation`/`map_permission` + `parse_output`), worktree manager, process runner (timeout!), result collector. CLI-testable standalone before any MCP. Contract tests per backend.
- **Phase 2 - usage:** `events.jsonl` + `summary.json`, price table, `source` tagging, OpenCode native + on-disk reconciliation, Cursor Admin-API path, `usage` command.
- **Phase 3 - MCP server:** the 15 tools + `fleet.config.yaml` loader + persistent fleet state + localhost hardening.
- **Phase 4 - Skills:** the `marshal-*` driver playbooks - `marshal-orchestrate` (decompose → spawn → review → integrate), `marshal-benchmark` (measured strategy comparison), `marshal-workflow` (declarative YAML recipes), `marshal-review-gate` + `marshal-plan-consensus` (consensus review / approach convergence).
- **Phase 5 - harden + docs:** retries/backoff, concurrency caps, worktree cleanup, dry-run, OpenCode warm-server fast path, README/onboarding → flip public.

## Anchors to study before/while building
- **AWS `awslabs/cli-agent-orchestrator`** - architectural gold standard (provider resolution, tmux/PTY isolation, dual MCP servers, localhost hardening).
- **shinpr/sub-agents-mcp + sub-agents-skills** - closest match (permission-mapping table, MCP+Skills dual surface). Beat its global `AGENT_TYPE` with per-call backend.
- **ORCH** - worktree isolation + review state machine + live per-run cost.
- **litellm `BaseConfig`** - the adapter triad to adapt to a process world.

---

## 11. Product-driven design (from the PRD - see `docs/internal/vision.md`)

Positioning: **"the control plane for AI coding agents."** Thesis: keep the best model planning;
route execution to cheaper/specialized workers; isolate context; **prove the savings**. Four things
become first-class and must be designed in (even if full logic lands in V2):

1. **Routing by ROLE, not provider.** `TaskSpec` carries a `role` (planner/coder/writer/reviewer/
   researcher/bulk-processor/test-fixer/refactorer). Config maps role → client. The engine stays
   *mechanism*; the routing decision is *policy* (config + Skills). Added `role` + `context_files`
   to `TaskSpec`.
2. **Benchmarking + cost intelligence (first-class).** Beyond `usage`: run the same task through N
   routing strategies and record cost/latency/completion/test-pass/merge/retries/quality. Adds MCP
   tools **`benchmark`** and **`report`**. Builds directly on the usage schema (§6) - each run
   already logs cost+source; a benchmark just groups runs by a `strategy` label.
3. **Policy / customer-config layer.** Extend `fleet.config.yaml` defaults with
   `strategy: quality-first|cost-first|balanced`, `budget` ceilings (per task/repo), a role→client
   map, and `require_approval_before_merge`. The user expresses intent; the engine enforces.
4. **Context scoping per worker.** Each worker runs in its own worktree with a **fresh context** -
   never the planner's session. `context_files` are surfaced as prompt hints; restricting the
   worker's visible file set is future work (the worktree is a full checkout). Aimed at token waste +
   drift, not an optimization.

**Fleet-state records** must capture (basis for reporting/benchmarking): task, role, client/backend,
model, cost, tokens, duration, artifacts/diff, checks (test pass/fail), merged?, strategy label.

**Roadmap mapping:** Phases 0-5 == PRD **V1** (control-plane primitive + cost logging + simple
benchmark). **V2** (role-routing engine, policy engine, comparison reports/dashboards, team configs,
budgets) and **V3** (auto routing recommendations, historical provider scoring, org policy,
approvals, multi-repo) are post-v0. Design the data model so V2 reporting is a *query, not a rewrite*.
Keep V1 focused - the #1 risk is becoming "yet another agent framework."

### Backends in scope (built)

Six adapters derive from `CodingAgentBackend`, each with pure `build_invocation`/`map_permission`
and contract tests:

| Backend | Headless invocation | read-only / safe-edit / yolo | Usage in output |
|---|---|---|---|
| Codex | `codex exec --json` | `-s read-only` / `-s workspace-write` / `--dangerously-bypass-approvals-and-sandbox` | tokens in JSON (cost `admin-api` via EastRouter `usage_api`, else estimated/unavailable) |
| Cursor | `cursor-agent -p --output-format json` | `--mode plan` / `--force` / `--yolo` | none (admin API later) |
| OpenCode | `opencode run --format json` | `--agent plan` / `--dangerously-skip-permissions` (+deny list) | cost+tokens in `step-finish` (native only when cost is positive; an unpriced custom provider stays `unavailable`) |
| Command Code | `command-code -p` (text only) | `--permission-mode plan` / `--permission-mode auto-accept` / `--yolo` | none (hosted account → `unavailable`) |
| Antigravity | `agy -p` (text only) | - / `--dangerously-skip-permissions` / `--dangerously-skip-permissions` | none |
| Claude Code | `claude -p --output-format json` | `--permission-mode plan` / `acceptEdits` / `bypassPermissions` | cost+tokens in JSON (native) |

Antigravity caveats (young CLI): text-only output (no stable JSON), OAuth-first auth, needs a PTY
wrapper in the runner, no headless session capture, no reliable read-only mode → only safe-edit/yolo
exposed. Codex account is usage-limited until ~Jul 18 2026, so its success-path JSON parsing is
verified for the failure path only (live success run pending).

**Live verification (2026-06-19).** OpenCode ✅ fully (read + safe-edit worktree write + native
usage/cost; forced `opencode-go/*` to bill the Go sub, not Fireworks) and Cursor ✅ fully (read +
safe-edit worktree write; usage unavailable by design, env `CURSOR_API_KEY` authenticates). 
**Antigravity ✅ writes fixed (2026-06-27):** headless edits used to divert to
`~/.gemini/antigravity-cli/scratch` (no TTY → no workspace trust); the adapter's `prepare()` now
pre-registers the run's worktree in agy's `trustedWorkspaces` (+ `--add-dir <cwd>`), so edits land in
the worktree - live-verified end-to-end. Still text-only output (no native usage). **Codex ✅
verified end-to-end through EastRouter:** worktree writes land, the JSONL parser extracts text +
tokens, and a `usage_api: eastrouter` client puts its real `admin-api` cost on the ledger; a
token-only Codex client stays `estimated`/`unavailable`. **Claude Code ✅ fully (2026-06-26):**
read/safe-edit (`acceptEdits`) writes land in the worktree, native `total_cost_usd`+tokens flow to
the ledger, and `-p` mode is non-blocking with stdin closed. **Command Code ✅ live-verified headless
(model `zai-org/GLM-5.2`):** `-p` prints plain text with no token/cost accounting, so usage is
`unavailable` (hosted account; spend lives in its own dashboard).
