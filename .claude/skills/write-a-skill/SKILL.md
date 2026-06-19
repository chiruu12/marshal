---
name: write-a-skill
description: |
  Create new Claude Code skills with proper structure, progressive disclosure, and quality
  standards. Use when user wants to create, write, build, or design a new skill. Also use
  when asked to improve, audit, or restructure an existing skill. Covers standalone skills
  and MCP-enhanced workflows.
---

# Write a Skill

Skills encode repeatable workflows so Claude follows them consistently without re-explanation. This skill ensures every new skill meets quality standards: specific enough to trigger correctly, structured for progressive disclosure, and verifiable against evidence-based criteria.

## When to Use

- User wants to create a new skill from scratch
- User wants to restructure or improve an existing skill
- User wants to audit skill quality against best practices
- **NOT** when user wants to use an existing skill — just invoke it directly

## Process

### 1. Gather requirements

Interview the user. Don't start writing until you understand:

- **What task/domain?** What repeatable workflow does this skill cover?
- **2-3 concrete use cases.** "When would you invoke this?" Get specific trigger phrases.
- **What tools needed?** Built-in Claude capabilities, MCP servers, Bash scripts?
- **Who's the audience?** Solo dev? Team? Community? This affects how much context to embed.
- **What exists already?** Check for similar skills that could be extended instead.

> "Describe 2-3 scenarios where you'd reach for this skill. What would you say to trigger it?"

### 2. Design the skill structure

Decide what goes where using **progressive disclosure** (3 levels):

