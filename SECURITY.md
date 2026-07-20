# Security Policy

Marshal spawns **headless coding agents that execute real shell commands and file edits** on the
host machine. That makes its security posture more than boilerplate. Please read the security model
below before running Marshal against untrusted input.

## Supported versions

Marshal is pre-1.0. Only the **latest release** receives security fixes.

| Version | Supported |
|---------|-----------|
| latest  | yes       |
| older   | no        |

## Reporting a vulnerability

**Do not open a public GitHub issue for security vulnerabilities.**

Report privately via **GitHub Security Advisories** ("Report a vulnerability" on the repository's
Security tab), or by email to **chirag.gupta.290403@gmail.com**.

Please include: affected version/commit, backend(s) involved, a description of the issue, and a
minimal reproduction if possible. We aim to acknowledge a report within **5 business days** and to
agree on a disclosure timeline with you. Please give us a reasonable window to fix before any public
disclosure.

## Security model

Marshal's job is to run autonomous coding agents safely. The guarantees and boundaries:

- **Worktree isolation is the safety boundary.** Each run executes inside its own isolated git
  worktree under `.marshal/worktrees/`. The agent edits files there, not in your working tree.
- **Your main branch is never touched until you explicitly integrate.** Reviewing a diff
  (`collect_run`) is read-only; merging (`integrate`) is a separate, explicit step.
- **Permission tiers gate what an agent may do.** `read-only` (no edits), `safe-edit` (the default -
  edits confined to the worktree), and `yolo` (unrestricted, opt-in). `yolo` removes the guardrails
  by design; only use it when you trust the task prompt and the backend.
- **Every run has a hard timeout and a process-group kill.** A run that exceeds its timeout is
  terminated, and the whole process group is killed so agent grandchildren (subagents, MCP servers,
  tool shells) are not orphaned (`src/marshal_engine/backends/base.py`).
- **Marshal never injects secrets.** Backend authentication is the responsibility of each CLI's own
  login (e.g. `opencode auth login`, `cursor-agent login`, `codex login`). `secret_ref` in
  `fleet.config.yaml` is an **advisory preflight check only** - Marshal verifies the named env var
  is present but does not read, store, or inject its value.

## What you are responsible for

- **Running untrusted task prompts through a write-enabled backend executes code on your host.** A
  prompt is an instruction to an autonomous agent; treat it with the same caution as running an
  arbitrary script. Prefer `read-only` or `safe-edit` and review diffs before integrating.
- **Keep your backend CLIs and their credentials secure.** Marshal inherits whatever access the
  logged-in CLI has.
- **Review what `integrate` will merge.** Always `collect_run` and inspect the diff first.

## Known trust-boundary gaps (honest inventory)

These are intentional or not-yet-hardened behaviors. `marshal doctor` surfaces several as warnings.

- **`safe-edit` ≈ unrestricted edit/shell inside the worktree for several backends.** Cursor,
  OpenCode, Command Code, and Goose need a non-prompting mode for headless runs (closed stdin).
  For those adapters, `safe-edit` maps to the backend's auto-approve / skip-permissions flag;
  **worktree isolation is the hard boundary**, not a per-tool deny list. A full per-backend
  permission config layer (OpenCode `question: deny`, Cursor force+deny lists) is still TODO.
- **`worktree_setup` / `verify` are config-driven subprocesses.** They run arbitrary argv from
  `fleet.config.yaml` in each worktree as your user. Treat that file like executable code; only
  use trusted configs.
- **`commit_run` / `integrate` use `git --no-verify`.** Hooks are skipped so a prompting
  pre-commit cannot deadlock a headless merge. Gate with `verify:`, review diffs, and CI.
- **Memory secrets:** prefer `export LLM_API_KEY=...`. When both env and inline
  `memory.llm_api_key` are set, **env wins**. Inline YAML is deprecated; doctor warns when it is
  present.
- **Budgets default to soft-warn.** Caps never block spawns unless you set `enforce: true` on a
  budget entry. Enforced budgets also serialize matching in-flight spawns (one at a time per
  budget) so concurrent fan-out cannot TOCTOU past the ledger snapshot before spend is recorded.
