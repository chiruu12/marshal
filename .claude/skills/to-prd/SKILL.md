---
name: to-prd
description: |
  Turn the current conversation context into a PRD and submit it as a GitHub issue.
  Synthesizes what's already known — does NOT interview the user. Extracts deep modules,
  extensive user stories, and implementation decisions. Use when user wants to create a
  PRD from the current context.
---

# To PRD

Synthesize the current conversation context and codebase understanding into a Product Requirements Document, then file it as a GitHub issue. This skill does NOT interview the user — it works from what's already been discussed.

## When to Use

- A conversation has established enough context about a feature to document it
- User says "create a PRD", "write this up", or "file this as an issue"
- User wants to hand off implementation to someone else
- **NOT** when the feature is still being explored — use `grill-me` first
- **NOT** for refactors — use `request-refactor-plan`

## Process

### 1. Explore the repo

Understand the current state of the codebase, if you haven't already.

### 2. Identify deep modules

Sketch the major modules needed. Look for opportunities to extract **deep modules** — significant functionality behind a simple, testable interface.

Check with the user that modules match expectations and which need tests.

### 3. Write and file the PRD

Submit via `gh issue create` using this template:

```markdown
## Problem Statement
[User's problem, from user's perspective]

## Solution
[Solution from user's perspective]

## User Stories
1. As an <actor>, I want a <feature>, so that <benefit>
[Extensive list covering all aspects]

## Implementation Decisions
- Modules built/modified
- Interface changes
- Architectural decisions
- Schema changes, API contracts
[No file paths or code snippets — they go stale]

## Testing Decisions
- What makes a good test (external behavior only)
- Which modules will be tested
- Prior art in the codebase

## Out of Scope
[Explicit exclusions]

## Further Notes
[Optional additional context]
```

## Common Rationalizations

| Shortcut | Why It Fails |
|----------|-------------|
| "The conversation has all the context, I don't need a PRD" | Conversations are linear and full of tangents. A PRD forces you to organize decisions into a structure someone else can act on. |
| "I'll file a short issue and fill in details later" | You won't. The context is freshest now. A vague issue becomes a blocking question in implementation. |
| "User stories are busywork" | User stories expose gaps. If you can't write "As X, I want Y, so that Z" for a behavior, you haven't thought through who needs it and why. |
| "Implementation decisions belong in code" | Code shows WHAT was built. A PRD captures WHY decisions were made and WHAT was deliberately excluded. |

## Red Flags

- PRD has fewer than 8 user stories — being too high-level
- Implementation decisions include file paths — these go stale immediately
- Out of Scope section is empty — scope will creep
- Problem statement describes the solution, not the problem

## Verification Checklist

- [ ] Problem statement describes user pain, not technical debt
- [ ] User stories are extensive (8+ minimum)
- [ ] Implementation decisions capture WHY, not just WHAT
- [ ] Out of scope is explicit
- [ ] No file paths or code snippets in issue body
- [ ] Deep modules identified
- [ ] GitHub issue created successfully

## Anti-patterns

- **DO NOT** interview the user — this skill synthesizes existing context
- **DO NOT** include file paths or code snippets — they go stale
- **DO NOT** write vague user stories ("As a user, I want it to work")
- **DO NOT** skip module identification — it's where architectural quality comes from
