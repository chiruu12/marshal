# Skill Quality Criteria

Four criteria every skill must meet. Use this as a review rubric when auditing or upgrading skills.

---

## The Four Criteria

### 1. Specific

The skill targets a defined workflow with clear boundaries.

**Pass:**
- Description includes 3+ trigger phrases from real user language
- "When to Use" includes negative cases (when NOT to use)
- Could distinguish this skill from 20+ others based on description alone

**Fail:**
- Description says "helps with X" without trigger phrases
- No negative triggers — skill fires on tangentially related queries
- Process steps use vague language ("validate the data", "check things")

### 2. Verifiable

Completion can be assessed objectively, not by "seems right."

**Pass:**
- Verification checklist uses observable evidence (tests pass, file exists, metric within threshold)
- Thinking gates ask for specific explanations, not "do you understand?"
- Anti-patterns are concrete prohibitions, not vague warnings

**Fail:**
- Checklist says "output looks good" or "quality is acceptable"
- No verification checklist at all
- Anti-patterns say "be careful" instead of "DO NOT do X because Y"

### 3. Battle-tested

The skill handles real-world failure modes, not just the happy path.

**Pass:**
- Common Rationalizations table addresses 3+ domain-specific shortcuts
- Process includes error handling for common failures
- Red Flags section catches misapplication

**Fail:**
- Only covers the golden path — no error handling
- Common Rationalizations are generic ("I'll do it later") not domain-specific
- No consideration of edge cases or failure modes

### 4. Minimal

The skill is concise, using progressive disclosure to manage token budget.

**Pass:**
- SKILL.md body under 4000 words
- Detailed reference material in references/ directory
- No redundant sections (no separate "Overview" + "Purpose" + "Introduction")
- Instructions are actionable, not explanatory

**Fail:**
- SKILL.md over 5000 words with no references/ directory
- Long explanations where a command example would suffice
- Multiple sections saying the same thing in different words
- "Background" or "Theory" sections that don't affect execution

---

## Quick Audit Checklist

Run this against any skill to assess quality:

```
SPECIFIC:
[ ] Description includes 3+ trigger phrases
[ ] "When to Use" has negative cases
[ ] Description distinguishes from similar skills

VERIFIABLE:
[ ] Verification checklist uses objective evidence
[ ] Anti-patterns are concrete prohibitions
[ ] Thinking gates (if present) ask for explanations

BATTLE-TESTED:
[ ] Common Rationalizations has 3+ domain-specific entries
[ ] Process handles at least one failure mode
[ ] Red Flags section present (recommended)

MINIMAL:
[ ] SKILL.md body under 4000 words
[ ] Detailed docs in references/ (if body would exceed limit)
[ ] No redundant sections
[ ] Instructions are actionable, not explanatory
```

---

## Scoring Guide

For each criterion, score 0-2:

| Score | Meaning |
|-------|---------|
| 0 | Missing or fundamentally broken |
| 1 | Present but incomplete or generic |
| 2 | Strong, domain-specific, well-crafted |

**Total: 0-8**
- **7-8**: Ship-ready
- **5-6**: Needs minor improvements
- **3-4**: Needs structural work (Tier 2 upgrade)
- **0-2**: Needs full rewrite (Tier 1 upgrade)
