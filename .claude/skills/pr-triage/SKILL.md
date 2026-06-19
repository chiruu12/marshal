---
name: pr-triage
description: |
  Triage a PR's AI review comments and CI failures. Fetches bot reviews (Greptile,
  CodeRabbit, Copilot, etc.), classifies each comment, and diagnoses CI failures.
  Use when a PR has bot review comments to address, CI failures to debug, or both.
  Trigger: "pr triage", "check my PR", "CI failed", "bot comments", "review comments",
  "triage this PR", "what's blocking my PR".
---

# PR Triage

Post-push triage for PRs: handle AI bot review comments and CI failures in one pass. You push, then deal with the fallout — this skill covers both.

## When to Use

- PR has comments from AI review bots (Greptile, CodeRabbit, Copilot, Codex, etc.)
- CI is failing on your PR
- Both — bot comments AND CI failures at the same time
- You want a single dashboard of "what do I need to fix before this merges"
- **NOT** for writing code reviews yourself — that's `/review`
- **NOT** for pre-push review of your own changes — that's `/review`

## Prerequisites

- `gh` CLI authenticated
- A PR on the current branch (or a PR number provided)

## Process

### 1. Identify the PR

Detect the PR from the current branch:

```bash
gh pr view --json number,url,headRefName,baseRefName,statusCheckRollup,reviewDecision,state
```

If a PR number was provided, use that instead:

```bash
gh pr view {number} --json number,url,headRefName,baseRefName,statusCheckRollup,reviewDecision,state
```

If no PR exists for the current branch, tell the user and stop.

### 2. Gather everything

Fetch in parallel:

**Bot review comments:**
```bash
# Inline review comments (file-level)
gh api repos/{owner}/{repo}/pulls/{number}/comments --paginate

# Conversation-level comments (issue comments)
gh api repos/{owner}/{repo}/issues/{number}/comments --paginate
```

**CI status:**
```bash
gh pr checks {number}
gh run list --branch {branch} --limit 5
```

Identify which comments are from bots vs humans. Common bot account patterns:
- Username contains `[bot]` suffix
- Known bots: `greptile-apps[bot]`, `coderabbitai[bot]`, `github-actions[bot]`, `copilot-pull-request-review[bot]`, `codex[bot]`, `sonarcloud[bot]`, `codecov[bot]`
- Account type is "Bot" in the API response

Present a summary dashboard:

```
PR #{number}: {title}
Branch: {head} → {base}

Bot Reviews: {N} comments from {bot names}
CI Status:   {passing/failing} — {N passed, M failed, K pending}
Human Reviews: {summary if any}
```

If there are no bot comments AND CI is green, say so and stop — nothing to triage.

### 3A. Triage bot review comments

For each bot comment, read the comment body and the code it references. Classify:

| Classification | Meaning | Action |
|---|---|---|
| **Valid + actionable** | Real issue, needs a code change | Fix it |
| **Valid + already handled** | Real issue, but code already addresses it | Respond explaining why |
| **False positive** | Bot misread the code or context | Dismiss with explanation |
| **Style / nit** | Preference, not correctness | Adopt if cheap, skip if not |
| **Duplicate** | Same concern raised by multiple bots or repeated | Address once |
| **Informational** | Bot is explaining something, no action needed | Acknowledge or skip |

For each **valid + actionable** comment:
1. Show the bot's comment (quoted)
2. Show the relevant code context (read the file at that line)
3. Explain what the bot is flagging and whether it's correct
4. Suggest a fix approach — describe what to change, don't write the code

For **false positives**:
1. Explain WHY it's a false positive
2. Draft a short response the user can post to the thread (user reviews before posting)

Present results as a summary table:

```
| # | Bot | File:Line | Classification | Action |
|---|-----|-----------|---------------|--------|
| 1 | Greptile | src/api.ts:42 | Valid + actionable | Fix null check |
| 2 | CodeRabbit | src/auth.ts:15 | False positive | Already validated upstream |
| 3 | Greptile | src/db.ts:88 | Style nit | Optional: rename var |
```

