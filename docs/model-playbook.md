# Model & client routing playbook

Marshal routes work to **clients** - named `backend + model + permission` combos you declare in
`fleet.config.yaml`. The driver picks a client *by name*, never a raw model. So "which model for
which task" really means: **set up clients per task weight, then route each task to the right one.**

Two rules before the tables:

1. **Route by task weight, not habit.** Heavy reasoning → a frontier model; mechanical bulk → a
   small fast one. Paying Opus rates to rename a variable is waste; asking Haiku to redesign an
   architecture is rework you'll pay for twice.
2. **Measure, don't guess.** Model "strength" shifts release to release and varies by task. Have the
   driver call the `benchmark` tool - `benchmark("<goal>", ["client_a", "client_b"])` - to put the
   same task through several clients and compare *real* cost / latency / outcome from the ledger.
   Treat the tiers below as sensible
   defaults to benchmark against - not gospel.

## The three weights

| Weight | What it is | Examples |
|--------|------------|----------|
| **Heavy** | Open-ended reasoning, cross-file design, gnarly bugs - where a wrong *approach* costs hours | architecture, tricky refactors, root-causing a heisenbug, security-sensitive code |
| **Standard** | The workhorse: well-specified work with clear acceptance criteria | implement an endpoint, add tests for a module, a contained refactor |
| **Light** | Mechanical, low-judgment, high-volume | formatting, docstrings, renames, boilerplate, simple test stubs, doc edits |

## Model menu, by backend