| Level | What | When Loaded | Budget |
|-------|------|-------------|--------|
| **Frontmatter** (YAML) | Name + description with triggers | Always — in system prompt | < 1024 chars |
| **SKILL.md body** | Full instructions, process, checklists | When skill is triggered | < 4000 words |
| **references/** | Detailed docs, examples, lookup tables | On demand when needed | No hard limit |

```
skill-name/
├── SKILL.md           # Required — instructions
├── references/        # Optional — detailed docs loaded on demand
│   ├── examples.md
│   └── api-patterns.md
├── scripts/           # Optional — deterministic operations
│   └── validate.sh
└── assets/            # Optional — templates, fonts, icons
```

**Rules:**
- Folder name: kebab-case only (`my-skill-name`)
- File must be exactly `SKILL.md` (case-sensitive)
- No `README.md` inside skill folders
- No XML angle brackets (`<` `>`) in frontmatter
- No "claude" or "anthropic" in the skill name

### 3. Write the frontmatter

The description is the most important part — it's the ONLY thing Claude sees when deciding whether to load the skill.

**Structure:** `[What it does] + [When to use — trigger phrases] + [Key capabilities]`

```yaml
---
name: my-skill
description: |
  [One sentence: what it does]. Use when [trigger 1], [trigger 2],
  or [trigger 3]. Also handles [capability]. Not for [negative trigger].
---
```

**Good:**
```yaml
description: |
  Analyze Figma design files and generate developer handoff docs. Use when
  user uploads .fig files, asks for "design specs", "component docs", or
  "design-to-code handoff". Outputs structured markdown with measurements.
```

**Bad:**
```yaml
description: Helps with designs.
```

Test the description: could Claude distinguish this skill from 20 others based solely on the description? If not, add specificity.

### 4. Write the SKILL.md body

Follow this template — see [references/target-template.md](references/target-template.md) for the full annotated version.

**Required sections:**

1. **Opening paragraph** — What the skill does and WHY it exists (philosophy, not just mechanics)
2. **When to Use** — Specific scenarios + negative cases ("NOT when X — use Y instead")
3. **Process** — Step-by-step with concrete commands/examples. Include **thinking gates** where user understanding matters.
4. **Common Rationalizations** — Table of shortcuts people try + why they fail. Prevents agent laziness.
5. **Verification Checklist** — Evidence-based completion criteria. Not "seems right" but "test X passes".
6. **Anti-patterns** — Explicit DO NOT list with reasoning.

**Optional sections (include when relevant):**

- **Prerequisites** — What must be true before starting
- **Red Flags** — Signs the skill is being misapplied
- **Related Skills** — Workflow connections (`<- previous`, `-> next`)

### 5. Write reference files

Move content to `references/` when:

- SKILL.md exceeds ~100 lines or ~4000 words
- Content has distinct domains (e.g., separate API patterns from examples)
- Advanced features are rarely needed
- Lookup tables or format specs are involved

Link clearly from SKILL.md:

```markdown
Before writing queries, consult [references/api-patterns.md](references/api-patterns.md) for rate limiting and pagination.
```

### 6. Add scripts (if needed)

Add utility scripts when the operation is **deterministic** — validation, formatting, file generation. Scripts save tokens and improve reliability vs. generated code.

```bash
# Example: validation script
python scripts/validate.py --input {filename}
```

### 7. Review against quality criteria

See [references/quality-criteria.md](references/quality-criteria.md) for the full rubric.

**Quick check — every skill must be:**

- [ ] **Specific** — Description includes trigger phrases, not vague capabilities
- [ ] **Verifiable** — Verification checklist has evidence-based criteria, not subjective judgment
- [ ] **Battle-tested** — Process handles common failure modes (error handling, edge cases)
- [ ] **Minimal** — SKILL.md is concise; detailed content lives in references/

**Structural check:**

- [ ] Folder: kebab-case, no spaces/capitals
- [ ] SKILL.md: exact filename, valid YAML frontmatter
- [ ] Description: under 1024 chars, includes WHAT + WHEN + NOT WHEN
- [ ] Body: under 4000 words (soft limit)
- [ ] No README.md in skill folder
- [ ] All referenced files exist and links resolve
- [ ] Common Rationalizations table present (minimum 3 entries)
- [ ] Anti-patterns section present (minimum 3 items)

### 8. Test the skill

Three types of testing:

**Trigger test:** Run 3 prompts that SHOULD trigger the skill, 2 that should NOT. Verify correct routing.

**Functional test:** Execute the skill end-to-end on a real scenario. Verify:
- Process steps followed in order
- Thinking gates actually stop and wait
- Anti-patterns respected
- Verification checklist items are checkable

**Comparison test:** Run the same task with and without the skill. The skill should produce better results with fewer back-and-forth messages.

## Common Rationalizations

| Shortcut | Why It Fails |
|----------|-------------|
| "The description can be short, Claude will figure it out" | Description is the ONLY signal for trigger routing. Vague descriptions cause under-triggering or over-triggering across 70+ installed skills. |
| "I'll skip the anti-patterns section, it's obvious" | Agents take the path of least resistance. Without explicit prohibitions, they rationalize shortcuts — especially under long contexts where instructions blur. |
| "Common Rationalizations table is meta/recursive, skip it" | This table is the single most effective section for preventing agent laziness. It pre-empts the exact excuses agents use to cut corners. |
| "I don't need a verification checklist, I'll know when it's done" | Subjective completion = inconsistent results across sessions. Evidence-based criteria make the skill reproducible. |
| "SKILL.md can be long, I'll put everything in one file" | Progressive disclosure exists for a reason. Large SKILL.md burns tokens on every invocation even when only basic instructions are needed. |

## Red Flags

- Skill description matches 5+ unrelated queries (too broad)
- SKILL.md over 5000 words with no references/ directory (not using progressive disclosure)
- No anti-patterns section (agent will find creative ways to cut corners)
- Verification checklist uses subjective language ("looks good", "seems right")

## Verification Checklist

- [ ] User confirmed the skill covers their intended use cases
- [ ] Description triggers correctly on 3 test queries and doesn't trigger on 2 unrelated queries
- [ ] All 6 required sections present in SKILL.md body
- [ ] Referenced files in references/ and scripts/ exist and links resolve
- [ ] SKILL.md body under 4000 words
- [ ] Frontmatter description under 1024 characters
- [ ] Common Rationalizations table has 3+ entries relevant to the skill's domain
- [ ] Anti-patterns list has 3+ items with reasoning

## Related Skills

- **If auditing existing skills**: Use this skill's quality criteria + template as the benchmark
- **If building OSS skills**: -> `oss-explore-repo` to understand the repo first
- **If skill needs MCP integration**: Consult [references/target-template.md](references/target-template.md) for MCP coordination patterns

## Anti-patterns

- **DO NOT** start writing SKILL.md before gathering requirements — you'll build the wrong skill and waste time restructuring
- **DO NOT** copy another skill's structure without adapting — each skill's Common Rationalizations and Anti-patterns must be domain-specific, not generic
- **DO NOT** put "helps with X" as the description — this tells Claude nothing about when to trigger the skill
- **DO NOT** skip the review step — structural issues (missing sections, oversized body, broken links) are cheap to catch now and expensive to debug later
- **DO NOT** create a README.md inside the skill folder — all documentation goes in SKILL.md or references/
