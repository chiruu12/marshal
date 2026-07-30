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

- **Worktree isolation is a git-branch boundary, not a filesystem sandbox.** Each run gets its
  own checkout on a separate `marshal/<id>` branch under `.marshal/worktrees/`. No commits reach
  your branch without an explicit `integrate`. That is the guarantee — not that the agent cannot
  write elsewhere on disk. Worktrees live inside the repo, and a determined or prompt-injected
  agent can reach paths outside its checkout (e.g. via `../..`); only point Marshal at repos and
  backends you trust. Driver-supplied `task_id` / run directory names are validated before any
  `git worktree` op: charset `[A-Za-z0-9._-]` (must start alphanumeric; no leading `.` or `-`),
  length-capped, and the resolved path must be a strict descendant of `.marshal/worktrees/`
  (`is_relative_to`, equality with the base dir refused so cleanup cannot wipe the shared root).
  Hostile ids fail closed with a clear error — they are never sanitize-rewritten. Cleanup also
  refuses to `git branch -D` any name outside the managed `marshal/` prefix.
- **`run_id` is validated before it touches the filesystem.** The run-handle tools (`get_run`,
  `collect_run`, `read_run_file`, `cancel_run`, `integrate`, `clean`) locate a run by composing
  or stat'ing `<workspace>/.marshal/runs/<run_id>.json`. A `run_id` must be a safe flat segment
  — the same charset, leading-character, and length rules as `task_id` — before any such path
  exists: a `../` id would otherwise resolve into *another workspace's* ledger (reading one
  tenant's run tagged as another's) or onto an arbitrary host path as an existence oracle.
  Ledgers, worktrees, and run state stay per-workspace; they are never shared across them.
- **Your main branch is never touched until you explicitly integrate.** Reviewing a diff
  (`collect_run`) is read-only; merging (`integrate`) is a separate, explicit step.
- **Permission tiers gate what an agent may do.** `read-only` (no edits), `safe-edit` (the default -
  non-prompting writes inside the worktree), and `yolo` (unrestricted, opt-in). `safe-edit` is
  **not** uniformly a deny sandbox across backends — see `permission_fidelity` below. `yolo`
  removes the guardrails by design; only use it when you trust the task prompt and the backend.
- **`permission_fidelity` tells you what `safe-edit` actually enforces.** Surfaced on
  `list_clients`, `marshal backends`, and `doctor` (`permission:<backend>`):
  - `enforced-denies` — Cursor, OpenCode, and Codex: a backend or Marshal restriction beyond the
    worktree (curated deny overlay or native workspace sandbox). Still not a true process sandbox.
  - `boundary-only` — Command Code, Goose, Antigravity, and Claude Code: Marshal cannot promise a
    deny layer; the worktree and explicit `integrate` remain the dependable boundary. Doctor warns
    (never fails) on `boundary-only`. Claude Code's native `acceptEdits` mode has **no Marshal
    deny layer** around it.
- **Every run has a hard timeout and a process-group kill.** A run that exceeds its timeout is
  terminated, and the whole process group is killed so agent grandchildren (subagents, MCP servers,
  tool shells) are not orphaned (`src/marshal_engine/backends/base.py`).
- **Marshal never injects secrets.** Backend authentication is the responsibility of each CLI's own
  login (e.g. `opencode auth login`, `cursor-agent login`, `codex login`). `secret_ref` in
  `fleet.config.yaml` is an **advisory preflight check only** - Marshal verifies the named env var
  is present but does not read, store, or inject its value.

## MCP driver authority

The MCP driver (the agent connected to `marshal mcp`) is a powerful caller, and a compromised or
prompt-injected driver exercises that power with your credentials. What a driver can do:

- **Choose an ad-hoc backend/model** on calls that permit it (`run_agent`, `spawn`, `run_many`
  jobs): passing a bare `backend` bypasses the configured clients in `fleet.config.yaml`, subject
  to the CLIs installed and logged in on the host and the requested permission tier.
- **Invoke `integrate`** - the one explicit operation that merges a run's branch into the selected
  workspace's **current branch**. Everything before it is worktree-isolated; integrate is where
  agent work lands on your branch.