Pick a model for the *weight*, and note how its cost is known - Marshal never fabricates a cost
(see [Cost honesty](#cost-honesty)).

`list_models` / each adapter's `available_models()` surfaces what you can configure. When the CLI
exposes a headless catalogue the adapter probes it (bounded timeout; never raises); otherwise it
returns the curated static ids in this table. The answer is always tagged with its `source`, so
`probed` (the CLI said so just now) is distinguishable from `static` (this table, which may be
stale — it is not evidence your account can run that model).

| Backend | Model | Best weight | Cost source | Discovery | Notes |
|---------|-------|-------------|-------------|-----------|-------|
| `claude-code` | `claude-opus-4-8` | Heavy | native | static (this table) | Strongest reasoning, priciest (~$15/$75 per Mtok). |
| `claude-code` | `claude-sonnet-4-6` | Standard | native | static (this table) | The default workhorse (~$3/$15). |
| `claude-code` | `claude-haiku-4-5` | Light | native | static (this table) | Fast + cheap for bulk/mechanical work. |
| `opencode` | `opencode-go/kimi-k2.6` | Standard-Heavy | native | probe `opencode models` | Strong coder; bills the Go subscription. |
| `opencode` | `opencode-go/glm-5.2` | Standard | native | probe `opencode models` | The OpenCode default. |
| `opencode` | `opencode-go/minimax-m3` | Standard | native | probe `opencode models` | General coder. |
| `opencode` | `opencode-go/deepseek-v4-flash` | Light | native | probe `opencode models` | Fast/cheap for bulk. |
| `cursor` | `composer-2.5` | Standard-Heavy | **unavailable** | probe `cursor-agent models` | Strong coder; individual plans expose no per-run cost (`doctor` shows plan tier). |
| `codex` | `gpt-5.6-luna` | Standard-Heavy | **unavailable** (tokens only) | static (this table; `codex models` needs a TTY) | Stock OpenAI Codex: auth via `codex login` (ChatGPT) or `OPENAI_API_KEY`. Reports tokens but no native cost — use `usage_api: eastrouter` for real **admin-api** cost. |
| `command-code` | `zai-org/glm-5.2` | Standard | **unavailable** | probe `command-code --list-models` | Hosted coding agent on its own account; `-p` prints text with no tokens/cost, so spend lives in its own dashboard (`doctor` surfaces its provider + default model). |
| `antigravity` *(experimental)* | `gemini-3.1-pro` (heavy), `gemini-3.5-flash` (light), also `claude-sonnet-4.6` / `claude-opus-4.6` / `gpt-oss-120b` | varies | **unavailable** | probe `agy models` | Worktree **writes** now land correctly (worktree pre-registered as a trusted workspace); supports `safe-edit`/`yolo` only (no `read-only`). Doctor is path-only (no cheap auth probe). |
| `zcode` | `glm-5.3` (heavy), `glm-5-turbo` (light); bare or `provider/model` (e.g. `builtin:zai-start-plan/glm-5.3`) | Standard-Heavy | **unavailable** (tokens only) | static (this table; no headless model-list command) | Z.ai's GLM agent. Ships **no PATH binary** — the headless CLI is a Node bundle inside the desktop app; point `ZCODE_BIN`/`MARSHAL_ZCODE_BIN` at it or rely on bundle autodetect. Model is routed via the `ZCODE_MODEL` env var, not a flag. Auth is interactive OAuth (`zcode login`); doctor is launcher-presence only. |
| `goose` | `cursor-agent/auto` (Cursor-backed), or bare model / `provider/model` for other providers | Standard | **native** when provider reports positive cost; else **unavailable** | static (`cursor-agent/auto`; `local-models` is GGUF/MLX only) | Headless via `GOOSE_MODE=auto`; `permission_fidelity=boundary-only`. Pin Cursor with `cursor-agent/auto` (needs `cursor-agent login`). CLI ≥ 1.43 live-verified. Doctor auth via `goose info -v --check`. |

> OpenCode defaults to an `opencode-go/*` model, which bills the Go subscription; omitting `model`
> resolves to `opencode-go/glm-5.2`. A `fireworks-ai/*` model is allowed and warns at load - it bills
> Fireworks credits instead, and in exchange reports **real per-run USD** (`source=native`), which is
> the only measured cost some fleets have.

> **Optional: real cost via EastRouter (third-party).** If you route a `codex` client through
> EastRouter instead of stock OpenAI, set `usage_api: eastrouter` to read its **real** per-run cost
> back from EastRouter's `/v1/usage` (reported `admin-api`, not an estimate). Point the client at
> EastRouter's Codex config directory with per-client `env` — the Codex CLI selects provider and
> auth via `CODEX_HOME`, so one Marshal server can run stock ChatGPT Codex and an EastRouter route
> side by side:

```yaml
clients:
  codex-stock:
    backend: codex
    model: gpt-5.6-luna
    # default ~/.codex — ChatGPT auth from `codex login`

  codex-eastrouter:
    backend: codex
    model: z-ai/glm-5.1
    usage_api: eastrouter
    env:
      CODEX_HOME: ~/.codex-eastrouter   # third-party router config; ~ expanded at load
```

> `opencode` can also use EastRouter as a custom OpenAI-compatible provider (models named
> `eastrouter/<id>`), but OpenCode can't price a custom provider, so that client's cost stays
> `unavailable`.

## A tiered fleet you can copy

Name clients by *role*, not by model - that's what the driver routes on, and it lets you swap the
model behind a role without touching the driver's playbook.

```yaml
defaults: { permission: safe-edit, timeout_s: 900 }
# worktree_setup: uv sync --extra dev --extra mcp   # optional: provision each worktree's venv

clients:
  architect:   { backend: claude-code, model: claude-opus-4-8,                 permission: safe-edit }  # heavy
  builder:     { backend: claude-code, model: claude-sonnet-4-6,               permission: safe-edit }  # standard (default)
  builder-alt: { backend: opencode,    model: opencode-go/kimi-k2.6,           permission: safe-edit }  # standard, benchmark vs builder
  bulk:        { backend: opencode,    model: opencode-go/deepseek-v4-flash,   permission: safe-edit }  # light / cheap
  reviewer:    { backend: cursor,                                              permission: read-only }  # independent review
```

## Routing heuristics

- **Default to `builder` (standard).** Escalate to `architect` only when the task is open-ended or a
  wrong approach is expensive. Drop to `bulk` for mechanical, high-volume work.
- **Pair the permission tier with the task.** `read-only` for planning/review (no edits, cheaper,
  safe), `safe-edit` for implementation, `yolo` only when you truly mean it.
- **Review with a *different* model than you built with.** An independent reviewer (e.g. `cursor`
  read-only) catches blind spots the builder shares with itself. See the `marshal-review-gate` Skill.
- **Fan out, then judge.** For uncertain approaches, run the same task across 2-3 clients
  (`run_many` / `benchmark`) and keep the best diff. Cheaper models win standard tasks more often
  than you'd expect - benchmark to find out, don't assume.
- **Sequential work runs in rounds.** If task B needs A's result, integrate A first, then plan B
  against the new state (a worker is headless - it can't ask you anything mid-run).

## Cost honesty

`marshal usage` / `report` tag every run's cost with its provenance, and never present a guess as
ground truth:

- **native** (`claude-code`, `opencode`) - the backend reported real tokens **and** cost. Trust it.
- **admin-api** - the real per-run charge read back from a provider's usage API. A `codex` client with
  `usage_api: eastrouter` reports its actual EastRouter `/v1/usage` cost this way. Runs are attributed by
  model + time window with a token-reconciliation guard; a run that can't be uniquely attributed falls
  back rather than claim a wrong cost.
- **unavailable** (`cursor`, `command-code`, `antigravity`, `zcode`, Goose when the provider reports no cost,
  a token-only `codex` with no `usage_api`, or `opencode` pointed at an unpriced custom provider like
  EastRouter) - no per-run cost is known; tokens may still be recorded. Never a fake `$0`.

When you need true cost accounting (e.g. for a benchmark you'll act on), prefer **native-cost**
or **admin-api** clients so "cheapest" ranks on facts.

Deep dive (benchmark methodology, permission matrix, threat model):
[`nerds.md`](nerds.md).
