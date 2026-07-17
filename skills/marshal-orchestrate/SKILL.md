---
name: marshal-orchestrate
description: >
  Drive a fleet of headless coding agents through Marshal's MCP server: decompose a goal into
  independent tasks, run them in parallel in isolated git worktrees, review each diff, and
  integrate the good ones. Use when you have a multi-part coding goal to delegate to worker agents
  (Cursor, OpenCode, Codex, Antigravity, Claude Code) instead of doing it all yourself. The engine is mechanism;
  this playbook is the judgment - decomposition, prompt-writing, and merge decisions live here.
---

# Driving the Marshal fleet

You are the **driver**. You keep the expensive thinking - planning, review, merge decisions - and
Marshal spawns cheaper or specialized worker agents that each execute one task autonomously in its
own git worktree. Your job is to decide *who works, on what, with how much context, and whether the
result was worth keeping.* Marshal is exposed as MCP tools; the loop is **plan → spawn → monitor →
collect → integrate.**

## Targeting a workspace (when the server has more than one repo)
One Marshal server can be wired to several repos at once. Call `list_workspaces` first to see them
(name, path, `configured`, `client_count`). **Every action tool takes an optional `workspace`** -
pass a name to target that repo; omit it to use the default (primary) workspace. Workspaces are
fully isolated: each has its own clients (`list_clients(workspace=…)`), its own worktrees, its own
run ledger.
- Each run record you get back carries a `workspace` field. When you later `collect_run`,
  `integrate`, or `cancel_run`, pass that same `workspace` so the call routes to the right repo (it
  still resolves correctly if you omit it - the id is looked up across workspaces).
- `status()` with no `workspace` lists runs across **all** workspaces (each tagged); pass a name to
  scope to one.
- Don't mix repos in a single `run_many` - issue one `run_many` per workspace.
- Need a repo that isn't registered yet? `add_workspace(name, path, scaffold?)` registers it in the
  central `~/.marshal/workspaces.yaml` and it's usable immediately (no reconnect). Pass
  `scaffold=true` to drop a starter `fleet.config.yaml` if the repo has none; then check
  `list_clients(workspace=name)` and have the user fill in clients before routing real work.

If `list_workspaces` shows only `default`, ignore all of this: it behaves exactly like the
single-repo server.

## 0. Know your clients
**Run `doctor` before the first batch** (read-only). It now verifies *auth*, not just that a CLI is
on PATH: a logged-out backend that still answers `--version` is reported `CLI present but not
authenticated` (with the login command), instead of a green "available" that then dies one second
into a real run. Treat a backend's login as standing setup you confirm up front — cheap to check,
expensive to skip across a whole fan-out.
Call `list_clients` to see the configured workers (name, backend, model, permission). Each client is
a routing choice the user set up (a cheap bulk worker, a careful reviewer, etc.). You route tasks to
clients **by name** - you never choose backends directly. To decide *which* client a task should go
to (by task weight - heavy/standard/light - and cost), see [`docs/model-playbook.md`](../../docs/model-playbook.md).

## 1. Plan - decompose into INDEPENDENT tasks
Split the goal into tasks that can run in parallel **without colliding**:
- Give each task a disjoint set of files where possible. Two tasks editing the same file will
  conflict at integrate time - separate their scope or run them in different rounds.
- Size each task so one worker can finish it autonomously. Workers are **headless: they cannot ask
  you anything mid-run.** The prompt must contain everything needed to finish.
- Write a self-contained prompt per task: the goal, acceptance criteria, and the *minimal* files and
  context the worker needs. The worker sees only what you give it plus its worktree - not your whole
  session. Scope tightly; that is the point (less drift, less token waste).
- When you know it, declare `files_touched` per task - it documents the intended scope.

**Never fan out a dependency chain in one `run_many`.** Marshal shines on *independent* work. If task
B needs A's output, batching them in parallel makes each branch off the same base, blind to the
others - they re-invent the same scaffolding and collide at integrate. For sequential work, do one of:
- **Rounds (simplest):** integrate A into your branch, then plan B against the new state.
- **Chain off A's branch (no integrate yet):** `commit_run(A)` freezes A's work as a commit on its own
  branch (your branch stays untouched), then `spawn`/`run_agent` B with `base_branch` = A's branch so B
  builds on A's actual output. Without `commit_run`, basing B on A's branch sees only the spawn base -
  the agent leaves its work uncommitted, so the branch ref never moved.
