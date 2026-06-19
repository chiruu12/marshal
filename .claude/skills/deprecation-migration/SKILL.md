---
name: deprecation-migration
description: |
  Plan and execute deprecation and migration paths for APIs, dependencies, or patterns.
  Ensures nothing breaks during transition. Use when removing old code, upgrading
  dependencies, migrating between APIs, or when user mentions "deprecate", "migrate",
  "upgrade", "remove old", or "phase out".
---

# Deprecation and Migration

Remove old things without breaking anything. Every deprecation needs a migration path — a clear route from old to new that callers can follow incrementally. Never remove without replacing, and never replace without a transition period.

## When to Use

- Removing old API endpoints, functions, or modules
- Upgrading major dependency versions with breaking changes
- Migrating between services or patterns (e.g., REST → GraphQL)
- Phasing out deprecated library usage
- **NOT** for adding new features alongside old ones — that's just feature development
- **NOT** for deleting unused code — just delete it, no migration needed

## Process

### 1. Identify what's changing

Map the scope:

- What's being deprecated (old API, old pattern, old dependency)?
- What replaces it (new API, new pattern, new version)?
- Who depends on the old thing? (internal callers, external consumers, other services)

```bash
# Find all callers of the deprecated thing
grep -rn "oldFunction\|OldClass\|old-endpoint" src/ --include="*.ts" --include="*.py"
```

### 2. Design the migration path

Every caller must have a clear path from old → new:

| Migration Type | Strategy |
|---------------|----------|
| Internal function | Add new function, update callers one-by-one, delete old |
| Public API endpoint | Version the API, maintain both, set sunset date |
| Dependency upgrade | Update in isolated branch, fix breaking changes, verify tests |
| Pattern migration | Write codemod or provide find-replace instructions |

### 3. Mark deprecation

Make the old thing visibly deprecated:

```typescript
/** @deprecated Use newFunction() instead. Will be removed in v3.0. */
function oldFunction() { ... }
```

- Compiler warnings where possible
- Runtime warnings in dev mode
- Documentation updated to point to replacement

### 4. Migrate callers incrementally

Migrate one caller at a time. Each migration is its own commit that:

- Passes all tests
- Doesn't change behavior for end users
- Is independently revertable

### 5. Remove the deprecated code

Only after ALL callers are migrated:

- Verify zero usages remain: `grep -rn "oldFunction" src/`
- Remove the deprecated code
- Remove any compatibility shims
- Update documentation

## Common Rationalizations

| Shortcut | Why It Fails |
|----------|-------------|
| "Let's just remove it and fix what breaks" | You'll miss callers in tests, scripts, config files, and downstream consumers. The "fix what breaks" approach means production incidents. Incremental migration prevents all of them. |
| "We'll maintain both forever, it's fine" | Technical debt compounds. Two implementations means two things to test, two things to fix when bugs are found, two things to explain to new developers. Set a sunset date. |
| "Nobody uses the old API anymore" | Verify with data, not assumptions. Check logs, grep the codebase, check downstream consumers. "Nobody uses it" has caused more outages than any other assumption. |
| "The upgrade is small, we can do it in one PR" | Major version upgrades have cascading breakage. What looks like "one small change" in the changelog becomes 50 call sites that need updating, each with edge cases. Go incremental. |

## Red Flags

- Deprecated code with no replacement documented — callers don't know where to go
- No sunset date on deprecated APIs — they'll live forever
- "Big bang" migration PR that changes 100+ files — too risky, should be incremental
- Removing code without verifying zero remaining callers — breakage incoming

## Verification Checklist

- [ ] All callers of deprecated code identified
- [ ] Replacement exists and is documented
- [ ] Migration path is clear (caller knows exactly what to change)
- [ ] Each migration step passes tests independently
- [ ] Zero usages of deprecated code remain after migration
- [ ] Deprecated markers removed along with the code (no orphaned @deprecated tags)
- [ ] Documentation updated to reference only the new approach

## Anti-patterns

- **DO NOT** remove without replacing — every deprecation needs a migration path
- **DO NOT** "big bang" migrate everything in one PR — go caller by caller
- **DO NOT** maintain deprecated code indefinitely — set a sunset date and honor it
- **DO NOT** assume zero callers without verifying — grep, check logs, check downstream
