---
name: edit-article
description: |
  Edit and improve articles by restructuring sections as a DAG respecting information
  dependencies, then rewriting each section for clarity and flow. Max 240 characters per
  paragraph. Use when user wants to edit, revise, or improve an article draft.
---

# Edit Article

Restructure and tighten an article by treating information as a directed acyclic graph. Readers lose trust when an article references concepts it hasn't introduced yet. This skill ensures every section lands in the right order, then rewrites each one for maximum clarity with tight paragraphs.

## When to Use

- User has a draft article they want improved
- User says "edit this", "revise", "tighten this up", or "improve this article"
- An article feels disorganized or hard to follow
- **NOT** for writing from scratch — help them outline first, then use this after a draft exists
- **NOT** for copy-editing only (typos, grammar) — that's a simpler task

## Process

### 1. Map the information DAG

Read the article. For each section and key concept, identify:
- What prior knowledge does this section assume?
- What does this section introduce that later sections depend on?

The article's section order must respect these dependencies (topological sort).

### 2. Propose restructured sections

Present the proposed section order to the user:

```
Proposed structure:
1. {Section title} — introduces: {concepts} | depends on: nothing
2. {Section title} — introduces: {concepts} | depends on: Section 1
3. {Section title} — introduces: {concepts} | depends on: Sections 1, 2
```

Highlight any dependency violations in the original. **Wait for user confirmation before rewriting.**

### 3. Rewrite each section

For each section, rewrite to improve clarity, coherence, and flow:

- **Maximum 240 characters per paragraph** — forces concrete, scannable prose
- One idea per paragraph — if a paragraph covers two ideas, split it
- Cut filler: "it is important to note that" becomes nothing
- Cut hedging: "it seems like" becomes a direct statement
- Preserve the author's voice — tighten, don't replace

### 4. Present the rewrite

Show each rewritten section. Note what was cut, moved, or was missing.

## Common Rationalizations

| Shortcut | Why It Fails |
|----------|-------------|
| "The reader can figure out the order" | They can't. If section 3 uses a term from section 5, the reader re-reads or gives up. |
| "Short paragraphs feel choppy" | Choppy beats impenetrable. 240 chars forces you to say one thing well. If it feels choppy, fix the transitions. |
| "I need longer paragraphs for nuance" | Nuance comes from precise word choice, not paragraph length. A wall of text buries nuance. |
| "The structure is fine, just clean up the prose" | If dependencies are wrong, no amount of prose polish saves it. Structure first. |

## Red Flags

- Multiple sections reference concepts not yet introduced — the DAG has violations
- Paragraphs over 240 chars survive with justification "this one needs to be longer"
- Rewrite changes the author's argument rather than their prose
- User skips structural step and jumps to prose polish

## Verification Checklist

- [ ] Information dependency DAG mapped and section order respects it
- [ ] User confirmed proposed structure before rewriting
- [ ] Every paragraph is at most 240 characters
- [ ] Each paragraph expresses exactly one idea
- [ ] No section references concepts introduced later
- [ ] Author's voice and argument preserved

## Anti-patterns

- **DO NOT** rewrite without mapping dependencies first — structure before prose
- **DO NOT** exceed 240 characters per paragraph — the constraint is the point
- **DO NOT** change the author's argument — tighten what's there
- **DO NOT** skip user confirmation of structure — they may have ordering reasons you don't see
