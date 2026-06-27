"""MarshalService - the testable core the MCP server (and CLI) call into.

Maps a named client to its backend/model/permission and drives the Fleet. Backends can be
injected for tests; in production they come from the registry.
"""

from __future__ import annotations

import uuid
from collections.abc import Mapping
from pathlib import Path
from typing import Any

from pydantic import BaseModel

from .backends.base import CodingAgentBackend
from .config import ConfigError, FleetConfig, resolve_model
from .doctor import run_checks, summarize
from .fleet import (
    BenchmarkResult,
    CollectResult,
    Fleet,
    IntegrateResult,
    RunRequest,
    StrategyResult,
)
from .state import RunRecord
from .registry import make_backend
from .types import TaskSpec
from .usage import UsageSummary
from .workflow import (
    WorkflowResult,
    WorkflowRunner,
    WorkflowSpec,
    find_workflow,
    load_workflow,
)
from .workflow import list_workflows as _discover_workflows


class ClientInfo(BaseModel):
    """A configured client as surfaced to the driver (resolved model, permission as a string)."""

    name: str
    backend: str
    model: str | None
    permission: str


class DoctorCheck(BaseModel):
    """One preflight result, serialized for the driver (the doctor `Check` over the MCP surface)."""

    name: str
    status: str          # ok | warn | fail
    detail: str
    fix: str = ""


class DoctorReport(BaseModel):
    """Preflight verdict: per-check results plus a roll-up. `ok` is true when nothing failed."""

    checks: list[DoctorCheck]
    fails: int
    warns: int
    ok: bool


class MarshalService:
    def __init__(
        self,
        repo_root: Path | str,
        config: FleetConfig,
        *,
        base_dir: Path | str | None = None,
        backends: Mapping[str, CodingAgentBackend] | None = None,
        config_path: Path | str | None = None,
    ) -> None:
        self.config = config
        self.repo_root = Path(repo_root)
        # Where the config was loaded from - the preflight re-checks this file parses on disk.
        self.config_path = Path(config_path) if config_path else self.repo_root / "fleet.config.yaml"
        if backends is None:
            names = {c.backend for c in config.clients.values()}
            backends = {name: make_backend(name) for name in names}
        self.fleet = Fleet(
            repo_root, backends, base_dir=base_dir, worktree_setup=config.worktree_setup
        )

    def list_clients(self) -> list[ClientInfo]:
        return [
            ClientInfo(
                name=c.name,
                backend=c.backend,
                model=resolve_model(c),
                permission=c.permission.value,
            )
            for c in self.config.clients.values()
        ]

    def _request_for(
        self,
        client_name: str,
        goal: str,
        task_id: str | None = None,
        files_touched: list[str] | None = None,
        context_files: list[str] | None = None,
    ) -> RunRequest:
        client = self.config.clients.get(client_name)
        if client is None:
            known = ", ".join(self.config.clients) or "(none configured)"
            raise ValueError(f"no such client: {client_name!r}; known: {known}")
        # `role` is a semantic routing role (planner/coder/reviewer) that a future policy layer maps
        # to a backend - NOT the client name (the client is carried on RunRequest/RunRecord already).
        # Leave it unset until that policy exists, so the field never claims a role nothing assigned.
        task = TaskSpec(
            id=task_id or uuid.uuid4().hex[:8],
            goal=goal,
            context_files=context_files or [],
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
        context_files: list[str] | None = None,
    ) -> RunRecord:
        req = self._request_for(client_name, goal, task_id, files_touched, context_files)
        return self.fleet.run(
            req.backend_name,
            req.task,
            permission=req.permission,
            model=req.model,
            client=req.client,
            timeout_s=req.timeout_s,
        )

    def run_many(self, jobs: list[dict[str, Any]], *, max_concurrency: int = 4) -> list[RunRecord]:
        """Run several clients in parallel. Each job is
        {client, goal, task_id?, files_touched?, context_files?}.

        Client names are validated up front, so a typo fails fast before any run starts.
        """
        requests = [
            self._request_for(
                j["client"], j["goal"], j.get("task_id"), j.get("files_touched"),
                j.get("context_files"),
            )
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
        context_files: list[str] | None = None,
    ) -> RunRecord:
        """Start a run in the background; return its RUNNING record at once. Poll status()/get_run()."""
        req = self._request_for(client_name, goal, task_id, files_touched, context_files)
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

    def cancel_run(self, run_id: str) -> RunRecord:
        return self.fleet.cancel_run(run_id)

    def integrate(self, run_id: str, *, cleanup: bool = False) -> IntegrateResult:
        return self.fleet.integrate(run_id, cleanup=cleanup)

    def doctor(self) -> DoctorReport:
        """Preflight the setup (toolchain, repo, config, per-backend CLI availability + auth).

        Read-only and side-effect-light - it only probes versions/availability - so a driver can
        check a backend is ready *before* spawning, instead of learning it from a failed run. Probes
        the fleet's configured backends (the same instances runs use).
        """
        checks = run_checks(self.repo_root, self.config_path, backends=self.fleet.backends)
        fails, warns = summarize(checks)
        return DoctorReport(
            checks=[
                DoctorCheck(name=c.name, status=c.status, detail=c.detail, fix=c.fix) for c in checks
            ],
            fails=fails,
            warns=warns,
            ok=fails == 0,
        )

    def status(self) -> list[RunRecord]:
        return self.fleet.state.list()

    def usage(self) -> UsageSummary:
        return self.fleet.usage.summary()

    # --- workflows: run a declared recipe by sequencing the primitives above -----------------

    @property
    def workflows_dir(self) -> Path:
        return self.repo_root / "workflows"

    def list_workflows(self) -> list[WorkflowSpec]:
        """Discover the well-formed workflow recipes under ``<repo>/workflows/``."""
        return _discover_workflows(self.workflows_dir)

    def run_workflow(
        self, name: str, inputs: dict[str, Any] | None = None, *, max_concurrency: int = 4
    ) -> WorkflowResult:
        """Run a workflow by name (or path). Validates the recipe before any agent spawns."""
        path = Path(name)
        if path.suffix in (".yaml", ".yml"):
            if not path.exists():
                raise ConfigError(f"no workflow file at {path}")
        else:
            path = find_workflow(name, self.workflows_dir)
        spec = load_workflow(path)
        return WorkflowRunner(self).run(spec, inputs or {}, max_concurrency=max_concurrency)
