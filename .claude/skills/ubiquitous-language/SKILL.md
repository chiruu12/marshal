---
name: ubiquitous-language
description: Extract a DDD-style ubiquitous language glossary from the current conversation, flagging ambiguities and proposing canonical terms. Saves to UBIQUITOUS_LANGUAGE.md. Use when user wants to define domain terms, build a glossary, harden terminology, create a ubiquitous language, or mentions "domain model" or "DDD".
disable-model-invocation: true
---

# Ubiquitous Language

Extract and formalize domain terminology from the current conversation into a consistent glossary, saved to a local file.

## Process

1. **Scan the conversation** for domain-relevant nouns, verbs, and concepts
2. **Identify problems**:
   - Same word used for different concepts (ambiguity)
   - Different words used for the same concept (synonyms)
   - Vague or overloaded terms
3. **Propose a canonical glossary** with opinionated term choices
4. **Write to `UBIQUITOUS_LANGUAGE.md`** in the working directory using the format below
5. **Output a summary** inline in the conversation

## Output Format

Write a `UBIQUITOUS_LANGUAGE.md` file with this structure:

```md
# Ubiquitous Language

## Order lifecycle

| Term        | Definition                                              | Aliases to avoid      |
| ----------- | ------------------------------------------------------- | --------------------- |
| **Order**   | A customer's request to purchase one or more items      | Purchase, transaction |
| **Invoice** | A request for payment sent to a customer after delivery | Bill, payment request |

## People

| Term         | Definition                                  | Aliases to avoid       |
| ------------ | ------------------------------------------- | ---------------------- |
| **Customer** | A person or organization that places orders | Client, buyer, account |
| **User**     | An authentication identity in the system    | Login, account         |

## Relationships

- An **Invoice** belongs to exactly one **Customer**
- An **Order** produces one or more **Invoices**

## Example dialogue

> **Dev:** "When a **Customer** places an **Order**, do we create the **Invoice** immediately?"
> **Domain expert:** "No — an **Invoice** is only generated once a **Fulfillment** is confirmed. A single **Order** can produce multiple **Invoices** if items ship in separate **Shipments**."
> **Dev:** "So if a **Shipment** is cancelled before dispatch, no **Invoice** exists for it?"
> **Domain expert:** "Exactly. The **Invoice** lifecycle is tied to the **Fulfillment**, not the **Order**."

## Flagged ambiguities

- "account" was used to mean both **Customer** and **User** — these are distinct concepts: a **Customer** places orders, while a **User** is an authentication identity that may or may not represent a **Customer**.
```

## Rules

- **Be opinionated.** When multiple words exist for the same concept, pick the best one and list the others as aliases to avoid.
- **Flag conflicts explicitly.** If a term is used ambiguously in the conversation, call it out in the "Flagged ambiguities" section with a clear recommendation.
- **Only include terms relevant for domain experts.** Skip the names of modules or classes unless they have meaning in the domain language.
- **Keep definitions tight.** One sentence max. Define what it IS, not what it does.
- **Show relationships.** Use bold term names and express cardinality where obvious.
- **Only include domain terms.** Skip generic programming concepts (array, function, endpoint) unless they have domain-specific meaning.
- **Group terms into multiple tables** when natural clusters emerge (e.g. by subdomain, lifecycle, or actor). Each group gets its own heading and table. If all terms belong to a single cohesive domain, one table is fine — don't force groupings.
- **Write an example dialogue.** A short conversation (3-5 exchanges) between a dev and a domain expert that demonstrates how the terms interact naturally. The dialogue should clarify boundaries between related concepts and show terms being used precisely.

<example>

## Example dialogue

> **Dev:** "How do I test the **sync service** without Docker?"

> **Domain expert:** "Provide the **filesystem layer** instead of the **Docker layer**. It implements the same **Sandbox service** interface but uses a local directory as the **sandbox**."

> **Dev:** "So **sync-in** still creates a **bundle** and unpacks it?"

> **Domain expert:** "Exactly. The **sync service** doesn't know which layer it's talking to. It calls `exec` and `copyIn` — the **filesystem layer** just runs those as local shell commands."

</example>

## Re-running

When invoked again in the same conversation:

1. Read the existing `UBIQUITOUS_LANGUAGE.md`
2. Incorporate any new terms from subsequent discussion
3. Update definitions if understanding has evolved
4. Re-flag any new ambiguities
5. Rewrite the example dialogue to incorporate new terms

## Common Rationalizations

| Shortcut | Why It Fails |
|----------|-------------|
| "Everyone knows what we mean, we don't need a glossary" | They don't. Ambiguous terms cause silent misalignment — the frontend team thinks "user" means the logged-in person, the backend thinks it means the API consumer. The glossary prevents this. |
| "Let's just use whatever terms are in the code" | Code terms drift without a glossary anchoring them. Different modules may already use different words for the same concept. The glossary is the source of truth that code should conform to. |
| "We can figure out the aliases later" | Aliases accumulate. The longer you wait, the more "transaction" vs "order" vs "purchase" spreads through code, docs, and conversations. Flagging aliases early is 10x cheaper than renaming later. |
| "The example dialogue is overkill" | Definitions tell you what terms mean in isolation. Dialogues show how terms interact — cardinality, lifecycle relationships, edge cases. They catch ambiguities that definitions miss. |

## Red Flags

- Same word appears in multiple definition rows with different meanings — ambiguity not resolved, just documented
- Glossary includes programming terms (array, endpoint, handler) — these aren't domain terms
- No "Aliases to avoid" column — synonyms will proliferate unchecked
- Example dialogue is absent or doesn't probe edge cases — glossary is untested

## Verification Checklist

- [ ] Every domain-relevant term from the conversation is captured
- [ ] Each term has a one-sentence definition (what it IS, not what it does)
- [ ] Ambiguous terms flagged with clear recommendation
- [ ] Aliases to avoid listed for each term
- [ ] Example dialogue demonstrates term interactions and edge cases
- [ ] `UBIQUITOUS_LANGUAGE.md` written to working directory

## Anti-patterns

- **DO NOT** include programming concepts unless they have domain-specific meaning — "endpoint" is not a domain term, "Order" is
- **DO NOT** write multi-sentence definitions — one sentence max, define what it IS
- **DO NOT** leave ambiguities unresolved — pick the canonical term, list the rest as aliases to avoid
- **DO NOT** skip the example dialogue — it's the test case for your glossary
