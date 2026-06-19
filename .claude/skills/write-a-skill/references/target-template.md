# Target Skill Template

Use this as the structural reference when writing any new skill. Every section is annotated with its purpose and when it's optional.

---

## Full Template

```markdown
---
name: skill-name
description: |
  [What it does — one sentence]. Use when [trigger phrase 1], [trigger phrase 2],
  or [trigger phrase 3]. Also handles [secondary capability]. Not for [negative trigger
  — when to use a different skill instead]. Max 1024 chars total.
---

# Skill Name

[One-paragraph purpose statement. Not just WHAT the skill does but WHY it exists —
the philosophy, the problem it solves, what goes wrong without it. This paragraph
should make someone understand the skill's value in 10 seconds.]

## When to Use

- [Specific scenario 1 — describe the situation, not just the keyword]
- [Specific scenario 2]
- [Specific scenario 3]
- **NOT** when [scenario where a different skill or approach is better] — use `other-skill` instead

## Prerequisites

[Optional section — include when the skill requires setup, prior steps, or context]

- [Prerequisite 1 — e.g., "Repo cloned and tests passing"]
- [Prerequisite 2 — e.g., "User has explained their understanding of the issue"]

## Process

### 1. [First Phase Name]

[Clear instructions with concrete commands or examples]

```bash
# Example command with explanation
grep -rn "pattern" src/ --include="*.ts"
```

Expected output: [describe what success looks like]

**Thinking gate** (include in steps where user understanding matters):

> "[Question that forces the user to demonstrate understanding before proceeding.
> Be specific — reference the research/context presented above. Don't ask 'do you
> understand?' — ask them to explain WHAT and WHY.]"

Wait for their answer. Do NOT proceed until they've articulated their understanding.

### 2. [Second Phase Name]

[Continue with clear steps]

### 3. [Continue as needed]

## Common Rationalizations

[Required section. List 3-8 shortcuts people (or agents) commonly try, paired with
WHY each shortcut produces bad outcomes. Be specific to THIS skill's domain.]

| Shortcut | Why It Fails |
|----------|-------------|
| "[Thing people try to skip]" | [Concrete negative consequence — not vague, not preachy] |
| "[Another common shortcut]" | [What specifically goes wrong] |
| "[Third rationalization]" | [Evidence or example of failure] |

## Red Flags

[Optional but recommended. Signs the skill is being misapplied or the situation
doesn't actually call for this skill.]

- [Observable symptom 1 — something you'd notice during execution]
- [Observable symptom 2]
- [Observable symptom 3]

## Verification Checklist

[Required section. Evidence-based completion criteria. Every item must be
objectively checkable — no "seems right" or "looks good".]

- [ ] [Criterion 1 — e.g., "All tests pass including new test for edge case X"]
- [ ] [Criterion 2 — e.g., "PR description includes linked issue number"]
- [ ] [Criterion 3 — e.g., "User can explain the root cause without referencing the fix"]

## Related Skills

[Optional but recommended for skills that are part of a workflow chain.]

- **Previous step**: <- `skill-name` — [one-line context]
- **Next step**: -> `skill-name` — [one-line context]
- **If [condition]**: -> `skill-name` — [one-line context]

## Anti-patterns

[Required section. Explicit prohibitions with reasoning. Minimum 3 items.
These are the most concrete boundary instructions the agent receives.]

- **DO NOT** [anti-pattern 1] — [why this causes problems]
- **DO NOT** [anti-pattern 2] — [specific negative consequence]
- **DO NOT** [anti-pattern 3] — [what goes wrong]
```

---

## Section Purposes

| Section | Required? | Purpose |
|---------|-----------|---------|
| Frontmatter | Yes | Trigger routing — the ONLY thing Claude sees when deciding to load |
| Opening paragraph | Yes | Quick understanding of value — why this skill exists |
| When to Use | Yes | Positive AND negative triggers for accurate routing |
| Prerequisites | No | Gate: don't start if setup isn't done |
| Process | Yes | The actual workflow — what the agent does step by step |
| Common Rationalizations | Yes | Prevents agent laziness by pre-empting excuses |
| Red Flags | No | Catches misapplication early |
| Verification Checklist | Yes | Ensures completion is objective, not subjective |
| Related Skills | No | Connects skills into workflow chains |
| Anti-patterns | Yes | Hard boundaries the agent must not cross |

## Progressive Disclosure Budget

| Location | When Loaded | Word Budget |
|----------|-------------|-------------|
| Frontmatter description | Always (system prompt) | < 1024 chars |
| SKILL.md body | When skill triggers | < 4000 words |
| references/ files | On demand | No hard limit |
| scripts/ | When executed | N/A |

Move content to `references/` when SKILL.md body approaches 4000 words or when content is only needed in specific scenarios (detailed examples, lookup tables, API specs, format references).
