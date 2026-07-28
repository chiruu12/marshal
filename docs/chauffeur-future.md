# Chauffeur (roadmap): the product layer above Marshal

Marshal is deliberately an **infrastructure layer**: a well-factored engine plus an MCP server and
Skills for driving a fleet of headless coding agents. **Chauffeur** is a planned, separate product
that will sit on top of Marshal as an end-user autonomous coding system. This note explains the split
and what it implies for Marshal's design. Chauffeur is not part of the current release.

## The two-tier design

```
Marshal  (infrastructure layer, available now)
  orchestration engine
    - adapters      one per backend: cursor / opencode / codex / claude-code / command-code
    - worktrees     isolated parallel execution
    - MCP server    user-configured N clients, lean tool surface
    - workflows     multi-step pipelines over the fleet

Chauffeur  (end-user product, planned, built on Marshal)
  self-driving coding system
    - built on Marshal   consumes Marshal as a library and MCP surface
    - planning           turn a goal into a task graph automatically
    - routing            pick the right backend/model per task
    - self-driving workflows
    - agent-management UI
```

## Why split them

- **Marshal stays clean and embeddable.** Because the engine is a library plus an MCP server,
  Chauffeur is "just another driver" on top of it, and so is anyone else's product. The same
  infra-versus-product split is what lets the engine be useful, and open source, on its own.
- **Different audiences.** Marshal serves developers and tool-builders who want to orchestrate
  headless agents directly. Chauffeur will serve users who want an autonomous coding system with a
  UI and minimal setup.
- **Sequencing.** Marshal has to exist and be robust before Chauffeur has anything to drive.

## What this asks of Marshal's design

These constraints keep Marshal a good foundation for a product layer:

- Expose Marshal's capabilities through both a clean Python API and the MCP surface, so a driver can
  consume either.
