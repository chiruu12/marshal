---
name: to-issues
description: |
  Break a plan, spec, or PRD into independently-grabbable GitHub issues using tracer-bullet
  vertical slices. Each slice cuts through ALL layers end-to-end. Supports HITL/AFK
  classification and dependency ordering. Use when user wants to convert a plan into issues,
  create implementation tickets, or break down work.
---

# To Issues

Break a plan into independently-grabbable GitHub issues using vertical slices (tracer bullets). Each issue is a thin end-to-end path through every integration layer, not a horizontal slice of one layer.

## When to Use

- A PRD, plan, or spec exists and needs to become actionable work items
- User says "break this into issues", "create tickets", or "convert to issues"
- A large feature needs to be parallelized across sessions or developers
- **NOT** when no plan exists yet — create one first (use `to-prd` or `grill-me`)
- **NOT** when user wants horizontal slices ("do all the backend, then frontend")

## Process

### 1. Gather context

Work from whatever is already in the conversation. If user passes a GitHub issue number, fetch it with `gh issue view <number>` (with comments).

### 2. Explore the codebase (optional)

If you haven't already, explore to understand current state.

### 3. Draft vertical slices

Break into **tracer bullet** issues. Each is a thin vertical slice through ALL integration layers end-to-end.

Slices may be **HITL** (requires human interaction) or **AFK** (can be implemented without human). Prefer AFK.

Rules:
- Each slice delivers a narrow but COMPLETE path through every layer (schema, API, UI, tests)
- A completed slice is demoable or verifiable on its own
- Prefer many thin slices over few thick ones

### 4. Quiz the user

Present proposed breakdown. For each slice: Title, Type (HITL/AFK), Blocked by, User stories covered.

Ask: granularity right? Dependencies correct? Merge or split any? HITL/AFK correct?

Iterate until approved.

### 5. Create the GitHub issues

Create in dependency order (blockers first) using `gh issue create`:

```markdown
## Parent
#<parent-issue-number>

## What to build
[End-to-end behavior description, not layer-by-layer]

## Acceptance criteria
- [ ] Criterion 1
- [ ] Criterion 2

## Blocked by
- Blocked by #<issue-number>
Or "None - can start immediately"
```

Do NOT close or modify any parent issue.

## Common Rationalizations

| Shortcut | Why It Fails |
|----------|-------------|
| "Let's do all the backend first, then frontend" | Horizontal slices hide integration bugs until the end. A vertical slice proves integration works early. |
| "This is too small to be its own issue" | If it's demoable and independently verifiable, it's an issue. Small issues get merged fast and unblock other work. |
| "We don't need user approval, the breakdown is obvious" | Obvious to you, not to the implementer. The quiz step catches wrong assumptions about dependencies. |
| "HITL is fine for most of these" | Every HITL issue is a potential multi-day delay. Convert to AFK by making the decision now. |

## Red Flags

- All slices are HITL — haven't made enough decisions upfront
- A single slice touches 5+ unrelated modules — too thick, split further
- Dependency chain more than 3 levels deep — look for ways to parallelize
- No acceptance criteria that can be verified independently

## Verification Checklist

- [ ] Every slice cuts through ALL relevant layers (not horizontal)
- [ ] Each slice is independently demoable or verifiable
- [ ] HITL vs AFK classification is correct
- [ ] User approved the breakdown before issues were created
- [ ] Issues created in dependency order with real issue numbers
- [ ] Acceptance criteria are specific and testable

## Anti-patterns

- **DO NOT** create horizontal slices ("backend issue", "frontend issue") — always vertical
- **DO NOT** create issues without user approval of the breakdown
- **DO NOT** mark slices as AFK if they require undecided architectural decisions
- **DO NOT** create vague acceptance criteria ("it works") — each must be verifiable
- **DO NOT** close or modify the parent issue