- **When dependence is unavoidable, ship the contract in the prompt:** the exact signatures/imports of
  the foundation plus "this already exists - import it, do not redefine it."

## 2. Spawn
- One task: `run_agent(client, goal, task_id?)`.
- Several independent tasks: `run_many(jobs, max_concurrency?)`, where `jobs` is a list of
  `{client, goal, task_id?}`. They run in parallel, each in its own worktree, capped at
  `max_concurrency` (default 4 - each agent CLI is heavy; do not uncap a large fan-out).
- Every run returns a record with a unique `run_id`, its `worktree`, `status`, and `cost_usd`.

## 3. Monitor
- `status()` lists every run with status + cost; `get_run(run_id)` fetches one.
- A run ends in `succeeded`, `empty` (ran clean but produced no work - do not integrate it),
  `failed`, `timed_out`, `cancelled`, or `verify_failed` (the work exists but the workspace's
  `verify:` gate rejected it - collect the diff and read the record's `verify_output` before
  deciding; not an integration candidate as-is). Only `succeeded` runs are integration candidates,
  and when the workspace configures a `verify:` command, `succeeded` also means that gate passed.

## 4. Collect - review before you trust
- `collect_run(run_id)` returns the run's **diff + changed files**, read-only. Read it. You are the
  reviewer; `succeeded` means "the process exited cleanly," not "the code is correct."
- Reject work that is wrong or off-scope by simply not integrating it. The worktree stays isolated;
  main is untouched.

## 5. Integrate - merge the good ones
`integrate(run_id, cleanup?)` merges the run's branch into the branch you currently have checked out.
Handle the outcome:
- `merged` - landed; `merged_into` and `changed_files` say what/where. Pass `cleanup=true` to remove
  the worktree when you're done with it.
- `conflict` - the merge was aborted and the repo left clean; `conflicts` lists the files. Resolve by
  re-planning the task (or integrating the other runs first), then retry.
- `blocked` - the target checkout is dirty/colliding or on a detached HEAD; nothing changed. Fix the
  target (commit/stash your edits, check out a branch) and retry - the work is safe on its branch.
- `empty` - nothing to integrate.
- `error` - a git operation failed in a way that needs a human (read `message`); do not blindly retry.

Integrate **one run at a time**, reviewing each. Worktree isolation means main is never touched until
this step.

## 6. Clean up - reclaim the worktrees
A long session leaves a worktree + branch per run. When you're done, `clean(scope?, dry_run?)` tears
them down in one call (the usage ledger and run-state history are kept; only the disk-heavy worktrees
and branches go). It **never** touches a running run. Scopes:
- `merged` - only runs you already integrated. Safest.
- `finished` (default) - merged runs plus failed/timed_out/cancelled/empty/verify_failed ones;
  **protects un-integrated `succeeded` runs** (a candidate you might still want to review). A
  `verify_failed` run's worktree holds reviewable work - collect/review it before cleaning.
- `all` - every finished run, including un-integrated succeeded work.

Run `clean(dry_run=true)` first to see what would go, or `clean(run_ids=[…])` to tear down specific
runs. Don't clean a run whose work you haven't collected/integrated unless you're sure you're done
with it.

## Cost
`usage()` shows per-provider cost (totals and by backend/client/model, with `$/run` and
`$/succeeded`). Every figure is tagged by `source` (native / admin-api / estimated / unavailable) -
never treat an estimate (or an `unavailable` `$0`) as ground truth. To compare routing strategies head-to-head on a real task, use the
**marshal-benchmark** skill.

## Invariants to respect
- When several repos are wired, pick the right `workspace` per call; a run integrates into **its own**
  workspace's repo, never another.
- Workers are headless - prompts must be self-sufficient (no questions are possible).
- Review diffs before integrating; `succeeded` is not `correct`.
- Keep tasks independent to avoid merge conflicts; **never fan out a dependency chain** - sequence it
  in rounds, or chain off a committed branch with `commit_run` + `base_branch`.
- Confirm backends are authenticated (`doctor`) before the first batch, not after a wasted run.
- Worktree isolation is the safety net - main is untouched until you integrate.
- Clean up finished runs with `clean` when done; it never removes a running run or the usage ledger.
