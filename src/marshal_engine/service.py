"""MarshalService — the testable core the MCP server (and CLI) call into.

Maps a named client to its backend/model/permission and drives the Fleet. Backends can be
injected for tests; in production they come from the registry.
"""

from __future__ import annotations

import uuid
from collections.abc import Mapping
from pathlib import Path
from typing import Any

from .backends.base import CodingAgentBackend
from .config import FleetConfig, resolve_model
from .fleet import CollectResult, Fleet, IntegrateResult, RunRequest
from .registry import make_backend
from .state import RunRecord
from .types import TaskSpec


class MarshalService:
    def __init__(
        self,
        repo_root: Path | str,
        config: FleetConfig,
        *,
        base_dir: Path | str | None = None,
        backends: Mapping[str, CodingAgentBackend] | None = None,
    ) -> None:
        self.config = config
        if backends is None:
            names = {c.backend for c in config.clients.values()}
            backends = {name: make_backend(name) for name in names}
        self.fleet = Fleet(repo_root, backends, base_dir=base_dir)

    def list_clients(self) -> list[dict[str, Any]]:
        return [
            {
                "name": c.name,
                "backend": c.backend,
                "model": resolve_model(c),
                "permission": c.permission.value,
            }
            for c in self.config.clients.values()
        ]

    def _request_for(
        self,
        client_name: str,
        goal: str,
        task_id: str | None = None,
        files_touched: list[str] | None = None,
    ) -> RunRequest:
        client = self.config.clients.get(client_name)
        if client is None:
            raise ValueError(f"no such client: {client_name!r}")
        task = TaskSpec(
            id=task_id or uuid.uuid4().hex[:8],
            goal=goal,
            role=client_name,
            files_touched=files_touched or [],
        )
        return RunRequest(
            backend_name=client.backend,
            task=task,
            permission=client.permission,
            model=resolve_model(client),
            client=client.name,
            timeout_s=client.timeout_s,
        )

    def run_agent(
        self,
        client_name: str,
        goal: str,
        *,
        task_id: str | None = None,
        files_touched: list[str] | None = None,
    ) -> RunRecord:
        req = self._request_for(client_name, goal, task_id, files_touched)
        return self.fleet.run(
            req.backend_name,
            req.task,
            permission=req.permission,
            model=req.model,
            client=req.client,
            timeout_s=req.timeout_s,
        )

    def run_many(self, jobs: list[dict[str, Any]], *, max_concurrency: int = 4) -> list[RunRecord]:
        """Run several clients in parallel. Each job is {client, goal, task_id?, files_touched?}.

        Client names are validated up front, so a typo fails fast before any run starts.
        """
        requests = [
            self._request_for(j["client"], j["goal"], j.get("task_id"), j.get("files_touched"))
            for j in jobs
        ]
        return self.fleet.run_many(requests, max_concurrency=max_concurrency)

    def get_run(self, run_id: str) -> RunRecord | None:
        return self.fleet.state.get(run_id)

    def collect_run(self, run_id: str) -> CollectResult:
        return self.fleet.collect_run(run_id)

    def integrate(self, run_id: str, *, cleanup: bool = False) -> IntegrateResult:
        return self.fleet.integrate(run_id, cleanup=cleanup)

    def status(self) -> list[RunRecord]:
        return self.fleet.state.list()

    def usage(self) -> dict[str, Any]:
        return self.fleet.usage.summary()
