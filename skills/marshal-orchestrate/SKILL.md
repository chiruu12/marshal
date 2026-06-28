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

If the work is inherently sequential (task B needs A's output), run it in **rounds**: integrate A,
then plan B against the new state.

## 2. Spawn
- One task: `run_agent(client, goal, task_id?)`.
- Several independent tasks: `run_many(jobs, max_concurrency?)`, where `jobs` is a list of
  `{client, goal, task_id?}`. They run in parallel, each in its own worktree, capped at
  `max_concurrency` (default 4 - each agent CLI is heavy; do not uncap a large fan-out).
- Every run returns a record with a unique `run_id`, its `worktree`, `status`, and `cost_usd`.

## 3. Monitor
- `status()` lists every run with status + cost; `get_run(run_id)` fetches one.
- A run ends in `succeeded`, `empty` (ran clean but produced no work - do not integrate it),
  `failed`, `timed_out`, or `cancelled`. Only `succeeded` runs are integration candidates.

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

## Cost
`usage()` shows per-provider cost (totals and by backend/client/model, with `$/run` and
`$/succeeded`). Every figure is tagged by `source` (native / estimated / unavailable) - never treat
an estimate as ground truth. To compare routing strategies head-to-head on a real task, use the
**marshal-benchmark** skill.

## Invariants to respect
- When several repos are wired, pick the right `workspace` per call; a run integrates into **its own**
  workspace's repo, never another.
- Workers are headless - prompts must be self-sufficient (no questions are possible).
- Review diffs before integrating; `succeeded` is not `correct`.
- Keep tasks independent to avoid merge conflicts; sequence dependent work in rounds.
- Worktree isolation is the safety net - main is untouched until you integrate.
