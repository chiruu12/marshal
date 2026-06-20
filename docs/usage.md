# Using Marshal

Marshal drives a fleet of headless coding agents from one driver. You declare named
**clients** (each pinning a backend + model + permission), then call Marshal three ways: as an
MCP server, as a CLI, or as a Python library.

> **Status:** early development. The engine, CLI, and MCP server work; merge-back and parallel
> fan-out are on the roadmap (see [`status.md`](status.md)).

## Install

```bash
uv sync --extra mcp --extra dev
```

The base package is stdlib + PyYAML only. The `mcp` extra adds the MCP server; `dev` adds the
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

- **Secrets are referenced, never inlined** (`env:VAR`).
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

Example Claude Code MCP entry:

```json
{
  "mcpServers": {
    "marshal": {
      "command": "marshal",
      "args": ["mcp"],
      "env": { "MARSHAL_REPO": "/path/to/your/project" }
    }
  }
}
```

Tools exposed to the driver:

| Tool | Purpose |
|------|---------|
| `list_clients` | List configured clients (name, backend, model, permission). |
| `run_agent(client, goal, task_id?)` | Run a task on a client's backend in an isolated worktree; returns the run record. |
| `get_run(run_id)` | Fetch one run record. |
| `status()` | List all runs with status + cost. |
| `usage()` | Per-provider usage summary (totals + by backend/client/model). |

## Use it as a CLI

```bash
marshal backends     # list backends and availability
marshal status       # list fleet runs
marshal usage        # per-provider usage summary
marshal mcp          # run the MCP server over stdio
```

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
`.marshal/fleet.json` and usage in `.marshal/usage/`.

## Where things land

```
.marshal/
├── worktrees/<task>.<backend>/   # isolated checkout per run (kept until you integrate)
├── fleet.json                    # every run's status + cost
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
