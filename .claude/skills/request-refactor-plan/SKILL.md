---
name: request-refactor-plan
description: |
  Create a detailed refactor plan with tiny commits via user interview, then file as a
  GitHub issue. Interviews about the problem, explores alternatives, verifies test coverage,
  and breaks implementation into Martin Fowler-style small steps. Use when user wants to plan
  a refactor, create a refactoring RFC, or break a refactor into safe incremental steps.
---

# Request Refactor Plan

Create a detailed, safe refactor plan through interview, then file it as a GitHub issue. Refactors modify working code without changing behavior — the risk isn't in the code, it's in the plan. This skill ensures every step leaves the codebase working.

## When to Use

- User wants to plan a refactor before starting it
- User says "refactor plan", "I want to restructure this", or "this needs cleaning up"
- User wants to create an RFC for a refactor someone else will implement
- **NOT** when adding new functionality — use `to-prd`
- **NOT** when the change is a single rename — just do it
- **NOT** when exploring whether a refactor is needed — use `improve-codebase-architecture` first

## Process

### 1. Gather the problem

Ask the user for a detailed description of the problem and any potential solutions.

### 2. Explore the repo

Verify their assertions. Look at the actual code — don't take claims at face value.

### 3. Present alternatives

Ask whether they've considered other options. Present alternatives including "do nothing" if the status quo is tolerable.

### 4. Interview about implementation

Be extremely detailed. Cover: what changes, how callers are affected, intermediate states, how you verify each step.

### 5. Scope the work

Hammer out what you plan to change and what you plan NOT to change. Explicit exclusions prevent scope creep.

### 6. Assess test coverage

Check existing tests. If insufficient, ask the user their testing plans. Refactoring without tests is refactoring without a safety net.

### 7. Break into tiny commits

Each commit must: leave the codebase working, be independently reviewable, have a clear single purpose.

Martin Fowler: "make each refactoring step as small as possible."

### 8. File as GitHub issue

Create with `gh issue create`:

```markdown
## Problem Statement
[Developer's problem, from their perspective]

## Solution
[The proposed restructuring]

## Commits
[Detailed plan — tiny commits, each leaving codebase working]

## Decision Document
[WHY decisions were made — modules, interfaces, architecture. No file paths.]

## Testing Decisions
[What gets tested, how, prior art in codebase]

## Out of Scope
[Explicit exclusions]
```

## Common Rationalizations

| Shortcut | Why It Fails |
|----------|-------------|
| "I'll just refactor as I go, no plan needed" | Unplanned refactors expand scope silently. You start with "rename this" and end with "rewrite the data layer." |
| "Each commit doesn't need to be independently working" | It does. Broken intermediate states mean you can't bisect, can't revert one step, can't pause halfway. |
| "We don't have tests, but I'll be careful" | You won't be careful enough. Add tests FIRST as a separate commit, then refactor. |
| "This is too interconnected for small steps" | Break it into phases: extract, then restructure, then clean up. Each phase is tiny commits. |
| "Let's skip alternatives, I know what I want" | "Do nothing" is always an option. Sometimes the refactor costs more than living with the debt. |

## Red Flags

- No tests exist and user says "we'll add them later" — tests come first
- A single planned commit touches 5+ files — too large, split further
- Out of Scope is empty — every refactor has boundaries
- User can't articulate WHY the current code is a problem

## Verification Checklist

- [ ] Problem clearly stated from developer's perspective
- [ ] Alternatives explored (including "do nothing")
- [ ] Scope explicit — what changes AND what does not
- [ ] Test coverage assessed — gaps addressed in plan
- [ ] Every commit leaves codebase working
- [ ] Commits small enough to be independently reviewable
- [ ] Out of Scope has explicit exclusions
- [ ] GitHub issue filed successfully

## Anti-patterns

- **DO NOT** skip alternatives — "do nothing" is always worth evaluating
- **DO NOT** plan commits that leave codebase broken
- **DO NOT** include file paths in the GitHub issue — they go stale
- **DO NOT** plan refactors in untested areas without addressing the test gap first
- **DO NOT** let scope expand during interview — new scope goes to "Out of Scope" or a separate plan
