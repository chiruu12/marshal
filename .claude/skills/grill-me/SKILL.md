---
name: grill-me
description: |
  Stress-test a plan or design through relentless questioning until every branch of the
  decision tree is resolved. Walks dependencies between decisions, provides recommendations,
  and explores the codebase to answer questions directly. Use when user wants to stress-test
  a plan, get grilled on their design, or mentions "grill me".
---

# Grill Me

Stress-test a plan through relentless, structured interrogation. Most plans fail not because the builder lacks skill, but because they haven't thought through the branches — the edge cases, the dependencies between decisions, the second-order effects. This skill forces those into the open by walking every branch of the decision tree until nothing is left unresolved.

## When to Use

- User has a plan or design they want to pressure-test
- User says "grill me", "stress-test this", "poke holes", or "challenge this"
- A design doc exists but hasn't been scrutinized for completeness
- **NOT** when user wants brainstorming or ideation — use office-hours instead
- **NOT** when user wants implementation help — use tdd, oss-contribute, etc.
- **NOT** when user hasn't formed a plan yet — you need something to interrogate

## Process

### 1. Identify the decision tree

Read the plan. Map the major decisions and their dependencies. Identify which decisions block others.

### 2. Walk each branch

For each unresolved branch:

1. Ask **one question at a time** — never stack multiple questions
2. Provide your **recommended answer** with reasoning
3. Wait for the user's response before moving on
4. If a question can be answered by **exploring the codebase**, explore instead of asking

### 3. Resolve dependencies

When a decision depends on another:
- Resolve the dependency first
- Surface the dependency explicitly: "This depends on your answer to X — let's resolve that first"

### 4. Probe second-order effects

For each locked decision:
- What does this force downstream?
- What does this prevent?
- If this assumption is wrong, what breaks?

### 5. Synthesize

Once all branches are resolved, present:
- The complete decision tree with all locked answers
- Any remaining risks or assumptions that couldn't be validated
- Recommended next steps

## Common Rationalizations

| Shortcut | Why It Fails |
|----------|-------------|
| "I've already thought about this, just validate it" | If you've thought it through, articulating answers takes seconds. If you can't answer a branch question, you haven't actually thought it through — you've thought around it. |
| "That's an implementation detail, we'll figure it out later" | Implementation details that depend on architectural decisions aren't deferrable. If your API shape depends on your storage model, "later" means "rework". |
| "Let's just pick one and iterate" | Valid for reversible decisions. Not valid for decisions with high switching costs — migrations, public APIs, data schemas. This skill distinguishes between the two. |
| "We're overthinking this" | Underthinking is invisible until it hits production. If a question has a clear answer, it takes 5 seconds to say it. If it doesn't, that's exactly why we're asking. |

## Red Flags

- User deflects questions with "we'll figure that out later" on decisions with downstream dependencies
- Multiple branches lead to the same unresolvable blocker — there's a foundational assumption that needs challenging
- User's answers contradict each other across different branches — the mental model isn't coherent
- User gets frustrated after 3 questions — the plan may be underbaked

## Verification Checklist

- [ ] Every major branch of the decision tree has been explored
- [ ] Dependencies between decisions are explicitly identified
- [ ] Each branch either has a locked answer or is flagged as an open risk
- [ ] Second-order effects of key decisions are surfaced
- [ ] Codebase was explored where it could answer questions directly

## Anti-patterns

- **DO NOT** stack multiple questions — one question, one recommendation, one response, then next
- **DO NOT** accept "we'll figure it out later" for decisions that block other decisions
- **DO NOT** ask questions the codebase can answer — grep first, ask the user only what code can't tell you
- **DO NOT** turn this into a lecture — you're interviewing, not presenting
- **DO NOT** stop at surface-level answers — push until the branch is fully resolved
