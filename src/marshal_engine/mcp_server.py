"""MCP server exposing Marshal to a driver (e.g. Claude Code).

Thin wrapper over MarshalService. Repo + config come from the environment:
  MARSHAL_REPO    working repo root        (default: cwd)
  MARSHAL_CONFIG  path to fleet.config.yaml (default: <repo>/fleet.config.yaml)

If no config file exists the server still starts, with zero clients, so a freshly installed
plugin never crashes on connect; it logs how to configure a fleet. The `mcp` dependency is
optional (install extra `mcp`); it is imported lazily inside `build_app` so the rest of the
package works without it. Config messages go to STDERR - never stdout, which is the JSON-RPC
channel for stdio transport.
"""

from __future__ import annotations

import os
import sys
from pathlib import Path
from typing import Any

from .config import FleetConfig, load_config, validate
from .service import MarshalService


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
        return MarshalService(repo, FleetConfig())
    config = load_config(cfg_path)
    for warning in validate(config):
        print(f"[marshal] config warning: {warning}", file=sys.stderr)
    return MarshalService(repo, config)


def build_app(service: MarshalService) -> Any:
    """Construct the FastMCP app with Marshal's tool surface (backend is a per-call param)."""
    from mcp.server.fastmcp import FastMCP

    app = FastMCP("marshal")

    @app.tool()
    def list_clients() -> list[dict[str, Any]]:
        """List configured backend clients (name, backend, model, permission)."""
        return [c.model_dump(mode="json") for c in service.list_clients()]

    @app.tool()
    def run_agent(client: str, goal: str, task_id: str | None = None) -> dict[str, Any]:
        """Run a task on a client's backend in an isolated git worktree; returns the run record."""
        return service.run_agent(client, goal, task_id=task_id).model_dump(mode="json")

    @app.tool()
    def run_many(jobs: list[dict[str, Any]], max_concurrency: int = 4) -> list[dict[str, Any]]:
        """Run several clients in parallel, each in its own worktree; returns all run records.

        jobs is a list of {client, goal, task_id?}. Concurrency is capped at max_concurrency.
        """
        records = service.run_many(jobs, max_concurrency=max_concurrency)
        return [r.model_dump(mode="json") for r in records]

    @app.tool()
    def spawn(client: str, goal: str, task_id: str | None = None) -> dict[str, Any]:
        """Start a run in the background; returns its RUNNING record immediately. Poll get_run/status."""
        return service.spawn(client, goal, task_id=task_id).model_dump(mode="json")

    @app.tool()
    def benchmark(
        goal: str, clients: list[str], task_id: str | None = None, max_concurrency: int = 4
    ) -> dict[str, Any]:
        """Run one goal through several clients (routing strategies) and compare cost/latency/outcome."""
        result = service.benchmark(goal, clients, task_id=task_id, max_concurrency=max_concurrency)
        return result.model_dump(mode="json")

    @app.tool()
    def report(task_id: str) -> dict[str, Any]:
        """Derive the strategy comparison for a past benchmark task_id from the ledger (read-only)."""
        return service.report(task_id).model_dump(mode="json")

    @app.tool()
    def get_run(run_id: str) -> dict[str, Any] | None:
        """Get a run record by id."""
        rec = service.get_run(run_id)
        return rec.model_dump(mode="json") if rec else None

    @app.tool()
    def collect_run(run_id: str) -> dict[str, Any]:
        """Collect a run's diff and changed files (read-only; nothing is merged)."""
        return service.collect_run(run_id).model_dump(mode="json")

    @app.tool()
    def cancel_run(run_id: str) -> dict[str, Any]:
        """Cancel a running run by id; returns the updated run record."""
        return service.cancel_run(run_id).model_dump(mode="json")

    @app.tool()
    def integrate(run_id: str, cleanup: bool = False) -> dict[str, Any]:
        """Merge a run's worktree branch into the current branch; reports merge conflicts."""
        return service.integrate(run_id, cleanup=cleanup).model_dump(mode="json")

    @app.tool()
    def list_workflows() -> list[dict[str, Any]]:
        """List declared workflow recipes (name, description, inputs, phase summary)."""
        return [
            {
                "name": w.name,
                "description": w.description,
                "inputs": w.inputs,
                "phases": [{"name": p.name, "run": p.run} for p in w.phases],
            }
            for w in service.list_workflows()
        ]

    @app.tool()
    def run_workflow(name: str, inputs: dict[str, Any] | None = None) -> dict[str, Any]:
        """Run a workflow recipe by name. Integration is gated off by default - the result's
        `next_actions` lists the runs to review and integrate. Validates before any agent spawns."""
        return service.run_workflow(name, inputs).model_dump(mode="json")

    @app.tool()
    def status() -> list[dict[str, Any]]:
        """List all fleet runs with status and cost."""
        return [r.model_dump(mode="json") for r in service.status()]

    @app.tool()
    def usage() -> dict[str, Any]:
        """Per-provider usage summary (totals + by backend/client/model)."""
        return service.usage().model_dump(mode="json")

    return app


def main() -> None:
    build_app(build_service()).run()


if __name__ == "__main__":
    main()
