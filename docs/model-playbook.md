# Model & client routing playbook

Marshal routes work to **clients** — named `backend + model + permission` combos you declare in
`fleet.config.yaml`. The driver picks a client *by name*, never a raw model. So "which model for
which task" really means: **set up clients per task weight, then route each task to the right one.**

Two rules before the tables:

1. **Route by task weight, not habit.** Heavy reasoning → a frontier model; mechanical bulk → a
   small fast one. Paying Opus rates to rename a variable is waste; asking Haiku to redesign an
   architecture is rework you'll pay for twice.
2. **Measure, don't guess.** Model "strength" shifts release to release and varies by task. Run
   `marshal benchmark "<goal>" client_a client_b ...` to put the same task through several clients
   and compare *real* cost / latency / outcome from the ledger. Treat the tiers below as sensible
   defaults to benchmark against — not gospel.

## The three weights

| Weight | What it is | Examples |
|--------|------------|----------|
| **Heavy** | Open-ended reasoning, cross-file design, gnarly bugs — where a wrong *approach* costs hours | architecture, tricky refactors, root-causing a heisenbug, security-sensitive code |
| **Standard** | The workhorse: well-specified work with clear acceptance criteria | implement an endpoint, add tests for a module, a contained refactor |
| **Light** | Mechanical, low-judgment, high-volume | formatting, docstrings, renames, boilerplate, simple test stubs, doc edits |

## Model menu, by backend

Pick a model for the *weight*, and note how its cost is known — Marshal never fabricates a cost
(see [Cost honesty](#cost-honesty)).

| Backend | Model | Best weight | Cost source | Notes |
|---------|-------|-------------|-------------|-------|
| `claude-code` | `claude-opus-4-8` | Heavy | native | Strongest reasoning, priciest (~$15/$75 per Mtok). |
| `claude-code` | `claude-sonnet-4-6` | Standard | native | The default workhorse (~$3/$15). |
| `claude-code` | `claude-haiku-4-5` | Light | native | Fast + cheap for bulk/mechanical work. |
| `opencode` | `opencode-go/kimi-k2.6` | Standard–Heavy | native | Strong coder; bills the Go subscription. |
| `opencode` | `opencode-go/glm-5.2` | Standard | native | The OpenCode default. |
| `opencode` | `opencode-go/minimax-m3` | Standard | native | General coder. |
| `opencode` | `opencode-go/deepseek-v4-flash` | Light | native | Fast/cheap for bulk. |
| `cursor` | `composer-2.5` | Standard–Heavy | **unavailable** | Strong coder; individual plans expose no per-run cost (`doctor` shows plan tier). |
| `codex` | `gpt-5-codex` | Standard | **estimated** | Cost = tokens × price table (~$1.25/$10); re-verify when rate limits reset. |
| `antigravity` *(experimental)* | `gemini-3.1-pro` (heavy), `gemini-3.5-flash` (light), also `claude-sonnet-4.6` / `claude-opus-4.6` / `gpt-oss-120b` | varies | none | Headless **writes** currently divert under workspace-trust; use read-only/reply until fixed. |

> OpenCode must use an `opencode-go/*` model — a `fireworks-ai/*` model is rejected at config load so
> you never burn Fireworks credits. Omitting `model` defaults to `opencode-go/glm-5.2`.

## A tiered fleet you can copy

Name clients by *role*, not by model — that's what the driver routes on, and it lets you swap the
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
- **Fan out, then judge.** For uncertain approaches, run the same task across 2–3 clients
  (`run_many` / `benchmark`) and keep the best diff. Cheaper models win standard tasks more often
  than you'd expect — benchmark to find out, don't assume.
- **Sequential work runs in rounds.** If task B needs A's result, integrate A first, then plan B
  against the new state (a worker is headless — it can't ask you anything mid-run).

## Cost honesty

`marshal usage` / `report` tag every run's cost with its provenance, and never present a guess as
ground truth:

- **native** (`claude-code`, `opencode`) — the backend reported real tokens **and** cost. Trust it.
- **estimated** (`codex`) — cost computed from tokens × `src/marshal_engine/data/prices.yaml`
  (USD per Mtok). Those are *starter* values — update them to your providers' current prices. An
  estimate reflects the table at the moment of the run.
- **unavailable** (`cursor`) — the backend exposes no per-run cost; tokens may still be recorded.

When you need true cost accounting (e.g. for a benchmark you'll act on), prefer **native-cost**
clients so "cheapest" ranks on facts, not estimates.
