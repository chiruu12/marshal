"""MCP server exposing Marshal to a driver (e.g. Claude Code).

Thin wrapper over MarshalService. Repo + config come from the environment:
  MARSHAL_REPO    working repo root        (default: cwd)
  MARSHAL_CONFIG  path to fleet.config.yaml (default: <repo>/fleet.config.yaml)

If no config file exists the server still starts, with zero clients, so a freshly installed
plugin never crashes on connect; it logs how to configure a fleet. The `mcp` dependency is
optional (install extra `mcp`); it is imported lazily inside `build_app` so the rest of the
package works without it. Config messages go to STDERR - never stdout, which is the JSON-RPC
channel for stdio transport.

Every tool is async and offloads its (possibly long-running) service call to a worker thread, so
a blocking `run` never freezes the event loop: the driver can still poll `status`/`get_run` and
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

_T = TypeVar("_T")

# Shared parameter descriptions so the tool schema the driver sees is self-describing (not just
# title + type). Reused across the tools and the run_many Job model.
_DESC_CLIENT = "Name of a configured client (from list_clients)."
_DESC_GOAL = "Natural-language task for the worker agent."
_DESC_TASK_ID = "Optional grouping id; runs sharing a task_id can be compared head-to-head by report()."
_DESC_CONTEXT = "Optional repo-relative paths to point the worker at (injected into its prompt)."
_DESC_RUN_ID = "A run id returned by run_agent / spawn / run_many."


class Job(BaseModel):
    """One parallel job for run_many: which client runs what, optionally scoped."""

    client: Annotated[str, Field(description=_DESC_CLIENT)]
    goal: Annotated[str, Field(description=_DESC_GOAL)]
    task_id: Annotated[str | None, Field(description=_DESC_TASK_ID)] = None
    context_files: Annotated[list[str] | None, Field(description=_DESC_CONTEXT)] = None


def build_service() -> MarshalService:
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


def build_app(service: MarshalService) -> Any:
    """Construct the FastMCP app with Marshal's tool surface (backend is a per-call param).

    Each tool is async and offloads its service call to a worker thread via anyio, so a blocking
    run() never holds the event loop - the driver can poll/cancel an in-flight run concurrently.
    """
    import anyio.to_thread
    from mcp.server.fastmcp import FastMCP

    app = FastMCP("marshal")

    async def offload(fn: Callable[..., _T], *args: Any, **kwargs: Any) -> _T:
        """Run a (possibly long, blocking) service call off the event loop."""
        return await anyio.to_thread.run_sync(lambda: fn(*args, **kwargs))

    @app.tool()
    async def list_clients() -> dict[str, Any]:
        """List configured backend clients (name, backend, model, permission) plus the fleet's
        driver-facing context. Returns {clients, driver_context}."""
        return (await offload(service.list_clients)).model_dump(mode="json")

    @app.tool()
    async def doctor() -> dict[str, Any]:
        """Preflight the setup: toolchain, repo, config, and each configured backend's CLI
        availability + auth. Read-only - run it before spawning to catch a missing/unauthenticated
        backend up front rather than via a failed run. Returns per-check results + a fails/warns roll-up."""
        return (await offload(service.doctor)).model_dump(mode="json")

    @app.tool()
    async def run_agent(
        client: Annotated[str, Field(description=_DESC_CLIENT)],
        goal: Annotated[str, Field(description=_DESC_GOAL)],
        task_id: Annotated[str | None, Field(description=_DESC_TASK_ID)] = None,
        context_files: Annotated[list[str] | None, Field(description=_DESC_CONTEXT)] = None,
    ) -> dict[str, Any]:
        """Run a task on a client's backend in an isolated git worktree; returns the run record.

        Blocks until the run finishes; for long work prefer spawn (returns at once + cancellable)."""
        rec = await offload(
            service.run_agent, client, goal, task_id=task_id, context_files=context_files
        )
        return rec.model_dump(mode="json")

    @app.tool()
    async def run_many(
        jobs: Annotated[list[Job], Field(description="Jobs to run in parallel, each in its own worktree.")],
        max_concurrency: Annotated[int, Field(description="Max jobs running at once.")] = 4,
    ) -> list[dict[str, Any]]:
        """Run several clients in parallel, each in its own worktree; returns all run records."""
        records = await offload(
            service.run_many, [j.model_dump() for j in jobs], max_concurrency=max_concurrency
        )
        return [r.model_dump(mode="json") for r in records]

    @app.tool()
    async def spawn(
        client: Annotated[str, Field(description=_DESC_CLIENT)],
        goal: Annotated[str, Field(description=_DESC_GOAL)],
        task_id: Annotated[str | None, Field(description=_DESC_TASK_ID)] = None,
        context_files: Annotated[list[str] | None, Field(description=_DESC_CONTEXT)] = None,
    ) -> dict[str, Any]:
        """Start a run in the background; returns its RUNNING record immediately. Poll get_run/status,
        and cancel_run to stop it."""
        rec = await offload(
            service.spawn, client, goal, task_id=task_id, context_files=context_files
        )
        return rec.model_dump(mode="json")

    @app.tool()
    async def benchmark(
        goal: Annotated[str, Field(description=_DESC_GOAL)],
        clients: Annotated[list[str], Field(description="Client names to race the same goal through.")],
        task_id: Annotated[str | None, Field(description=_DESC_TASK_ID)] = None,
        max_concurrency: Annotated[int, Field(description="Max clients running at once.")] = 4,
    ) -> dict[str, Any]:
        """Run one goal through several clients (routing strategies) and compare cost/latency/outcome."""
        result = await offload(
            service.benchmark, goal, clients, task_id=task_id, max_concurrency=max_concurrency
        )
        return result.model_dump(mode="json")

    @app.tool()
    async def report(
        task_id: Annotated[str, Field(description="The benchmark task_id whose runs to compare.")],
    ) -> dict[str, Any]:
        """Derive the strategy comparison for a past benchmark task_id from the ledger (read-only)."""
        return (await offload(service.report, task_id)).model_dump(mode="json")

    @app.tool()
    async def get_run(run_id: Annotated[str, Field(description=_DESC_RUN_ID)]) -> dict[str, Any] | None:
        """Get a run record by id."""
        rec = await offload(service.get_run, run_id)
        return rec.model_dump(mode="json") if rec else None

    @app.tool()
    async def collect_run(run_id: Annotated[str, Field(description=_DESC_RUN_ID)]) -> dict[str, Any]:
        """Collect a run's diff and changed files (read-only; nothing is merged)."""
        return (await offload(service.collect_run, run_id)).model_dump(mode="json")

    @app.tool()
    async def cancel_run(run_id: Annotated[str, Field(description=_DESC_RUN_ID)]) -> dict[str, Any]:
        """Cancel a running run by id (process-group SIGTERM); returns the updated run record."""
        return (await offload(service.cancel_run, run_id)).model_dump(mode="json")

    @app.tool()
    async def integrate(
        run_id: Annotated[str, Field(description=_DESC_RUN_ID)],
        cleanup: Annotated[bool, Field(description="Remove the worktree after a successful merge.")] = False,
    ) -> dict[str, Any]:
        """Merge a run's worktree branch into the current branch; reports merge conflicts."""
        return (await offload(service.integrate, run_id, cleanup=cleanup)).model_dump(mode="json")

    @app.tool()
    async def list_workflows() -> list[dict[str, Any]]:
        """List declared workflow recipes (name, description, inputs, phase summary)."""
        return [
            {
                "name": w.name,
                "description": w.description,
                "inputs": w.inputs,
                "phases": [{"name": p.name, "run": p.run} for p in w.phases],
            }
            for w in await offload(service.list_workflows)
        ]

    @app.tool()
    async def run_workflow(
        name: Annotated[str, Field(description="Workflow recipe name (from list_workflows).")],
        inputs: Annotated[dict[str, Any] | None, Field(description="Inputs the recipe declares.")] = None,
    ) -> dict[str, Any]:
        """Run a workflow recipe by name. Integration is gated off by default - the result's
        `next_actions` lists the runs to review and integrate. Validates before any agent spawns."""
        return (await offload(service.run_workflow, name, inputs)).model_dump(mode="json")

    @app.tool()
    async def status() -> list[dict[str, Any]]:
        """List all fleet runs with status and cost."""
        return [r.model_dump(mode="json") for r in await offload(service.status)]

    @app.tool()
    async def usage() -> dict[str, Any]:
        """Per-provider usage summary (totals + by backend/client/model)."""
        return (await offload(service.usage)).model_dump(mode="json")

    return app


def main() -> None:
    build_app(build_service()).run()


if __name__ == "__main__":
    main()
