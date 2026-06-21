# Using Marshal

Marshal drives a fleet of headless coding agents from one driver. You declare named
**clients** (each pinning a backend + model + permission), then call Marshal three ways: as an
MCP server, as a CLI, or as a Python library.

> **Status:** V1 core complete, pre-1.0. The engine, CLI, and MCP server work, including merge-back
> (`collect_run` + `integrate`), capped parallel fan-out (`run_many`), and a measured savings
> benchmark (`benchmark`/`report`). See [`status.md`](status.md).

## Install

New here? Start with **[`../SETUP.md`](../SETUP.md)** for the full clone-to-first-run path:
prerequisites (Python ≥ 3.11, uv, git) and how to install + authenticate the backend CLIs —
Marshal does **not** install them.

```bash
uv sync --extra mcp --extra dev
```

The base package is Pydantic + PyYAML. The `mcp` extra adds the MCP server; `dev` adds the
test/lint toolchain.

## Configure a fleet

Copy the example and edit it:

```bash
cp fleet.config.example.yaml fleet.config.yaml
```

```yaml
defaults:
  permission: safe-edit        # read-only | safe-edit | yolo
  timeout_s: 600

clients:
  implementer:
    backend: opencode          # opencode | cursor | codex | antigravity
    model: opencode-go/glm-5.2 # Go sub — a fireworks-ai/* model here is rejected
    permission: safe-edit
    secret_ref: env:OPENCODE_API_KEY

  reviewer:
    backend: cursor
    permission: read-only
    secret_ref: env:CURSOR_API_KEY
```

- **Auth is per-CLI**: run each backend's login once (`opencode auth login`, `cursor-agent login`,
  `codex login`). `secret_ref: env:VAR` is an optional preflight check — `marshal doctor` warns if
  unset — but Marshal does **not** inject it; the CLI's own login is what authenticates.
- An OpenCode client with no `model` defaults to `opencode-go/glm-5.2` so runs bill the Go
  subscription, not Fireworks credits. A `fireworks-ai/*` model is rejected outright.

### Permission tiers

| Tier | Meaning |
|------|---------|
| `read-only` | Plan/inspect only — no edits. |
| `safe-edit` | Edit and run **inside the worktree**, no prompts. The default. |
| `yolo` | Fully unrestricted. Opt-in only. |

Headless agents have no stdin, so Marshal never uses a prompting mode (it would deadlock).

## Use it as an MCP server

Point your driver at `marshal mcp`. Environment:

| Var | Default | Meaning |
|-----|---------|---------|
| `MARSHAL_REPO` | `.` | The repo agents work in. |
| `MARSHAL_CONFIG` | `<repo>/fleet.config.yaml` | The fleet config. |

Example Claude Code MCP entry. A bare `uv sync` does not put a `marshal` command on your PATH, so
invoke it through uv with the absolute path to your Marshal checkout (or run `uv tool install .`
first to use a bare `"command": "marshal"`). Run `marshal doctor` before wiring this up.

```json
{
  "mcpServers": {
    "marshal": {
      "command": "uv",
      "args": ["--directory", "/abs/path/to/marshal", "run", "marshal", "mcp"],
      "env": {
        "MARSHAL_REPO": "/abs/path/to/your/project",
        "MARSHAL_CONFIG": "/abs/path/to/your/project/fleet.config.yaml"
      }
    }
  }
}
```

Tools exposed to the driver:

| Tool | Purpose |
|------|---------|
| `list_clients` | List configured clients (name, backend, model, permission). |
| `run_agent(client, goal, task_id?)` | Run a task on a client's backend in an isolated worktree; returns the run record. |
| `run_many(jobs, max_concurrency?)` | Run several `{client, goal}` jobs in parallel, each in its own worktree; returns all records. |
| `spawn(client, goal, task_id?)` | Start a run in the background; returns its RUNNING record at once — poll `get_run`/`status`. |
| `benchmark(goal, clients, task_id?)` | Run one goal through several clients (strategies) and compare cost/latency/outcome. |
| `report(task_id)` | Re-derive a past benchmark's strategy comparison from the ledger (read-only). |
| `get_run(run_id)` | Fetch one run record. |
| `collect_run(run_id)` | A run's diff + changed files (read-only; nothing is merged). |
| `integrate(run_id, cleanup?)` | Merge a run's worktree branch into the current branch; reports conflicts. |
| `status()` | List all runs with status + cost. |
| `usage()` | Per-provider usage summary (totals + by backend/client/model). |

## Use it as a CLI

```bash
marshal doctor       # preflight: check the setup is ready to run agents
marshal backends     # list backends and availability
marshal status       # list fleet runs
marshal usage        # per-provider usage summary
marshal mcp          # run the MCP server over stdio
```

The CLI is **inspection-only** (doctor/backends/status/usage) plus `mcp`. You *run* agents by
driving the MCP tools from your driver (see above), not from the CLI.

## Use it as a library

```python
from pathlib import Path
from marshal_engine.config import load_config
from marshal_engine.service import MarshalService

service = MarshalService(Path("."), load_config("fleet.config.yaml"))
record = service.run_agent("implementer", "Add a docstring to hello()")
print(record.status, record.cost_usd, record.worktree)
print(service.usage()["totals"])
```

Each run lands in its own git worktree under `.marshal/worktrees/`, with state in
`.marshal/runs/<run_id>.json` (one file per run) and usage in `.marshal/usage/`.

## Collect and integrate a run

A run's work stays isolated in its worktree until you explicitly merge it back. Review it first,
then integrate:

```python
collected = service.collect_run(record.run_id)
print(collected.changed_files)        # what the agent touched
print(collected.diff)                 # full diff, including new files

result = service.integrate(record.run_id, cleanup=True)
if result.status == "conflict":
    print("resolve these:", result.conflicts)   # merge was aborted; repo left clean
else:
    print(result.status, "->", result.merged_into)  # "merged" (or "empty" if nothing changed)
```

`collect_run` is read-only. `integrate` commits the worktree's changes onto its
`marshal/<run_id>` branch and merges that into the branch you currently have checked out; a
conflict is reported and the merge aborted so you resolve it deliberately. `cleanup=True` removes
the worktree after a successful merge.

## Where things land

```
.marshal/
├── worktrees/<task>.<backend>/   # isolated checkout per run (kept until you integrate)
├── runs/<run_id>.json            # one file per run: status + cost (single writer per run)
└── usage/
    ├── events.jsonl              # one line per run
    └── summary.json              # rolled-up totals
```

## Backend notes

| Backend | Edits | Usage in output | Notes |
|---------|-------|-----------------|-------|
| OpenCode | yes | yes (tokens + cost) | Force `opencode-go/*` for the Go sub. |
| Cursor | yes | no | Tokens/cost only via Team/Enterprise Admin API. |
| Codex | yes | best-effort | `workspace-write` sandbox for safe-edit. |
| Antigravity | reply-only today | no | Headless writes currently divert to a scratch dir. |

See [`design.md`](design.md) for per-backend invocation details and [`status.md`](status.md)
for what's verified.