- **Invoke `add_workspace`** - but **only** when the operator started the server with
  `MARSHAL_ALLOW_MCP_WORKSPACE_REGISTRATION=1` (exact value; captured once at server start - see
  `docs/config.md`). By default the tool refuses every call before any path lookup, registry
  write, or scaffolding, so a driver cannot expand the set of repos Marshal may modify.

Worktree isolation assumes the **workspace set is operator-selected**. It protects files within
whichever repository Marshal targets; it does not protect the host from the driver choosing a
different repository. The safe default flow: leave MCP registration disabled, register repos
yourself with `marshal workspace add <name> <path>` (hot-reloaded - no reconnect), review diffs
with `collect_run`, and `integrate` deliberately.

Residual risk of opting in: `MARSHAL_ALLOW_MCP_WORKSPACE_REGISTRATION=1` delegates registration of
**any existing directory on the host** to the driver. It is not a path sandbox or allowlist -
enable it only when you trust the driver and everything that can reach its prompt.

## What you are responsible for

- **Running untrusted task prompts through a write-enabled backend executes code on your host.** A
  prompt is an instruction to an autonomous agent; treat it with the same caution as running an
  arbitrary script. Prefer `read-only` or `safe-edit` and review diffs before integrating.
- **Keep your backend CLIs and their credentials secure.** Marshal inherits whatever access the
  logged-in CLI has.
- **Review what `integrate` will merge.** Always `collect_run` and inspect the diff first.

## Known trust-boundary gaps (honest inventory)

These are intentional or not-yet-hardened behaviors. `marshal doctor` surfaces several as warnings.

