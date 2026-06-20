"""MCP server exposing Marshal to a driver (e.g. Claude Code).

Thin wrapper over MarshalService. Repo + config come from the environment:
  MARSHAL_REPO    working repo root        (default: cwd)
  MARSHAL_CONFIG  path to fleet.config.yaml (default: <repo>/fleet.config.yaml)

The `mcp` dependency is optional (install extra `mcp`); it is imported lazily inside `build_app`
so the rest of the package works without it. Config warnings go to STDERR — never stdout, which
is the JSON-RPC channel for stdio transport.
"""

from __future__ import annotations

import os
import sys
from dataclasses import asdict
from pathlib import Path
from typing import Any

from .config import load_config, validate
from .service import MarshalService


def build_service() -> MarshalService:
    repo = Path(os.environ.get("MARSHAL_REPO", "."))
    cfg_path = Path(os.environ.get("MARSHAL_CONFIG") or repo / "fleet.config.yaml")
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
        return service.list_clients()

    @app.tool()
    def run_agent(client: str, goal: str, task_id: str | None = None) -> dict[str, Any]:
        """Run a task on a client's backend in an isolated git worktree; returns the run record."""
        return asdict(service.run_agent(client, goal, task_id=task_id))

    @app.tool()
    def run_many(jobs: list[dict[str, Any]], max_concurrency: int = 4) -> list[dict[str, Any]]:
        """Run several clients in parallel, each in its own worktree; returns all run records.

        jobs is a list of {client, goal, task_id?}. Concurrency is capped at max_concurrency.
        """
        return [asdict(r) for r in service.run_many(jobs, max_concurrency=max_concurrency)]

    @app.tool()
    def get_run(run_id: str) -> dict[str, Any] | None:
        """Get a run record by id."""
        rec = service.get_run(run_id)
        return asdict(rec) if rec else None

    @app.tool()
    def collect_run(run_id: str) -> dict[str, Any]:
        """Collect a run's diff and changed files (read-only; nothing is merged)."""
        return asdict(service.collect_run(run_id))

    @app.tool()
    def integrate(run_id: str, cleanup: bool = False) -> dict[str, Any]:
        """Merge a run's worktree branch into the current branch; reports merge conflicts."""
        return asdict(service.integrate(run_id, cleanup=cleanup))

    @app.tool()
    def status() -> list[dict[str, Any]]:
        """List all fleet runs with status and cost."""
        return [asdict(r) for r in service.status()]

    @app.tool()
    def usage() -> dict[str, Any]:
        """Per-provider usage summary (totals + by backend/client/model)."""
        return service.usage()

    return app


def main() -> None:
    build_app(build_service()).run()


if __name__ == "__main__":
    main()
