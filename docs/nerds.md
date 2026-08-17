# Numbers, methodology, and the stuff we argue about

Deep dives pulled from the measured benchmark, the usage ledger, permission model, and security
model. For day-to-day use see [`usage.md`](usage.md) and [`model-playbook.md`](model-playbook.md).

## Benchmark methodology

Marshal's headline feature is a **measured** routing comparison, not a guess. Run one goal through
several strategies with `benchmark(goal, clients)` (MCP) or the equivalent service call; `report(task_id)`
re-derives a source-honest table from each run's recorded facts in the immutable ledger
(`usage/events.jsonl`).

**Rules:**

- Every strategy runs the **identical goal** in its **own isolated git worktree**.
- Cost, latency, and token counts come from each run's recorded facts — never invented at report time.
- **`cheapest` ranks only strategies with a known cost.** Clients showing `unavailable` are excluded
  (not treated as $0). The same rule applies to human CLI output (`marshal status`, `marshal usage`):
  unknown provenance renders as `unavailable`, not `$0.0000`.
- Correctness is verified **separately** from `report` — run the produced tests yourself.

### Example run (TokenBucket rate limiter)

**Goal:** implement a `TokenBucket` rate limiter (stdlib-only, with injectable-clock pytest tests),
run across four configured clients.

| Strategy | Backend | Model | Status | Cost | Source | Duration | Tokens (in / out) |
|---|---|---|---|---|---|---|---|
| deepseek | opencode | opencode-go/deepseek-v4-flash | exited_clean | **$0.0029** | native | 81.8 s | 11,740 / 1,977 |
| claude | claude-code | claude-sonnet-4-6 | exited_clean | $0.3374 | native | 121.4 s | 17 / 6,837 |
| cmdcode | command-code | zai-org/GLM-5.2 | exited_clean | `unavailable` | unavailable | 252.6 s | 0 / 0 |
| codex-glm | codex | z-ai/glm-5.1 (via EastRouter) | exited_clean | `unavailable` | unavailable | 283.0 s | 231,075 / 7,812 |

```
cheapest: deepseek (opencode)  $0.0029   [cmdcode, codex-glm not ranked: cost unavailable]
fastest:  deepseek (opencode)  81.8 s
```

**Verified outcome:** we ran each produced solution's tests. `deepseek`, `claude`, and `cmdcode`
passed 6/6; `codex-glm`'s test file failed to import (collection error). The cheapest, fastest
client was also correct — for ~1/115th of `claude`'s cost.

**What this demonstrates:**

- **Measured, not guessed.** Facts are stamped at run time; interpretation is derived on read.
- **Honest sourcing.** `cmdcode` (hosted account, no tokens/cost) and `codex-glm` (long EastRouter
  session that fell past a single `/v1/usage` page in an early run) both show `unavailable`. The
  reader now **paginates** `/v1/usage` to recover long runs' real `admin-api` cost (~$0.16 for that
  session, recoverable from the provider API).
- **More reasoning ≠ better.** `codex-glm` spent **231K input tokens** over-exploring a one-class
  task, ran slowest, and shipped broken code.

Underlying shape: `BenchmarkResult` with `strategies: [StrategyResult{client, backend, model,
status, cost_usd, source, duration_ms, input_tokens, output_tokens}]` plus derived `cheapest` and
`fastest` labels. `cost_usd` is **null when nothing was measured** (read `source` for why); a
literal `0.0` means a provider reported a real zero, so summing the column never invents spend for
a fleet whose backends cannot report any. See [`examples/benchmark-output.md`](../examples/benchmark-output.md) and
`src/marshal_engine/orchestration/fleet.py`.

## Cost-provenance taxonomy

`marshal usage` / `report` tag every run's cost with its provenance. Never present a guess as
ground truth.

| Source | Meaning | Typical backends |
|---|---|---|
| **native** | Backend reported real tokens **and** cost | `claude-code`, `opencode`; Goose when provider reports positive cost |
| **admin-api** | Real per-run charge read back from a provider usage API | `codex` with `usage_api: eastrouter` (EastRouter `/v1/usage`) |
| **unavailable** | No per-run cost known; tokens may still be recorded | `cursor`, `command-code`, token-only `codex`, `opencode` on unpriced custom provider, `antigravity` |

**Two-layer split:** the engine stamps *facts* to an immutable ledger (`usage/events.jsonl`);
interpretation (cost-per-outcome, savings, `cheapest`) is *derived on read* in the report layer,
never stored. Historical ledger lines tagged `estimated` still load; the `cost_estimated` summary
field remains as a zero tombstone.

