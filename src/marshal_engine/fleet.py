"""The Fleet orchestrator - ties backends + worktrees + usage + state into one run loop.

`Fleet.run(...)` is the cohesive unit: create an isolated worktree, run the chosen backend in it,
record the usage event, persist the run's state, and (by default) keep the worktree so its diff can
be collected/integrated later. Backends are injected (a dict name -> backend) so the Fleet is
testable without real CLIs; the MCP/CLI layer supplies real ones via the registry.
"""

from __future__ import annotations

import contextlib
import logging
import os
import signal
import sys
import threading
import time
import uuid
from collections.abc import Callable, Mapping
from concurrent.futures import ThreadPoolExecutor
from datetime import datetime, timedelta, timezone
from pathlib import Path

from pydantic import BaseModel, ValidationError

from .backends.base import CodingAgentBackend
from .budgets import BudgetExceeded as BudgetExceeded
from .budgets import BudgetStatus as BudgetStatus
from .budgets import EnforceBudgetGate as EnforceBudgetGate
from .budgets import check_budget as check_budget
from .budgets import compute_budget_status as compute_budget_status
from .config import BudgetSpec
from .eastrouter import CostResolver, default_cost_resolvers
from .env import merge_user_path
from .layout import marshal_dir
from .logs import RunLogStore
from .pricing import PriceTable, PricingError
from .retry import RetryPolicy, is_transient_failure
from .state import FleetState, RunRecord
from .types import AgentResult, PermissionMode, RunOpts, RunStatus, TaskSpec, UsageRecord, UsageSource
from .usage import UsageEvent, UsageTracker
from .worktree import Worktree, WorktreeError, WorktreeManager

logger = logging.getLogger(__name__)


def _now() -> str:
    return datetime.now(timezone.utc).isoformat()


def _still_running(rec: RunRecord) -> bool:
    """update_if predicate: stamp a terminal status only if the run hasn't already reached one
    (e.g. been cancelled concurrently), so a cancel that won the race is never overwritten."""
    return rec.status == RunStatus.RUNNING.value


#: Terminal, non-success run statuses that `clean` reclaims by default (no un-landed work worth keeping).
#: VERIFY_FAILED is included deliberately: its worktree survives the run itself for review, but a
#: driver-invoked clean of finished runs is a post-review action - review before cleaning.
_CLEANABLE_NONSUCCESS = frozenset(
    {
        RunStatus.FAILED.value,
        RunStatus.TIMED_OUT.value,
        RunStatus.CANCELLED.value,
        RunStatus.EMPTY.value,
        RunStatus.VERIFY_FAILED.value,
    }
)


def _is_terminal(rec: RunRecord) -> bool:
    """True once a run has stopped - i.e. it is neither queued nor still running."""
    return rec.status not in (RunStatus.RUNNING.value, RunStatus.QUEUED.value)


def _in_clean_scope(rec: RunRecord, scope: str) -> bool:
    """Whether `clean(scope=...)` should reclaim this run (a running/queued run never is)."""
    if not _is_terminal(rec):
        return False
    if scope == "all":
        return True
    if scope == "merged":
        return rec.merged_into is not None
    if scope == "finished":
        return rec.merged_into is not None or rec.status in _CLEANABLE_NONSUCCESS
    raise ValueError(f"unknown clean scope: {scope!r} (use 'merged', 'finished', or 'all')")


def _ended_before(rec: RunRecord, cutoff: datetime | None) -> bool:
    """True if the run ended at or before `cutoff` (always True when no age filter is set).

    A run with no parseable `ended_at` is treated as NOT old enough under an age filter - we don't
    reclaim a run whose age we can't establish.
    """
    if cutoff is None:
        return True
    if not rec.ended_at:
        return False
    try:
        ended = datetime.fromisoformat(rec.ended_at)
    except ValueError:
        return False
    if ended.tzinfo is None:
        ended = ended.replace(tzinfo=timezone.utc)
    return ended <= cutoff


def _load_default_prices() -> PriceTable:
    """Load the shipped price table; on any problem fall back to empty (everything unpriced)."""
    try:
        return PriceTable.load()
    except PricingError as exc:
        print(f"[marshal] price table unavailable: {exc}; costs will be unpriced", file=sys.stderr)
        return PriceTable({})


class CollectResult(BaseModel):
    """A run's uncommitted work, surfaced read-only for the driver to review."""

    run_id: str
    branch: str | None
    worktree: str | None
    changed_files: list[str]
    diff: str


class IntegrateResult(BaseModel):
    """Outcome of merging a run's worktree branch back into the current branch.

    status is one of: "merged" (changes landed), "conflict" (merge aborted, resolve manually),
    "blocked" (the target checkout is dirty/colliding or detached HEAD - nothing changed, fixable
    then retry), "empty" (the run produced no changes to integrate), or "error" (a git operation
    failed in a way the engine can't classify as cleanly recoverable - commit failure, repo left
    mid-merge, op timeout; surface to a human, see `message`, don't blindly retry).
    """

    run_id: str
    status: str
    branch: str | None = None
    merged_into: str | None = None
    changed_files: list[str] = []
    conflicts: list[str] = []
    commit: str | None = None
    message: str = ""


