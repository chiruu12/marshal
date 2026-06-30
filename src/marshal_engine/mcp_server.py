"""MCP server exposing Marshal to a driver (e.g. Claude Code).

Thin wrapper over a WorkspaceRegistry of single-repo MarshalServices. Repo(s) + config come from the
environment:
  MARSHAL_REPO        the DEFAULT workspace's repo root        (default: cwd), named "default"
  MARSHAL_CONFIG      the DEFAULT workspace's fleet.config.yaml (default: <repo>/fleet.config.yaml)
  MARSHAL_WORKSPACES  additional workspaces: comma/newline-separated `name=/abs/path` entries, each
                      with its OWN <repo>/fleet.config.yaml and its OWN isolated .marshal ledger
  MARSHAL_MAX_CONCURRENT  process-wide cap on concurrent agent runs across ALL workspaces

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

from __future__ import annotations

import os
import sys
from collections.abc import Callable
from pathlib import Path
from typing import Annotated, Any, TypeVar

from pydantic import BaseModel, Field

from .config import FleetConfig, load_config, validate
from .service import MarshalService
from .workspaces import DEFAULT_WORKSPACE, WorkspaceRegistry, scaffold_fleet_config

_T = TypeVar("_T")

# Shared parameter descriptions so the tool schema the driver sees is self-describing (not just
# title + type). Reused across the tools and the run_many Job model.
_DESC_CLIENT = "Name of a configured client (from list_clients)."
_DESC_GOAL = "Natural-language task for the worker agent."
_DESC_TASK_ID = "Optional grouping id; runs sharing a task_id can be compared head-to-head by report()."
_DESC_CONTEXT = "Optional repo-relative paths to point the worker at (injected into its prompt)."
_DESC_RUN_ID = "A run id returned by run_agent / spawn / run_many."
_DESC_WORKSPACE = "Target workspace name (from list_workspaces); defaults to the primary workspace."
_DESC_WS_HINT = (
    "Optional workspace hint (from the run's `workspace` field) to skip the ledger scan; the lookup "
    "falls back to scanning all workspaces if it is wrong or omitted."
)


class Job(BaseModel):
    """One parallel job for run_many: which client runs what, optionally scoped.

    All jobs in a run_many call run in the call's `workspace` (cross-workspace batches are not yet
    supported - issue separate run_many calls per workspace).
    """

    client: Annotated[str, Field(description=_DESC_CLIENT)]
    goal: Annotated[str, Field(description=_DESC_GOAL)]
    task_id: Annotated[str | None, Field(description=_DESC_TASK_ID)] = None
    context_files: Annotated[list[str] | None, Field(description=_DESC_CONTEXT)] = None


def build_service() -> MarshalService:
    """Build the single DEFAULT-workspace service from the environment (the legacy entry point).

    Retained for the library/test path and reused by the registry's default builder. Multi-workspace
    wiring goes through WorkspaceRegistry.from_env() in main().
    """
    repo = Path(os.environ.get("MARSHAL_REPO", "."))
    cfg_path = Path(os.environ.get("MARSHAL_CONFIG") or repo / "fleet.config.yaml")
    if not cfg_path.exists():
        # Start anyway, with zero clients, so the server (e.g. a freshly installed plugin) never
        # crashes on connect. list_clients() returns [] and the driver is told to configure a fleet.
        print(
            f"[marshal] no fleet config at {cfg_path}; starting with zero clients. "
            "Copy fleet.config.example.yaml to fleet.config.yaml (or set MARSHAL_CONFIG), then "
            "reconnect. See SETUP.md.",
            file=sys.stderr,
        )
        return MarshalService(repo, FleetConfig(), config_path=cfg_path)
    config = load_config(cfg_path)
    for warning in validate(config):
        print(f"[marshal] config warning: {warning}", file=sys.stderr)
    return MarshalService(repo, config, config_path=cfg_path)


def build_app(target: WorkspaceRegistry | MarshalService) -> Any:
    """Construct the FastMCP app over a WorkspaceRegistry (backend AND workspace are per-call params).

    Accepts a bare MarshalService too (wrapped as a one-workspace registry) for the single-repo and
    test paths. Each tool is async and offloads its service call to a worker thread via anyio, so a
    blocking run() never holds the event loop - the driver can poll/cancel an in-flight run.
    """
    import anyio.to_thread
    from mcp.server.fastmcp import FastMCP

    registry = target if isinstance(target, WorkspaceRegistry) else WorkspaceRegistry.for_service(target)
    app = FastMCP("marshal")

    async def offload(fn: Callable[..., _T], *args: Any, **kwargs: Any) -> _T:
        """Run a (possibly long, blocking) service call off the event loop."""
        return await anyio.to_thread.run_sync(lambda: fn(*args, **kwargs))

    def tag(payload: dict[str, Any], workspace: str) -> dict[str, Any]:
        """Stamp a result with the workspace it came from, so the driver can route follow-ups."""
        return {**payload, "workspace": workspace}

    @app.tool()
    async def list_workspaces() -> list[dict[str, Any]]:
        """List the repos this server can target: name, path, config_path, configured, client_count,
        and which is the default. Pass a name as the `workspace` param on the other tools."""
        return await offload(registry.describe)

    @app.tool()
    async def add_workspace(
        name: Annotated[str, Field(description="Short name to register the repo under (letters, digits, ._-).")],
        path: Annotated[str, Field(description="Absolute path to the repo (an existing directory).")],
        scaffold: Annotated[
            bool, Field(description="Also drop a starter fleet.config.yaml if the repo has none.")
        ] = False,
    ) -> dict[str, Any]:
        """Register a repo as a workspace in the central registry (~/.marshal/workspaces.yaml) so it
        can be targeted by `workspace=`. Available immediately - no reconnect. The path must be an
        existing directory; a repo with no fleet.config.yaml registers with zero clients until one is
        added (pass scaffold=true to drop a starter in). Then call list_workspaces / list_clients."""

        def _do() -> dict[str, Any]:
            wdef = registry.add(name, path)  # writes the file this registry reads; hot-reloads in
            return {
                "name": wdef.name,
                "path": str(wdef.path),
                "config_path": str(wdef.config_path),
                "scaffolded": scaffold_fleet_config(wdef.path) if scaffold else False,
            }

        return await offload(_do)

    @app.tool()
    async def list_clients(
        workspace: Annotated[str | None, Field(description=_DESC_WORKSPACE)] = None,
    ) -> dict[str, Any]:
        """List configured backend clients (name, backend, model, permission) plus the fleet's
        driver-facing context, for the chosen workspace. Returns {clients, driver_context, workspace}."""
        svc = await offload(registry.get, workspace)
        return tag((await offload(svc.list_clients)).model_dump(mode="json"), workspace or DEFAULT_WORKSPACE)

    @app.tool()
    async def doctor(
        workspace: Annotated[str | None, Field(description=_DESC_WORKSPACE)] = None,
    ) -> dict[str, Any]:
        """Preflight the SELECTED workspace: toolchain, repo, config, and each configured backend's
        CLI availability + auth. Read-only - run it before spawning to catch a missing/unauthenticated
        backend up front. Returns per-check results + a fails/warns roll-up + the workspace."""
        svc = await offload(registry.get, workspace)
        return tag((await offload(svc.doctor)).model_dump(mode="json"), workspace or DEFAULT_WORKSPACE)

    @app.tool()
    async def run_agent(
        client: Annotated[str, Field(description=_DESC_CLIENT)],
        goal: Annotated[str, Field(description=_DESC_GOAL)],
        task_id: Annotated[str | None, Field(description=_DESC_TASK_ID)] = None,
        context_files: Annotated[list[str] | None, Field(description=_DESC_CONTEXT)] = None,
        workspace: Annotated[str | None, Field(description=_DESC_WORKSPACE)] = None,
    ) -> dict[str, Any]:
        """Run a task on a client's backend in an isolated git worktree (in `workspace`'s repo);
        returns the run record stamped with its workspace.

        Blocks until the run finishes; for long work prefer spawn (returns at once + cancellable)."""
        svc = await offload(registry.get, workspace)
        rec = await offload(
            svc.run_agent, client, goal, task_id=task_id, context_files=context_files
        )
        return tag(rec.model_dump(mode="json"), workspace or DEFAULT_WORKSPACE)

    @app.tool()
    async def run_many(
        jobs: Annotated[list[Job], Field(description="Jobs to run in parallel, each in its own worktree.")],
        max_concurrency: Annotated[int, Field(description="Max jobs running at once.")] = 4,
        workspace: Annotated[str | None, Field(description=_DESC_WORKSPACE)] = None,
    ) -> list[dict[str, Any]]:
        """Run several clients in parallel in one workspace, each in its own worktree; returns all
        run records (each tagged with the workspace)."""
        svc = await offload(registry.get, workspace)
        records = await offload(
            svc.run_many, [j.model_dump() for j in jobs], max_concurrency=max_concurrency
        )
        ws = workspace or DEFAULT_WORKSPACE
        return [tag(r.model_dump(mode="json"), ws) for r in records]

    @app.tool()
    async def spawn(
        client: Annotated[str, Field(description=_DESC_CLIENT)],
        goal: Annotated[str, Field(description=_DESC_GOAL)],
        task_id: Annotated[str | None, Field(description=_DESC_TASK_ID)] = None,
        context_files: Annotated[list[str] | None, Field(description=_DESC_CONTEXT)] = None,
        workspace: Annotated[str | None, Field(description=_DESC_WORKSPACE)] = None,
    ) -> dict[str, Any]:
        """Start a run in the background in `workspace`'s repo; returns its RUNNING record immediately.
        Poll get_run/status, and cancel_run to stop it."""
        svc = await offload(registry.get, workspace)
        rec = await offload(
            svc.spawn, client, goal, task_id=task_id, context_files=context_files
        )
        return tag(rec.model_dump(mode="json"), workspace or DEFAULT_WORKSPACE)

    @app.tool()
    async def benchmark(
        goal: Annotated[str, Field(description=_DESC_GOAL)],
        clients: Annotated[list[str], Field(description="Client names to race the same goal through.")],
        task_id: Annotated[str | None, Field(description=_DESC_TASK_ID)] = None,
        max_concurrency: Annotated[int, Field(description="Max clients running at once.")] = 4,
        workspace: Annotated[str | None, Field(description=_DESC_WORKSPACE)] = None,
    ) -> dict[str, Any]:
        """Run one goal through several clients (routing strategies) in one workspace and compare
        cost/latency/outcome."""
        svc = await offload(registry.get, workspace)
        result = await offload(
            svc.benchmark, goal, clients, task_id=task_id, max_concurrency=max_concurrency
        )
        return tag(result.model_dump(mode="json"), workspace or DEFAULT_WORKSPACE)

    @app.tool()
    async def report(
        task_id: Annotated[str, Field(description="The benchmark task_id whose runs to compare.")],
        workspace: Annotated[str | None, Field(description=_DESC_WORKSPACE)] = None,
    ) -> dict[str, Any]:
        """Derive the strategy comparison for a past benchmark task_id from the workspace's ledger
        (read-only). task_ids are per-workspace, so pass the workspace the benchmark ran in."""
        svc = await offload(registry.get, workspace)
        return tag((await offload(svc.report, task_id)).model_dump(mode="json"), workspace or DEFAULT_WORKSPACE)

    @app.tool()
    async def get_run(
        run_id: Annotated[str, Field(description=_DESC_RUN_ID)],
        workspace: Annotated[str | None, Field(description=_DESC_WS_HINT)] = None,
    ) -> dict[str, Any] | None:
        """Get a run record by id, located across all workspaces (or via the `workspace` hint).

        status is one of: succeeded | empty (ran clean but produced no work - do NOT integrate) |
        failed | timed_out | cancelled. Only `succeeded` runs are integration candidates."""
        resolved = await offload(registry.resolve_run, run_id, workspace)
        if resolved is None:
            return None
        name, svc = resolved
        rec = await offload(svc.get_run, run_id)
        return tag(rec.model_dump(mode="json"), name) if rec else None

    @app.tool()
    async def collect_run(
        run_id: Annotated[str, Field(description=_DESC_RUN_ID)],
        workspace: Annotated[str | None, Field(description=_DESC_WS_HINT)] = None,
    ) -> dict[str, Any]:
        """Collect a run's diff and changed files (read-only; nothing is merged)."""
        name, svc = await offload(registry.require_run, run_id, workspace)
        return tag((await offload(svc.collect_run, run_id)).model_dump(mode="json"), name)

    @app.tool()
    async def cancel_run(
        run_id: Annotated[str, Field(description=_DESC_RUN_ID)],
        workspace: Annotated[str | None, Field(description=_DESC_WS_HINT)] = None,
    ) -> dict[str, Any]:
        """Cancel a running run by id (process-group SIGTERM); returns the updated run record."""
        name, svc = await offload(registry.require_run, run_id, workspace)
        return tag((await offload(svc.cancel_run, run_id)).model_dump(mode="json"), name)

    @app.tool()
    async def integrate(
        run_id: Annotated[str, Field(description=_DESC_RUN_ID)],
        cleanup: Annotated[bool, Field(description="Remove the worktree after a successful merge.")] = False,
        workspace: Annotated[str | None, Field(description=_DESC_WS_HINT)] = None,
    ) -> dict[str, Any]:
        """Merge a run's worktree branch into its workspace's current branch.

        REVIEW THE DIFF FIRST with collect_run - `succeeded` means the process exited cleanly, NOT
        that the code is correct. Integrate one run at a time. Outcome status is one of: merged |
        conflict (aborted, repo left clean) | blocked (target dirty/detached, or the run is still
        running - fix and retry) | empty (nothing to integrate) | error (a git op needs a human)."""
        name, svc = await offload(registry.require_run, run_id, workspace)
        return tag((await offload(svc.integrate, run_id, cleanup=cleanup)).model_dump(mode="json"), name)

    @app.tool()
    async def list_workflows(
        workspace: Annotated[str | None, Field(description=_DESC_WORKSPACE)] = None,
    ) -> list[dict[str, Any]]:
        """List declared workflow recipes (name, description, inputs, phase summary) for a workspace."""
        svc = await offload(registry.get, workspace)
        return [
            {
                "name": w.name,
                "description": w.description,
                "inputs": w.inputs,
                "phases": [{"name": p.name, "run": p.run} for p in w.phases],
            }
            for w in await offload(svc.list_workflows)
        ]

    @app.tool()
    async def run_workflow(
        name: Annotated[str, Field(description="Workflow recipe name (from list_workflows).")],
        inputs: Annotated[dict[str, Any] | None, Field(description="Inputs the recipe declares.")] = None,
        workspace: Annotated[str | None, Field(description=_DESC_WORKSPACE)] = None,
    ) -> dict[str, Any]:
        """Run a workflow recipe by name in `workspace`'s repo. Integration is gated off by default -
        the result's `next_actions` lists the runs to review and integrate. Validates before any spawn."""
        svc = await offload(registry.get, workspace)
        return tag((await offload(svc.run_workflow, name, inputs)).model_dump(mode="json"), workspace or DEFAULT_WORKSPACE)

    @app.tool()
    async def status(
        workspace: Annotated[str | None, Field(description=_DESC_WORKSPACE + " Omit to list ALL workspaces.")] = None,
    ) -> list[dict[str, Any]]:
        """List fleet runs with status and cost (status ∈ succeeded/empty/failed/timed_out/cancelled).
        Omit `workspace` to aggregate across every workspace (each run tagged with its workspace);
        pass one to scope to it."""
        return [
            tag(rec.model_dump(mode="json"), ws)
            for ws, rec in await offload(registry.ledger_runs, workspace)
        ]

    @app.tool()
    async def usage(
        workspace: Annotated[str | None, Field(description=_DESC_WORKSPACE)] = None,
    ) -> dict[str, Any]:
        """Per-provider usage summary (totals + by backend/client/model) for one workspace."""
        svc = await offload(registry.get, workspace)
        return tag((await offload(svc.usage)).model_dump(mode="json"), workspace or DEFAULT_WORKSPACE)

    return app


def main() -> None:
    registry = WorkspaceRegistry.from_env()
    # Build the default workspace eagerly so the connect-time config message + warnings still fire at
    # startup (named workspaces build lazily on first touch).
    registry.get(DEFAULT_WORKSPACE)
    build_app(registry).run()


if __name__ == "__main__":
    main()
