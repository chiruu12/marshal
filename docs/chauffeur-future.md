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
  judgment.
- Keep usage tracking and fleet state queryable programmatically, not just as CLI text, so a UI can
  render dashboards on top of them.
- Define workflows as data the engine executes, so a higher layer can generate them.

## Status

Chauffeur is a roadmap item, revisited once Marshal reaches a documented, stable release. Until then,
development focuses on Marshal itself.
