---
name: design-an-interface
description: |
  Generate multiple radically different interface designs for a module using parallel
  sub-agents, then compare on simplicity, depth, and correctness of use. Based on "Design
  It Twice" from A Philosophy of Software Design. Use when user wants to design an API,
  explore interface options, compare module shapes, or mentions "design it twice".
---

# Design an Interface

Based on "Design It Twice" from "A Philosophy of Software Design": your first idea is unlikely to be the best. Generate multiple radically different designs, then compare. The value isn't in any single design — it's in the contrast that reveals which trade-offs actually matter.

## When to Use

- User needs to design an API, module interface, or public contract
- User says "design it twice", "explore interface options", or "what are my options?"
- A module shape is being debated and multiple approaches seem viable
- **NOT** when the interface already exists (use `request-refactor-plan`)
- **NOT** when user needs implementation (use `tdd`)
- **NOT** when there's only one reasonable approach — don't force exploration for its own sake

## Process

### 1. Gather Requirements

Before designing, understand:

- [ ] What problem does this module solve?
- [ ] Who are the callers? (other modules, external users, tests)
- [ ] What are the key operations?
- [ ] Any constraints? (performance, compatibility, existing patterns)
- [ ] What should be hidden inside vs exposed?

### 2. Generate Designs (Parallel Sub-Agents)

Spawn 3+ sub-agents simultaneously. Each must produce a **radically different** approach:

- Agent 1: "Minimize method count — aim for 1-3 methods max"
- Agent 2: "Maximize flexibility — support many use cases"
- Agent 3: "Optimize for the most common case"
- Agent 4: "Take inspiration from [specific paradigm/library]"

Each produces: interface signature, usage example, what it hides, trade-offs.

### 3. Present Designs

Show each design with signature, usage examples, and what it hides. Present sequentially so user can absorb each before comparison.

### 4. Compare Designs

Compare on:
- **Interface simplicity**: fewer methods, simpler params
- **Depth**: small interface hiding significant complexity (good) vs large interface with thin implementation (bad)
- **Ease of correct use** vs **ease of misuse**
- **General-purpose vs specialized**: flexibility vs focus

Discuss in prose. Highlight where designs diverge most.

### 5. Synthesize

The best design often combines insights from multiple options. Ask:
- "Which design best fits your primary use case?"
- "Any elements from other designs worth incorporating?"

## Common Rationalizations

| Shortcut | Why It Fails |
|----------|-------------|
| "I already know the right design" | You don't know it's right until you've seen what you're giving up. Alternatives reveal trade-offs invisible from a single vantage point. |
| "These designs are too different to compare" | That's the point. Similar designs teach nothing. Radical difference exposes the fundamental tension. |
| "Let's just go with the simplest one" | Simplest interface doesn't mean best. A 1-method interface that forces complex caller setup has merely moved complexity. |
| "We can always change the interface later" | Once callers depend on your interface, changing it requires migrating all of them. Get it right before the first caller commits. |

## Red Flags

- Sub-agents produce variations on the same theme — enforce radical difference
- Comparison focuses on "which is easiest to implement" — caller experience matters, not implementation effort
- User picks a design without articulating WHY
- Synthesis adds methods from every design — that's a kitchen sink, not synthesis

## Verification Checklist

- [ ] Requirements gathered before any design work
- [ ] 3+ designs generated, each radically different
- [ ] Each design includes: signature, usage example, what it hides, trade-offs
- [ ] Designs compared on simplicity, depth, flexibility, and ease of correct use
- [ ] User articulated which trade-offs they're accepting

## Anti-patterns

- **DO NOT** let sub-agents produce similar designs — enforce divergent constraints
- **DO NOT** skip comparison — the value is in contrast
- **DO NOT** implement anything — this is purely about interface shape
- **DO NOT** evaluate based on implementation effort — what matters is caller experience
- **DO NOT** recommend a winner — present trade-offs and let the user decide
