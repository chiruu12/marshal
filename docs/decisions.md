# Marshal — Decisions & Findings Log

Running log of non-obvious decisions and verified findings. Newest first.

## 2026-06-20 — Roadmap reorder: cost-proof first; engine/report split

Reviewed product + architecture after shipping `collect_run`/`integrate` and running a multi-agent
codebase analysis + adversarial review.

**Reorder.** The differentiator ("prove the savings") had been sequenced last. Pulled it forward:
- **P1 cost-proof** (single-threaded): trustworthy per-provider cost. See
  [`plans/phase1-cost-proof.md`](plans/phase1-cost-proof.md).
- **P2 solidify**: the 5 known `collect_run`/`integrate` bugs, a git-spawn timeout + stdin guard on
  `WorktreeManager._git`, globally-unique `run_id`, run-loop terminal-stamp on exception.
- **P3 parallel + measured benchmark**: `ThreadPoolExecutor` behind a swappable Fleet API; per-run
  state files (`runs/<run_id>.json`, aggregates derived on read) for concurrency safety; then the
  savings report done MEASURED.

**Engine/report two-layer split (locked).** Engine stamps facts to an immutable ledger; the report
layer derives interpretation on read; nothing derived is stored. Estimated cost is priced at run
time. Keeps "engine = mechanism."

**Savings-vs-baseline deferred to P3.** A P1 "what it would have cost on Opus" rests on an
equal-token assumption that is usually false — too speculative to present as proof. The honest
version is measured in P3 benchmarking (run the same task through multiple models, measure both).

**Product model.** Marshal is self-installed beside the user's own Claude Code, against their own
subscriptions. No per-client / multi-tenant logic; ship the engine + setup guidance, users
configure `fleet.config.yaml`.

**Known bugs to fix in P2** (from the adversarial review): quoted/unicode paths in
`changed_files()`/`_conflicted_files()` (need `-z`); `integrate` on a dirty main raises instead of
returning a structured status; conflict-retry returns "empty" while the branch holds an unmerged
commit; `current_branch()` returns literal `"HEAD"` when detached; `commit_all --no-verify` has no
test.

## 2026-06-19 — Live backend verification

Installed/authed the available CLIs and ran each through its adapter end-to-end.

| Backend | Verdict | Notes |
|---|---|---|
| OpenCode | ✅ fully verified | read + safe-edit worktree write + native usage/cost. Forced `opencode-go/*` (Go sub), not Fireworks. |
| Cursor | ✅ fully verified | read + safe-edit worktree write. `CURSOR_API_KEY` env authenticates headless even when `cursor-agent status` says "not logged in". No tokens/cost in CLI output → usage `unavailable` (Admin-API path later). |
| Antigravity | ⚠️ reply only | auth (shared app creds) + reply work; **headless file writes divert to `~/.gemini/antigravity-cli/scratch`** instead of the target dir. See investigation below. |
| Codex | ⛔ blocked | adapter ready, failure-path verified; account usage-limited until ~Jul 18 2026 (or Plus). |

Verification cost: ~$0.03 on OpenCode Go + negligible Cursor/Antigravity. **Zero Fireworks.**

### Decision: force Go models for OpenCode
Both **OpenCode Go** and **Fireworks AI** are authed in `opencode`. Go models use the prefix
`opencode-go/` (e.g. `opencode-go/glm-5.2`); Fireworks uses `fireworks-ai/...`. Marshal must pass an
explicit `-m opencode-go/...` so runs bill the Go subscription, not Fireworks credits. The config
layer will default to a Go model and guard against Fireworks ids.

### Investigation: Antigravity headless writes (routed to Gemini, via Antigravity itself)
Symptom: `agy --dangerously-skip-permissions -p ...` writes into its scratch dir, not `cwd` —
it can't establish "workspace trust" without a TTY.

We routed the investigation to **Gemini (through the Antigravity adapter)** — Gemini investigating
its own CLI. Gemini proposed:
- the `--add-dir <path>` flag, and/or
- `~/.gemini/antigravity-cli/settings.json` with `trustedWorkspaces[]` + `allowNonWorkspaceAccess`.

We tested `--add-dir`: the flag **exists** ("Add a directory to the workspace") but did **not** fix
it — agy still wrote to scratch ("you currently do not have an active workspace"). So `--add-dir`
alone is insufficient; the blocker is establishing an *active/trusted* workspace headlessly.

**Decision:** treat Antigravity's worktree-write path as a **known limitation** for now (reply/read
works). Possible future fix: dynamically register each worktree path in `trustedWorkspaces` before a
run, or a PTY-based trust step — both hacky; revisit as `agy` matures. Not blocking: OpenCode and
Cursor cover write use cases today.

## 2026-06-19 — Naming & scope
- **Marshal** = the infra/engine (this repo). **Chauffeur** = a future end-user product built on it.
- Backends derive from one base class; backend is a **per-call parameter**. Four adapters:
  Cursor, OpenCode, Codex, Antigravity. Import package `marshal_engine` (literal `marshal` shadows
  the stdlib builtin); CLI command `marshal`.
- Differentiator: **per-provider usage tracking** (`events.jsonl` + `summary.json` + `marshal usage`).