class CommitResult(BaseModel):
    """Outcome of freezing a run's work onto its own branch (so a dependent run can chain off it).

    status: "committed" (a new commit was made), "clean" (no *new* commit was needed - the working
    tree was already clean; this is NOT "the branch is empty", e.g. an agent that self-committed),
    "blocked" (the run is still in progress; wait for it to finish), or "error" (a git op failed -
    see `message`). To chain, always use `branch`/`commit` regardless of status - `commit` is the
    branch tip whenever it could be resolved, the concrete ref to base a dependent run on
    (`spawn(..., base_branch=branch)`). Don't gate chaining on `status == "committed"`.
    """

    run_id: str
    status: str
    branch: str | None = None
    commit: str | None = None
    message: str = ""


class CleanResult(BaseModel):
    """Outcome of tearing down finished runs' worktrees + branches (the usage ledger is untouched).

    Reclaims the disk-heavy worktrees; the run-state records are kept so status/history stay
    queryable. A run that is still running is never cleaned (reported under `skipped`).
    """

    removed: list[str] = []
    skipped: list[dict[str, str]] = []  # {run_id, reason}
    errors: list[dict[str, str]] = []   # {run_id, error}
    # Worktree dirs under the manager's base_dir with NO (readable) run record - leaked by a
    # hand-pruned or torn ledger file. Reaped by scope-mode cleans (see Fleet.clean).
    orphans_removed: list[str] = []
    dry_run: bool = False


class StrategyResult(BaseModel):
    """One strategy's measured outcome in a benchmark (the run's recorded facts)."""

    run_id: str
    client: str | None
    backend: str
    model: str | None
    status: str
    cost_usd: float
    source: str | None
    duration_ms: int
    input_tokens: int
    output_tokens: int


class BenchmarkResult(BaseModel):
    """Same task run through N strategies, compared on measured cost/latency/outcome (derived).

    `cheapest`/`fastest` name the winning client among *comparable* strategies only - succeeded,
    and (for cheapest) with a known cost (native/admin-api/estimated, never `unavailable`). None when
    no strategy qualifies. The per-strategy rows carry `source` so an estimate is never read as truth.
    """

    task_id: str
    goal: str
    strategies: list[StrategyResult] = []
    cheapest: str | None = None
    fastest: str | None = None


class RunRequest(BaseModel):
    """One unit of work for a parallel batch (the same parameters Fleet.run takes)."""

    backend_name: str
    task: TaskSpec
    permission: PermissionMode = PermissionMode.SAFE_EDIT
    model: str | None = None
    client: str | None = None
    timeout_s: int = 600
    usage_api: str | None = None  # provider usage-API for real cost (e.g. "eastrouter"); see eastrouter.py


