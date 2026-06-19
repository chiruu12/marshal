---
name: domain-model
description: Grilling session that challenges your plan against the existing domain model, sharpens terminology, and updates documentation (CONTEXT.md, ADRs) inline as decisions crystallise. Use when user wants to stress-test a plan against their project's language and documented decisions.
disable-model-invocation: true
---

Interview me relentlessly about every aspect of this plan until we reach a shared understanding. Walk down each branch of the design tree, resolving dependencies between decisions one-by-one. For each question, provide your recommended answer.

Ask the questions one at a time, waiting for feedback on each question before continuing.

If a question can be answered by exploring the codebase, explore the codebase instead.

## Domain awareness

During codebase exploration, also look for existing documentation:

### File structure

Most repos have a single context:

```
/
├── CONTEXT.md
├── docs/
│   └── adr/
│       ├── 0001-event-sourced-orders.md
│       └── 0002-postgres-for-write-model.md
└── src/
```

If a `CONTEXT-MAP.md` exists at the root, the repo has multiple contexts. The map points to where each one lives:

```
/
├── CONTEXT-MAP.md
├── docs/
│   └── adr/                          ← system-wide decisions
├── src/
│   ├── ordering/
│   │   ├── CONTEXT.md
│   │   └── docs/adr/                 ← context-specific decisions
│   └── billing/
│       ├── CONTEXT.md
│       └── docs/adr/
```

Create files lazily — only when you have something to write. If no `CONTEXT.md` exists, create one when the first term is resolved. If no `docs/adr/` exists, create it when the first ADR is needed.

## During the session

### Challenge against the glossary

When the user uses a term that conflicts with the existing language in `CONTEXT.md`, call it out immediately. "Your glossary defines 'cancellation' as X, but you seem to mean Y — which is it?"

### Sharpen fuzzy language

When the user uses vague or overloaded terms, propose a precise canonical term. "You're saying 'account' — do you mean the Customer or the User? Those are different things."

### Discuss concrete scenarios

When domain relationships are being discussed, stress-test them with specific scenarios. Invent scenarios that probe edge cases and force the user to be precise about the boundaries between concepts.

### Cross-reference with code

When the user states how something works, check whether the code agrees. If you find a contradiction, surface it: "Your code cancels entire Orders, but you just said partial cancellation is possible — which is right?"

### Update CONTEXT.md inline

When a term is resolved, update `CONTEXT.md` right there. Don't batch these up — capture them as they happen. Use the format in [CONTEXT-FORMAT.md](./CONTEXT-FORMAT.md).

Don't couple `CONTEXT.md` to implementation details. Only include terms that are meaningful to domain experts.

### Offer ADRs sparingly

Only offer to create an ADR when all three are true:

1. **Hard to reverse** — the cost of changing your mind later is meaningful
2. **Surprising without context** — a future reader will wonder "why did they do it this way?"
3. **The result of a real trade-off** — there were genuine alternatives and you picked one for specific reasons

If any of the three is missing, skip the ADR. Use the format in [ADR-FORMAT.md](./ADR-FORMAT.md).

## Common Rationalizations

| Shortcut | Why It Fails |
|----------|-------------|
| "We all know what 'account' means, no need to define it" | You don't. "Account" means auth identity to the backend team, billing entity to finance, and customer profile to the frontend. Undefined terms cause silent misalignment that surfaces as bugs. |
| "Let's skip CONTEXT.md, the code is the documentation" | Code tells you what IS, not what SHOULD BE. When code and intent diverge, without CONTEXT.md you can't tell which is wrong. Domain terms in code also drift without a glossary anchoring them. |
| "We don't need an ADR for this, it's obvious" | If all three criteria are met (hard to reverse, surprising, real trade-off), it's not obvious — it just feels obvious NOW because you have context. Future you won't. |
| "The user seems confident, no need to challenge their terminology" | Confident doesn't mean precise. Challenging fuzzy language now prevents bugs that surface weeks later as "I thought X meant Y." |

## Red Flags

- User uses the same term for different concepts in different sentences — unresolved ambiguity
- CONTEXT.md hasn't been updated during the session despite multiple term clarifications — decisions are being lost
- ADRs are being created for every minor decision — threshold is too low, cluttering the record
- User gets defensive about terminology challenges — reframe as "let's make sure the code matches what we mean"

## Verification Checklist

- [ ] Every resolved term is captured in CONTEXT.md (not batched for later)
- [ ] Vague terms were challenged and sharpened to precise canonical terms
- [ ] Code was cross-referenced against stated behavior — contradictions surfaced
- [ ] ADRs only created when all three criteria are met
- [ ] User can explain domain relationships using consistent terminology

## Anti-patterns

- **DO NOT** batch CONTEXT.md updates for the end — capture terms as they're resolved
- **DO NOT** create ADRs for every decision — only for hard-to-reverse, surprising, genuine trade-offs
- **DO NOT** accept fuzzy terms without challenge — "account", "service", "process" need precision
- **DO NOT** couple CONTEXT.md to implementation details — only include terms meaningful to domain experts
