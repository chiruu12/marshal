"""MarshalService - the testable core the MCP server (and CLI) call into.

Maps a named client to its backend/model/permission and drives the Fleet. Backends can be
injected for tests; in production they come from the registry.
"""

from __future__ import annotations

import sys
import threading
import uuid
from collections.abc import Mapping
from datetime import datetime
from pathlib import Path
from typing import Any

from pydantic import BaseModel

from .backends.base import CodingAgentBackend
from .config import (
    ClientConfig,
    ConfigError,
    FleetConfig,
    ModelSpec,
    _reject_fireworks,
    resolve_duration,
    resolve_model,
)
from .doctor import DoctorReport, doctor_report, run_checks
from .env import merge_user_path
from .fleet import (
    BenchmarkResult,
    BudgetStatus,
    CleanResult,
    CollectResult,
    CommitResult,
    EnforceBudgetGate,
    Fleet,
    IntegrateResult,
    RunRequest,
    StrategyResult,
)
from .retry import RetryPolicy
from .state import RunRecord
from .registry import make_backend
from .types import RunStatus, TaskSpec, UsageSource
from .usage import UsageSummary
from .workflow import (
    WorkflowListing,
    WorkflowResult,
    WorkflowRunner,
    discover_workflows,
    find_workflow,
    load_workflow,
)


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
    permission_fidelity: str


class ClientList(BaseModel):
    """list_clients() result: the configured clients plus the fleet's driver-facing context."""

    clients: list[ClientInfo]
    driver_context: str | None = None