class Fleet:
    def __init__(
        self,
        repo_root: Path | str,
        backends: Mapping[str, CodingAgentBackend],
        *,
        base_dir: Path | str | None = None,
        worktree_base: Path | str | None = None,
        worktree_setup: list[str] | None = None,
        verify: list[str] | None = None,
        allow_unsafe_commands: bool = False,
        integrate_run_hooks: bool = False,
        retries: RetryPolicy | None = None,
        prices: PriceTable | None = None,
        cost_resolvers: Mapping[str, CostResolver] | None = None,
        run_gate: threading.Semaphore | None = None,
        budgets: list[BudgetSpec] | None = None,
        on_run_complete: Callable[[RunRecord, str | None], None] | None = None,
    ) -> None:
        # Recover the user's interactive PATH so a Fleet constructed in a context that didn't
        # source the user's rc files (an MCP server with a stripped PATH) still spawns agent
        # CLIs from user-managed locations (Homebrew, npm-global, ~/.local/bin). mcp_server.main
        # and cli.main already do this at process entry, but Fleet is a public engine primitive -
        # a library caller (or test) that constructs a Fleet directly without going through the
        # CLI/MCP entry would otherwise spawn agents against a broken PATH. Idempotent + cached,
        # so the duplicate call is a no-op. MARSHAL_NO_PATH_FIX=1 still opts out.
        merge_user_path()
        self.repo_root = Path(repo_root)
        base = Path(base_dir) if base_dir is not None else marshal_dir(self.repo_root)
        self.worktrees = WorktreeManager(
            self.repo_root,
            worktree_base or base / "worktrees",
            setup_cmd=worktree_setup,
            verify_cmd=verify,
            allow_unsafe_commands=allow_unsafe_commands,
            integrate_run_hooks=integrate_run_hooks,
        )
        self.state = FleetState(base / "runs")
        self.usage = UsageTracker(base / "usage")
        self.logs = RunLogStore(base / "logs")
        self.backends: dict[str, CodingAgentBackend] = dict(backends)
        self.prices = prices if prices is not None else _load_default_prices()
        # Provider usage-API resolvers (keyed by a client's `usage_api`) that backfill REAL cost from a
        # provider's ledger after a run. Injectable for tests; defaults to the built-ins (EastRouter).
        self.cost_resolvers: dict[str, CostResolver] = (
            dict(cost_resolvers) if cost_resolvers is not None else default_cost_resolvers()
        )
        # Retry only transient (infra/transport) failures. Default off so a bare Fleet behaves
        # exactly as before; the service turns it on from config (see MarshalService).
        self.retries = retries if retries is not None else RetryPolicy()
        # Optional PROCESS-WIDE cap on concurrent agent runs. One Fleet per repo caps its own
        # fan-out (run_many pool, spawn pool), but a multi-workspace server runs N Fleets - so the
        # MCP layer shares ONE semaphore across all of them to bound total agent processes (each
        # CLI is 150-400 MB). None = uncapped here (a single-repo Fleet keeps its prior behavior).
        self._run_gate = run_gate
        # $ budgets per scope (backend / client / global) and time window (session / week / month).
        # None / [] = no budgets. Default is soft-warn; enforce=true raises BudgetExceeded and
        # serializes matching in-flight spawns via EnforceBudgetGate (see budgets.py).
        self.budgets: list[BudgetSpec] = list(budgets) if budgets else []
        self._budget_gate = EnforceBudgetGate()
        # `git worktree add` is the one step that races across threads; serialize just that (it's
        # milliseconds - the long-running agent runs still proceed fully in parallel).
        self._create_lock = threading.Lock()
        # integrate() commits + merges in the SHARED repo checkout; serialize it so two concurrent
        # integrates can't race git's index.lock and leave the repo mid-merge.
        self._integrate_lock = threading.Lock()
        # Persistent pool for non-blocking spawn(); lives as long as this Fleet (i.e. the long-lived
        # MCP server) so background runs outlive the driver turn that started them. Guard its lazy
        # init so concurrent first spawns don't build two pools (one would leak, undrained).
        self._bg: ThreadPoolExecutor | None = None
        self._bg_lock = threading.Lock()
        self._bg_max = 4
        # When this Fleet (the long-lived MCP server) started. The MCP `usage` tool maps a `window`
        # of "session" to this instant, so the driver can see what it has spent THIS session
        # without restating the timestamp.
        self.session_start: datetime = datetime.now(timezone.utc)
        self._on_run_complete = on_run_complete

    def run(
        self,
        backend_name: str,
        task: TaskSpec,
        *,
        permission: PermissionMode = PermissionMode.SAFE_EDIT,
        model: str | None = None,
        client: str | None = None,
        timeout_s: int = 600,
        usage_api: str | None = None,
        ts: str | None = None,
        cleanup: bool = False,
    ) -> RunRecord:
        """Run one task synchronously: worktree -> backend -> usage -> persist. Blocks until done."""
        req = RunRequest(
            backend_name=backend_name,
            task=task,
            permission=permission,
            model=model,
            client=client,
            timeout_s=timeout_s,
            usage_api=usage_api,
        )
        return self.run_request(req, ts=ts, cleanup=cleanup)

    def run_request(
        self,
        req: RunRequest,
        *,
        ts: str | None = None,
        cleanup: bool = False,
    ) -> RunRecord:
        """Run one RunRequest synchronously: worktree -> backend -> usage -> persist. Blocks until done."""
        run_id, wt, started = self._start(req, ts)
        return self._execute(req, run_id, wt, started, cleanup=cleanup)

    def spawn(self, request: RunRequest, *, ts: str | None = None) -> str:
        """Start a run in the background and return its run_id immediately (does NOT wait).

        The run is recorded RUNNING synchronously (so `status()`/`get_run()` see it at once), then
        the agent executes on a persistent pool that outlives this call - so background runs survive
        the driver turn that started them. The driver polls for the terminal status.
        """
        run_id, wt, started = self._start(request, ts)
        try:
            self._executor().submit(self._execute_bg, request, run_id, wt, started)
        except RuntimeError as exc:
            # The pool was shut down between _start and submit; don't strand a RUNNING record
            # or an enforce-budget concurrency slot.
            self._budget_gate.release_run(run_id)
            self.state.update(
                run_id, status=RunStatus.FAILED.value, ended_at=_now(),
                error=f"spawn: executor unavailable: {exc}",
            )
            raise
        return run_id

    def shutdown(self, *, wait: bool = True) -> None:
        """Shut the background spawn pool (drains in-flight runs). A no-op if none were spawned."""
        if self._bg is not None:
            self._bg.shutdown(wait=wait)
            self._bg = None

    def _executor(self) -> ThreadPoolExecutor:
        if self._bg is None:
            with self._bg_lock:
                if self._bg is None:
                    self._bg = ThreadPoolExecutor(
                        max_workers=self._bg_max, thread_name_prefix="marshal-spawn"
                    )
        return self._bg

    def _start(self, req: RunRequest, ts: str | None) -> tuple[str, Worktree, str]:
        """Synchronous prefix: validate, create the worktree, record RUNNING -> (run_id, wt, ts)."""
        # Budget gate FIRST - BEFORE the worktree is created. Advisory budgets soft-warn;
        # enforce=true budgets raise BudgetExceeded (ledger cap and/or concurrent in-flight slot).
        # Advisory lookup failures degrade silently; enforced lookup failures fail closed.
        budget_keys = self._budget_gate.begin(
            self.usage, self.session_start, self.budgets, req
        )
        try:
            if req.backend_name not in self.backends:
                raise ValueError(f"no such backend: {req.backend_name!r}")
            backend = self.backends[req.backend_name]
            modes = backend.capabilities.permission_modes
            if modes and req.permission not in modes:
                supported = ", ".join(sorted(m.value for m in modes))
                raise ValueError(
                    f"backend {req.backend_name!r} does not support permission "
                    f"{req.permission.value!r} (supported: {supported})"
                )
            # Pure argv preflight before worktree create (e.g. Goose rejects `provider/` / `/model`).
            backend.build_invocation(
                req.task,
                RunOpts(
                    cwd=self.repo_root,
                    permission=req.permission,
                    model=req.model,
                    timeout_s=req.timeout_s,
                ),
            )
            started = ts or _now()
            # Globally unique: a retry or same-task fan-out must not collide on the branch, the worktree
            # dir, or the state record. task_id stays the grouping key on RunRecord.
            run_id = f"{req.task.id}.{req.backend_name}.{uuid.uuid4().hex[:8]}"
            # Serialize only `git worktree add` (it races across threads but is milliseconds). Provision
            # the worktree (`setup`, e.g. `uv sync`) OUTSIDE the lock so a fan-out runs N setups in
            # parallel instead of one-at-a-time behind the lock. setup() tears the worktree down + raises
            # on failure, so a failed provision leaves no orphan and never records a RUNNING run.
            with self._create_lock:
                wt = self.worktrees.create(run_id, base_branch=req.task.base_branch)
            self.worktrees.setup(wt)
            self.state.add(
                RunRecord(
                    run_id=run_id,
                    task_id=req.task.id,
                    backend=req.backend_name,
                    client=req.client,
                    model=req.model,
                    status=RunStatus.RUNNING.value,
                    worktree=str(wt.path),
                    branch=wt.branch,
                    started_at=started,
                )
            )
            self._budget_gate.bind(budget_keys, run_id)
            return run_id, wt, started
        except Exception:
            self._budget_gate.release(budget_keys)
            raise

    def _check_budget(self, req: RunRequest) -> None:
        """Ledger-only budget check (tests / diagnostics). Spawn path uses ``_budget_gate.begin``."""
        check_budget(self.usage, self.session_start, self.budgets, req)

    def budget_status(self, now: datetime | None = None) -> list[BudgetStatus]:
        return compute_budget_status(
            self.usage, self.session_start, self.budgets, now or datetime.now(timezone.utc),
        )

    def _execute(
        self, req: RunRequest, run_id: str, wt: Worktree, ts: str, *, cleanup: bool = False
    ) -> RunRecord:
        """Execute suffix: run the backend, price + classify, persist the terminal record."""
        backend = self.backends[req.backend_name]
        result: AgentResult | None = None
        record: RunRecord | None = None
        try:
            def _record_pid(pid: int) -> None:
                self.state.update(run_id, pid=pid)

            opts = RunOpts(
                cwd=wt.path,
                permission=req.permission,
                model=req.model,
                timeout_s=req.timeout_s,
                on_pid=_record_pid,
            )
            # Hold a slot for the agent run (the heavy, memory-hungry part) - including any transient
            # retry backoff, since the run is still in flight. Worktree creation/provision in _start
            # already happened outside the slot; a no-op context when ungated.
            gate = self._run_gate if self._run_gate is not None else contextlib.nullcontext()
            with gate:
                result, attempts = self._run_with_retries(backend, req.task, opts, run_id)
            usage = backend.extract_usage(result)    # the seam (default: result.usage)
            self._price_usage(usage, req.model)      # normalize cost + source (estimate/unavailable)
            self._apply_external_cost(usage, req, start_iso=ts)  # backfill REAL cost if a usage_api is set
            status = self._authoritative_status(result, wt)
            # The workspace's optional verify gate: only a would-be-SUCCEEDED run that actually
            # CHANGED FILES is gated (the EMPTY downgrade already happened above; a text-only
            # reply can't have broken the repo, so don't burn a full test run on an unchanged
            # tree). A failed gate demotes to VERIFY_FAILED; the worktree is kept for review.
            verify_passed: bool | None = None
            verify_output = ""
            if (
                status is RunStatus.SUCCEEDED
                and self.worktrees.verify_cmd
                and self._worktree_has_changes(wt)
            ):
                verify_passed, verify_output = self.worktrees.verify(wt)
                if not verify_passed:
                    status = RunStatus.VERIFY_FAILED
            event = UsageEvent.from_result(
                result, run_id=run_id, backend=req.backend_name, ts=ts, usage=usage,
                client=req.client, model=req.model,
            )
            event.status = status.value              # report the authoritative outcome (incl. EMPTY)
            self.usage.record(event)
            # Stamp the terminal record ONLY if the run is still running, so a `cancel_run` that
            # already marked it `cancelled` (the common cancel-wins-first race) is preserved rather
            # than clobbered by this thread returning from the SIGTERM-killed subprocess. The usage
            # event above is the immutable spend record regardless; this is the lifecycle status.
            record = self.state.update_if(
                run_id,
                _still_running,
                status=status.value,
                cost_usd=event.cost_usd,
                input_tokens=event.input_tokens,
                output_tokens=event.output_tokens,
                duration_ms=result.duration_ms,
                source=event.source,
                text=result.text[:16000],  # the agent's final message, so reply/analysis tasks are reviewable
                ended_at=_now(),
                error=result.error,
                attempts=attempts,
                verify_passed=verify_passed,
                verify_output=verify_output,
            )
            if self._on_run_complete is not None:
                try:
                    diff: str | None = None
                    try:
                        d = self.worktrees.diff(wt)
                        diff = d if d else None
                    except Exception:  # noqa: BLE001 - best-effort diff for memory hook
                        pass
                    self._on_run_complete(record, diff)
                except Exception:  # noqa: BLE001 - memory hook must never fail a run
                    logger.exception("marshal: on_run_complete hook failed for run %s", run_id)
        except Exception as exc:  # noqa: BLE001 - never leave a run stranded as RUNNING
            # Terminal-stamp the record before re-raising, so one failure can't leave a zombie - but
            # only if still running, so a concurrent cancel's terminal status wins.
            self.state.update_if(
                run_id, _still_running, status=RunStatus.FAILED.value, ended_at=_now(), error=f"fleet: {exc}"
            )
            raise
        finally:
            # Release enforce-budget concurrency slots once spend is (or would have been) recorded
            # so the next matching spawn can re-check the ledger.
            self._budget_gate.release_run(run_id)
            # Persist the FULL raw stdout/stderr for every terminal run (success OR failure) so a
            # driver can inspect what the agent actually did after the fact. Best-effort: a logging
            # failure (disk full, permission, ...) must never break a finished run; stderr the cause
            # for visibility. Skipped when no AgentResult was produced (e.g. the backend crashed
            # before parse_output returned - there is nothing to log). On a retried run this is the
            # final attempt's output.
            if result is not None:
                try:
                    self.logs.write(
                        run_id,
                        result.raw_stdout or "",
                        result.raw_stderr or "",
                    )
                except Exception as exc:  # noqa: BLE001 - log persistence is best-effort, never breaks a run
                    print(f"[marshal] {run_id}: failed to persist run log: {exc}", file=sys.stderr)

        if cleanup:
            self.worktrees.remove(wt)
        return record

    def _run_with_retries(
        self, backend: CodingAgentBackend, task: TaskSpec, opts: RunOpts, run_id: str
    ) -> tuple[AgentResult, int]:
        """Run the backend, retrying only on a transient (infra/transport) failure with backoff.

        Returns the final result and the number of attempts made. The worktree is reused across
        attempts: the markers we retry on (DB lock, rate limit, 5xx, connection errors) happen at
        startup/transport time, before an agent writes anything, so there is nothing to reset. A
        genuine task failure or a timeout is returned as-is - never retried.
        """
        attempt = 1
        while True:
            result = backend.run(task, opts)
            if attempt >= self.retries.max_attempts or not is_transient_failure(result):
                return result, attempt
            delay = self.retries.delay_for(attempt)
            print(
                f"[marshal] {run_id}: transient failure (attempt {attempt}/"
                f"{self.retries.max_attempts}), retrying in {delay:.1f}s: {result.error}",
                file=sys.stderr,
            )
            time.sleep(delay)
            attempt += 1

    def _execute_bg(self, req: RunRequest, run_id: str, wt: Worktree, ts: str) -> None:
        """Background variant: the outcome (incl. failure) is already persisted; never propagate."""
        try:
            self._execute(req, run_id, wt, ts)
        except Exception:  # noqa: BLE001 - _execute already terminal-stamped; the driver polls status()
            pass

    def run_many(
        self,
        requests: list[RunRequest],
        *,
        max_concurrency: int = 4,
        stagger_s: float = 0.1,
    ) -> list[RunRecord]:
        """Run a batch of requests concurrently in isolated worktrees; block until all finish.

        Concurrency is capped at `max_concurrency` (each agent CLI is 150-400 MB, so an uncapped
        fan-out OOMs the host). Submissions are spaced by `stagger_s` to ease the Cursor
        concurrent-launch file-lock race. A single request's failure is captured as a FAILED record
        and never aborts the batch. Records are returned in the same order as `requests`.
        """
        results: list[RunRecord | None] = [None] * len(requests)
        with ThreadPoolExecutor(max_workers=max(1, max_concurrency)) as pool:
            futures = {}
            for i, req in enumerate(requests):
                if stagger_s and i:
                    time.sleep(stagger_s)
                futures[pool.submit(self._run_request, req)] = i
            for fut in futures:
                results[futures[fut]] = fut.result()  # _run_request never raises
        return [r for r in results if r is not None]

    def _run_request(self, req: RunRequest) -> RunRecord:
        """run_request one request, capturing any failure as a FAILED record so a batch survives it."""
        try:
            return self.run_request(req)
        except Exception as exc:  # noqa: BLE001 - one job's failure must not abort the batch
            return RunRecord(
                run_id=f"{req.task.id}.{req.backend_name}",
                task_id=req.task.id,
                backend=req.backend_name,
                client=req.client,
                model=req.model,
                status=RunStatus.FAILED.value,
                ended_at=_now(),
                error=f"run_many: {exc}",
            )

    def _price_usage(self, usage: UsageRecord | None, model: str | None) -> None:
        """Normalize cost + source in place: keep native cost, else estimate, else unavailable.

        `source` describes how we know the COST. Tokens are kept regardless; a tokened run with no
        price is `unavailable` (cost unknown), never a misleading $0.
        """
        if usage is None:
            return
        if usage.source is UsageSource.NATIVE:
            return  # backend authoritatively reported the cost (a real $0 included) - never override
        if usage.input_tokens + usage.output_tokens <= 0:
            usage.cost_usd = 0.0
            usage.source = UsageSource.UNAVAILABLE
            return
        est = self.prices.estimate(
            model or usage.model,
            input_tokens=usage.input_tokens,
            output_tokens=usage.output_tokens,
            cache_read_tokens=usage.cache_read_tokens,
        )
        if est is None:
            usage.cost_usd = 0.0
            usage.source = UsageSource.UNAVAILABLE  # unpriced -> cost unavailable (tokens kept)
        else:
            usage.cost_usd = est
            usage.source = UsageSource.ESTIMATED

    def _apply_external_cost(self, usage: UsageRecord | None, req: RunRequest, *, start_iso: str) -> None:
        """Override cost with the REAL charge from a provider usage-API, when the client opts in.

        Runs after `_price_usage`: if the client declares a `usage_api` (e.g. "eastrouter") and the
        provider can attribute an actual cost to this run, replace the estimate with that real cost
        (`source = admin-api`). A failure or an unattributable run is a no-op - the estimate/unavailable
        cost stands. This must NEVER raise: a completed run is done, cost reconciliation is best-effort.
        """
        if usage is None or not req.usage_api:
            return
        resolver = self.cost_resolvers.get(req.usage_api)
        if resolver is None:
            return
        try:
            ext = resolver(
                model=req.model,
                start_iso=start_iso,
                end_iso=_now(),
                input_tokens=usage.input_tokens,
                output_tokens=usage.output_tokens,
            )
        except Exception:  # noqa: BLE001 - external cost lookup must never break a finished run
            return
        if ext is not None:
            usage.cost_usd = ext.cost_usd
            usage.source = ext.source

    def _worktree_has_changes(self, wt: Worktree) -> bool:
        """Whether the worktree holds uncommitted changes - the verify gate's trigger.

        Can't tell (a git failure) counts as changed: a wasted gate run beats a missed regression.
        """
        try:
            return bool(self.worktrees.changed_files(wt))
        except WorktreeError:
            return True

    def _authoritative_status(self, result: AgentResult, wt: Worktree) -> RunStatus:
        """A clean exit that produced no work (no text, no file changes) is EMPTY, not success."""
        if result.status is not RunStatus.SUCCEEDED:
            return result.status
        if result.text.strip():
            return RunStatus.SUCCEEDED
        try:
            changed = self.worktrees.changed_files(wt)
        except WorktreeError:
            return RunStatus.SUCCEEDED  # can't tell -> don't mislabel a success as empty
        return RunStatus.SUCCEEDED if changed else RunStatus.EMPTY

    def collect_run(self, run_id: str) -> CollectResult:
        """Surface a run's uncommitted diff + changed files. Read-only - nothing is merged."""
        wt = self._worktree_for(run_id)
        return CollectResult(
            run_id=run_id,
            branch=wt.branch or None,
            worktree=str(wt.path),
            changed_files=self.worktrees.changed_files(wt),
            diff=self.worktrees.diff(wt),
        )

    def commit_run(self, run_id: str, *, message: str | None = None) -> CommitResult:
        """Freeze a finished run's work as a commit on its OWN branch, so a dependent run can chain
        off it via ``spawn(..., base_branch=<that run's branch>)``.

        This is integrate's first half without the merge: it commits the worktree's work onto
        ``marshal/<run_id>`` but NEVER touches the driver's branch (worktree isolation holds).
        Without it, basing a worktree on a prior run's branch gets only the spawn base, because the
        agent left its work uncommitted and the branch ref never moved. Refuses a still-running run
        (its files are half-written). The immutable usage ledger is untouched.
        """
        rec = self.state.get(run_id)
        if rec is None:
            raise ValueError(f"no such run: {run_id!r}")
        if rec.status == RunStatus.RUNNING.value:
            return CommitResult(
                run_id=run_id,
                status="blocked",
                branch=rec.branch,
                message="run is still in progress; wait for it to finish before committing",
            )
        wt = self._worktree_for(run_id)
        if not wt.branch:
            raise ValueError(f"run {run_id!r} has no branch to commit")
        try:
            sha = self.worktrees.commit_all(wt, message or f"marshal: {run_id}")
            tip = self.worktrees.branch_tip(wt.branch)
        except WorktreeError as exc:
            return CommitResult(run_id=run_id, status="error", branch=wt.branch, message=str(exc))
        self.state.update(run_id, commit=tip)
        return CommitResult(
            run_id=run_id,
            status="committed" if sha is not None else "clean",
            branch=wt.branch,
            commit=tip,
        )

    def clean(
        self,
        *,
        scope: str = "finished",
        run_ids: list[str] | None = None,
        older_than_hours: float | None = None,
        dry_run: bool = False,
    ) -> CleanResult:
        """Tear down finished runs' worktrees + branches to reclaim disk; never a running run.

        The run's persisted log is reclaimed alongside its worktree (both disk-heavy). The immutable
        usage ledger is never touched, and run-state records are kept so status and
        cost history stay queryable (a cleaned run's worktree path simply no longer exists, which is
        what `collect_run`/`integrate` already report). ``scope`` (ignored when ``run_ids`` is given):
          * ``"merged"``   - only runs already integrated (``merged_into`` set). Safest.
          * ``"finished"`` - (default) merged runs + failed/timed_out/cancelled/empty runs; protects
            un-integrated *succeeded* work (a candidate you may still want to review).
          * ``"all"``      - every terminal run, including un-integrated succeeded ones. DESTRUCTIVE:
            this ``git branch -D``\\s those branches too, so an un-reviewed succeeded run's commits
            survive only in git's reflog until gc.
        ``run_ids`` cleans exactly those (still refuses a running run, reported under ``skipped``;
        ``older_than_hours`` is ignored in this mode). ``older_than_hours`` (scope mode only) keeps
        only runs that ended at least that long ago. ``dry_run`` reports what would be removed
        without touching anything.

        Scope-mode cleans also reconcile the worktree base dir against the ledger and reap
        ORPHANS - dirs whose run record is missing or unreadable (hand-pruned, or torn; a live run
        always has a readable record, so it is never touched). Reported under ``orphans_removed``;
        ``older_than_hours`` does not apply (an orphan has no trustworthy end timestamp).
        """
        result = CleanResult(dry_run=dry_run)
        if run_ids is not None:
            targets: list[RunRecord] = []
            for rid in run_ids:
                rec = self.state.get(rid)
                if rec is None:
                    result.skipped.append({"run_id": rid, "reason": "no such run"})
                elif not _is_terminal(rec):
                    result.skipped.append(
                        {"run_id": rid, "reason": f"not finished (status={rec.status})"}
                    )
                else:
                    targets.append(rec)
        else:
            cutoff = datetime.now(timezone.utc) - timedelta(hours=older_than_hours) \
                if older_than_hours is not None else None
            targets = [
                r for r in self.state.list()
                if _in_clean_scope(r, scope) and _ended_before(r, cutoff)
            ]
        for rec in targets:
            if dry_run:
                result.removed.append(rec.run_id)
                continue
            try:
                self.worktrees.discard(rec.worktree or "", rec.branch)
                self.logs.remove(rec.run_id)  # reclaim the (untruncated) run log too; best-effort
                result.removed.append(rec.run_id)
            except WorktreeError as exc:
                result.errors.append({"run_id": rec.run_id, "error": str(exc)})
        if run_ids is None and self.worktrees.base_dir.exists():
            # Reconcile the worktree dir against the ledger: a dir whose run record is gone
            # (hand-pruned) or unreadable (torn/corrupt - state.list() silently skips those) is
            # invisible to every ledger-driven pass above and would leak forever. Scoped strictly
            # to Marshal's own base_dir, so foreign worktrees are never touched. A genuinely
            # running run always has a readable record (writes are atomic temp+replace) and is
            # skipped here; an explicit run_ids clean targets exactly those runs, so no sweep.
            for child in sorted(self.worktrees.base_dir.iterdir()):
                if not child.is_dir():
                    continue
                rid = child.name  # the dir name IS the run_id (_start passes it as the task_id)
                try:
                    known = self.state.get(rid) is not None
                except (ValidationError, OSError, ValueError):
                    known = False  # unreadable record: unreachable via get_run/cancel - garbage
                if known:
                    continue  # ledger-owned; the scope pass above already decided its fate
                if dry_run:
                    result.orphans_removed.append(rid)
                    continue
                try:
                    self.worktrees.discard(child, f"{self.worktrees.branch_prefix}/{rid}")
                    self.logs.remove(rid)
                    result.orphans_removed.append(rid)
                except WorktreeError as exc:
                    result.errors.append({"run_id": rid, "error": str(exc)})
        return result

    def cancel_run(self, run_id: str) -> RunRecord:
        """Cancel a running run: SIGTERM its process group, then mark cancelled.

        If the run is not running (or its pid is missing / already exited) this is a safe no-op
        that still returns the (updated) record. The run may finish concurrently between the status
        check and the kill - re-read the record before stamping to avoid overwriting a terminal
        status with ``cancelled``.
        """
        rec = self.state.get(run_id)
        if rec is None:
            raise ValueError(f"no such run: {run_id!r}")
        if rec.status != RunStatus.RUNNING.value:
            return rec
        if rec.pid is not None:
            try:
                os.killpg(rec.pid, signal.SIGTERM)
            except (ProcessLookupError, OSError):
                pass  # already exited
        # Stamp cancelled ONLY if the run is still running - update_if does the re-check and the
        # write atomically under the per-run lock, so a run that finished (succeeded/failed) between
        # the kill and now is never overwritten with "cancelled".
        return self.state.update_if(
            run_id, lambda r: r.status == RunStatus.RUNNING.value, status="cancelled", ended_at=_now()
        )

    def integrate(
        self, run_id: str, *, message: str | None = None, cleanup: bool = False
    ) -> IntegrateResult:
        """Merge a run's worktree branch back into the current branch, handling conflicts.

        Commits the worktree's uncommitted work onto its branch, then merges that branch into
        the repo's current branch. Outcomes: "merged" (stamps `merged_into`), "conflict" (aborted,
        repo left clean), "blocked" (target dirty/colliding or detached HEAD - fix it and retry),
        or "empty" (nothing to integrate). The blocked/conflict commit stays on the branch, so a
        retry after fixing the target re-merges it instead of reporting "empty".

        Serialized per Fleet (it commits + merges in the shared repo checkout, so two concurrent
        integrates would race git's index.lock and could leave the repo mid-merge).
        """
        with self._integrate_lock:
            return self._integrate_locked(run_id, message=message, cleanup=cleanup)

    def _integrate_locked(
        self, run_id: str, *, message: str | None = None, cleanup: bool = False
    ) -> IntegrateResult:
        rec = self.state.get(run_id)
        if rec is not None and rec.status == RunStatus.RUNNING.value:
            # Never commit a still-running agent's half-written files into the user's branch; the
            # run must reach a terminal state first. Recoverable -> "blocked" (wait, then retry).
            return IntegrateResult(
                run_id=run_id,
                status="blocked",
                branch=rec.branch,
                message="run is still in progress; wait for it to finish before integrating",
            )
        wt = self._worktree_for(run_id)
        if not wt.branch:
            raise ValueError(f"run {run_id!r} has no branch to integrate")
        try:
            target = self.worktrees.current_branch()  # refuses detached HEAD before committing
        except WorktreeError as exc:
            return IntegrateResult(run_id=run_id, status="blocked", branch=wt.branch, message=str(exc))

        try:
            commit = self.worktrees.commit_all(wt, message or f"marshal: integrate {run_id}")
            # "empty" only when the worktree is clean AND the branch has no commits past target.
            # (A prior blocked/conflict already committed the work, so a retry still has work to merge.)
            if commit is None and not self.worktrees.has_unmerged_commits(wt.branch, target):
                return IntegrateResult(run_id=run_id, status="empty", branch=wt.branch)
            # Report the FULL set of files this branch lands - every commit past the merge-base, not
            # just the last uncommitted delta (an agent may have self-committed). Computed BEFORE the
            # merge, since afterwards target...branch is empty.
            changed = self.worktrees.merged_diff_files(wt.branch, target)
            if commit is None:
                # retry: a prior blocked/conflict attempt already committed the work, so the
                # worktree is clean now - report the branch tip it lands.
                commit = self.worktrees.branch_tip(wt.branch)
            merge = self.worktrees.merge(wt.branch)
        except WorktreeError as exc:
            # a git op failed in a way we can't classify as cleanly recoverable (commit failure,
            # repo left mid-merge, timeout). Surface a distinct "error" status (not the recoverable
            # "blocked") so a driver doesn't blindly retry - the cause needs a human.
            return IntegrateResult(
                run_id=run_id, status="error", branch=wt.branch, merged_into=target, message=str(exc)
            )
        if merge.blocked:
            return IntegrateResult(
                run_id=run_id,
                status="blocked",
                branch=wt.branch,
                merged_into=target,
                commit=commit,
                message=merge.message,
            )
        if not merge.ok:
            return IntegrateResult(
                run_id=run_id,
                status="conflict",
                branch=wt.branch,
                merged_into=target,
                conflicts=merge.conflicts,
                commit=commit,
            )

        self.state.update(run_id, merged_into=target)
        if cleanup:
            self.worktrees.remove(wt)
        return IntegrateResult(
            run_id=run_id,
            status="merged",
            branch=wt.branch,
            merged_into=target,
            changed_files=changed,
            commit=commit,
        )

    def _worktree_for(self, run_id: str) -> Worktree:
        """Reconstruct the live Worktree for a recorded run, or raise if it is gone."""
        rec = self.state.get(run_id)
        if rec is None:
            raise ValueError(f"no such run: {run_id!r}")
        if not rec.worktree:
            raise ValueError(f"run {run_id!r} has no worktree")
        path = Path(rec.worktree)
        if not path.exists():
            raise ValueError(f"worktree for run {run_id!r} no longer exists: {path}")
        return Worktree(task_id=rec.task_id, path=path, branch=rec.branch or "")
