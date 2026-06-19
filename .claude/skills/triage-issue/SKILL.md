---
name: triage-issue
description: Triage a bug or issue by exploring the codebase to find root cause, then create a GitHub issue with a TDD-based fix plan. Use when user reports a bug, wants to file an issue, mentions "triage", or wants to investigate and plan a fix for a problem.
---

# Triage Issue

Investigate a reported problem, find its root cause, and create a GitHub issue with a TDD fix plan. This is a mostly hands-off workflow - minimize questions to the user.

## Process

### 1. Capture the problem

Get a brief description of the issue from the user. If they haven't provided one, ask ONE question: "What's the problem you're seeing?"

Do NOT ask follow-up questions yet. Start investigating immediately.

### 2. Explore and diagnose

Use the Agent tool with subagent_type=Explore to deeply investigate the codebase. Your goal is to find:

- **Where** the bug manifests (entry points, UI, API responses)
- **What** code path is involved (trace the flow)
- **Why** it fails (the root cause, not just the symptom)
- **What** related code exists (similar patterns, tests, adjacent modules)

Look at:
- Related source files and their dependencies
- Existing tests (what's tested, what's missing)
- Recent changes to affected files (`git log` on relevant files)
- Error handling in the code path
- Similar patterns elsewhere in the codebase that work correctly

### 3. Identify the fix approach

Based on your investigation, determine:

- The minimal change needed to fix the root cause
- Which modules/interfaces are affected
- What behaviors need to be verified via tests
- Whether this is a regression, missing feature, or design flaw

### 4. Design TDD fix plan

Create a concrete, ordered list of RED-GREEN cycles. Each cycle is one vertical slice:

- **RED**: Describe a specific test that captures the broken/missing behavior
- **GREEN**: Describe the minimal code change to make that test pass

Rules:
- Tests verify behavior through public interfaces, not implementation details
- One test at a time, vertical slices (NOT all tests first, then all code)
- Each test should survive internal refactors
- Include a final refactor step if needed
- **Durability**: Only suggest fixes that would survive radical codebase changes. Describe behaviors and contracts, not internal structure. Tests assert on observable outcomes (API responses, UI state, user-visible effects), not internal state. A good suggestion reads like a spec; a bad one reads like a diff.

### 5. Create the GitHub issue

Create a GitHub issue using `gh issue create` with the template below. Do NOT ask the user to review before creating - just create it and share the URL.

<issue-template>

## Problem

A clear description of the bug or issue, including:
- What happens (actual behavior)
- What should happen (expected behavior)
- How to reproduce (if applicable)

## Root Cause Analysis

Describe what you found during investigation:
- The code path involved
- Why the current code fails
- Any contributing factors

Do NOT include specific file paths, line numbers, or implementation details that couple to current code layout. Describe modules, behaviors, and contracts instead. The issue should remain useful even after major refactors.

## TDD Fix Plan

A numbered list of RED-GREEN cycles:

1. **RED**: Write a test that [describes expected behavior]
   **GREEN**: [Minimal change to make it pass]

2. **RED**: Write a test that [describes next behavior]
   **GREEN**: [Minimal change to make it pass]

...

**REFACTOR**: [Any cleanup needed after all tests pass]

## Acceptance Criteria

- [ ] Criterion 1
- [ ] Criterion 2
- [ ] All new tests pass
- [ ] Existing tests still pass

</issue-template>

After creating the issue, print the issue URL and a one-line summary of the root cause.

## Common Rationalizations

| Shortcut | Why It Fails |
|----------|-------------|
| "I'll skip the codebase exploration, the user described the problem clearly" | Users describe symptoms, not root causes. Without tracing the code path, the issue will describe the symptom and the fix plan will target the wrong layer. |
| "Let me ask the user a bunch of questions to understand better" | This is a hands-off workflow. Ask ONE question max, then investigate. The codebase has the answers — the user reported what they saw, your job is to find why. |
| "The TDD plan can have broad tests, we'll refine later" | Each RED-GREEN cycle must be a specific, vertical slice. Broad tests like "the feature works" are untestable. Describe exact behaviors: "returns 404 when resource is deleted." |
| "I'll include the file paths in the issue so developers know where to look" | File paths go stale after refactors. Describe modules, behaviors, and contracts instead. The issue should remain useful even after major restructuring. |

## Red Flags

- Issue body references specific file paths or line numbers — will go stale
- TDD plan has only one big RED-GREEN cycle — not enough granularity for vertical slices
- Root cause analysis describes symptoms, not causes — "it fails" vs "the validation skips empty strings"
- Multiple follow-up questions asked to the user — this should be investigation-heavy, not interview-heavy

## Verification Checklist

- [ ] Root cause identified (not just symptom described)
- [ ] TDD fix plan has 2+ specific RED-GREEN cycles
- [ ] Each test in the plan describes observable behavior, not implementation details
- [ ] Issue body has no file paths or line numbers
- [ ] Issue created via `gh issue create` and URL shared
- [ ] Acceptance criteria are specific and checkable

## Anti-patterns

- **DO NOT** ask more than one clarifying question — investigate the codebase instead
- **DO NOT** include file paths or line numbers in the issue — describe behaviors and contracts
- **DO NOT** write tests that reference internal state — test through public interfaces
- **DO NOT** skip the root cause analysis — symptom-level issues lead to symptom-level fixes