class ModelList(BaseModel):
    """list_models() result: the optional `models:` catalog plus the fleet's driver context.

    Parallel to ClientList: the catalog is pure data (it does not influence routing - clients
    still own backend+model), and the driver_context is surfaced so the driver can render
    fleet-level instructions alongside the model sheet.
    """

    models: list[ModelSpec]
    driver_context: str | None = None


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
        budget_gate: EnforceBudgetGate | None = None,
        session_start: datetime | None = None,
    ) -> None:
        # Defense-in-depth: mcp_server.main() and cli.main() already call this at process entry,
        # but a library user (or any future code path) that constructs a MarshalService directly
        # would otherwise skip the recovery and hit the "doctor says backend missing" trap when
        # PATH was stripped at launch. merge_user_path() is idempotent and cached, so a redundant
        # call is a no-op. Honor MARSHAL_NO_PATH_FIX=1 (hermetic CI / users who want engine to
        # match the host's PATH exactly).
        merge_user_path()
        self.config = config
        self.repo_root = Path(repo_root)
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
            verify=config.verify,
            allow_unsafe_commands=config.allow_unsafe_commands,
            integrate_run_hooks=config.integrate_run_hooks,
            retries=RetryPolicy(max_attempts=config.retries + 1),
            run_gate=run_gate,
            budgets=config.budgets,
            # Pass-through injection (default None keeps Fleet's own defaults): the workspace
            # registry supplies a durable per-repo gate + session clock so a config hot-reload
            # rebuild doesn't fork enforce-budget state or reset session-window accounting.
            budget_gate=budget_gate,
            session_start=session_start,
        )
        # Serializes lazy ad-hoc backend registration (_ensure_backend) so concurrent MCP tool
        # threads don't race the fleet.backends mutation or a doctor() snapshot of it.
        self._adhoc_lock = threading.Lock()

    def list_clients(self) -> ClientList:
        self._reprobe_skipped()
        return ClientList(
            clients=[
                ClientInfo(
                    name=c.name,
                    backend=c.backend,
                    model=resolve_model(c),
                    permission=c.permission.value,
                    permission_fidelity=self.fleet.backends[c.backend].capabilities.permission_fidelity.value,
                )
                for c in self._clients.values()
            ],
            driver_context=self.config.context.driver,
        )

    def list_models(self) -> ModelList:
        # Mirror list_clients: the catalog from FleetConfig (the same dict the CLI/MCP surface)
        # plus the fleet's driver context, so a driver can render fleet-level instructions
        # alongside the model sheet.
        return ModelList(
            models=list(self.config.models),
            driver_context=self.config.context.driver,
        )

    def client_available(self, client_name: str) -> bool:
        client = self.config.clients.get(client_name)
        if client is None:
            return False
        backend = self.fleet.backends.get(client.backend)
        return backend.check_available() if backend is not None else False

    def _compose_goal(self, goal: str) -> str:
        # Layered context: the worker preamble + the fleet's `worker` context prefix the user's
        # goal. Everything (run_agent/run_many/spawn/benchmark/workflows) funnels through
        # _request_for, so this is the single injection point.
        parts = [_WORKER_PREAMBLE]
        worker_ctx = self.config.context.worker
        if worker_ctx:
            parts.append(worker_ctx.strip())
        parts.append(goal)
        return "\n\n".join(parts)

    def _request_for(
        self,
        client_name: str | None,
        goal: str,
        task_id: str | None = None,
        context_files: list[str] | None = None,
        *,
        base_branch: str | None = None,
        model: str | None = None,
        backend: str | None = None,
        duration: str | int | None = None,
    ) -> RunRequest:
        # Harness-first model selection: pick the strategy by (client, [model], [backend]).
        #   - client only: today's path (lookup + resolve_model).
        #   - client + model: same, but the caller's model overrides the client's resolved model.
        #   - backend only: synthesize an ad-hoc client (does NOT need to exist in fleet.config.yaml);
        #     uses ClientConfig's safe defaults (safe-edit, 600s). Validated against the backend
        #     registry and the Fireworks guard.
        #   - client + backend: client wins; backend is ignored.
        #   - neither: fail loud.
        # `role` is a semantic routing role (planner/coder/reviewer) that a future policy layer maps
        # to a backend - NOT the client name (the client is carried on RunRequest/RunRecord already).
        # Leave it unset until that policy exists, so the field never claims a role nothing assigned.
        # `duration` is a per-spawn timeout override: a preset name (short/medium/large/long) or a
        # positive int of seconds. When set, it OVERRIDES the resolved timeout_s on the RunRequest.
        # Validated up front so a typo fails fast before any worktree is created.
        task = TaskSpec(
            id=task_id or uuid.uuid4().hex[:8],
            goal=self._compose_goal(goal),
            context_files=context_files or [],
            base_branch=base_branch,
        )
        timeout_override = resolve_duration(duration) if duration is not None else None
        if client_name:
            client = self._clients.get(client_name)
            if client is None:
                # The name may belong to a client skipped at construction because its backend CLI
                # was unavailable then (e.g. a stripped PATH since healed, or the CLI installed
                # mid-session). Re-probe before failing so a healed backend self-heals its clients.
                self._reprobe_skipped()
                client = self._clients.get(client_name)
            if client is None:
                raise self._unknown_client_error(client_name)
            resolved_model = model if model is not None else resolve_model(client)
            if model is not None:
                # A model override bypasses load_config's guard, so re-check it here (same rule):
                # pointing an opencode client at a fireworks-ai/* model would bill Fireworks credits.
                _reject_fireworks(
                    ClientConfig(name=client.name, backend=client.backend, model=resolved_model)
                )
            return RunRequest(
                backend_name=client.backend,
                task=task,
                permission=client.permission,
                model=resolved_model,
                client=client.name,
                timeout_s=timeout_override if timeout_override is not None else client.timeout_s,
                usage_api=client.usage_api,
            )
        if backend:
            # Ad-hoc: synthesize a client that doesn't need to be in fleet.config.yaml. It uses
            # ClientConfig's own defaults (permission=safe-edit, timeout_s=600) - the safe defaults
            # for an unconfigured run, NOT the repo's `defaults:` block (which is merged into named
            # clients at load time and not retained on FleetConfig).
            ephemeral = ClientConfig(name=f"adhoc-{backend}", backend=backend, model=model)
            _reject_fireworks(ephemeral)  # same hard guard load_config applies; a typo'd model fails fast
            self._ensure_backend(backend)  # lazy-add so the Fleet knows the backend; raises ValueError on unknown
            return RunRequest(
                backend_name=ephemeral.backend,
                task=task,
                permission=ephemeral.permission,
                model=resolve_model(ephemeral),
                client=ephemeral.name,
                timeout_s=timeout_override if timeout_override is not None else ephemeral.timeout_s,
                usage_api=ephemeral.usage_api,
            )
        raise ValueError(
            "must provide either a configured 'client' or a bare 'backend' (with optional 'model'); "
            "hint: list_clients shows configured clients, 'marshal backends' lists backend names"
        )

    def _unknown_client_error(self, client_name: str) -> ValueError:
        """Build an actionable error when a named client cannot be resolved.

        Distinguishes three common failure modes that used to collapse into a vague
        ``known: (none configured)``:
        - the name is configured but its backend CLI is unavailable (skipped)
        - no fleet config file at ``config_path`` (wrong ``--repo`` / cwd / env)
        - config loaded but the name is simply not declared
        """
        if client_name in self.skipped_clients:
            skipped = self.config.clients[client_name]
            return ValueError(
                f"client {client_name!r} skipped: backend {skipped.backend!r} CLI unavailable; "
                f"hint: install/authenticate the backend and re-run `marshal doctor` "
                f"(config: {self.config_path})"
            )
        known = ", ".join(self._clients) or "(none configured)"
        parts = [f"no such client: {client_name!r}", f"known: {known}"]
        if not self.config_path.exists():
            parts.append(
                f"no fleet config at {self.config_path} "
                "(pass --repo/--config, set MARSHAL_REPO/MARSHAL_CONFIG, or "
                "cp fleet.config.example.yaml fleet.config.yaml)"
            )
        elif not self.config.clients:
            parts.append(f"config at {self.config_path} declares no clients")
        else:
            parts.append(f"config: {self.config_path}")
            if self.skipped_clients:
                parts.append(
                    f"skipped (CLI unavailable): {', '.join(self.skipped_clients)}"
                )
        parts.append(
            "hint: pass backend=<name> (with optional model=) for an ad-hoc run, or "
            "check fleet.config.yaml and run doctor"
        )
        return ValueError("; ".join(parts))

    def _ensure_backend(self, name: str) -> CodingAgentBackend:
        """Lazily add a backend to the Fleet for ad-hoc (backend, model) spawns.

        Returns the live instance. Raises ValueError if the name is not in the backend registry
        (the registry's own message already lists the valid backend names). Guarded by
        `_adhoc_lock` so concurrent MCP tool threads don't race the mutation or a doctor() read.
        """
        with self._adhoc_lock:
            return self._ensure_backend_locked(name)

    def _ensure_backend_locked(self, name: str) -> CodingAgentBackend:
        # The unlocked body, split out so _reprobe_skipped (which already holds _adhoc_lock,
        # a non-reentrant threading.Lock) can call it without deadlocking.
        existing = self.fleet.backends.get(name)
        if existing is not None:
            return existing
        be = make_backend(name)  # raises ValueError("unknown backend ...; known: ...")
        self.fleet.backends[name] = be
        return be

    def _reprobe_skipped(self) -> None:
        """Promote clients whose backend CLI has become available since construction.

        Availability is snapshotted once in __init__; a CLI installed (or a PATH healed)
        mid-session would otherwise leave its clients skipped forever while doctor - which probes
        live - reports everything fine. No-op for healthy fleets; otherwise bounded at one
        check_available() per still-skipped client.
        """
        if not self.skipped_clients:
            return
        with self._adhoc_lock:
            healed: list[str] = []
            for n in self.skipped_clients:
                client = self.config.clients.get(n)
                if client is None:
                    continue
                try:
                    be = self._ensure_backend_locked(client.backend)
                except ValueError:
                    continue
                if be.check_available():
                    self._clients[n] = client
                    healed.append(n)
            if healed:
                self.skipped_clients = [n for n in self.skipped_clients if n not in healed]
                for n in healed:
                    print(f"marshal: client {n!r} is now available (backend CLI found)", file=sys.stderr)

    def run_agent(
        self,
        client_name: str | None = None,
        goal: str = "",
        *,
        task_id: str | None = None,
        context_files: list[str] | None = None,
        base_branch: str | None = None,
        model: str | None = None,
        backend: str | None = None,
        duration: str | int | None = None,
    ) -> RunRecord:
        req = self._request_for(
            client_name, goal, task_id, context_files,
            base_branch=base_branch,
            model=model, backend=backend, duration=duration,
        )
        return self.fleet.run(
            req.backend_name,
            req.task,
            permission=req.permission,
            model=req.model,
            client=req.client,
            timeout_s=req.timeout_s,
            usage_api=req.usage_api,
        )

    def job_request(self, job: dict[str, Any]) -> RunRequest:
        """Validate a run_many job dict into a ``RunRequest`` (no agent spawn).

        Same fields as ``run_many`` jobs: ``{client?, goal, task_id?, context_files?, model?,
        backend?, duration?}``. Used by single-repo ``run_many`` and the registry's cross-workspace
        fan-out so validation stays fail-fast before any worktree is created.
        """
        return self._request_for(
            job.get("client"),
            job["goal"],
            job.get("task_id"),
            job.get("context_files"),
            model=job.get("model"),
            backend=job.get("backend"),
            duration=job.get("duration"),
        )

    def run_request_captured(self, req: RunRequest) -> RunRecord:
        """Run one request; capture any failure as a FAILED record (batch-safe, never raises)."""
        return self.fleet._run_request(req)

    def run_many(self, jobs: list[dict[str, Any]], *, max_concurrency: int = 4) -> list[RunRecord]:
        """Run several clients in parallel. Each job is
        {client, goal, task_id?, context_files?, model?, backend?, duration?}.

        Client names are validated up front, so a typo fails fast before any run starts. A job may
        also be specified ad-hoc as {backend, model, goal, ...} with no 'client' key. A job's
        optional `duration` (preset name or positive seconds) overrides the resolved timeout_s.
        """
        requests = [self.job_request(j) for j in jobs]
        return self.fleet.run_many(requests, max_concurrency=max_concurrency)

    def spawn(
        self,
        client_name: str | None = None,
        goal: str = "",
        *,
        task_id: str | None = None,
        context_files: list[str] | None = None,
        base_branch: str | None = None,
        model: str | None = None,
        backend: str | None = None,
        duration: str | int | None = None,
    ) -> RunRecord:
        """Start a run in the background; return its RUNNING record at once. Poll status()/get_run()."""
        req = self._request_for(
            client_name, goal, task_id, context_files,
            base_branch=base_branch,
            model=model, backend=backend, duration=duration,
        )
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
            r for r in rows
            if r.status == RunStatus.SUCCEEDED.value
            and r.source in (UsageSource.NATIVE, UsageSource.ADMIN_API, UsageSource.ESTIMATED)
        ]
        cheapest = min(priced, key=lambda r: r.cost_usd).client if priced else None
        timed = [r for r in rows if r.status == RunStatus.SUCCEEDED.value and r.duration_ms > 0]
        fastest = min(timed, key=lambda r: r.duration_ms).client if timed else None
        return BenchmarkResult(
            task_id=task_id, goal=goal, strategies=rows, cheapest=cheapest, fastest=fastest
        )

    def get_run(self, run_id: str) -> RunRecord | None:
        return self.fleet.state.get(run_id)

    def run_log(self, run_id: str) -> str | None:
        """The full raw stdout/stderr persisted for a run, or None if no log was written.

        Each terminal run (success or failure) gets one file under `<base>/logs/<run_id>.log` with
        a clear `=== run <id> ===` header, a `--- stdout ---` section, and a `--- stderr ---`
        section - the FULL streams, not the truncated `text` on the run record. None when no log
        exists (e.g. a run predating log storage, or a backend that crashed before producing one).
        """
        return self.fleet.logs.read(run_id)

    def collect_run(self, run_id: str) -> CollectResult:
        return self.fleet.collect_run(run_id)

    def commit_run(self, run_id: str, *, message: str | None = None) -> CommitResult:
        """Freeze a finished run's work onto its branch so a dependent run can chain off it."""
        return self.fleet.commit_run(run_id, message=message)

    def cancel_run(self, run_id: str) -> RunRecord:
        return self.fleet.cancel_run(run_id)

    def integrate(self, run_id: str, *, cleanup: bool = False) -> IntegrateResult:
        return self.fleet.integrate(run_id, cleanup=cleanup)

    def clean(
        self,
        *,
        scope: str = "finished",
        run_ids: list[str] | None = None,
        older_than_hours: float | None = None,
        dry_run: bool = False,
    ) -> CleanResult:
        """Tear down finished runs' worktrees + branches (the usage ledger is never touched)."""
        return self.fleet.clean(
            scope=scope, run_ids=run_ids, older_than_hours=older_than_hours, dry_run=dry_run
        )

    def doctor(self) -> DoctorReport:
        """Preflight the setup (toolchain, repo, config, per-backend CLI + auth + fidelity).

        Read-only and side-effect-light - it only probes versions/availability - so a driver can
        check a backend is ready *before* spawning, instead of learning it from a failed run. Probes
        the fleet's configured backends (the same instances runs use). Also emits a static
        ``permission:`` check per known backend (ok for enforced-denies, warn for boundary-only).
        """
        with self._adhoc_lock:
            probed = dict(self.fleet.backends)
        return doctor_report(run_checks(self.repo_root, self.config_path, backends=probed))

    def status(self) -> list[RunRecord]:
        return self.fleet.state.list()

    def usage(
        self,
        since: datetime | None = None,
        until: datetime | None = None,
    ) -> UsageSummary:
        """Roll up this workspace's usage ledger, optionally restricted to a [since, until] window.

        No args = every event (unchanged behavior). `since`/`until` are compared in UTC against each
        event's `ts` (see `UsageTracker.summary`).
        """
        return self.fleet.usage.summary(since=since, until=until)

    def budget_status(self, now: datetime | None = None) -> list[BudgetStatus]:
        """Snapshot the configured advisory budgets: scope, window, windowed spend, limit, remaining.

        `remaining` = ``max(0, limit - spend)`` - so a $0 spend (e.g. a subscription backend that
        reports no cost, or a scope with no runs) reads ``limit`` remaining rather than a misleading
        negative. Returns an empty list when no budgets are configured (the "no behavior change"
        contract for users who don't opt in).
        """
        return self.fleet.budget_status(now=now)

    @property
    def session_start(self) -> datetime:
        """When this Fleet (the long-lived MCP server) started - a stable "session" anchor.

        The MCP `usage` tool maps `window="session"` to this instant, so a driver can ask "what
        have I spent since you woke up?" without restating the timestamp.
        """
        return self.fleet.session_start

    # --- workflows: run a declared recipe by sequencing the primitives above -----------------

    @property
    def workflows_dir(self) -> Path:
        return self.repo_root / "workflows"

    def list_workflows(self) -> WorkflowListing:
        """Discover workflow recipes under ``<repo>/workflows/`` (well-formed and broken)."""
        return discover_workflows(self.workflows_dir)

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
