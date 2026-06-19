---
name: spec-driven-development
description: |
  Enforce writing a spec before coding. Captures requirements, decisions, and acceptance
  criteria in a durable document that guides implementation. Use when starting a new
  feature, when scope is unclear, when multiple people will work on the same feature,
  or when user says "spec", "requirements", or "what should we build".
---

# Spec-Driven Development

Write the spec before the code. A spec forces you to think through what you're building, why, and how you'll know it works — before implementation decisions constrain your thinking. It's cheaper to change words than code.

## When to Use

- Starting a new feature or significant change
- Scope is unclear or disputed
- Multiple people (or agents) will work on the feature
- User says "spec", "requirements", or "what should we build"
- **NOT** for bug fixes with clear reproduction steps — use `triage-issue`
- **NOT** for one-line changes or obvious refactors — just do them

## Process

### 1. Gather intent

Ask the user (max 3 questions):

- "What problem does this solve for the user?"
- "What's the simplest version that delivers value?"
- "What's explicitly NOT included?"

### 2. Write the spec

Create a spec file at the location the project uses (check CLAUDE.md for conventions, default to `docs/specs/`).

**Spec structure:**

```markdown
# Feature Name

## Problem
[One paragraph: what's wrong or missing today]

## Solution
[One paragraph: what we're building, from the user's perspective]

## Acceptance Criteria
- [ ] [Observable behavior 1]
- [ ] [Observable behavior 2]
- [ ] [Observable behavior 3]

## Decisions
- [Decision 1]: [choice] because [reason]
- [Decision 2]: [choice] because [reason]

## Out of Scope
- [Thing we're NOT doing and why]

## Open Questions
- [Anything unresolved that blocks implementation]
```

### 3. Review with user

Present the spec. Ask:

> "Does this capture what you want? Anything missing, wrong, or out of scope?"

Iterate until the user approves.

### 4. Resolve open questions

If open questions exist, resolve them before implementation starts. Each resolved question becomes a decision.

### 5. Hand off to implementation

The spec is the contract. Implementation should satisfy every acceptance criterion. Link the spec from any related issues or PRs.

## Common Rationalizations

| Shortcut | Why It Fails |
|----------|-------------|
| "I know what to build, I'll just start coding" | You know what you THINK you'll build. Without a spec, scope creeps, edge cases surprise you mid-implementation, and you build something nobody actually wanted. |
| "The spec will be outdated by the time I finish" | A spec captures intent and decisions. Code changes; intent shouldn't. If the spec is outdated, either update it or you've drifted from the original goal. |
| "Specs slow me down" | Specs take 10-20 minutes. Rework from misalignment takes days. The spec IS the fast path — it catches "wait, that's not what I meant" before you've written 500 lines. |
| "I'll document it after I build it" | Post-hoc docs describe what you built, not what you should have built. They can't catch misalignment because the misaligned code already exists. |

## Red Flags

- Spec has no "Out of Scope" section — scope will creep because nothing is explicitly excluded
- Acceptance criteria use vague language ("works correctly", "handles edge cases") — not testable
- Open questions persist into implementation — decisions are being deferred, not made
- Spec describes implementation, not behavior — it's a design doc, not a spec

## Verification Checklist

- [ ] Problem statement describes user pain, not implementation details
- [ ] Solution is described from user perspective, not code perspective
- [ ] Every acceptance criterion is observable and testable
- [ ] At least one "Out of Scope" item exists
- [ ] All open questions resolved before implementation starts
- [ ] User approved the spec

## Anti-patterns

- **DO NOT** describe implementation in the spec — describe behavior and outcomes
- **DO NOT** leave open questions unresolved during implementation — they become assumptions that diverge from intent
- **DO NOT** write acceptance criteria you can't test — "works correctly" is not testable, "returns 404 for deleted resources" is
- **DO NOT** skip the user review — the spec exists to align, not to document your assumptions