**admin-api attribution:** runs are matched by model + time window with a token-reconciliation
guard; a run that can't be uniquely attributed falls back rather than claim a wrong cost.

When you need true cost accounting (e.g. for a benchmark you'll act on), prefer **native-cost**
or **admin-api** clients so "cheapest" ranks on facts. Full routing notes:
[`model-playbook.md`](model-playbook.md#cost-honesty).

## Permission-fidelity matrix

Headless agents have no stdin — Marshal never uses a prompting permission mode (it deadlocks).

| Tier | Meaning |
|---|---|
| `read-only` | Plan/inspect only — no edits |
| `safe-edit` | Edit and run inside the worktree, no prompts (default) |
| `yolo` | Unrestricted (opt-in) |

**`permission_fidelity`** is a coarse honesty signal — not a sandbox ranking. Backend surfaces
(`marshal backends`, doctor `permission:<backend>`) report **safe-edit** capability; `list_clients`
resolves the `(backend, permission)` pair (`yolo` → `unrestricted`).

| Value | Where | What it means |
|---|---|---|
| `enforced-denies` | Cursor/OpenCode/Codex safe-edit (and their read-only clients) | Backend or Marshal restriction beyond the worktree. Still not a true process sandbox. |
| `boundary-only` | Command Code, Goose, Antigravity, Claude Code | No Marshal deny layer; worktree + explicit `integrate` is the dependable boundary |
| `unrestricted` | Any `permission: yolo` client | Deny/sandbox overlay dropped by design |

Per-backend permission mapping (read-only / safe-edit / yolo → native flags):

| Backend | read-only | safe-edit | yolo | safe-edit fidelity |
|---|---|---|---|---|
| Cursor | `--mode plan` | `--force` + deny list in `.cursor/cli.json` | `--yolo` (`unrestricted`) | enforced-denies |
| OpenCode | `plan` / read + deny edit | skip-permissions + curated denies | skip-permissions + `question: deny` (`unrestricted`) | enforced-denies |
| Codex | `-s read-only` | `-s workspace-write` | bypass sandbox (`unrestricted`) | enforced-denies |
| Command Code | `--permission-mode plan` | `--yolo` | `--yolo` (`unrestricted`) | boundary-only |
| Antigravity | unsupported headless | `--dangerously-skip-permissions` + trusted workspace | same as safe-edit (`unrestricted`) | boundary-only |
| Claude Code | `--permission-mode plan` | `--permission-mode acceptEdits` | bypass (`unrestricted`) | boundary-only |
| Goose | `GOOSE_MODE=chat` | `GOOSE_MODE=auto` | `GOOSE_MODE=auto` (`unrestricted`) | boundary-only |

Prefer `enforced-denies` clients for sensitive work. Full cheat sheets: [`design.md`](design.md) §5.

## Worktree-isolation threat model

Worktree isolation is the **safety boundary**. Each run executes inside its own git worktree under
`~/.marshal/worktrees/<repo>-<digest>/`. The agent edits there, not in your working tree.

**Guarantees:**

- Driver-supplied `task_id` / run directory names are validated before any `git worktree` op:
  charset `[A-Za-z0-9._-]`, length-capped, resolved path must be a strict descendant of
  the repo's run root. Hostile ids fail closed — never sanitize-rewritten.
- **Main branch is untouched until explicit `integrate`.** `collect_run` is read-only; merge is a
  separate step you control.
- Every run has a **hard timeout and process-group kill** — agent grandchildren are not orphaned.

**Assumptions and limits:**

- Worktree isolation assumes the **workspace set is operator-selected**. It protects files within
  whichever repository Marshal targets; it does not protect the host from a driver registering or
  targeting a different repository. Default: register repos with `marshal workspace add`; leave
  `MARSHAL_ALLOW_MCP_WORKSPACE_REGISTRATION` unset so the MCP driver cannot add workspaces.
- **`safe-edit` is not uniformly a deny sandbox.** `boundary-only` backends rely on the worktree
  boundary; curated denies on Cursor/OpenCode/Codex are not full sandboxes either.
- **`yolo` removes guardrails by design** — only when you trust the task and backend.
- **`worktree_setup` / `verify`** run config-driven subprocesses as your user; the basename
  allowlist is not a sandbox (`python -c` still passes). `verify` runs after the agent may have
  modified the worktree.
- **`integrate`** merges onto the workspace's current branch — review diffs first.
- **`cancel_run`** signals only a live child of the current process; an agent that outlived its
  supervisor cannot be stopped by Marshal.

Residual gaps (Cursor deny bypass via shell, Antigravity global `trustedWorkspaces`, orphan
processes, team prompt injection): [`SECURITY.md`](../SECURITY.md).
