"""MarshalService - the testable core the MCP server (and CLI) call into.

Maps a named client to its backend/model/permission and drives the Fleet. Backends can be
injected for tests; in production they come from the registry.
"""

from __future__ import annotations

import sys
import threading
import uuid
from collections.abc import Mapping
from pathlib import Path
from typing import Any

from pydantic import BaseModel

from .backends.base import CodingAgentBackend
from .config import ClientConfig, ConfigError, FleetConfig, resolve_model
from .memory import CogneeMemory
from .memory.store import _resolve_data_dir, _run_async
from .doctor import run_checks, summarize
from .fleet import (
    BenchmarkResult,
    CollectResult,
    Fleet,
    IntegrateResult,
    RunRequest,
    StrategyResult,
)
from .retry import RetryPolicy
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


_WORKER_PREAMBLE = (
    "You are a headless coding agent in a Marshal fleet, running in an isolated git worktree. "
    "You cannot ask questions or wait for input - make reasonable decisions and proceed. "
    "Make all edits inside this worktree only. "
    "If the repo root has an AGENTS.md, CLAUDE.md, or GEMINI.md, read it first for project conventions."
)


class ClientInfo(BaseModel):
    """A configured client as surfaced to the driver (resolved model, permission as a string)."""

    name: str
    backend: str
    model: str | None
    permission: str


class ClientList(BaseModel):
    """list_clients() result: the configured clients plus the fleet's driver-facing context."""

    clients: list[ClientInfo]
    driver_context: str | None = None


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
        run_gate: threading.Semaphore | None = None,
    ) -> None:
        self.config = config
        self.repo_root = Path(repo_root)
        self._repo_key = self.repo_root.name
        self._memory = CogneeMemory(self.config.memory)
        # Where the config was loaded from - the preflight re-checks this file parses on disk.
        self.config_path = Path(config_path) if config_path else self.repo_root / "fleet.config.yaml"
        if backends is None:
            names = {c.backend for c in config.clients.values()}
            backends = {name: make_backend(name) for name in names}
        # Keep the FULL backend set on the Fleet (doctor probes every configured backend, even
        # ones whose CLI is currently unavailable). Partition clients by availability so a missing
        # CLI skips that client instead of failing mid-run.
        avail = {name: be.check_available() for name, be in backends.items()}
        self._clients: dict[str, ClientConfig] = {
            n: c for n, c in config.clients.items() if avail.get(c.backend, False)
        }
        self.skipped_clients: list[str] = [
            n for n, c in config.clients.items() if not avail.get(c.backend, False)
        ]
        for n, c in config.clients.items():
            if not avail.get(c.backend, False):
                print(f"marshal: skipping client {n!r} (backend {c.backend!r} CLI unavailable)", file=sys.stderr)
        self.fleet = Fleet(
            repo_root,
            backends,
            base_dir=base_dir,
            worktree_setup=config.worktree_setup,
            retries=RetryPolicy(max_attempts=config.retries + 1),
            run_gate=run_gate,
            on_run_complete=self._on_run_complete_hook,
        )

    def list_clients(self) -> ClientList:
        return ClientList(
            clients=[
                ClientInfo(
                    name=c.name,
                    backend=c.backend,
                    model=resolve_model(c),
                    permission=c.permission.value,
                )
                for c in self._clients.values()
            ],
            driver_context=self.config.context.driver,
        )

    def client_available(self, client_name: str) -> bool:
        client = self.config.clients.get(client_name)
        if client is None:
            return False
        backend = self.fleet.backends.get(client.backend)
        return backend.check_available() if backend is not None else False

    def _on_run_complete_hook(self, record: RunRecord, diff: str | None) -> None:
        self._memory.remember_sync(record, diff, repo=self._repo_key)

    def _compose_goal(self, goal: str) -> str:
        # Layered context: the worker preamble + the fleet's `worker` context prefix the user's
        # goal. Everything (run_agent/run_many/spawn/benchmark/workflows) funnels through
        # _request_for, so this is the single injection point.
        parts = [_WORKER_PREAMBLE]
        worker_ctx = self.config.context.worker
        if worker_ctx:
            parts.append(worker_ctx.strip())
        recalled = self._memory.recall_sync(goal, self._repo_key)
        if recalled:
            parts.append(f"## Memory from past runs\n\n{recalled}")
        parts.append(goal)
        return "\n\n".join(parts)

    def _request_for(
        self,
        client_name: str,
        goal: str,
        task_id: str | None = None,
        files_touched: list[str] | None = None,
        context_files: list[str] | None = None,
    ) -> RunRequest:
        client = self._clients.get(client_name)
        if client is None:
            known = ", ".join(self._clients) or "(none configured)"
            raise ValueError(f"no such client: {client_name!r}; known: {known}")
        # `role` is a semantic routing role (planner/coder/reviewer) that a future policy layer maps
        # to a backend - NOT the client name (the client is carried on RunRequest/RunRecord already).
        # Leave it unset until that policy exists, so the field never claims a role nothing assigned.
        task = TaskSpec(
            id=task_id or uuid.uuid4().hex[:8],
            goal=self._compose_goal(goal),
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
            usage_api=client.usage_api,
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
            usage_api=req.usage_api,
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
        # cheapest: only strategies that succeeded AND have a known cost - native, a real provider
        # admin-api cost (e.g. EastRouter), or an estimate. Never an "unavailable" one.
        priced = [
            r for r in rows if r.status == "succeeded" and r.source in ("native", "admin-api", "estimated")
        ]
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

    # --- memory: Marshal Recall surface for CLI/MCP ----------------------------------------

    def memory_query(self, text: str) -> str:
        """Recall a memory snippet for ``text`` in this repo's dataset."""
        return self._memory.recall_sync(text, self._repo_key)

    def memory_stats(self) -> dict[str, Any]:
        """Config-level memory stats for this workspace (best-effort; no Cognee required)."""
        cfg = self.config.memory
        stats: dict[str, Any] = {
            "enabled": cfg.enabled,
            "recall_enabled": cfg.recall_enabled,
            "remember_enabled": cfg.remember_enabled,
            "data_dir": str(_resolve_data_dir(cfg)),
            "repo_key": self._repo_key,
            "recall_top_k": cfg.recall_top_k,
            "recall_max_chars": cfg.recall_max_chars,
        }
        try:
            import importlib.util

            stats["cognee_installed"] = importlib.util.find_spec("cognee") is not None
        except Exception:
            stats["cognee_installed"] = False
        return stats

    def memory_improve(self) -> None:
        """Run memify on this repo's memory dataset."""
        _run_async(self._memory.improve(self._repo_key))

    def memory_forget(self, *, all: bool = False) -> None:
        """Forget this repo's dataset, or wipe all memory when ``all`` is true."""
        if all:
            _run_async(self._memory.forget(everything=True))
        else:
            _run_async(self._memory.forget(self._repo_key))

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