### 3B. Triage CI failures

If all checks pass, skip this section entirely.

For each failing check/job:

**Step 1: Fetch the failure logs**
```bash
gh run view {run-id} --log-failed
```

If `--log-failed` output is too large, fetch the specific job:
```bash
gh run view {run-id} --log --job {job-id} | tail -50
```

**Step 2: Classify the failure**

| Failure Type | Signals | Fix Strategy |
|---|---|---|
| **Build error** | Compilation failed, import not found, type error | Fix the syntax or type issue locally |
| **Test failure** | Assertion failed, expected X got Y | Read the test, understand what it expects, fix your code |
| **Lint / format** | Style violation, unused import, formatting diff | Run the repo's formatter/linter locally |
| **Type check** | Type mismatch, missing property, incompatible types | Fix the type error |
| **Dependency** | Package not found, version conflict, lockfile | Regenerate lockfile |
| **Environment** | Works locally, fails in CI | Different OS, tool version, or missing env var |
| **Flaky test** | Passes sometimes, fails sometimes, same code | Not your fault — check if it fails on main too |
| **Pre-existing** | Main branch has the same failure | Not your fault |
| **Timeout** | Job exceeded time limit | Check if your changes are slow, or CI is under load |
| **Permission / secret** | Auth failed, secret not found | Fork PRs can't access repo secrets |

**Step 3: Determine fault**

This is the critical question. Check if the same job fails on the base branch:

```bash
gh run list --branch {base-branch} --limit 5 --workflow {workflow-name}
```

If the latest base branch run also fails the same job: **it's pre-existing, not your fault.**

If only your branch fails: **it's your change that broke it.**

For pre-existing failures, draft a PR comment:
```
CI failure in `{job-name}` is pre-existing — same failure on `{base-branch}` ([run link]). Not introduced by this PR.
```

For your failures: show the relevant error, explain it, suggest the fix approach.

### 4. Action plan

Present a prioritized list of everything that needs to happen:

**Priority 1 — CI fixes (blocking merge):**
- List each CI failure that's YOUR fault, with fix approach

**Priority 2 — Valid + actionable bot comments (blocking review):**
- List each, with fix approach

**Priority 3 — Responses needed (unblock review thread):**
- False positive explanations to post
- Pre-existing CI failure comments to post

**Priority 4 — Optional (quick wins):**
- Style nits worth adopting
- Informational acknowledgments

### 5. Execute

Work through the action plan in priority order. For each item:

- **Code fixes**: Explain the approach, user writes the code. Help with unfamiliar linter rules or CI tools.
- **PR comment responses**: Draft the response, user reviews and posts. Keep responses terse and factual.
- **After fixes**: Push and re-check CI.

```bash
git push
gh pr checks {number} --watch
```

If new failures appear after the push, loop back to step 3B for the new failures only.

## Common Traps

| Shortcut | Why It Fails |
|---|---|
| Ignore all bot comments | Bots catch real issues 30-50% of the time. Ignoring signals you didn't read them. |
| Push random CI fixes without reading logs | Diagnose first. Blind pushes waste CI minutes and clutter git history. |
| Dismiss bot comments without explanation | Classify first. If false positive, explain WHY — reviewers read the thread. |
| Assume "passes locally = CI is wrong" | CI uses pinned versions and clean state. If it fails there, it's usually real. |
| Fix pre-existing failures in your PR | Check main first. Don't own problems that aren't yours. |

## Rules

- **DO NOT** write fix code — suggest the approach, user writes the code
- **DO NOT** auto-dismiss bot comments — classify each one with reasoning
- **DO NOT** skip fault determination for CI — always check if base branch has the same failure
- **DO NOT** post PR comments without user reviewing them first
- **DO NOT** treat all bot comments as equal — classification drives the action
- **DO** present a clear summary dashboard before diving into details
- **DO** prioritize CI fixes over bot comments (CI blocks merge, comments block review)
- **DO** check for pre-existing failures before blaming the user's changes