- Keep planning and routing out of the engine. Those are policy that lives in Skills today and in
  Chauffeur later. The engine provides mechanism (spawn, monitor, collect, integrate, usage), not
  judgment. See [The freeze line](#the-freeze-line-mechanism-vs-judgment) and [`design.md` §12](design.md#12-marshal--chauffeur-freeze-line).
- Keep usage tracking and fleet state queryable programmatically, not just as CLI text, so a UI can
  render dashboards on top of them.
- Define workflows as data the engine executes, so a higher layer can generate them.

## Status

Chauffeur is a roadmap item, revisited once Marshal reaches a documented, stable release. Until then,
development focuses on Marshal itself.

---

## The freeze line (mechanism vs judgment)

This section is the normative boundary between **Marshal** (mechanism) and **Chauffeur / Skills**
(judgment). PR review should cite it before adding engine features. The inventory table lives in
[`design.md` §12](design.md#12-marshal--chauffeur-freeze-line).

### Mechanism — stays in Marshal

Marshal owns **trusted, correct execution** of agent runs: isolation, process control, accounting,
and git primitives. Concrete modules:

| Area | Module(s) | What it does |
|---|---|---|
| Worktree isolation | `worktree.py`, `fleet.py` | `git worktree add/remove`; the safety boundary — main branch untouched until explicit integrate. |
| Run loop | `backends/base.py`, `fleet.py` | Spawn backend in worktree; external timeout + process-group kill on every run; optional verify gate. |
| Backend adapters | `backends/*.py`, `registry.py` | Pure `build_invocation` / `map_permission`; normalize CLI output to `AgentResult`. |
| Usage ledger | `usage.py`, `pricing.py`, `eastrouter.py` | Append-only `events.jsonl`; tag every record with `source`; never present estimates as ground truth. |
| Fleet state | `state.py`, `logs.py` | One `runs/<run_id>.json` per run; durable stdout/stderr logs. |
| Service primitives | `service.py`, `fleet.py` | `run_agent`, `run_many`, `spawn`, `collect_run`, `integrate`, `cancel_run` — the imperative verbs drivers call. |
| MCP tenancy (not engine) | `workspaces.py` | Resolve named repos; cache one `MarshalService` per workspace; shared concurrency cap only — ledgers stay per-repo. |
| Config load / validation | `config.py`, `budgets.py` | Parse `fleet.config.yaml`; enforce declared caps when `enforce: true`; fail closed on bad input. |

These pieces must be **correct** (timeouts fire, ledgers append, worktrees don't escape). They do
not decide *what* to run, *who* should run it, or *whether* output is good enough to merge.

### Judgment — Skills today, Chauffeur later

The following belong **outside** `marshal_engine`:

- **Task decomposition** — turning a goal into a task graph (`marshal-orchestrate` Skill).
- **Routing / model choice** — which client, backend, or model for a given subtask (driver + config;
  `TaskSpec.role` is a hint field, not an engine router).
- **Prompt authorship** — writing goals, rubrics, and review lenses (driver / Skill).
- **Merge decisions** — whether a succeeded run is correct enough to integrate (`integrate` is
  explicit; workflows default `auto: false`).
- **Review verdicts** — parsing `REVIEW: approve|reject` lines, truth tables, consensus (`marshal-review-gate` Skill).

The engine may **sequence** judgment (run N agents with N goals a human wrote); it must not **compute**
judgment (tally votes, pick a winner, auto-merge on prose).

### Borderline features — deliberately grandfathered

Some features look like policy but were admitted because they add **no new execution path** — they
only call existing primitives in a declared order, with judgment left to the caller.

#### `workflow.py` — declarative recipes (mechanism)

YAML phases (`fan_out` / `agent` / `collect` / `integrate`) map 1:1 to `run_many` / `run_agent` /
`collect_run` / `integrate`. The runner never spawns a process, touches git, or writes run state.
Integration defaults **`auto: false`** so `succeeded` ≠ merged. Which recipe to run and when to
merge stay in the `marshal-workflow` Skill.

#### `teams.py` — adversarial review panels (mechanism)

A team fans N read-only reviewer roles out via one `run_many` call (shared `task_id`, parallel
isolation). The runner collects subject material (`collect_run`, `diff_range`, or supplied plan
text), builds delimited prompts, and returns per-role reports.

**The engine does not judge.** It does not parse verdicts, tally votes, or compute pass/fail — that
was both a layering violation and a security hole (verdict-shaped text in a diff could forge
approval). `next_actions` explicitly tell the driver to read reports and decide. Same admission test
as workflows: no new execution path, no machine verdict.

#### `service.py` `_compose_goal` — static context prefix (mechanism)

Every spawn funnels through `_request_for` → `_compose_goal`, which prepends a fixed worker
preamble plus optional `context.worker` from `fleet.config.yaml`. This is operator-declared static
text, not dynamic recall or routing. (Marshal Recall / memory injection was extracted to
`feature/marshal-recall-cognee`; context routing by recall would be judgment.)

#### `workspaces.py` registry `run_many` — shared pool, separate ledgers (mechanism)

Cross-workspace `run_many` (#22) routes each job to its workspace's `MarshalService` under one
process-wide concurrency cap. Each repo keeps its own config, worktrees, and usage ledger. What is
**not** here: cross-workspace spend aggregation, org-wide budget enforcement, or automatic
repo selection — those belong to Chauffeur if needed.

### Decision rule for new features

Before landing code in `marshal_engine`, ask:

1. **Does it add a new execution path?** (spawn/kill/git-write/ledger-write the runner did not already
   have). If yes → likely Chauffeur/Skill, unless it is a new backend adapter or a safety primitive.
2. **Does it decide something on the user's behalf?** (route tasks, choose models, parse verdicts,
   auto-merge, recall memory). If yes → Skill or Chauffeur.
3. **Must it be trusted, or only correct?** Marshal implements **correct mechanism** (timeouts,
   isolation, honest accounting). **Trusted policy** (what to run, what to merge) lives above.

**Pass both tests** (no new path + no machine judgment) → eligible for engine admission as a
*sequencer*, like `workflow.py` and `teams.py`. When in doubt, ship the Skill first; promote to
engine only when the sequencing pattern stabilizes and the safety property is provable.

### What Chauffeur is expected to replace

Chauffeur consumes Marshal as a library/MCP client. It is expected to own:

| Today (Marshal / MCP) | Chauffeur (planned) |
|---|---|
| Driver agent + Skills for planning | Automatic goal → task graph |
| Per-call backend/model choice | Routing engine |
| `marshal-workflow` / hand-authored YAML | Self-driving workflow generation |
| `workspaces.py` file registry | Real multi-tenancy (orgs, projects, auth) |
| CLI + MCP for tool-builders | Agent-management UI |
| Cross-workspace concurrency only | Org-wide policy, spend dashboards, approvals |

Marshal stays embeddable: Chauffeur is "just another driver" with richer policy, not a fork of the
engine.
