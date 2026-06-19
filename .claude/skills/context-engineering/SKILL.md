---
name: context-engineering
description: |
  Optimize AI agent context for maximum effectiveness using a 5-level hierarchy.
  Use when setting up a new project for AI-assisted development, when Claude keeps
  losing context, when responses are inconsistent across sessions, or when you want
  to reduce token waste. Also use when asked about "context", "rules files", or
  "how to set up Claude for this project".
---

# Context Engineering

Structure project context so AI agents produce consistent, high-quality results without re-explanation every session. The 5-level hierarchy ensures the right information is available at the right time — always-loaded rules at the top, conversation-specific details at the bottom.

## When to Use

- Setting up a new project for AI-assisted development
- Claude keeps forgetting conventions or making the same mistakes
- Responses are inconsistent across sessions or workspaces
- Token usage is high because you're re-explaining context every time
- **NOT** when debugging a specific bug — use `triage-issue` instead
- **NOT** for one-off tasks that won't repeat — just explain in the conversation

## Process

### 1. Audit existing context

Check what context already exists in the project:

```bash
# Check for existing context files
ls -la CLAUDE.md .claude/settings.json .claude/rules/ 2>/dev/null
find . -name "CONTEXT.md" -o -name "*.rules.md" 2>/dev/null
```

### 2. Apply the 5-level hierarchy

| Level | What | When Loaded | Examples |
|-------|------|-------------|----------|
| **1. Rules files** | Non-negotiable constraints | Always (every request) | CLAUDE.md, .claude/rules/*.md |
| **2. Specs/Plans** | Current feature context | When referenced | PRDs, design docs, plan files |
| **3. Source code** | Implementation reality | When files are read | The actual codebase |
| **4. Error output** | Runtime feedback | During debugging | Test failures, stack traces, lint errors |
| **5. Conversation** | Session-specific context | Current session only | User messages, clarifications |

### 3. Write Level 1 — Rules files

CLAUDE.md and .claude/rules/ are always in context. They should contain:

- **Architecture decisions** — "Routes → Services → CRUD → Models"
- **Conventions** — naming, file structure, import patterns
- **Boundaries** — "never import from X in Y", "always use Z for database access"
- **Tool commands** — how to run tests, lint, build

Keep these concise — every token here is loaded on every request.

### 4. Write Level 2 — Specs and plans

For active features, create spec files that capture:

- What the feature does (user perspective)
- Key decisions already made (so Claude doesn't re-litigate)
- Acceptance criteria
- Out of scope

Link from CLAUDE.md: "For feature X, see `docs/specs/feature-x.md`"

### 5. Optimize for token efficiency

- Move detailed reference material to files Claude reads on-demand (Level 3)
- Keep Level 1 rules under 2000 words total
- Use progressive disclosure — summary in CLAUDE.md, details in linked files
- Remove stale context — outdated rules cause confusion, not just waste

### 6. Verify context effectiveness

Test that the context is working by asking Claude to perform a common task without any additional explanation. If it follows conventions correctly, the context is sufficient.

## Common Rationalizations

| Shortcut | Why It Fails |
|----------|-------------|
| "I'll just explain things in the conversation each time" | Conversation context is ephemeral. You're paying tokens every session to re-teach what should be in Level 1. It also drifts — you'll explain differently each time. |
| "I'll put everything in CLAUDE.md so it's always available" | CLAUDE.md is loaded on EVERY request. A 5000-word CLAUDE.md burns tokens even for simple questions. Use progressive disclosure — summary at Level 1, details at Level 3. |
| "The code is self-documenting, no rules needed" | Code shows WHAT exists, not WHY it's that way or WHAT to do next time. Without explicit rules, Claude will infer conventions from whatever code it reads first — which may be the exception, not the rule. |
| "I'll add context as issues come up" | Reactive context accumulates as patches. Every 2-3 months, audit and restructure. Proactive context engineering prevents the drift in the first place. |

## Red Flags

- CLAUDE.md over 3000 words — too much always-loaded context, burning tokens on every request
- Rules contradict each other — Claude will follow whichever it sees last, which is non-deterministic
- Stale rules reference deleted files or patterns — causes confusion and hallucination
- No Level 2 specs for active features — Claude fills gaps with assumptions

## Verification Checklist

- [ ] CLAUDE.md under 2000 words and contains only non-negotiable constraints
- [ ] .claude/rules/ files cover common workflows without being overly prescriptive
- [ ] Active features have Level 2 spec files linked from CLAUDE.md
- [ ] Claude follows project conventions without re-explanation on a fresh session
- [ ] No contradictions between rules files
- [ ] Stale context removed (no references to deleted files/patterns)

## Anti-patterns

- **DO NOT** put everything in CLAUDE.md — use progressive disclosure across all 5 levels
- **DO NOT** write rules about things that are obvious from the code — only non-obvious constraints
- **DO NOT** reference specific file paths in rules (they go stale) — describe patterns and conventions instead
- **DO NOT** skip the audit step — existing context may contradict what you're about to add
