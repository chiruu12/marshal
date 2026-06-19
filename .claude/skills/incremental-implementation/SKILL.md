---
name: incremental-implementation
description: |
  Build features in small, always-compilable increments. Each step compiles, tests pass,
  and the system remains deployable. Use when implementing features that span multiple
  files, when working on a shared codebase, or when user says "incremental", "step by
  step", or "keep it working". Not for one-file changes.
---

# Incremental Implementation

Build features in steps where the system compiles and tests pass after every change. Never leave the codebase in a broken state — not even for "just a minute." This discipline prevents integration nightmares, enables early feedback, and makes every intermediate state deployable.

## When to Use

- Implementing features that touch multiple files or modules
- Working on a shared codebase where others may pull at any time
- Changes that will take more than one commit
- **NOT** for single-file changes that are atomic by nature
- **NOT** for exploratory prototyping where you'll throw the code away

## Process

### 1. Plan the increment sequence

Before coding, identify the order of changes that keeps the system working at each step:

- What can be added without changing existing behavior? (new files, new functions, new types)
- What existing code needs modification? (do this last)
- What's the smallest change that demonstrates progress?

**Rule: additions before modifications.** Add new code first, then wire it in.

### 2. Execute each increment

For each step:

```
1. Make the change (smallest unit that's meaningful)
2. Verify: does it compile? do tests pass?
3. If yes: commit. If no: fix before moving on.
```

### 3. Use feature flags for incomplete features

If a feature isn't ready for users but you want to merge intermediate work:

```typescript
if (featureFlags.newCheckout) {
  // new behavior
} else {
  // existing behavior
}
```

This lets you deploy incomplete work without exposing it.

### 4. Keep the diff small

Each commit should be reviewable in under 5 minutes. If it's larger:

- Split it into multiple increments
- Each increment should be independently understandable

## Common Rationalizations

| Shortcut | Why It Fails |
|----------|-------------|
| "I'll fix the tests after I finish the whole feature" | Broken tests for days means you're flying blind. When you finally run them, 15 failures compound and you can't tell which change broke what. Fix as you go. |
| "It's faster to do it all at once" | It feels faster. But "all at once" means one giant PR, longer review cycles, harder debugging, merge conflicts with teammates, and no rollback points. Small increments ship faster in practice. |
| "This change can't be split up, it's all-or-nothing" | Almost nothing is truly atomic. You can add new code without wiring it, add tests for future behavior, create interfaces before implementations, migrate one caller at a time. Look harder. |
| "Feature flags are overengineering for this" | Feature flags let you merge daily instead of branching for weeks. They're not overengineering — they're the mechanism that makes incremental delivery possible for user-facing features. |

## Red Flags

- A branch has been open for more than 3 days without merging — increments are too large
- Tests have been red for multiple commits — the "always-compilable" rule is being violated
- Single PR has 1000+ lines changed — this wasn't incremental
- New code can only be tested after the full feature is complete — missing intermediate test points

## Verification Checklist

- [ ] System compiles after every commit
- [ ] Tests pass after every commit
- [ ] Each commit is independently reviewable (under 300 lines ideally)
- [ ] No commit introduces dead code without a clear next step
- [ ] Feature flags used for incomplete user-facing features
- [ ] Additions before modifications pattern followed

## Anti-patterns

- **DO NOT** commit broken tests with "will fix later" — fix them now or revert the change
- **DO NOT** make one giant commit at the end of a feature — if you can't split it, you didn't plan incrementally
- **DO NOT** leave feature flags in permanently — remove them once the feature ships or is abandoned
- **DO NOT** skip the compile/test check between increments — the whole point is continuous validity
