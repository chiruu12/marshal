# Chauffeur — FUTURE product (notes only, not in scope now)

> Parked. We build **Marshal** (the infra/engine) first. Chauffeur is a separate, later product
> that sits on top of Marshal. These are just notes so the vision isn't lost — do NOT start
> building Chauffeur until Marshal is solid and we explicitly revisit this.

## The two-tier vision

```
Marshal  (infrastructure layer — building NOW)
└── orchestration engine
    ├── adapters          # one per backend: cursor / opencode / codex / gemini
    ├── worktrees         # isolated parallel execution
    ├── MCP server        # user-configured N clients, lean tool surface
    └── workflows         # multi-step pipelines over the fleet

Chauffeur  (end-user autonomous coding system — FUTURE, built ON Marshal)
└── self-driving product
    ├── built on Marshal  # consumes Marshal as a library/engine
    ├── planning          # turn a goal into a task DAG automatically
    ├── routing           # pick the right backend/model per task
    ├── self-driving workflows
    └── agent-management UI
```

## Why split them

- **Marshal stays clean and embeddable.** If the engine is a well-factored library + MCP server,
  Chauffeur is "just another driver" on top of it — and so is anyone else's product. This is the
  same infra-vs-product split that lets the engine be useful (and open-source-able) on its own.
- **Different audiences.** Marshal = developers/tool-builders who want to orchestrate headless
  agents. Chauffeur = end users who want an autonomous coding system with a UI and no setup.
- **Sequencing.** Marshal must exist and be robust before Chauffeur has anything to drive.

## Design implications for Marshal (so Chauffeur is easy later)

- Expose Marshal's capabilities through a **clean public API** (Python) AND the **MCP surface** —
  Chauffeur should be able to consume either.
- Keep **planning/routing OUT of the engine** — those are policy that lives in Skills now and in
  Chauffeur later. The engine provides mechanism (spawn/monitor/collect/integrate/usage), not
  judgment.
- The **usage tracking** + **fleet state** must be queryable programmatically (not just CLI text)
  so Chauffeur's UI can render dashboards.
- Workflows should be defined as data the engine executes, so Chauffeur can generate them.

## When we revisit

After Marshal hits a usable, documented v0 (Phases 0–5 in `design.md`). Then re-open this file
and turn it into a real Chauffeur plan.
