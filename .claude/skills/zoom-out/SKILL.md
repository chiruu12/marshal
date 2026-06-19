---
name: zoom-out
description: >
  Step back from a specific problem to map the broader system context.
  Identifies upstream/downstream dependencies, non-obvious connections, and
  risks of local optimization. Use when user feels lost in details, asks
  "how does this fit in", wants a system map, or says "zoom out".
---

# Zoom Out

Steps back from the current focus area to map the broader system — upstream dependencies, downstream consumers, non-obvious connections, and interaction patterns. Local optimization without system context creates solutions that work in isolation but break the whole. The cheapest time to find these conflicts is before writing code.

## When to Use

- User feels lost in implementation details and needs the bigger picture
- Starting work on a module without understanding what depends on it
- User says "zoom out", "how does this fit in", "what's the bigger picture"
- Before refactoring — to see what will break
- Debugging a problem that might originate in a different module
- **NOT** when user wants to dive deeper into specifics — that's the opposite direction
- **NOT** for greenfield projects with no existing system to map

## Process

### 1. Identify the focus point

Determine what the user is currently looking at:

> "What are you focused on right now? Give me the file/module/feature."

### 2. Map one layer up

Find immediate callers and consumers:

```bash
# Who calls this?
grep -rn "import.*from.*<module>" src/
grep -rn "<function-name>" src/ --include="*.ts"
```

Identify:
- **Upstream**: who calls this
- **Downstream**: what this calls
- **Data flow**: inputs/outputs at the boundary

### 3. Map the interaction boundary

For each connection, document:
- What contract exists (types, APIs, shared state)
- What assumptions are baked in
- What happens if this module changes behavior

```
[Upstream A] --calls--> [FOCUS MODULE] --calls--> [Downstream X]
[Upstream B] --reads-->                 --writes-> [Downstream Y]
```

### 4. Surface non-obvious connections

Look for hidden coupling:
- Shared database tables or state
- Event buses / pub-sub channels
- Implicit ordering dependencies
- Shared configuration or environment variables
- Cache invalidation dependencies

### 5. Identify local optimization risks

For the user's intended change:
- Will this change the contract with upstream callers?
- Will downstream consumers still work?
- Are there performance implications at system level?
- Does this duplicate logic that exists elsewhere?

### 6. Present the map

Deliver:
1. Visual dependency diagram (mermaid or ASCII)
2. Key contracts at each boundary
3. Risks/concerns for the intended change
4. Suggested next steps (files to read, tests to check)

## Common Rationalizations

| Shortcut | Why It Fails |
|----------|-------------|
| "I already know the system, I'll skip the map" | You know what you've worked on, not what changed since. A 2-minute map catches surprises that cost hours. |
| "This module is isolated, nothing depends on it" | Almost nothing is truly isolated. Shared DB, shared types, shared config — coupling hides in places code search doesn't reveal. |
| "I'll just fix this and see if tests catch regressions" | Tests cover known cases. Zoom-out reveals UNKNOWN dependencies with no test coverage. |
| "The system diagram is in the docs" | Docs rot. Code is truth. Zoom-out maps what exists NOW. |

## Red Flags

- User is about to change a function called by 10+ files without knowing expectations
- Module has no tests but many consumers (high risk, low safety net)
- Shared mutable state connected to the focus area
- Recent changes to adjacent modules that might conflict

## Verification Checklist

- [ ] Upstream callers identified (grep confirms who imports/calls)
- [ ] Downstream dependencies identified (what the focus module calls)
- [ ] Non-obvious connections surfaced (shared state, events, config)
- [ ] Visual diagram produced (mermaid or ASCII)
- [ ] At least one risk articulated for the intended change
- [ ] Specific files or tests recommended for the user to examine next

## Anti-patterns

- **DO NOT** produce diagrams from memory or assumption — ground in actual code search
- **DO NOT** go more than 2 layers deep — zoom-out is one level up, not full architecture doc
- **DO NOT** just list files without explaining contracts — value is in RELATIONSHIPS
- **DO NOT** skip the risks step — a map without concerns doesn't change behavior
- **DO NOT** zoom out when user needs to zoom IN — answer specific questions directly
