# Marshal — Vision & Strategy (distilled)

> Distilled from the product PRD. Captures the points that actually shape what we build.
> Full architecture is in `design.md`; future end-user product in `chauffeur-future.md`.

**Tagline:** *The control plane for AI coding agents.*

## Thesis

Today people force one expensive model to do every part of a coding task — even the parts that
don't need top-tier reasoning. That wastes tokens, adds latency, and pollutes context. Marshal is a
**provider-agnostic control plane for AI labor**: keep the best model *thinking* (planning, review,
recovery, merge decisions), delegate execution to cheaper/specialized workers, preserve context by
isolating tasks, and **prove the savings with hard numbers**.

The differentiator is not "we can run agents." It's **route AI labor intelligently and prove the
result.**

## The three problems Marshal attacks

1. **Reasoning waste** — premium models spending expensive tokens on cheap tasks (boilerplate, docs, bulk edits).
2. **Context bloat** — one session accumulating planning + impl + tests + retries until signal is diluted.
3. **No economics layer** — teams can't tell which provider is cheapest/fastest/best per task category.

## Four surfaces

- **Python engine** — adapters, worktrees, process execution, fleet state, diff/result collection, usage tracking, routing primitives.
- **MCP server** — the interface the planner talks to: `spawn/status/list/collect/cancel/integrate/benchmark/report`.
- **Skills / workflows** — reusable playbooks: decompose → spawn → monitor → verify → merge → compare strategies.
- **Customer config layer** — where it becomes useful to teams: quality-first vs cost-first, planner model, allowed worker models, budget ceilings, role→provider routing, approval rules.

## Routing by ROLE (core abstraction)

Route by **role**, not by hardcoded provider. Roles: planner, coder, reviewer, writer, researcher,
bulk-processor, test-fixer, refactorer. Customers map roles → providers, e.g.:

```
planner → Claude   coder → Codex   writer → Kimi   bulk → DeepSeek   reviewer → Claude
```

This is more durable than provider-specific logic. The user promise:
*"Claude plans, Codex codes, Kimi writes, DeepSeek handles heavy context, and Marshal keeps the bill under control."*

## Cost & benchmarking (the business angle)

Benchmarking is first-class. Run the same task through multiple routing strategies (Claude-only vs
Claude-planner+cheaper-workers vs specialized mixes) and record **cost, latency, completion rate,
test-pass rate, merge success, retries, human-approval, file churn, context usage**. Don't promise a
fixed % saving — **measure real savings per customer**. The savings report shows, per task/workflow:
premium-only cost vs actual Marshal cost, latency, retries, pass/fail, diff size, quality score.

This turns model selection from guesswork into an evidence base, and creates a proprietary dataset
(cost/latency/merge/quality history) that is itself defensible.

## Context scoping

Each worker sees only: its task, the relevant files, minimal repo context, the instructions to
finish. The planner holds the big picture. Effect: less token waste, less drift, cleaner debugging
(each execution unit has a clean boundary).

## Positioning

- Avoid: "multi-agent platform / AI agent framework / autonomous developer assistant" (too generic).
- Use: **control plane for AI coding agents** / provider-agnostic orchestration for AI labor / benchmarkable AI work manager.
- Strongest story: *Keep the best models thinking. Let cheaper or specialized models do the execution. Marshal proves what it saved.*

## Defensibility

Routing intelligence (learned provider↔task fit) · proprietary historical cost/quality data ·
workflow lock-in once teams run production work through it · the policy layer · control-plane
positioning above any single provider.

## Business model

OSS engine + paid hosted control plane / governance tier (audit logs, spend limits, org reporting,
access control, approvals). Reinforces our private-first → public-OSS path.

## Roadmap (maps onto design.md phases)

- **V1** (== our Phases 0–5): backend adapter interface, worktree manager, process runner, result collector, MCP server, persistent state, basic CLI, **cost logging**, **simple benchmark command**.
- **V2**: role-based routing engine, policy engine, provider comparison reports/dashboards, team configs, task templates, budget controls.
- **V3**: automatic routing recommendations, historical provider scoring, org-wide policy enforcement, approval workflows, multi-repo visibility, model-migration suggestions.

## Metrics that matter

Efficiency: cost/task, cost/repo, cost/successful-merge, tokens/task, latency/task.
Quality: test-pass, human-acceptance, rollback, retry, failed-merge.
Routing: how often cheaper worker chosen, how often routing beats baseline, quality delta vs Claude-only.
Operational: worker uptime, worktree-cleanup success, MCP tool success, queue depth, throughput.

## Risks (and the guardrails)

1. Becomes "yet another agent framework" → stay on control-plane + cost + routing evidence.
2. Provider quality varies → customer-configurable policy + fallbacks.
3. Savings inconsistent → benchmark per workflow, never promise universal %.
4. Isolation adds complexity → make worktree lifecycle / collection / merge **boring and reliable**.
5. A provider ships this natively → stay provider-agnostic, own the cross-provider layer.

## Final principle

Marshal is not the agent that does everything. It's the thing that decides **who works, where they
work, how much context they get, and whether the result was worth the cost.** The product is the
control plane — not the workers.
