"""MCP server exposing Marshal to a driver (e.g. Claude Code).

Marshal is a **fleet primitive**: one driver agent spawns and coordinates many sub-agents, each in
its own isolated git worktree, in parallel, with per-provider cost tracking. Code delegation is the
best-developed path, not the only one - review panels, audits and research fan-out all run through
the same primitives. A run that only reads and reasons still returns its work: its final message is
on the record as `text` and the run is `exited_clean` - `collect_run` says which artifact it was
via `produced` and returns the message for a text run. Call `marshal_quickstart` first for the loop and the tool boundaries.

Thin wrapper over a WorkspaceRegistry of single-repo MarshalServices. Repo(s) + config come from the
environment:
  MARSHAL_REPO        the DEFAULT workspace's repo root        (default: cwd), named "default"
  MARSHAL_CONFIG      the DEFAULT workspace's fleet.config.yaml (default: <repo>/fleet.config.yaml)
  MARSHAL_WORKSPACES  additional workspaces: comma/newline-separated `name=/abs/path` entries, each
                      with its OWN <repo>/fleet.config.yaml and its OWN isolated .marshal ledger
  MARSHAL_MAX_CONCURRENT  process-wide cap on concurrent agent runs across ALL workspaces
  MARSHAL_ALLOW_MCP_WORKSPACE_REGISTRATION  the `add_workspace` tool is DISABLED unless this is
                      exactly "1" (captured once at build_app time); registration stays available
                      via `marshal workspace add`, the registry file, and the env vars above

Every action/query tool takes an optional `workspace` param (defaults to "default"); the run-handle
tools (get_run/collect_run/cancel_run/integrate) resolve a run's owning workspace by a cheap scan of
each repo's ledger, with an optional `workspace` hint to skip it. With MARSHAL_WORKSPACES unset and
no `workspace` arg, behavior is identical to the single-repo server. Tenancy lives here in the MCP
layer; the engine (MarshalService/Fleet) stays single-repo - see workspaces.py.

If a workspace has no config file it still serves, with zero clients, so a freshly installed plugin
never crashes on connect; it logs how to configure a fleet. The `mcp` dependency is optional (install
extra `mcp`); it is imported lazily inside `build_app` so the rest of the package works without it.
Config messages go to STDERR - never stdout, which is the JSON-RPC channel for stdio transport.

Every tool is async and offloads its (possibly long-running) service call to a worker thread, so a
blocking `run` never freezes the event loop: the driver can still poll `status`/`get_run` and
`cancel_run` a run that is in flight, not only ones started with `spawn`.
"""

from .server import build_app, build_service, main

__all__ = ["build_app", "build_service", "main"]
