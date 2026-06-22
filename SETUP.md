# Setting up Marshal

A clone-to-first-run guide. Marshal is **self-installed**: you run it beside your own driver agent
(e.g. Claude Code) and point it at your own backend subscriptions. It takes about ten minutes.

At each step you can run `marshal doctor` to check where you are.

## Prerequisites

Marshal itself needs:

| Tool | Why | Check |
|------|-----|-------|
| **Python ≥ 3.11** | the engine | `python3 --version` |
| **uv** | install + run | `uv --version` ([install](https://docs.astral.sh/uv/)) |
| **git** | worktree isolation + integrate shell out to it | `git --version` |

**Backend CLIs - Marshal does NOT install these.** Install and authenticate the CLI for each
backend you intend to use. Each one manages its *own* login; Marshal just shells out to it.

| Backend | Install | Authenticate |
|---------|---------|--------------|
| **opencode** | `npm i -g opencode-ai` | `opencode auth login` |
| **cursor** | install the Cursor CLI (`cursor-agent`) | `cursor-agent login` (or set `CURSOR_API_KEY`) |
| **codex** | install the OpenAI Codex CLI | `codex login` (ChatGPT account) or set `OPENAI_API_KEY` |
| **antigravity** | install the Antigravity CLI (`agy`) | complete its OAuth login |

You only need the backends your `fleet.config.yaml` references. One is enough to start.

## 1. Install Marshal

```bash
git clone <your-marshal-remote> marshal && cd marshal
uv sync --extra mcp --extra dev
```

The base package is Pydantic + PyYAML. `--extra mcp` adds the MCP server (the only way to *run*
agents); `--extra dev` adds the test/lint toolchain.

## 2. Configure a fleet

A **client** is a named worker pinning a backend + model + permission. Copy the example and edit:

```bash
cp fleet.config.example.yaml fleet.config.yaml
```

```yaml
defaults:
  permission: safe-edit        # read-only | safe-edit | yolo
  timeout_s: 600

clients:
  implementer:
    backend: opencode
    model: opencode-go/glm-5.2   # Go sub; a fireworks-ai/* model is rejected outright
    permission: safe-edit

  reviewer:
    backend: cursor
    permission: read-only
```

Permission tiers: `read-only` (plan/inspect, no edits), `safe-edit` (edit + run inside the
worktree, no prompts - the default), `yolo` (unrestricted, opt-in). Headless agents have no stdin,
so Marshal never uses a prompting mode (it would deadlock).

## 3. Authenticate your backends

**Auth is per-CLI.** Run each backend's login command once (see the Prerequisites table). That's
it - Marshal does not need any API key of its own.

> **About `secret_ref`:** you may add `secret_ref: env:OPENCODE_API_KEY` to a client. This is an
> **optional preflight check only** - `marshal doctor` warns if that variable is unset. Marshal
> **does not inject it** into the backend; the CLI's own login (or whatever you export into the
> environment) is what actually authenticates. If you logged the CLI in, an unset `secret_ref` is
> fine and shows as a warning, not a failure.

## 4. Verify the setup

```bash
uv run marshal doctor
```

It checks Python/uv/git, that your repo is a git work tree, that the config parses, the `mcp`
extra, each configured backend's CLI, and your `secret_ref` variables - printing a `fix:` line for
anything wrong. When a backend exposes account facts (e.g. Cursor's subscription tier + current
model), doctor also prints a `plan:<backend>` line. Exit code is non-zero if there are hard
failures. Example:

```
✓ python: 3.12.11
✓ git: git version 2.53.0
✓ repo: /path/to/project (branch main)
✓ config: fleet.config.yaml (2 clients)
✓ backend:opencode: available
⚠ secret:implementer: env:OPENCODE_API_KEY unset
    fix: export OPENCODE_API_KEY, or ignore this if you authenticated the opencode CLI via its own login

0 issue(s), 1 warning(s)
```

You can also list every backend and its availability with `uv run marshal backends`.

## 5. Wire Marshal into Claude Code (MCP)

Marshal exposes its tools over an MCP server (`marshal mcp`, stdio). It reads two env vars:

| Var | Default | Meaning |
|-----|---------|---------|
| `MARSHAL_REPO` | `.` | the repo agents work in |
| `MARSHAL_CONFIG` | `<repo>/fleet.config.yaml` | the fleet config |

### Option A - install the Claude Code plugin (one step)

The fastest path. From Claude Code:

```
/plugin marketplace add chiruu12/marshal
/plugin install marshal@marshal
```

This installs all three driver Skills **and** the MCP server. The server runs from the plugin's own
checkout via `uv` (auto-syncing the `mcp` extra on first run) and inherits the project you have
open, so `MARSHAL_REPO`/`MARSHAL_CONFIG` default to that project and its `fleet.config.yaml` - you
still complete steps 1-4 there (config + backend auth). If no config is found yet, the server
starts with zero clients and logs how to add one.

### Option B - wire it by hand

A bare `uv sync` does **not** put a `marshal` command on your PATH, so invoke it through uv with the
absolute path to your Marshal checkout:

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

(Prefer a bare `"command": "marshal"`? Run `uv tool install /abs/path/to/marshal` first to put it
on your PATH.)

## 6. Your first run

Marshal's **CLI is inspection-only** (`doctor`, `backends`, `status`, `usage`, `workflows`, `mcp`).
You *run* agents by driving the MCP tools from Claude Code - ask it to `list_clients`, then
`run_agent` a small task, then `collect_run` to review the diff, then `integrate` to merge it.
Use `spawn` + `cancel_run` for long-running background work; use `run_workflow` for declarative
recipes (see [`docs/usage.md`](docs/usage.md)).

To try the engine directly without a driver, use it as a library:

```python
from pathlib import Path
from marshal_engine.config import load_config
from marshal_engine.service import MarshalService

service = MarshalService(Path("."), load_config("fleet.config.yaml"))
record = service.run_agent("implementer", "Add a docstring to hello()")
print(record.status, record.cost_usd, record.worktree)

collected = service.collect_run(record.run_id)   # read-only: review first
print(collected.changed_files)
service.integrate(record.run_id, cleanup=True)    # merge into your current branch
```

Each run lands in its own worktree under `.marshal/worktrees/`; your main branch is untouched
until you `integrate`. State and usage live under `.marshal/`.

## Troubleshooting

| Symptom | Cause | Fix |
|---------|-------|-----|
| `marshal: command not found` | console script not on PATH after `uv sync` | use `uv run marshal …`, or `uv tool install .` |
| `doctor` says a backend's CLI is not available | the CLI isn't installed or isn't authenticated | install + log into that CLI (Prerequisites table) |
| `no fleet config at …` | no `fleet.config.yaml` | `cp fleet.config.example.yaml fleet.config.yaml` |
| `marshal mcp` exits with an extra message | the `mcp` extra isn't installed | `uv sync --extra mcp` |
| OpenCode model rejected at load | a `fireworks-ai/*` model bills Fireworks credits | use an `opencode-go/*` model |
| a backend run shows cost `unavailable` | the backend reports no native cost and its model isn't priced | add the model to `src/marshal_engine/data/prices.yaml` |
| `integrate` refuses with detached HEAD | no branch checked out in `MARSHAL_REPO` | `git checkout <branch>` |

For how to drive a fleet, see [`docs/usage.md`](docs/usage.md); for architecture, see
[`docs/design.md`](docs/design.md); for what's verified, see [`docs/status.md`](docs/status.md).
