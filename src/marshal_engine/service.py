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
from .fleet import (
    BenchmarkResult,
    CollectResult,
    Fleet,
    IntegrateResult,
    RunRequest,
    StrategyResult,
)
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

    def spawn(
        self,
        client_name: str,
        goal: str,
        *,
        task_id: str | None = None,
        files_touched: list[str] | None = None,
    ) -> RunRecord:
        """Start a run in the background; return its RUNNING record at once. Poll status()/get_run()."""
        req = self._request_for(client_name, goal, task_id, files_touched)
        run_id = self.fleet.spawn(req)
        rec = self.fleet.state.get(run_id)
        assert rec is not None  # _start just recorded it RUNNING
        return rec

    def shutdown(self) -> None:
        """Drain background spawns (for library/test use; the long-lived MCP server rarely needs it)."""
        self.fleet.shutdown()

    def benchmark(
        self,
        goal: str,
        clients: list[str],
        *,
        task_id: str | None = None,
        max_concurrency: int = 4,
    ) -> BenchmarkResult:
        """Run the SAME goal through each client (a routing strategy) and compare what it cost.

        All runs share one task_id (the grouping key); the comparison is derived on read by
        `report`, so it stays an honest query over the ledger rather than a stored verdict.
        """
        bench_id = task_id or uuid.uuid4().hex[:8]
        jobs = [{"client": c, "goal": goal, "task_id": bench_id} for c in clients]
        self.run_many(jobs, max_concurrency=max_concurrency)
        return self.report(bench_id, goal=goal)

    def report(self, task_id: str, *, goal: str = "") -> BenchmarkResult:
        """Derive a strategy comparison for one benchmark task_id from the recorded runs."""
        rows = [
            StrategyResult(
                run_id=r.run_id,
                client=r.client,
                backend=r.backend,
                model=r.model,
                status=r.status,
                cost_usd=r.cost_usd,
                source=r.source,
                duration_ms=r.duration_ms,
                input_tokens=r.input_tokens,
                output_tokens=r.output_tokens,
            )
            for r in self.fleet.state.list()
            if r.task_id == task_id
        ]
        # cheapest: only strategies that succeeded AND have a known cost (never an "unavailable" one).
        priced = [r for r in rows if r.status == "succeeded" and r.source in ("native", "estimated")]
        cheapest = min(priced, key=lambda r: r.cost_usd).client if priced else None
        timed = [r for r in rows if r.status == "succeeded" and r.duration_ms > 0]
        fastest = min(timed, key=lambda r: r.duration_ms).client if timed else None
        return BenchmarkResult(
            task_id=task_id, goal=goal, strategies=rows, cheapest=cheapest, fastest=fastest
        )

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
