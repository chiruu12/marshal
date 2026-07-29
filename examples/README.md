# Examples

Runnable, copy-paste examples for Marshal.

## Prerequisites

- `uv sync --extra mcp --extra dev` from the repo root.
- A `fleet.config.yaml` (copy `fleet.config.example.yaml` and edit your clients).
- **At least one backend CLI installed and authenticated** (e.g. `opencode auth login`). Marshal
  does not install or authenticate the backend CLIs for you; run `uv run marshal doctor` to check.

## Files

- [`library_quickstart.py`](library_quickstart.py) — use this when you want the shortest no-driver
  path: one task, collect the diff, integrate.
- [`pipelined_review.py`](pipelined_review.py) — use this when each implementer job should start its
  own reviewer as soon as *that* job finishes (`run_many` + per-job `then`).
- [`read_paths.py`](read_paths.py) — use this when a run must read a spec or reference file that
  lives outside its worktree (and to see secret-shaped paths refused).
- [`adversarial_review.py`](adversarial_review.py) — use this when you want a read-only review panel
  over a run's diff and will decide yourself (the engine computes no verdict).
- [`multi_workspace.py`](multi_workspace.py) — use this when one process should fan work across
  several repos (`list_workspaces` / `WorkspaceRegistry.run_many`).
- [`per_client_env.yaml`](per_client_env.yaml) — use this when two clients share a backend but need
  different provider homes via `env:` (e.g. two Codex `CODEX_HOME` values).
- [`benchmark-output.md`](benchmark-output.md) — a captured `benchmark` + `report` run: one goal
  across four clients with a source-honest cost/latency table, and the reasoning behind Marshal's
  cost-honesty rules.
- [`workflows/`](workflows/) — declarative workflow templates (`review.yaml`, `compare.yaml`,
  `build-adapters.yaml`) you can validate with `marshal workflows` and run via `run_workflow`.
- [`teams/`](teams/) — adversarial review team templates (`hard-gate.yaml`, `plan-review.yaml`);
  copy into `<repo>/teams/` and point each role at a `permission: read-only` client.

Run Python examples from the repo root:

```bash
uv run python examples/library_quickstart.py
uv run python examples/pipelined_review.py
uv run python examples/read_paths.py
uv run python examples/adversarial_review.py
uv run python examples/multi_workspace.py
```
