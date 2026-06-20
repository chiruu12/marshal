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
from .fleet import CollectResult, Fleet
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

    def run_agent(
        self,
        client_name: str,
        goal: str,
        *,
        task_id: str | None = None,
        files_touched: list[str] | None = None,
    ) -> RunRecord:
        client = self.config.clients.get(client_name)
        if client is None:
            raise ValueError(f"no such client: {client_name!r}")
        task = TaskSpec(
            id=task_id or uuid.uuid4().hex[:8],
            goal=goal,
            role=client_name,
            files_touched=files_touched or [],
        )
        return self.fleet.run(
            client.backend,
            task,
            permission=client.permission,
            model=resolve_model(client),
            client=client.name,
            timeout_s=client.timeout_s,
        )

    def get_run(self, run_id: str) -> RunRecord | None:
        return self.fleet.state.get(run_id)

    def collect_run(self, run_id: str) -> CollectResult:
        return self.fleet.collect_run(run_id)

    def status(self) -> list[RunRecord]:
        return self.fleet.state.list()

    def usage(self) -> dict[str, Any]:
        return self.fleet.usage.summary()
