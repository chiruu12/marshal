---
name: improve-codebase-architecture
description: Find deepening opportunities in a codebase, informed by the domain language in CONTEXT.md and the decisions in docs/adr/. Use when the user wants to improve architecture, find refactoring opportunities, consolidate tightly-coupled modules, or make a codebase more testable and AI-navigable.
---

# Improve Codebase Architecture

Surface architectural friction and propose **deepening opportunities** — refactors that turn shallow modules into deep ones. The aim is testability and AI-navigability.

## Glossary

Use these terms exactly in every suggestion. Consistent language is the point — don't drift into "component," "service," "API," or "boundary." Full definitions in [LANGUAGE.md](LANGUAGE.md).

- **Module** — anything with an interface and an implementation (function, class, package, slice).
- **Interface** — everything a caller must know to use the module: types, invariants, error modes, ordering, config. Not just the type signature.
- **Implementation** — the code inside.
- **Depth** — leverage at the interface: a lot of behaviour behind a small interface. **Deep** = high leverage. **Shallow** = interface nearly as complex as the implementation.
- **Seam** — where an interface lives; a place behaviour can be altered without editing in place. (Use this, not "boundary.")
- **Adapter** — a concrete thing satisfying an interface at a seam.
- **Leverage** — what callers get from depth.
- **Locality** — what maintainers get from depth: change, bugs, knowledge concentrated in one place.

Key principles (see [LANGUAGE.md](LANGUAGE.md) for the full list):

- **Deletion test**: imagine deleting the module. If complexity vanishes, it was a pass-through. If complexity reappears across N callers, it was earning its keep.
- **The interface is the test surface.**
- **One adapter = hypothetical seam. Two adapters = real seam.**

This skill is _informed_ by the project's domain model — `CONTEXT.md` and any `docs/adr/`. The domain language gives names to good seams; ADRs record decisions the skill should not re-litigate. See [CONTEXT-FORMAT.md](../domain-model/CONTEXT-FORMAT.md) and [ADR-FORMAT.md](../domain-model/ADR-FORMAT.md).

## Process

### 1. Explore

Read existing documentation first:

- `CONTEXT.md` (or `CONTEXT-MAP.md` + each `CONTEXT.md` in a multi-context repo)
- Relevant ADRs in `docs/adr/` (and any context-scoped `docs/adr/` directories)

If any of these files don't exist, proceed silently — don't flag their absence or suggest creating them upfront.

Then use the Agent tool with `subagent_type=Explore` to walk the codebase. Don't follow rigid heuristics — explore organically and note where you experience friction:

- Where does understanding one concept require bouncing between many small modules?
- Where are modules **shallow** — interface nearly as complex as the implementation?
- Where have pure functions been extracted just for testability, but the real bugs hide in how they're called (no **locality**)?
- Where do tightly-coupled modules leak across their seams?
- Which parts of the codebase are untested, or hard to test through their current interface?

Apply the **deletion test** to anything you suspect is shallow: would deleting it concentrate complexity, or just move it? A "yes, concentrates" is the signal you want.

### 2. Present candidates

Present a numbered list of deepening opportunities. For each candidate:

- **Files** — which files/modules are involved
- **Problem** — why the current architecture is causing friction
- **Solution** — plain English description of what would change
- **Benefits** — explained in terms of locality and leverage, and also in how tests would improve

**Use CONTEXT.md vocabulary for the domain, and [LANGUAGE.md](LANGUAGE.md) vocabulary for the architecture.** If `CONTEXT.md` defines "Order," talk about "the Order intake module" — not "the FooBarHandler," and not "the Order service."

**ADR conflicts**: if a candidate contradicts an existing ADR, only surface it when the friction is real enough to warrant revisiting the ADR. Mark it clearly (e.g. _"contradicts ADR-0007 — but worth reopening because…"_). Don't list every theoretical refactor an ADR forbids.

Do NOT propose interfaces yet. Ask the user: "Which of these would you like to explore?"

### 3. Grilling loop

Once the user picks a candidate, drop into a grilling conversation. Walk the design tree with them — constraints, dependencies, the shape of the deepened module, what sits behind the seam, what tests survive.

Side effects happen inline as decisions crystallize:

- **Naming a deepened module after a concept not in `CONTEXT.md`?** Add the term to `CONTEXT.md` — same discipline as `/domain-model` (see [CONTEXT-FORMAT.md](../domain-model/CONTEXT-FORMAT.md)). Create the file lazily if it doesn't exist.
- **Sharpening a fuzzy term during the conversation?** Update `CONTEXT.md` right there.
- **User rejects the candidate with a load-bearing reason?** Offer an ADR, framed as: _"Want me to record this as an ADR so future architecture reviews don't re-suggest it?"_ Only offer when the reason would actually be needed by a future explorer to avoid re-suggesting the same thing — skip ephemeral reasons ("not worth it right now") and self-evident ones. See [ADR-FORMAT.md](../domain-model/ADR-FORMAT.md).
- **Want to explore alternative interfaces for the deepened module?** See [INTERFACE-DESIGN.md](INTERFACE-DESIGN.md).

## Common Rationalizations

| Shortcut | Why It Fails |
|----------|-------------|
| "Let's just refactor this without the grilling session" | Refactoring without understanding WHY the current structure exists leads to re-introducing the same problems. The grilling session surfaces constraints you can't see from the code alone. |
| "This module is small, so it must be shallow" | Small modules can be deep if they hide real complexity behind a simple interface. Size ≠ depth. Apply the deletion test: would removing it spread complexity across callers? |
| "Let's extract this into a utility function for reuse" | Extraction for reuse creates shallow modules. Only extract when it deepens a module — hides complexity behind a simpler interface. If callers need to know the same details, you've just moved code, not simplified it. |
| "We should add an interface here for testability" | One adapter = hypothetical seam. Don't add interfaces until you have two real adapters. Testability should come from testing through public interfaces, not from adding mock points. |
| "This contradicts an ADR but the ADR is outdated" | ADRs are decisions with context. Before overriding, understand WHY the decision was made. If the friction is real enough, surface it and have the conversation — don't silently ignore it. |

## Red Flags

- Every suggestion is "extract into a separate module" — creating shallow modules, not deepening existing ones
- Suggestions reference modules by class/function name instead of domain language — the refactor won't be durable
- No deletion test applied — suggestions may be moving complexity rather than concentrating it
- CONTEXT.md vocabulary ignored in favor of code-level names — domain/architecture language gap

## Verification Checklist

- [ ] Each candidate has files, problem, solution, and benefits described
- [ ] Deletion test applied to every candidate — confirms deepening, not just reorganizing
- [ ] CONTEXT.md vocabulary used for domain concepts
- [ ] LANGUAGE.md vocabulary used for architectural concepts
- [ ] ADR conflicts surfaced where relevant (not silently ignored)
- [ ] User selected which candidates to explore (not auto-proceeding)
- [ ] CONTEXT.md updated inline when new terms are resolved during grilling

## Anti-patterns

- **DO NOT** propose interfaces before two real adapters exist — one adapter = hypothetical seam
- **DO NOT** suggest refactors that contradict existing ADRs without explicitly surfacing the conflict
- **DO NOT** use code-level names (FooHandler, BarService) when domain terms exist — use CONTEXT.md vocabulary
- **DO NOT** skip the deletion test — every candidate must be validated against it
- **DO NOT** auto-proceed with refactoring after presenting candidates — user picks which to explore
