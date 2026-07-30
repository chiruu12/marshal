"""MarshalService - the testable core the MCP server (and CLI) call into.

Maps a named client to its backend/model/permission and drives the Fleet. Backends can be
injected for tests; in production they come from the registry.
"""

from __future__ import annotations

from concurrent.futures import ThreadPoolExecutor

import sys
import threading
import uuid
from collections.abc import Mapping
from datetime import datetime
from pathlib import Path
from typing import Any

from pydantic import BaseModel, ValidationError

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
    RunManyJob,
    RunManyJobResult,
    RunRequest,
    StrategyResult,
)
from .retry import RetryPolicy
from .state import RunRecord
from .registry import make_backend
from .types import RunStatus, TaskSpec, UsageSource
from .usage import UsageSummary
from .layout import reports_dir
from .worktree import WorktreeError
from .teams import (
    TeamListing,
    TeamReview,
    TeamRunner,
    TeamSubject,
    discover_teams,
    find_team,
    load_team,
    render_role_report,
    render_unified_report,
    report_dirname,
    utc_stamp,
)
from .workflow import (
    WorkflowListing,
    WorkflowResult,
    WorkflowRunner,
    discover_workflows,
    find_workflow,
    load_workflow,
)


_WORKER_PREAMBLE = (
    "You are a headless agent in a Marshal fleet, running in an isolated git worktree. "
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


class SkippedClient(BaseModel):
    """A configured client that is NOT usable right now, and why.

    Marshal already knew this - it prints a warning to stderr at construction - but the MCP driver
    never sees stderr, so from its side the client simply vanished from `list_clients` with no
    error and no reason. Naming the client, its backend, and the cause turns a silent
    disappearance into something a driver can act on (install the CLI, fix the name, route
    elsewhere).
    """

    name: str
    backend: str
    reason: str


class RunFile(BaseModel):
    """One file read out of a run's worktree, with an explicit truncation flag.

    `truncated` is never implicit: a driver acting on a prefix of a report while believing it had
    the whole thing is the failure this field exists to prevent.
    """

    run_id: str
    path: str
    content: str
    truncated: bool
    size_bytes: int


class ClientList(BaseModel):
    """list_clients() result: the usable clients, the ones that were dropped, and driver context."""

    clients: list[ClientInfo]
    skipped: list[SkippedClient] = []
    driver_context: str | None = None


class ModelList(BaseModel):
    """list_models() result: the optional `models:` catalog plus the fleet's driver context.

    Parallel to ClientList: the catalog is pure data (it does not influence routing - clients
    still own backend+model), and the driver_context is surfaced so the driver can render
    fleet-level instructions alongside the model sheet.
    """

    models: list[ModelSpec]
    # Model ids the configured backends' CLIs report, keyed by backend. Populated ONLY when no
    # `models:` catalog is configured - a driver otherwise had to leave Marshal and run
    # `cursor-agent models` in a shell to learn what it could route at. `None` for a backend means
    # it exposes no way to ask, which is NOT "it has no models"; a caller must tell those apart.
    #
    # Kept as a SEPARATE field, never merged into `models`: the catalog is curated metadata a human
    # wrote, this is whatever a CLI said just now. Flattening them would erase that difference and
    # let a probe drift into looking like configuration.
    backend_models: dict[str, list[str] | None] = {}
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
        # Same facts, keyed for the driver: which client, on which backend, and why it is missing.
        # `avail` is False both for a known backend whose CLI is absent and for a name that is not
        # a backend at all - different problems with different fixes, so they are not collapsed.
        self._skipped_detail: dict[str, SkippedClient] = {
            n: SkippedClient(
                name=n,
                backend=c.backend,
                reason=(
                    f"backend {c.backend!r} is not a known backend"
                    if c.backend not in backends
                    else f"the {c.backend!r} CLI is not available on PATH (or failed its probe)"
                ),
            )
            for n, c in config.clients.items()
            if not avail.get(c.backend, False)
        }
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
            skipped=[self._skipped_detail[n] for n in self.skipped_clients
                     if n in self._skipped_detail],
            driver_context=self.config.context.driver,
        )

    def list_models(self) -> ModelList:
        # Mirror list_clients: the catalog from FleetConfig (the same dict the CLI/MCP surface)
        # plus the fleet's driver context, so a driver can render fleet-level instructions
        # alongside the model sheet.
        # Probing costs a subprocess per backend, so only do it when the catalog is empty - which
        # is exactly the case that sent a driver to a shell. A configured catalog is the curated
        # answer and stands on its own.
        # Probe CONCURRENTLY. Each probe is a subprocess with its own timeout, so running them
        # serially makes the worst case the SUM of those timeouts - enough to blow past an MCP
        # client's request deadline and turn a slow catalogue into a dead tool.
        probed: dict[str, list[str] | None] = {}
        if not self.config.models:
            names = sorted({c.backend for c in self._clients.values()})

            def _probe(name: str) -> tuple[str, list[str] | None]:
                backend = self.fleet.backends.get(name)
                if backend is None:
                    return name, None
                try:
                    return name, backend.available_models()
                except Exception:  # a probe must never take the whole listing down
                    return name, None

            if names:
                with ThreadPoolExecutor(max_workers=min(len(names), 8)) as pool:
                    probed = dict(pool.map(_probe, names))
        return ModelList(
            models=list(self.config.models),
            backend_models=probed,
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
        read_paths: list[str] | None = None,
        *,
        base_branch: str | None = None,
        model: str | None = None,
        backend: str | None = None,
        duration: str | int | None = None,
        output_schema: dict[str, Any] | None = None,
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
        # `task_id` is fail-closed (charset + length) via TaskSpec; map ValidationError → ValueError
        # so CLI/MCP surfaces match other driver-input errors (not a pydantic traceback).
        # Use `is not None` (not truthiness): an explicit empty string must hit the validator,
        # not silently become a generated id.
        try:
            task = TaskSpec(
                id=task_id if task_id is not None else uuid.uuid4().hex[:8],
                goal=self._compose_goal(goal),
                context_files=context_files or [],
                read_paths=read_paths or [],
                base_branch=base_branch,
                output_schema=output_schema,
            )
        except ValidationError as exc:
            raise ValueError(str(exc)) from exc
        timeout_override = resolve_duration(duration) if duration is not None else None
        if client_name and backend:
            # A contradiction, not a precedence question: the caller named a configured client AND
            # a bare backend, which are two different answers to "what runs this". Silently
            # preferring one meant the run happened on a backend the caller had not asked for, with
            # nothing in the result saying so. `model` is NOT in this rule - a client plus a model
            # is a coherent request (run this client's backend against that model) and stays a
            # documented override.
            raise ValueError(
                f"conflicting routing: client={client_name!r} and backend={backend!r} both given. "
                f"Pass `client` to use a configured client (add `model` to override its model), or "
                f"pass `backend` alone for an ad-hoc run - not both."
            )
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
                client_env=dict(client.env),
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
                client_env=dict(ephemeral.env),
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
        read_paths: list[str] | None = None,
        base_branch: str | None = None,
        model: str | None = None,
        backend: str | None = None,
        duration: str | int | None = None,
        output_schema: dict[str, Any] | None = None,
    ) -> RunRecord:
        req = self._request_for(
            client_name, goal, task_id, context_files, read_paths,
            base_branch=base_branch,
            model=model, backend=backend, duration=duration,
            output_schema=output_schema,
        )
        return self.fleet.run_request(req)

    def job_request(self, job: dict[str, Any]) -> RunRequest:
        """Validate a run_many job dict into a ``RunRequest`` (no agent spawn).

        Same fields as ``run_many`` jobs: ``{client?, goal, task_id?, context_files?,
        read_paths?, model?, backend?, duration?, output_schema?}``. Strips ``then`` and
        ``workspace`` (registry-only). Used by single-repo ``run_many`` and the registry's
        cross-workspace fan-out so validation stays fail-fast before any worktree is created.
        """
        body = {k: v for k, v in job.items() if k not in ("then", "workspace")}
        return self._request_for(
            body.get("client"),
            body["goal"],
            body.get("task_id"),
            body.get("context_files"),
            body.get("read_paths"),
            model=body.get("model"),
            backend=body.get("backend"),
            duration=body.get("duration"),
            output_schema=body.get("output_schema"),
        )

    def run_many_job(self, job: dict[str, Any]) -> RunManyJob:
        """Validate one run_many job dict (including optional ``then``) into a ``RunManyJob``."""
        then_raw = job.get("then")
        then_req = self.job_request(then_raw) if then_raw else None
        return RunManyJob(request=self.job_request(job), then=then_req)

    def run_request_captured(self, req: RunRequest) -> RunRecord:
        """Run one request; capture any failure as a FAILED record (batch-safe, never raises)."""
        return self.fleet._run_request(req)

    def run_many_chain_captured(self, job: RunManyJob) -> RunManyJobResult:
        """Run one run_many chain; capture failures as FAILED records (batch-safe, never raises)."""
        return self.fleet._run_many_chain(job)

    def run_many(self, jobs: list[dict[str, Any]], *, max_concurrency: int = 4) -> list[RunManyJobResult]:
        """Run several clients in parallel. Each job is
        {client, goal, task_id?, context_files?, read_paths?, model?, backend?, duration?, then?}.

        Optional ``then`` is the same field set as a job; it runs in the same worker as soon as that
        job's primary finishes (does not wait for sibling jobs). Client names and ``then`` specs are
        validated up front, so a typo fails fast before any run starts. A job may also be specified
        ad-hoc as {backend, model, goal, ...} with no 'client' key. A job's optional `duration`
        (preset name or positive seconds) overrides the resolved timeout_s.
        """
        prepared = [self.run_many_job(j) for j in jobs]
        return self.fleet.run_many(prepared, max_concurrency=max_concurrency)

    def spawn(
        self,
        client_name: str | None = None,
        goal: str = "",
        *,
        task_id: str | None = None,
        context_files: list[str] | None = None,
        read_paths: list[str] | None = None,
        base_branch: str | None = None,
        model: str | None = None,
        backend: str | None = None,
        duration: str | int | None = None,
        output_schema: dict[str, Any] | None = None,
    ) -> RunRecord:
        """Start a worker agent in the background; return its RUNNING record at once.

        Returns before worktree provisioning (``setup_cmd`` / read_paths) completes — the record
        is pollable and cancellable during setup. Same delegation primitive as ``run_agent``
        (product may be a diff or text). Poll ``status()`` / ``get_run()``.
        """
        req = self._request_for(
            client_name, goal, task_id, context_files, read_paths,
            base_branch=base_branch,
            model=model, backend=backend, duration=duration,
            output_schema=output_schema,
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
        # `is not None`: explicit "" must fail closed via TaskSpec, not become a generated id.
        bench_id = task_id if task_id is not None else uuid.uuid4().hex[:8]
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
        # cheapest: only strategies that succeeded AND have a known cost - native or a real provider
        # admin-api cost (e.g. EastRouter). Never an "unavailable" one.
        priced = [
            r for r in rows
            if r.status == RunStatus.EXITED_CLEAN.value
            and r.source in (UsageSource.NATIVE, UsageSource.ADMIN_API)
        ]
        cheapest = min(priced, key=lambda r: r.cost_usd).client if priced else None
        timed = [r for r in rows if r.status == RunStatus.EXITED_CLEAN.value and r.duration_ms > 0]
        fastest = min(timed, key=lambda r: r.duration_ms).client if timed else None
        return BenchmarkResult(
            task_id=task_id, goal=goal, strategies=rows, cheapest=cheapest, fastest=fastest
        )

    def get_run(self, run_id: str) -> RunRecord | None:
        self.fleet.reconcile_orphans()
        rec = self.fleet.state.get(run_id)
        return self.fleet.with_liveness(rec) if rec is not None else None

    def run_log(self, run_id: str) -> str | None:
        """The full raw stdout/stderr persisted for a run, or None if no log was written.

        Each terminal run (success or failure) gets one file under `<base>/logs/<run_id>.log` with
        a clear `=== run <id> ===` header, a `--- stdout ---` section, and a `--- stderr ---`
        section - the FULL streams, not the truncated `text` on the run record. None when no log
        exists (e.g. a run predating log storage, or a backend that crashed before producing one).
        """
        return self.fleet.logs.read(run_id)

    def read_run_file(self, run_id: str, path: str, *, max_bytes: int = 200_000) -> RunFile:
        """Read ONE file out of a run's worktree, so its output can reach the next agent.

        The gap this closes: an agent that produces a report has no way to hand it to the next run.
        `collect_run` returns the whole diff (the wrong granularity, and it re-derives content the
        driver already knows it wants), and `context_files` deliberately refuses paths outside the
        target worktree - correctly, since that guard is what keeps a run inside its boundary.

        This is a READ. It copies nothing and starts nothing, so the driver stays the one deciding
        what the next agent sees - which is where that judgement belongs, because the driver is what
        reviewed the output. To have a later run BUILD ON earlier work rather than read its
        conclusions, use `commit_run` + `base_branch` chaining instead; that is a different need.

        Containment is enforced the same way as `context_files`: an absolute path or one that
        escapes via `..` is refused, because `Path(wt) / "/etc/passwd"` is `/etc/passwd`.
        """
        rec = self.fleet.state.get(run_id)
        if rec is None:
            raise ValueError(f"no such run: {run_id!r}")
        if not rec.worktree:
            raise ValueError(f"run {run_id!r} has no worktree to read from")
        base = Path(rec.worktree).resolve()
        if not base.exists():
            raise ValueError(
                f"run {run_id!r}'s worktree is gone (cleaned); its files cannot be read"
            )
        target = (base / path).resolve()
        if target != base and base not in target.parents:
            raise ValueError(
                f"path {path!r} resolves outside run {run_id!r}'s worktree - it must be relative "
                f"to the worktree root and stay inside it"
            )
        if not target.is_file():
            # Re-check the worktree: a `clean` landing between the guard above and here makes every
            # path inside it "not a file", which would blame the caller's path for the worktree
            # being gone. Same state, same diagnostic, whenever the caller arrived.
            if not base.exists():
                raise ValueError(
                    f"run {run_id!r}'s worktree is gone (cleaned); its files cannot be read"
                )
            raise ValueError(f"{path!r} is not a file in run {run_id!r}'s worktree")
        # Read only what we will return (+1 byte to detect truncation), never the whole file.
        # `read_bytes()` would load an agent-produced artifact of ANY size into the MCP server's
        # memory before slicing it - the caller picks the path, so the size is not ours to assume.
        # `size_bytes` comes from stat(), so the reported size stays exact regardless.
        # The checks above are a snapshot: `clean` can remove the worktree between them and this
        # read. Report that as the SAME cleaned-worktree error the pre-check raises, rather than
        # leaking a raw OSError - one state must not produce two different diagnostics depending on
        # which microsecond the caller arrived in.
        try:
            size_bytes = target.stat().st_size
            with target.open("rb") as stream:
                raw = stream.read(max_bytes + 1)
        except OSError as exc:
            if not base.exists():
                raise ValueError(
                    f"run {run_id!r}'s worktree is gone (cleaned); its files cannot be read"
                ) from exc
            raise ValueError(f"cannot read {path!r} in run {run_id!r}'s worktree: {exc}") from exc
        # Truncate rather than hand back something unbounded - and SAY SO, because silently
        # returning a prefix would let a driver act on a partial report believing it was whole.
        truncated = len(raw) > max_bytes
        body = raw[:max_bytes].decode("utf-8", errors="replace")
        return RunFile(
            run_id=run_id, path=path, content=body, truncated=truncated, size_bytes=size_bytes
        )

    def collect_run(self, run_id: str) -> CollectResult:
        return self.fleet.collect_run(run_id)

    def commit_run(self, run_id: str, *, message: str | None = None) -> CommitResult:
        """Freeze a finished run's work onto its branch so a dependent run can chain off it."""
        return self.fleet.commit_run(run_id, message=message)

    def cancel_run(self, run_id: str) -> RunRecord:
        return self.fleet.cancel_run(run_id)

    def integrate(
        self, run_id: str, *, message: str | None = None, cleanup: bool = False
    ) -> IntegrateResult:
        # `message` was already supported by the Fleet but stopped here, so no caller could reach
        # it: every integrate landed as "marshal: integrate <run_id>", describing the tooling
        # instead of the change, and had to be rewritten by hand afterwards.
        return self.fleet.integrate(run_id, message=message, cleanup=cleanup)

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
        self.fleet.reconcile_orphans()
        return [self.fleet.with_liveness(r) for r in self.fleet.state.list()]

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

    # --- teams: adversarial review panels over the same primitives ---------------------------

    @property
    def teams_dir(self) -> Path:
        return self.repo_root / "teams"

    def diff_range(
        self, base: str, head: str | None = None, *, paths: list[str] | None = None
    ) -> str:
        """Read-only diff of ``base...head`` on the driver's checkout (a team `range` subject).

        Both refs are verified with ``rev-parse`` and refused if they look like options, because
        this argument is caller-supplied over MCP: a ``base`` starting with ``-`` would be parsed
        by git as a flag, and ``--output=<path>`` turns a "read-only" diff into an arbitrary file
        write (while emptying stdout, so the panel would then review nothing). ``--`` alone does
        not help - a rev cannot sit after it. Uses the WorktreeManager's guarded git wrapper
        (closed stdin, hard timeout, ``GIT_TERMINAL_PROMPT=0``).
        """
        head = head or "HEAD"
        for label, ref in (("base", base), ("head", head)):
            if not ref or ref.startswith("-"):
                raise ConfigError(
                    f"invalid {label} ref {ref!r}: refs cannot be empty or start with '-' "
                    "(that would be read as a git option, not a revision)"
                )
            probe = self.fleet.worktrees.git_read("rev-parse", "--verify", "--quiet", f"{ref}^{{commit}}")
            if probe.returncode != 0:
                raise ConfigError(f"unknown {label} ref {ref!r}")
        # Validate BEFORE building argv. An empty pathspec makes git fail with a message that
        # blames the refs, and a newline-bearing path would break out of the single-line subject
        # header the reviewer prompt is built from - both are caller errors, named as such here.
        for spec in paths or []:
            if not spec or spec.startswith("-"):
                raise ConfigError(
                    f"invalid path {spec!r}: paths cannot be empty or start with '-' "
                    "(that would be read as a git option, not a path)"
                )
            if any(ch in spec for ch in "\r\n"):
                raise ConfigError(f"invalid path {spec!r}: paths cannot contain newlines")
        # `--` separates revs from pathspecs, so a path can never be read as a rev or an option.
        pathspec = ["--", *paths] if paths else []
        try:
            proc = self.fleet.worktrees.git_read("diff", f"{base}...{head}", *pathspec)
        except WorktreeError as exc:  # a hung git must surface as the documented error type
            raise ConfigError(f"cannot diff {base}...{head}: {exc}") from exc
        if proc.returncode != 0:
            raise ConfigError(f"cannot diff {base}...{head}: {proc.stderr.strip() or 'git failed'}")
        return proc.stdout

    def list_teams(self) -> TeamListing:
        """Discover review teams under ``<repo>/teams/`` (well-formed and broken)."""
        return discover_teams(self.teams_dir)

    def run_team(
        self,
        name: str,
        subject: TeamSubject,
        *,
        max_concurrency: int = 4,
    ) -> TeamReview:
        """Run a review team by name (or path) against a subject and persist its reports.

        Validates the team - including the fail-closed read-only check on every role - before any
        reviewer spawns. Returns every reviewer's report plus a unified report for the requesting
        agent to read first; both are persisted under ``.marshal/reports/<stamp>-<team>-<id>/``.

        The engine computes no verdict and never integrates. Reading the reviews and deciding what
        they mean is the caller's job.
        """
        # A team file is prompt text delivered to fleet agents, so it must come from THIS repo's
        # teams/ directory. Without containment, `name="../../evil.yaml"` (or an absolute path)
        # would let a caller hand fully attacker-authored rubrics to the fleet. A bare name is
        # resolved against teams_dir; a path is accepted only if it lands inside it.
        if Path(name).suffix in (".yaml", ".yml"):
            directory = self.teams_dir.resolve()
            path = (directory / name if not Path(name).is_absolute() else Path(name)).resolve()
            if not path.is_relative_to(directory):
                raise ConfigError(
                    f"team file {name!r} is outside {directory}; teams must live in the "
                    "workspace's teams/ directory"
                )
            if not path.exists():
                raise ConfigError(f"no team file at {path}")
        else:
            path = find_team(name, self.teams_dir)
        spec = load_team(path)
        result = TeamRunner(self).run(
            spec,
            subject,
            team_run_id=uuid.uuid4().hex[:8],
            max_concurrency=max_concurrency,
        )
        self._write_reports(result)
        # Rendered after the per-role paths are stamped on, so the unified report can point at them.
        result.unified_report = render_unified_report(result)
        result.unified_report_path = self._write_unified(result)
        return result

    def _write_reports(self, result: TeamReview) -> None:
        """Persist one markdown file per reviewer. A write failure must not lose the reports."""
        try:
            directory = reports_dir(self.repo_root) / report_dirname(
                result.name, result.team_run_id, stamp=utc_stamp()
            )
            directory.mkdir(parents=True, exist_ok=True)
        except OSError as exc:
            print(f"marshal: could not create team report directory: {exc}", file=sys.stderr)
            return
        result.report_dir = str(directory)
        for review in result.reviews:
            try:
                out = directory / f"{review.role}.md"
                out.write_text(
                    render_role_report(
                        review, team=result.name, subject_summary=result.subject_summary
                    ),
                    encoding="utf-8",
                )
                review.report_path = str(out)
            except OSError as exc:
                print(f"marshal: could not write {review.role} report: {exc}", file=sys.stderr)

    def _write_unified(self, result: TeamReview) -> str | None:
        """Persist the unified report next to the per-role ones."""
        if result.report_dir is None:
            return None
        try:
            out = Path(result.report_dir) / "README.md"
            out.write_text(result.unified_report, encoding="utf-8")
            return str(out)
        except OSError as exc:
            print(f"marshal: could not write unified team report: {exc}", file=sys.stderr)
            return None
