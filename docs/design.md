# Marshal - Foundational Design

> **Marshal** is the infrastructure layer: one "driver" agent (Claude Code) plans work, then
> Marshal spawns and manages a **fleet of headless coding agents** (Cursor CLI, OpenCode, Codex,
> Command Code, Google Antigravity, Claude Code now), each in an isolated git worktree, in parallel - exposed to the driver as an
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
- **Backends:** one **base class**, one **adapter per backend**. Cursor + OpenCode + Codex + Command Code + Antigravity + Claude Code + Goose now. Adding another = new adapter only.
- **Runtime:** local CLIs (shell out). OpenCode additionally exposes an HTTP server (see §4) - optional fast path.
- **Surface:** MCP server (user-configured, N clients) + Skills (orchestration playbooks). Backend is a **per-client/per-call parameter**, never global, never encoded in tool names. Skills double as the **driver's manual** - they teach the harness (Claude Code or any host) *what* Marshal can do and *how* to drive it (decompose → spawn → monitor → integrate).
- **Differentiator:** **per-provider usage tracking** + a `usage` command. Nearly every competitor omits this.
- **Packaging:** Python package (`uv`), distribute via `uvx`. Private first → public when polished.
- **Naming:** product/repo/CLI/MCP id = `marshal`. The Python **import package must NOT be `marshal`** (it shadows the stdlib `marshal` builtin and won't import) → import package `marshal_engine`, CLI entry point `marshal`. PyPI distribution `marshal` if free, else `marshal-orchestrator`.
- **Two tiers:** Marshal = infra (this repo). Chauffeur = future end-user product built on Marshal. Keep Marshal a clean, embeddable library/engine so Chauffeur (and others) can build on it.
- **Layered package:** `marshal_engine` is organised as layers that import strictly downward -
  `interfaces → orchestration → backends → {runtime, accounting} → core`. `runtime` (processes,
  git, disk) and `accounting` (usage, cost, budgets) are siblings and must not import each other.
  The direction is enforced by `tests/test_import_layers.py`, which walks the AST import graph, so
  lazy and `TYPE_CHECKING` imports cannot dodge it. A handful of top-level modules remain as
  **re-export shims only** (`config`, `service`, `teams`, `state`, `workspaces`, `cli`) because
  published docs, examples, and the installed console script use those paths.

---

## 1. The spine: state must outlive the driver

Claude Code is **stateless across turns** - it forgets the fleet between messages, but background
agents outlive a turn. So fleet state lives in the **long-lived MCP server**, persisted to disk.
On Fleet construction the engine reconciles any persisted `running`/`queued` records left by a
prior supervisor (stamped `failed`, `pid` cleared) unless another live Fleet holds `fleet.lock`,
**the process that will write the run's outcome is still alive** (`supervisor_pid` + start time),
the agent subprocess is still running, or this process still has the run in its in-flight pool
(config hot-reload) — so stale pids can never be signalled by `cancel_run`. Supervisor and agent
are different questions, and a run is abandoned only when both are gone: after the agent exits its
supervisor still has pricing, a usage-API backfill, the `verify:` gate and artifact harvest ahead
of it, and the record reads `running` throughout. Inferring the supervisor from the agent's pid
declared those runs dead — reachable because `fleet.lock` gates *reaping*, not `run`. The
supervisor's identity is rendered under a pinned locale and timezone, since the process that
writes it is not the one that checks it. Records predating those fields fall back to the
agent-pid inference. A record with no pid
stamped yet is too young to judge (it may belong to a run another process started moments ago); it
is left alone and re-examined on the next `status`/`get_run` instead of being decided or forgotten.

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
    name: str            # "cursor" | "opencode" | "codex" | "claude-code"
    binary: str          # "cursor-agent" | "opencode" | "codex" | "claude"

    class Capabilities:          # feature flags → orchestrator degrades gracefully
        json_output: bool
        native_usage: bool       # emits tokens/cost in output
        permission_modes: set[str]   # {"read-only","safe-edit","yolo"}
        permission_fidelity: str     # "enforced-denies" | "boundary-only" | "unrestricted" (resolved)

    # four abstract hooks every backend implements:
    @abstractmethod
    def check_available(self) -> bool: ...           # presence only (which-binary + --version)

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
    def verifies_auth(self) -> bool: ...                         # True → doctor FAILs when account_info is None
    def available_models(self) -> ModelCatalog: ...              # default UNAVAILABLE; {models, source} - source says probed vs static

    # run() lives on the base: build_invocation -> spawn in worktree (timeout!) -> capture -> parse_output
```

**Prompt composition is shared.** `_compose_prompt` lives only on the base; the single
per-backend difference — whether the CLI resolves `@path` mentions into file content — is the
declared flag `resolves_at_mentions` (Cursor and Goose set it). Adapters must not override the
method: two copies means a change to the shared `read_paths` wording lands in one and is silently
forgotten in the other. Enforced by `tests/test_backend_contract.py`, which also asserts every
backend emits the identical read-only notice.

**Overriding `run()`:** an adapter may *wrap* the base loop but must never *replace* it. Cursor
wraps it in a `.cursor/cli.json` snapshot/restore transaction and Antigravity in a
`trustedWorkspaces` grant/release transaction; both are setup/teardown around `super().run()`.
That is the only acceptable shape — the external timeout and the process-**group** kill live in
`base.run()` and a second copy would be a second thing to get wrong. `tests/test_backend_contract.py`
enforces both halves: an override must contain a `super().run(` call and must not spawn a process
itself, and each overriding adapter's real `run()` is driven against a binary that never exits to
prove the timeout still fires, the group is still signalled, and the child is still reaped.

Rules: code against **capability flags**, not assumptions. Persist `session_id` yourself.
Add a **version probe** in `check_available` + **contract tests per backend** (their flags/JSON drift fast).
`check_available` is presence-only; backends with a cheap authenticated probe override `account_info`
+ `verifies_auth` so `marshal doctor` can distinguish installed from logged-in (preflight only —
spawn is not hard-gated on doctor auth FAIL).

**Model discovery (`available_models`):** every concrete adapter returns a `ModelCatalog`
(`{models, source}`) with a non-empty `models`. `source` carries the provenance, exactly as
`UsageSource` does for cost: `probed` when the CLI answered just now (`cursor-agent models`,
`opencode models`, `command-code --list-models`, `agy models`), `static` when the answer is the
curated list from [`model-playbook.md`](model-playbook.md) / the adapter docstring, `unavailable`
when there is nothing to report. The tag is the point - a bare list could not distinguish a live
answer from a fallback emitted by a backend that was not even installed, so a driver could route
at a model the account cannot run. Adapters whose CLI can be asked go through
`CodingAgentBackend._probe_models`, which owns the probe-then-degrade path so no adapter has to
remember to fall back honestly. Probes must never raise or hang (bounded timeout + static
fallback). Shared contract: `tests/test_backend_contract.py` (parametrised over
`registry.backend_names()`).

**Doctor auth probes (fail closed when `verifies_auth`):**

| Backend | Probe | Notes |
|---|---|---|
| Cursor | `cursor-agent status --format json` → `isAuthenticated === true`; enrich via `about` after | Do **not** trust exit code or bare `about`/`model: Auto` (logged-out about still exits 0). Headless argv still omits `--approve-mcps` (parked residual — MCP hang hazard, separate from auth). |
| Goose | `goose info -v --check` | Working reference. |
| Claude Code | `claude auth status` → `loggedIn === true` | Maps `subscriptionType` → plan. |
| Command Code | `command-code status --json` → `authenticated === true` | Config file alone is **not** auth. |
| OpenCode | `opencode auth list` → ≥1 credential or env-auth line | Coarse multi-provider (any usable credential). |
| Codex | `codex login status` (exit 0, not "Not logged in") | Env-key auth only if the CLI status itself reports authenticated. |
| Antigravity | *(none)* | No cheap dedicated auth/status/whoami in `agy --help`; `verifies_auth` stays False (path-only). |
| ZCode | *(none)* | No cheap headless auth probe (`login` is interactive OAuth); `verifies_auth` stays False (launcher-presence only). |
| Copilot CLI | *(none)* | No cheap authenticated status subcommand (`login` is interactive OAuth); `verifies_auth` stays False (path-only). Auth is a GitHub token — `COPILOT_GITHUB_TOKEN` → `GH_TOKEN` → `GITHUB_TOKEN`, or a stored credential. |
---

## 3. Per-backend cheat sheet (all implemented backends)

| | **Cursor (`cursor-agent`)** | **OpenCode (`opencode`)** | **Codex** | **Command Code (`command-code`)** | **Antigravity (`agy`)** | **Claude Code** | **Goose** | **ZCode** | **Copilot CLI (`copilot`)** |
|---|---|---|---|---|---|---|---|---| --- |
| Headless run | `cursor-agent -p "..."` | `opencode run "..."` | `codex ...` | `command-code -p "..."` | `agy -p "..."` | `claude --print` | `goose run -t "..."` | `zcode --prompt "..."` (bundle: `node .../zcode.cjs`) | `copilot -p "..."` |
| JSON | ``--output-format stream-json`` (NDJSON; default adapter path) | `--format json` (NDJSON event stream) | json | ``--output-format json`` (NDJSON event stream; terminal ``result`` line) | ``--output-format json`` (default adapter path; ``stream-json`` also parseable) | `--output-format json\|stream-json` | `--output-format stream-json` | `--json` (single object, not NDJSON) | ``--output-format json`` (JSONL event stream; terminal ``result`` line) |
| Final text | concat assistant ``message.content[].text``; prefer stream over shorter ``result.result`` | concat all `text` events' `part.text` | - | ``result.finalText`` (ANSI-stripped stdout scrape as the pre-JSON fallback) | JSON ``response`` (plain-text fallback on envelope drift) | json field | concat assistant message text | JSON `response` | concat ``assistant.message`` ``data.content`` (streamed ``message_delta`` events ignored so text is not duplicated) |
| Tokens/cost in output | tokens from the `result` event's `usage{inputTokens,outputTokens,cacheReadTokens,cacheWriteTokens}`; **no cost** — source stays `unavailable` (see §6) | `step_finish.cost` + `.tokens.{input,output,reasoning,cache.read,cache.write}` | - | tokens from the ``result`` line's ``usage{inputTokens,outputTokens,cacheReadTokens,cacheWriteTokens}``; **no cost** — hosted account, source stays `unavailable`, `native_usage=False` | tokens from JSON ``usage{input_tokens,output_tokens,cache_read_tokens}``; **no cost** — source stays `unavailable`; `native_usage=False` | `total_cost_usd` + `usage{...}` | stream cost native only when positive | ``outputTokens`` per ``assistant.message``; **no cost and no input tokens** — ``premiumRequests`` counts quota units, not money, so source stays `unavailable`; `native_usage=False` |
| File changes | `writeToolCall.result` events / diff worktree | inside `edit`/`write` tool outputs; or `GET /session/:id/diff` | - | diff worktree via git (`collect_run`); CLI emits none | diff worktree via git (`collect_run`); CLI emits none | - | diff worktree via git | diff worktree via git (`collect_run`); CLI emits none | diff worktree via git (`collect_run`); ``result.usage.codeChanges.filesModified`` is an in-stream signal only |
| Session resume | `--resume <id>` / `--continue` (persist `session_id` from JSON) | `-s <id>` / `-c` / `--fork` | - | `--resume`/`--continue` exist in the CLI; ``result.sessionId`` is now captured, resume not yet wired | `--conversation <id>`; JSON ``conversation_id`` → ``session_id`` (resume not advertised) | `session_id` returned | `--no-session` (Marshal one-shot) | `--resume <sess_...>` (validates the id exists) | ``--session-id <id>`` — ``--resume``'s value is optional, so ``--resume <id>`` parses the id as the prompt |
| Model select | `--model` / `cursor-agent models` | `-m provider/model` / `opencode models` | `-m MODEL` (no headless list) | `-m MODEL` / `--list-models` | `-m MODEL` / `agy models` | `--model` (no headless list) | `--provider` + `--model` (`provider/model`) | **no flag** — `ZCODE_MODEL` env (`model` or `provider/model`) via `prepare()` | ``--model`` — **plan-gated**: a Free account rejects every pinned id and takes only ``auto`` |
| `available_models` | probe `models` → static `composer-2.5` | probe `models` → static `opencode-go/*` playbook rows | static `gpt-5.6-luna` | probe `--list-models` → static `zai-org/glm-5.2` | probe `models` → static playbook/docstring ids | static playbook Claude ids | static `cursor-agent/auto` | static `glm-5.3` / `glm-5.2` / `glm-5-turbo` | probe ``help config`` model section → static `auto` + playbook ids |
| Doctor auth | `status` → `isAuthenticated` | `auth list` | `login status` | `status --json` | **none** (`verifies_auth=False`) | `auth status` | `info -v --check` | **none** (`verifies_auth=False`) | **none** (`verifies_auth=False`) |
| Working dir | **no `--cwd`**; `--workspace <path>`; `-w/--worktree [name]`, `--worktree-base` | `--dir <path>` (config walks up to git root) | `-C <path>` | none (uses the process `cwd` the runner sets; `-t` trusts the project) | `--add-dir <path>` + run-scoped `trustedWorkspaces` entry (`prepare()`/`run()`) | process `cwd` | process `cwd` | `--cwd <path>` (also where project config is discovered) | ``-C <path>`` |
| Server mode | no | **`opencode serve`** (OpenAPI on 127.0.0.1:4096) + `opencode acp` | no | no | no | no | no | `app-server` (ZCode Protocol stdio; not used) | no (`--acp` starts an ACP server; not used) |

> **File changes, every backend:** Marshal derives file changes the same way regardless of CLI -
> after the run it diffs the worktree via git (`collect_run`). None of the CLIs emits a structured
> file list, so the per-backend "File changes" cells name only an optional in-stream signal (when one
> exists); the authoritative diff is always the worktree.

---

## 4. OpenCode server mode (a real advantage)

`opencode serve` → headless HTTP server, **OpenAPI 3.1 at `/doc`**, default `127.0.0.1:4096`.
Auth via `OPENCODE_SERVER_PASSWORD`. Key endpoints: `POST /session`, `POST /session/:id/message`
(blocking) or `POST /session/:id/prompt_async`, `GET /session/:id/diff` (authoritative diff),
`GET /event` (SSE). SDK: `@opencode-ai/sdk`.

A future warm-`serve` path could keep a long-lived process and attach for lower latency, with
subprocess `opencode run` as today's fallback (cmuxlayer-style fast/slow path). Not modeled as a
Capabilities flag until a consumer exists.

---

## 5. Normalized permission model (3 tiers → native flags)

The single most reusable artifact (from shinpr/sub-agents-mcp). Headless = **no interactive
approvals, ever** - "sub-agents have no stdin, so any approval prompt deadlocks the run."

| Tier | Cursor | OpenCode | Codex | Command Code | Antigravity | Claude Code | Goose | ZCode | **Copilot CLI** |
|---|---|---|---|---|---|---|---|---| --- |
| **read-only** | `--mode plan` (or no `--force` + allowlist) | agent `plan` / `permission` read+deny edit/bash | `-s read-only` | `--permission-mode plan` | - (unsupported headless) | `--permission-mode plan` | `GOOSE_MODE=chat` (via `prepare`) | `--mode plan` | `--mode plan` (+ `--allow-all-tools`; plan mode is the enforcement) |
| **safe-edit** (default) | `--force` + engine-managed deny list in `.cursor/cli.json` | `--dangerously-skip-permissions` + `OPENCODE_CONFIG_CONTENT` (`question: deny` + curated denies) | `-s workspace-write` | `--yolo` | `--dangerously-skip-permissions` (+ `trustedWorkspaces` via `prepare`) | `--permission-mode acceptEdits` | `GOOSE_MODE=auto` (via `prepare`; no argv flags) | `--mode edit` | `--allow-all-tools` + curated `--deny-tool` overlay (deny beats allow) |
| **yolo** (opt-in) | `--yolo` (no deny list) | `--dangerously-skip-permissions` + `question: deny` only | workspace-write, no approval | `--yolo` | `--dangerously-skip-permissions` | bypass | `GOOSE_MODE=auto` (same as safe-edit) | `--mode yolo` | `--allow-all` (overlay dropped by design) |
| **permission_fidelity** (safe-edit capability) | `enforced-denies` | `enforced-denies` | `enforced-denies` | `boundary-only` | `boundary-only` | `boundary-only` | `boundary-only` | `boundary-only` | `enforced-denies` |

`Capabilities.permission_fidelity` is the backend's **safe-edit** honesty (`marshal backends`,
doctor `permission:<backend>`). `list_clients` resolves fidelity from the
`(backend, permission)` pair via `resolve_permission_fidelity` — not a sandbox ranking:

- **`enforced-denies`**: this tier installs a backend or Marshal restriction beyond the worktree
  (Cursor/OpenCode/Copilot curated denies; Codex `--sandbox workspace-write`; plan/read-only modes
  on enforcing backends). Still not a true sandbox.
- **`boundary-only`**: Marshal cannot promise a deny layer; the worktree and explicit integrate
  remain the dependable boundary (Command Code, Goose, Antigravity, Claude Code, ZCode).
- **`unrestricted`**: client `permission: yolo` — deny/sandbox overlay dropped by design
  (OpenCode still denies `question` only so headless cannot deadlock).

Key per-backend detail:
- **Cursor / OpenCode permission config layer (v0, issues #17 / #40):** `safe-edit` is no longer a bare auto-approve flag for these two backends. Cursor `prepare()` merges a curated deny list into the worktree's `.cursor/cli.json` (`Shell(rm)`, `Write(**/.env*)`, `Write(**/.git/**)`, `Write(.cursor/cli.json)`, `Write(**/.cursor/cli.json)`, `Read(**/.env*)`) alongside `--force`. Reads of the policy file stay allowed. That write is a **transaction owned by `CursorBackend.run()`** (#37): the file's exact prior state (existence, bytes, mode) is snapshotted before the shared run loop and restored in a finally path before Fleet observes the worktree - so a no-op run stays EMPTY, verify isn't triggered by the overlay, and `commit_run`/`integrate` never land Marshal's transient policy. An existing malformed/unreadable/non-object/symlink/non-regular `cli.json` (or a symlinked `.cursor/`) fails the run closed (file preserved byte-for-byte, process never spawned); restore re-validates the same path constraints before unlink/replace so a mid-run `.cursor/`→symlink swap cannot escape the worktree, and a restoration failure fails the run. The Write deny protects the policy file through Cursor's permission grammar only — same-user shell/Python can still rewrite it mid-run; exact restore limits persistence, not mid-run bypass. Neither Cursor nor OpenCode denies are a sandbox. OpenCode `prepare()` stamps `OPENCODE_CONFIG_CONTENT` with `question: deny` plus curated `bash`/`edit`/`read`/`external_directory` denies for `safe-edit` (bash: `rm`, `git config`, redirection/`tee`/`sed` into `.env`/`.git` after `"*": "allow"` — simple `*` wildcards only; wrappers and alternate writers can bypass). Yolo still gets `question: deny` only so headless cannot deadlock on the `question` tool, which skip-permissions does not cover. Never emit `ask`.
- **Still process-equivalent / deferred:** Command Code `safe-edit`/`yolo` both map to `--yolo` (no per-tool deny grammar). Goose `safe-edit`/`yolo` both set `GOOSE_MODE=auto` via `prepare()` (argv has no permission flags; read-only is `GOOSE_MODE=chat`). Antigravity `safe-edit`/`yolo` are identical (`--dangerously-skip-permissions`); `prepare()` briefly adds the run worktree to host-global `trustedWorkspaces` and `run()` removes it on completion (malformed settings fail closed; parallel runs serialize on the shared file); it still lacks a PTY wrapper (stdout can be swallowed without a TTY) and has no distinct safe-edit scoping beyond that trust grant. Claude Code uses native `acceptEdits` for safe-edit with **no Marshal deny layer**. ZCode's `edit` mode is genuinely narrower than `yolo` (automatic file edits, but not arbitrary command execution), so its tiers are not process-equivalent — but Marshal installs no deny layer on top, so it stays `boundary-only`. Its `--disallowed-tools` flag works and could carry curated denies later; `--allowed-tools` is advertised but rejected by the 0.16.3 parser, so an allowlist is not available. Worktree isolation remains the dominant safety primitive; containers later for untrusted code. See `docs/usage.md` backend notes for the live matrix.
- **Copilot permission grammar:** `--deny-tool` / `--allow-tool` take `kind(argument)` patterns —
  `shell(cmd)` (first-level subcommand, `:*` for prefixes: `shell(git push)`, `shell(gh:*)`),
  `write(path)` (matches by **trailing path component**, so `write(.env)` hits any directory but
  does **not** glob suffixes — `.env.local` needs its own rule), `url(domain)`, and
  `<mcp-server>(tool)`. **Deny beats allow, including `--allow-all-tools`** (documented, and
  live-verified). Separately, `--available-tools`/`--excluded-tools` filter what the model can
  *see*, which is a different layer from approval. `--mode plan` enforces read-only independently
  of all of it.
- **Cursor permission grammar:** `--force`/`--yolo` = "allow everything **not explicitly denied**". Tokens live in `~/.cursor/cli-config.json` / `.cursor/cli.json`: `Shell(git)`, `Read(glob)`, `Write(src/**)`, `WebFetch(*.github.com)`, `Mcp(server:tool)`. **Deny beats allow.** Redirections (`>`,`|`) can't be allowlisted inline. Also needs `--trust` (headless workspace trust) and `--approve-mcps` for MCP.
- **OpenCode permission grammar:** `permission` keys: `read, edit, glob, grep, bash, task, skill, lsp, question, webfetch, websearch, external_directory, doom_loop`; values `allow|ask|deny`; **last matching rule wins**. **CRITICAL for server mode:** `serve`+`attach` **hangs if any permission is `ask`** → all `allow` + `question: deny`. `--dangerously-skip-permissions` does NOT cover the `question` tool.
- **Worktree isolation is the dominant safety primitive** across all serious tools (ORCH, Crystal, Orca). Main branch untouched until explicit merge. Worktrees share host FS/network → fine for trusted local use; for untrusted code use containers later (agentbox/scion).

---

## 6. Usage tracking (the differentiator) - and the Cursor asymmetry

**Major finding: backends are NOT symmetric on usage.**

- **OpenCode - easy.** Per-step `cost`+`tokens` in the stream; `opencode stats --days --models`; on-disk store at `~/.local/share/opencode/storage/` (note: message files store `cost: 0` → recompute from tokens via price table). Caveat: stream may **drop the final `step_finish`** → read final accounting from on-disk store / `opencode export`, not the stream.
- **Cursor - hard.** **No tokens, no cost in CLI output at all.** Programmatic usage only via the **Admin API** (`api.cursor.com`, HTTP Basic `-u KEY:`) - **Team/Enterprise only**. `POST /teams/filtered-usage-events` returns per-event tokens+cost with an **`isHeadless`** flag and **`serviceAccountId`**. Pattern: give each worker its own **service-account key**, attribute via `serviceAccountId`. Pro/individual accounts → dashboard only, no API.
- **Codex - likely no JSON cost** → cost `admin-api` or `unavailable`; tokens from JSONL events. **Gemini** reports token counts in `--output-format json` `stats` (per-model `tokens.prompt`/`candidates`/`cached`) but not USD → tokens kept, cost `unavailable` (never a fake $0).
- **EastRouter (real `admin-api` cost) - implemented.** A client may set `usage_api: eastrouter` to have its REAL per-run cost read from EastRouter's `/v1/usage` after the run (`eastrouter.py`), reported as `admin-api`. Attribution is by model + the run's `[start, end]` time window with a token-reconciliation guard; an unattributable run (e.g. two clients on the same EastRouter model concurrently) keeps `unavailable` instead of asserting a wrong one. Codex routed through EastRouter uses this; OpenCode pointed at EastRouter (`eastrouter/<id>`) can't be priced by the CLI and stays `unavailable`.

**Local schema (no DB; file-based like ORCH's `.orchestry/`):**

`usage/events.jsonl` (append-only, one line per run):
```json
{"ts":"...","run_id":"...","client":"reviewer","backend":"cursor","model":"...",
 "input_tokens":1234,"output_tokens":567,"cache_read_tokens":0,"cache_write_tokens":0,
 "cost_usd":0.041,"duration_ms":8200,"status":"exited_clean",
 "source":"native|admin-api|unavailable",
 "task_kind":"refactor","goal_digest":"a1b2c3d4e5f67890"}
```
`task_kind` / `goal_digest` are optional routing facts (additive; pre-field lines omit them and
still parse). `task_kind` is a caller-supplied free-text tag (safe token; not a closed enum).
`goal_digest` is a truncated sha256 of the goal text — **never the goal itself** (the ledger is
long-lived and user-readable). Judgment about the work is deliberately *not* on the usage line: it
arrives after the line is written, so successful `integrate` stamps `RunRecord.outcome` instead of
rewriting the usage line or appending a second cost event — the ledger stays immutable and
one-line-per-run for cost rollups. A clean exit is not an outcome, and rejection stays
explicit-only (never inferred from absence).
`usage/summary.json` (cumulative rollup, updated each run): `by_client`, `by_backend`, `by_model`, `totals`, plus a compound `by_backend_model` keyed `<backend>/<model>` for when one backend runs multiple models.

**Per-run raw logs** (`logs/<run_id>.log`): one file per terminal run (success or failure) with the
agent's full raw stdout + stderr under a `=== run <id> ===` / `--- stdout ---` / `--- stderr ---`
header. The run record's `text` field is the agent's *final message* truncated to 16 KB - useful
for a reply/analysis task but rarely enough to debug a failure. The log file preserves the *whole*
stream so a driver can `get_run_log` (MCP) or `marshal logs <run_id>` (CLI) and inspect tool calls,
tracebacks, and stderr noise after the fact. Writes are atomic (unique temp + `os.replace`, same
idiom as `FleetState`); a write failure is swallowed in `Fleet._execute` and stderr-logged, so
the log store is best-effort and never breaks a finished run.

**Tag every record `source`** so unknown costs are never presented as ground truth.
Surface a `usage` MCP tool / `<name> usage` CLI that prints all breakdowns (backend/client/model + compound backend/model) with token columns, time-windowed via the shared `session|day|week|month|all` vocabulary (`usage_window_since` in `usage.py`). MCP `session` maps to the Fleet's `session_start` (stamped at process start); CLI `session` is since that invocation (no long-lived Fleet). A driver can ask "what have I spent since the MCP server woke up?" without restating the timestamp.

**Budgets (soft-warn default; optional hard refuse).** An optional top-level `budgets:` list in
`fleet.config.yaml` declares $ caps per scope (a `backend:`, a `client:`, or the whole fleet when
neither is set) per time window (`session` / `week` / `month`). `Fleet._start` is the FIRST
statement of the run path, so the check runs before the worktree is provisioned. Default
`enforce: false`: a scope whose windowed spend meets/exceeds its cap prints a stderr warning like
`[marshal] budget: client:implementer spent $5.40 >= cap $5.00 (week)` and the run proceeds;
advisory lookup failures (corrupt ledger, IO error) degrade silently to "no warning". With
`enforce: true`: raise `BudgetExceeded` before worktree create; enforced lookup failures fail
closed; `EnforceBudgetGate` admits at most one in-flight matching spawn per enforce budget,
serialized across processes via `.marshal/budget_gate.json` + `fcntl.flock` (see `budgets.py`;
knob census in [`config.md`](config.md)).
**Per-workspace only (intentional non-goal).** Budgets and usage ledgers are scoped to one repo's
`.marshal` + that workspace's `fleet.config.yaml`. A multi-workspace MCP server does **not** merge
ledgers or evaluate a cap across workspaces. The shared control plane is concurrency only
(`run_many` pool + optional process-wide `run_gate`). Registry-level aggregate spend /
cross-workspace enforce is out of scope for Marshal; org-wide policy belongs to a future product
layer (Chauffeur) if needed.
**Honesty / cost coverage.** A budget's "spend" comes from the ledger's `cost_usd`, which is real
only for meterable backends (`native` / `admin-api`, plus legacy ledger lines tagged `estimated`);
backends that record `$0` or `unavailable` never trip a dollar cap — see the live matrix in
[`usage.md`](usage.md) (Backend notes) rather than treating `enforce: true` as a universal
kill-switch. We do NOT fabricate a percentage or "remaining" from a missing cost. The MCP `usage`
tool (and `marshal usage --config fleet.config.yaml --json`) returns a `budgets` list with
`scope / window / spent_usd / limit_usd / remaining_usd / enforce / spent_known` (remaining
floored at 0) per budget, so the driver can see remaining alongside spend. When
`spent_known` is false, spend is unknown — do not treat the dollar fields as measured.

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

**Optional model catalog (the driver's "sheet").** A top-level `models:` list is pure data the
driver can read (`list_models` MCP tool / `marshal models` CLI) — `id` (provider/model), which
`backends` can run it, and short free-form strings for `cost` (e.g. `native`/`admin-api`/
`unavailable`), `quota_type` (e.g. `metered`/`subscription`/`unavailable`), and
`notes`. **The catalog is metadata only — it does NOT change routing** (clients still own
backend+model). Absent or empty = no catalog to expose; a malformed entry raises `ConfigError`
at load (the same hard-fail behavior as the other config errors).

**Per-spawn timeout override (duration presets).** `run_agent`, `spawn`, and `run_many` accept
an optional `duration` — either a preset name (`short`=300s, `medium`=1200s, `large`=6000s,
`long`=24000s) or a positive integer of seconds. The override replaces the resolved `timeout_s`
on the `RunRequest` for that one call; the client's `timeout_s` in `fleet.config.yaml` stays the
default. Same idea for the CLI: `marshal run --duration large ...`. Validation happens up front
in `_request_for` (via `resolve_duration`), so a typo or non-positive value fails fast before
any worktree is created.

Runtime state - worktrees, per-run JSON, usage, **per-run raw logs**, and **team review reports**
(`.marshal/reports/<stamp>-<team>-<id>/`, one markdown file per reviewer plus the unified
`README.md`) - lands under `.marshal/`, alongside `fleet.lock`: a small file naming the process
that currently supervises this repo's runs. Only its holder reconciles run state at startup, and it
is never released - a long-lived server keeps it, while a short-lived CLI leaves a dead pid that the
next process takes over. The holder is identified by pid **and** start time, the same pairing run
records use: a bare pid the OS later recycled would otherwise impersonate a live supervisor forever
and permanently suppress reaping. Start times are probed under a pinned locale and timezone and
carry a marker saying so - `ps -o lstart=` renders through `TZ`/`LC_TIME`, so unpinned, a launchd-
spawned server and a terminal CLI read one live pid as two identities, and "different" is the
reading that authorises reaping a live run. A value without that marker predates the pinning and
is treated as unverifiable, never as a mismatch. Auth is per-CLI login;
an optional `secret_ref: env:VAR` is an advisory preflight check only (not injected).

**Per-run record locking (cross-process).** Each run is `runs/<run_id>.json`. Most runs have a
single writer (the thread executing them), but `cancel_run` can write the same record from another
thread, and a CLI + long-lived MCP server can race the same record across processes. Per-run
writes are serialized by a per-run `threading.Lock` (in-process) plus an `fcntl.flock` on a
sidecar `runs/<run_id>.json.lock` (cross-process). The flock is held only for the
read-modify-write critical section in `update`/`update_if` — never across a backend run. Callers
take the thread lock before the flock (consistent ordering). The flock auto-releases on process
death, so no stale-lock reaper is needed. `list` and `clean` glob `*.json`, so `.json.lock`
sidecars are never mistaken for records. Each write goes through a unique temp file then an
atomic `os.replace`.

**NFS / no-flock filesystems (document, don't fix).** `fcntl.flock` on NFS or filesystems without
flock support fails loudly inside `update`/`update_if` — fail-closed; no soft fallback. All
documented state layouts assume local disk.

**Worktree environment isolation.** The driver usually runs inside its own activated venv, so
`os.environ` carries `VIRTUAL_ENV`/`PYTHONHOME` pointing at the *driver's* interpreter. The
driver/MCP process also sets `MARSHAL_*` session variables (`MARSHAL_CONFIG`, `MARSHAL_REPO`, …).
Every spawned child (agents and the worktree-setup command) is built from an **allowlist**
(`env.child_env`): operational base vars, plus (for agents) that backend's
`credential_env_vars` only. `VIRTUAL_ENV` / `PYTHONHOME` / `MARSHAL_*` and unrelated secrets are
dropped so the worktree's own `.venv` wins, a worker's tests/`marshal` CLI resolve the worktree,
and ambient credentials do not leak into agent processes or run logs. A fresh worktree has no `.venv` (it's gitignored), so the optional
top-level `worktree_setup` command (e.g. `uv sync --extra dev --extra mcp`) provisions one right
after the run's clone is created; a non-zero exit tears the run directory down and fails the run early.
Non-allowlisted setup/verify basenames and relative path argv[0] (without
`allow_unsafe_commands`) are refused at config load / `WorktreeManager` construction — never
create-then-teardown — with the same check kept as a runtime backstop in `setup()` / `verify()`.
The allowlist screens basename only (not args); see `SECURITY.md` / `docs/config.md`.

**Verify gate.** The optional top-level `verify` command (e.g. `uv run pytest -q`) is
`worktree_setup`'s post-run counterpart: it runs in the worktree after a run that would otherwise
be `exited_clean` *and changed files* (agents love passing their own narrower test subset while
breaking the repo gate). A non-zero exit/timeout demotes the run to `verify_failed` - the worktree
and diff are KEPT for review (unlike a setup failure, verify never tears down), and the command's
output tail lands on the run record (`verify_passed` / `verify_output`). Runs with no gate
configured, no file changes, or a non-success outcome are untouched (`verify_passed=None`).
Trust model: verify is **post-agent** host execution of worktree content under the operator
identity — the basename allowlist is **not** a sandbox, so allowlisted tools still load
agent-editable project files (tests, Makefiles, npm scripts, etc.). Acceptable when you trust
the config and treat the agent's tree as code you might run yourself; still review
`collect_run` / CI before integrate. Contrast `worktree_setup`, which runs **pre-agent** on the
base checkout. See `SECURITY.md`.

**Integrate hooks.** `integrate_run_hooks` defaults to `false` (`git --no-verify` on
`commit_run` / `integrate`) so prompting hooks cannot deadlock headless merges and so Marshal
does not run possibly agent-touched hook scripts. When `true`, hooks may execute
repo-/worktree-controlled scripts the agent could have changed — opt in only for known
non-interactive hooks with trusted provenance. Depth lives in `SECURITY.md` / `docs/config.md`.

**Graceful backend skip.** `MarshalService.__init__` probes each configured backend's CLI at
startup; a client whose backend is unavailable is **skipped** (stderr warning, recorded on
`skipped_clients`) rather than failing a run mid-flight. The **full** backend set still goes to the
Fleet, so `doctor` (which probes every configured backend) still reports a missing one as a FAIL.

**Lean tool surface** (backend is a param, NOT in tool names - avoids the 2N-tool explosion).
The normative tool reference - every tool, its parameters, and its return shape - is
[`mcp-tools.md`](mcp-tools.md); current implementation state is tracked in `docs/status.md`.

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
runs as candidates with `next_actions`, and the driver merges the good ones after review - `exited_clean`
is not `correct`. The judgment (which recipe, when to merge) stays in the `marshal-workflow` Skill;
the engine only sequences. Discover/validate with `marshal workflows`; run via `run_workflow`.

### Adversarial review teams (a panel is a fan-out that produces reports, not a decision)

A **team** (`teams.py`) is a declarative panel of independent reviewer **roles**, each pinned to the
client best suited to its lens, that review one subject - a run's diff, a commit `range`, a `plan`,
or an `audit` of the repo - and produce **one report per reviewer plus a unified report** the
requesting agent reads first. It shares the workflow safety property: **the runner adds no new
execution path**, issuing only `collect_run` / `diff_range` / `run_many`, so every reviewer still
flows through `Fleet.run`.

**The engine does not judge.** It parses no verdicts, tallies no votes, and computes no pass/fail.
That is both a layering rule (judgment belongs to the driver, per "engine is mechanism") and a
security property: a decision derived from reviewer prose can be forged by the material under
review - a diff carrying a verdict-shaped line, or a reviewer echoing the contract back, was enough
to invert a rejection. Removing the derived decision removes the whole attack class. Reviewers are
asked for a structured report (Bottom line / Findings / Blocking / Confidence) that a human or a
driver agent reads; nothing in it is machine-interpreted.

What the engine does guarantee:

- **Fail-closed read-only.** `validate_team` rejects a role whose client is not configured
  `permission: read-only`, before any spawn. Precisely: Marshal will not *route* a role to a
  writable client, and Codex's `--sandbox read-only` is OS-enforced, but where `read-only` maps to a
  cooperative `plan` mode it is a strong hint, not a jail (see `PermissionFidelity`). The dependable
  boundary is the worktree plus explicit integrate.
- **Independent.** All roles go out in one `run_many` call under a shared `task_id`, so they cannot
  observe each other and the panel prices as one unit. There is no synthesis agent.
- **A shrunken panel is visible.** A role that failed, timed out, or whose backend was missing is
  reported in `incomplete_roles` with its report absent - never dropped, because a panel that
  quietly lost a lens must not read as consensus.
- **Reviewed material is data.** The subject is delimited by a per-run nonce (a markdown fence it
  could close would let content escape into the strongest prompt position, after the contract) and
  labelled untrusted; refs reaching `diff_range` are validated, since a `base` of `--output=<path>`
  would otherwise turn a read-only diff into an arbitrary file write while emptying stdout.

Reports persist to `.marshal/reports/<stamp>-<team>-<id>/` - `<role>.md` per reviewer plus
`README.md` (the unified one). Judgment - which panel, how to write a lens, what to do with the
objections - lives in the `marshal-adversarial-review` Skill. Discover/validate with
`marshal teams`; run via `run_team`.

---

## 8. Edge-case hardening checklist (MUST defend - from real GitHub/forum issues)

1. **External timeout + kill on EVERY run.** Both Cursor (`-p` hang, version-gated) and OpenCode (hangs on API error/429 with no exit code; hangs after tool calls) hang. Treat absence of stdout as a hang.
2. **Never inherit the host's stdin or controlling terminal.** As a stdio MCP server Marshal is a child of its host, so its stdin is the JSON-RPC pipe and its process group is the host's. A child inheriting the first eats protocol bytes; a child inheriting the second can raise SIGTTIN/SIGTTOU, which stops the **whole group** — the host is suspended (`ps` STAT `T`) with no crash and no stderr. Every spawn site splats `runtime.env.DETACHED_STDIO` (`stdin=DEVNULL` + `start_new_session=True`), enforced over the AST by `tests/test_invariants.py`; the server itself `setsid`s at startup unless stdin is a tty. Highest-risk child is the PATH probe — an *interactive* login shell running arbitrary user rc files.
3. **No-stdin deadlock is the #1 footgun.** Never default to a prompting permission mode. Default `safe-edit` (non-prompting). OpenCode: set `question: deny`.
4. **OpenCode stream drops final `step_finish`** → read final cost/tokens from on-disk store / `export`, not the stream.
5. **OpenCode `serve`+`attach` hangs if any permission is `ask`** → all `allow` + `question: deny` (engine stamps via `OPENCODE_CONFIG_CONTENT` on write-tier runs).
6. **OpenCode rate-limit = immediate exit, no auto-retry** → implement orchestrator backoff/retry.
7. **Cursor: pin & assert version** at startup (hang/race/terminal-release fixes are version-gated). Parse stdout JSON **only on exit 0**; on failure there's no JSON, only stderr.
8. **Cursor wants a TTY** → run under pseudo-tty (`script -q /dev/null`) or `--print`, stdin from `/dev/null`, **clean shell** (a heavy `.zshrc` causes completion-detection hangs).
9. **Cursor concurrent launches:** stagger ~100ms + use worktrees (file-lock race, fixed but stagger anyway).
10. **Cursor workspace trust:** `--trust` / pre-seed trusted config - esp. required for MCP in headless.
11. **Worktree lifecycle:** spec creation, naming, owner-tracking, orphan detection, `git worktree prune` on crash. Track which run owns which worktree in the usage log. **Id validation is Marshal-owned** (charset + length + `is_relative_to(base_dir)` containment on create/remove/discard) — not git-accidental; see `SECURITY.md`.
12. **Concurrency caps:** each CLI is 150-400 MB RAM → cap parallel runs per fleet and per client or a fan-out OOMs the host.
13. **Secrets by reference** (`env:VAR`/file), validate presence at load, fail fast with a clear message. Never inline.
14. **Durable per-run logs are best-effort.** `RunLogStore.write` is atomic (unique temp + `os.replace`, same idiom as `FleetState`), so a torn read never sees partial content; but a *write failure* (disk full, permission) must never break a finished run — `Fleet._execute` wraps the write in `try/except` and stderr-logs the cause. A run that predates log storage simply has no file (the CLI returns non-zero, the MCP tool returns `log=null`).

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
- **Phase 3 - MCP server:** the MCP tool surface ([`mcp-tools.md`](mcp-tools.md); incl. `list_workspaces`/`add_workspace`, `commit_run` for dependent chaining, `clean` for worktree teardown; each action/query tool takes an optional `workspace`) + `fleet.config.yaml` loader + persistent fleet state + localhost hardening. Multi-workspace tenancy lives in `workspaces.py` (one server, several repos via `~/.marshal/workspaces.yaml`, hot-reloaded); the engine stays single-repo. `run_many` may mix per-job `workspace` keys under one concurrency cap; ledgers stay per-workspace.
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

1. **Routing by ROLE, not provider.** Drivers / Skills pick a client (planner/coder/writer/
   reviewer/…). Config + Skills own the role→client map. The engine stays *mechanism*; the routing
   decision is *policy*. `TaskSpec` carries `context_files` (and related work hints), not a role
   field — role routing lives above the engine.
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

---

## 12. Marshal ↔ Chauffeur freeze line

**Rule:** the engine is **mechanism** (spawn, monitor, collect, integrate, usage); **judgment**
(decompose, route, prompt, review, merge) lives in **Skills** today and **Chauffeur** later.
Full rationale, module inventory, decision rule, and what Chauffeur replaces:
[`docs/chauffeur-future.md`](chauffeur-future.md#the-freeze-line-mechanism-vs-judgment).

**Admission test for borderline engine features:** does the code add a **new execution path** (spawn,
git write, ledger write the primitives did not already have)? Does it **decide on the user's behalf**
(route, verdict, auto-merge, dynamic recall)? Features that pass both negatives — they only
**sequence existing primitives** and leave judgment to the caller — may live in the engine (see
`workflow.py`, `teams.py`).

### Inventory (borderline + ruling)

| Feature | Location | Ruling | Rationale |
|---|---|---|---|
| Declarative YAML workflows | `workflow.py` | **Mechanism (grandfathered)** | Phases map to `run_many` / `run_agent` / `collect_run` / `integrate`; runner adds no spawn/git/ledger path; `auto: false` default keeps merge judgment with the driver. |
| Adversarial review teams | `teams.py` | **Mechanism (grandfathered)** | One `run_many` over read-only roles; returns reports only — **no verdict parsing** (forgery-safe); driver decides. |
| Static worker context prefix | `service.py` `_compose_goal` | **Mechanism** | Prepends fixed preamble + config `context.worker`; not dynamic recall or routing. |
| Registry cross-workspace `run_many` | `workspaces.py` | **Mechanism (MCP layer)** | Shared thread pool + concurrency cap only; per-workspace config, worktrees, and ledgers stay isolated. |
| Memory / recall injection | *(extracted)* | **Judgment (out of core)** | Context routing by recall belongs above the engine; preserved on `feature/marshal-recall-cognee`. |
| Task decomposition | Skills (`marshal-orchestrate`) | **Judgment** | Driver plans subtasks; engine runs what it is told. |
| Model / backend routing | Driver + config | **Judgment** | No engine role→client router; caller names `client` / `backend`. |
| Review verdict / merge gate | Skills (`marshal-review-gate`) | **Judgment** | Truth table over parsed `REVIEW:` lines; engine never integrates on prose. |
| Org-wide budgets / spend | — | **Chauffeur (future)** | Per-workspace ledgers by design (§6); no registry merge. |
| Self-driving workflows | — | **Chauffeur (future)** | Engine executes human-authored YAML; generation is product policy. |

### Backends in scope (built)

Adapters derive from `CodingAgentBackend`, each with pure `build_invocation`/`map_permission`
and contract tests:

| Backend | Headless invocation | read-only / safe-edit / yolo | Usage in output |
|---|---|---|---|
| Codex | `codex exec --json` | `-s read-only` / `-s workspace-write` / `--dangerously-bypass-approvals-and-sandbox` | tokens in JSON (cost `admin-api` via EastRouter `usage_api`, else `unavailable`) |
| Cursor | `cursor-agent -p --output-format stream-json` | `--mode plan` / `--force` / `--yolo` | tokens in CLI JSON (incl. cache read/write); cost Admin API later (`native_usage=False`) |
| OpenCode | `opencode run --format json` | `--agent plan` / `--dangerously-skip-permissions` (+deny list) | cost+tokens in `step-finish` (native only when cost is positive; an unpriced custom provider stays `unavailable`) |
| Command Code | `command-code -p --output-format json` | `--permission-mode plan` / `--yolo` / `--yolo` | tokens in the terminal ``result`` line; cost `unavailable` (hosted account, `native_usage=False`) |
| Antigravity | `agy -p --output-format json` | - / `--dangerously-skip-permissions` / `--dangerously-skip-permissions` | tokens in JSON ``usage``; cost `unavailable` (`native_usage=False`) |
| Claude Code | `claude -p --output-format json` | `--permission-mode plan` / `acceptEdits` / `bypassPermissions` | cost+tokens in JSON (native) |
| Goose | `goose run --output-format stream-json` | `GOOSE_MODE=chat` / `GOOSE_MODE=auto` / `GOOSE_MODE=auto` | stream-json cost native **only when positive**; `cost: 0` / tokens-only → `unavailable` (estimate path may apply; OpenCode parity) |

Antigravity caveats (young CLI): structured ``--output-format json`` works (agy ≥ 1.1.8; tokens
parsed, no USD → `unavailable` / `native_usage=False`), OAuth-first auth with **no
cheap dedicated auth/status probe** (doctor is path-only; `verifies_auth=False`), needs a PTY
wrapper in the runner, ``conversation_id`` stamped from JSON (resume not advertised), no reliable
read-only mode → only safe-edit/yolo exposed. Codex account is
usage-limited until ~Jul 18 2026, so its success-path JSON parsing is verified for the failure
path only (live success run pending).

**Antigravity trust / single-process-per-host.** `prepare()` registers the run worktree in
agy's host-global `trustedWorkspaces`; the settings JSON transaction is **file-locked** across
processes (sidecar flock; atomic temp+replace write). The in-flight **refcount** that decides when
to revoke a Marshal-introduced entry is **process-local** (`_trust_added`). Assumption: **one
Marshal process per host** when using this backend (MCP *or* CLI, not both). Rationale: per-run
worktree paths are globally unique, so a same-cwd collision across processes is a narrow edge; a
cross-process claim ledger was rejected because stale claims would leak trust grants forever and
widen agy's write scope. Within one process, overlapping runs on the same cwd keep the grant until
the last claimant releases. Documented for operators in [`usage.md`](usage.md).

**Live verification (2026-06-19).** OpenCode ✅ fully (read + safe-edit worktree write + native
usage/cost; defaults to `opencode-go/*` so runs bill the Go sub, with `fireworks-ai/*` an explicit opt-in that reports native USD) and Cursor ✅ fully (read +
safe-edit worktree write; tokens in the CLI ``result.usage`` envelope, cost Admin API /
``unavailable``, env `CURSOR_API_KEY` authenticates). 
**Antigravity ✅ writes fixed (2026-06-27):** headless edits used to divert to
`~/.gemini/antigravity-cli/scratch` (no TTY → no workspace trust); the adapter's `prepare()` now
briefly registers the run's worktree in agy's host-global `trustedWorkspaces` (+ `--add-dir <cwd>`),
removed when `run()` completes, so edits land in the worktree - live-verified end-to-end. Tokens
via `--output-format json` (cost still `unavailable`; `native_usage=False`). **Codex ✅
verified end-to-end through EastRouter:** worktree writes land, the JSONL parser extracts text +
tokens, and a `usage_api: eastrouter` client puts its real `admin-api` cost on the ledger; a
token-only Codex client stays `unavailable`. **Claude Code ✅ fully (2026-06-26):**
read/safe-edit (`acceptEdits`) writes land in the worktree, native `total_cost_usd`+tokens flow to
the ledger, and `-p` mode is non-blocking with stdin closed. **Command Code ✅ live-verified headless
(model `zai-org/GLM-5.2`):** `-p` prints plain text with no token/cost accounting, so usage is
`unavailable` (hosted account; spend lives in its own dashboard).
