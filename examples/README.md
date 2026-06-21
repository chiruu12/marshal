# Examples

Runnable, copy-paste examples for Marshal.

## Prerequisites

- `uv sync --extra mcp --extra dev` from the repo root.
- A `fleet.config.yaml` (copy `fleet.config.example.yaml` and edit your clients).
- **At least one backend CLI installed and authenticated** (e.g. `opencode auth login`). Marshal
  does not install or authenticate the backend CLIs for you; run `uv run marshal doctor` to check.

## Files

- [`library_quickstart.py`](library_quickstart.py) — the shortest no-driver path: construct
  `MarshalService`, run one trivial task in an isolated worktree, print its status/cost/source,
  review the diff, and integrate it. This is the fastest way to see Marshal actually run an agent
  without wiring an MCP driver.

Run it from the repo root:

```bash
uv run python examples/library_quickstart.py
```