- **Permission config layer is partial (v0).** Cursor `safe-edit` applies an engine-managed deny
  list (destructive `rm`, `.env` read/write, `.git` writes, and Write to `.cursor/cli.json` via
  Cursor's permission grammar) alongside `--force` via a *temporary* merge into the worktree's
  `.cursor/cli.json`: the file's exact prior state (existence, bytes, mode) is restored before
  the run returns, so the overlay is visible to the live agent but never to run status, diffs,
  commits, or integration. An existing malformed/unreadable/non-object/symlink/non-regular
  `cli.json` (or a symlinked `.cursor/`) fails the run closed (preserved byte-for-byte, agent
  never launched). Restore re-checks those path constraints before unlink/replace so a mid-run
  `.cursor/`→symlink swap cannot redirect cleanup outside the worktree; a restoration failure
  fails the run rather than reporting success with policy residue. The Write deny does **not**
  stop same-user shell/Python from rewriting the policy file mid-run — exact restore limits
  persistence only. These remain curated denies, not a sandbox. OpenCode `safe-edit` stamps
  `OPENCODE_CONFIG_CONTENT` with `question: deny` plus curated bash/edit/read/`external_directory`
  denies (bash also covers cheap `git config` / redirection / `tee` / `sed` cases into `.env` /
  `.git`; wrappers and alternate writers can bypass); `yolo` still gets `question: deny` only
  (headless: skip-permissions does not cover `question`). **`boundary-only` backends:** Command
  Code (`safe-edit`/`yolo` both `--yolo`, no per-tool deny grammar), Goose (`safe-edit`/`yolo` both
  `GOOSE_MODE=auto`), Antigravity (`prepare()` briefly adds the run worktree to host-global
  `trustedWorkspaces` in `~/.gemini/antigravity-cli/settings.json`; `run()` removes it on
  completion, with in-process reference counting so overlapping runs cannot revoke each other's
  grant early; malformed settings fail closed; residual: parallel Antigravity runs still share and
  serialize on that global file, and a run whose teardown never executed - a hard kill, or a run
  later reaped as an orphan - leaves its path trusted until the worktree is removed by `clean`; no PTY wrapper; stdout can be swallowed without a TTY; no distinct
  safe-edit scoping beyond the run-scoped trust grant), and Claude Code (`acceptEdits` with no
  Marshal deny layer). Worktree isolation remains the hard boundary for those adapters and for
  everything the curated denies do not cover. See `permission_fidelity` on `list_clients` /
  `marshal backends` / `doctor`.
- **`cancel_run` signals only a live child of the current process.** Signalling goes through an
  in-process handle that tracks the child from spawn until it is reaped; the OS cannot recycle a
  child's pid before its parent reaps it, so within that window the pid is unambiguous. A cancel
  that arrives before the pid is known is applied as soon as it is; a cancel after the child is
  reaped does not signal at all. A run owned by another (or dead) Marshal process is stamped
  cancelled *without* a signal and says so on the record. The trade-off is real and worth stating
  exactly: **an agent that outlived its supervisor cannot be stopped by Marshal at all.**
  Reconciliation does not stamp such a run terminal — it deliberately skips a record whose agent is
  still alive, because that is running work, not an orphan to clean up. Reconciliation only stamps
  `failed` once the process is gone. So the sequence to know about is: supervisor dies, agent keeps
  running, `cancel_run` stamps the record `cancelled` **without ending the process**. The record
  keeps the pid and its `error` names it, `clean` refuses to remove that worktree while the process
  lives, and ending it is a manual `kill -TERM -<pid>`.
- **A team file is prompt text delivered to fleet agents.** `<repo>/teams/*.yaml` rubrics are
  concatenated into every reviewer's goal, so anyone who can write that directory can instruct the
  fleet. `run_team` refuses a team path outside the workspace's own `teams/` directory, and the
  reviewed subject (a diff, a plan) is nonce-delimited and labelled as untrusted data so it cannot
  close its container and impersonate the instructions. Reviewer output is **not** parsed: the
  engine derives no verdict from it, precisely so reviewed material cannot forge one. Treat
  `teams/` with the same care as any other executable project config.
- **Reviewer `read-only` is fail-closed in routing, not always OS-enforced.** `validate_team`
  refuses a role whose client is not `permission: read-only`, before any spawn — Marshal will not
  route a reviewer to a writable client. What that mode *enforces* varies: Codex's
  `--sandbox read-only` is OS-level, while Cursor / Claude Code `plan` mode is cooperative. As
  everywhere else, the worktree plus explicit integrate is the dependable boundary.
- **`worktree_setup` / `verify` are config-driven subprocesses** when configured. They run
  argv from `fleet.config.yaml` in each worktree as your user. By default only an allowlisted
  binary basename may run (`uv`, `npm`, `pnpm`, `make`, `cargo`, `go`, `pytest`, `python`, …);
  shells (`sh`/`bash`) and anything else require `allow_unsafe_commands: true`. The allowlist
  is **not** a sandbox (allowlisted tools can still execute arbitrary scripts/code). **Timing
  matters:** `worktree_setup` runs **before** the agent (base checkout + operator config).
  `verify` runs **after** the agent may have modified the worktree, so allowlisted runners
  (`make`, `npm`, `pytest`, `uv`, `python`, …) execute project content the agent could have
  authored or changed (`Makefile`, `package.json` scripts, tests, `conftest.py`, package code)
  under your identity. Use `verify:` when you trust the workspace config **and** treat agent
  tasks as code you might run yourself; prefer narrow allowlisted runners; still review
  `collect_run` / CI before integrate. Treat the config like executable code; only use trusted
  configs. See `docs/config.md`.
- **`commit_run` / `integrate` default to `git --no-verify`.** Hooks are skipped so a prompting
  pre-commit cannot deadlock a headless merge, and so Marshal does not execute
  repo-/worktree-controlled hook scripts the agent may have changed. Set
  `integrate_run_hooks: true` only when hooks are known **non-interactive** *and* you trust
  their provenance for your threat model (agent-writable hook paths / husky / lefthook / etc.);
  prompting hooks can still hang until the git timeout. Prefer `verify:` + human/CI review over
  hooks for gating; review diffs and CI regardless.
- **Budgets default to soft-warn.** Caps never block spawns unless you set `enforce: true` on a
  budget entry. Enforced budgets also serialize matching in-flight spawns (one at a time per
  budget) so concurrent fan-out cannot TOCTOU past the ledger snapshot before spend is recorded.
  Budgets are **not** a cross-workspace control: registering multiple workspaces does not create a
  fleet-wide dollar gate. Operators who need org-wide spend limits must set `enforce` (and use
  backends that report meaningful cost) **per workspace**, or enforce outside Marshal.
