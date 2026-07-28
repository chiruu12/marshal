"""The Fleet orchestrator - ties backends + worktrees + usage + state into one run loop.

`Fleet.run(...)` is the cohesive unit: create an isolated worktree, run the chosen backend in it,
record the usage event, persist the run's state, and (by default) keep the worktree so its diff can
be collected/integrated later. Backends are injected (a dict name -> backend) so the Fleet is
testable without real CLIs; the MCP/CLI layer supplies real ones via the registry.
"""

from __future__ import annotations

import contextlib
import fcntl
import json
import logging
import os
import signal
import subprocess
import sys
import tempfile
import threading
import time
import uuid
from collections.abc import Mapping
from concurrent.futures import ThreadPoolExecutor
from datetime import datetime, timedelta, timezone
from pathlib import Path
from typing import Any

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

# Process-wide in-flight run ids keyed by ``<repo>/.marshal`` (resolved). A replacement Fleet
# constructed in the same MCP server process (config hot-reload) shares this map with the evicted
# Fleet's background pool, so startup reaping must not touch those runs even when they have no pid
# yet (e.g. a test backend that overrides run() without spawning).
_active_runs_guard = threading.Lock()
_active_runs: dict[str, dict[str, "_RunHandle"]] = {}


class _RunHandle:
    """Live state for a run started by THIS process, used to cancel it safely.

    A pid alone is not safe to signal: the OS recycles pids. A child's pid is held until its parent
    reaps it, so signalling is safe exactly while the run loop is between spawn and reap - which is
    what ``exited`` tracks. ``cancel_requested`` covers the other end: a cancel that arrives before
    the pid is known is applied as soon as it is.
    """

    __slots__ = ("pid", "exited", "cancel_requested")

    def __init__(self) -> None:
        self.pid: int | None = None
        self.exited = False
        self.cancel_requested = False


def _marshal_base_key(runs_dir: Path) -> str:
    return str(runs_dir.resolve().parent)


def _register_inflight_run(runs_dir: Path, run_id: str) -> "_RunHandle":
    key = _marshal_base_key(runs_dir)
    with _active_runs_guard:
        handle = _RunHandle()
        _active_runs.setdefault(key, {})[run_id] = handle
        return handle


def _unregister_inflight_run(runs_dir: Path, run_id: str) -> None:
    key = _marshal_base_key(runs_dir)
    with _active_runs_guard:
        active = _active_runs.get(key)
        if active is not None:
            active.pop(run_id, None)
            if not active:
                del _active_runs[key]


def _publish_pid(handle: "_RunHandle", pid: int) -> bool:
    """Record a newly spawned child's pid on ``handle``; True if a cancel is already pending.

    Clears ``exited``: a published pid means a LIVE child. The handle is reused across retries, so
    an exit recorded by a previous attempt would otherwise make cancel skip signalling the retry.
    """
    with _active_runs_guard:
        handle.pid = pid
        handle.exited = False
        return handle.cancel_requested


def _inflight_handle(runs_dir: Path, run_id: str) -> "_RunHandle | None":
    key = _marshal_base_key(runs_dir)
    with _active_runs_guard:
        return _active_runs.get(key, {}).get(run_id)


def _inflight_in_this_process(runs_dir: Path, run_id: str) -> bool:
    return _inflight_handle(runs_dir, run_id) is not None


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


#: Raw status strings that mean "not finished". Used as a cheap text pre-filter over run records
#: before paying for model validation (see `_reap_orphaned_runs`).
_NON_TERMINAL_STATUSES = (RunStatus.RUNNING.value, RunStatus.QUEUED.value)


def _is_terminal(rec: RunRecord) -> bool:
    """True once a run has stopped - i.e. it is neither queued nor still running."""
    return rec.status not in (RunStatus.RUNNING.value, RunStatus.QUEUED.value)


#: Stale non-terminal runs reaped at Fleet startup are stamped ``failed``: the supervising process
#: vanished before Marshal recorded an outcome, so we cannot honestly claim success, cancellation,
#: or timeout. ``error`` carries the reap reason; ``pid`` is cleared so ``cancel_run`` can never
#: signal a reused OS pid.
_ORPHAN_REAP_ERROR = (
    "fleet: run orphaned at startup (supervising process exited before run completed)"
)


def _pid_alive(pid: int) -> bool:
    """True when ``pid`` still names a live process (signal 0 probe).

    Liveness only - it says nothing about WHOSE process it is. See ``_pid_is_still_ours``.
    """
    try:
        os.kill(pid, 0)
        return True
    except ProcessLookupError:
        return False
    except OSError:
        # Permission denied or other ambiguity: assume alive so we never reap a live run.
        return True


def _pid_start_time(pid: int) -> str | None:
    """The OS-reported start time of ``pid``, or None when it cannot be determined.

    A pid alone is not an identity: the OS reuses pids, so "something is alive at pid 4242" does
    not mean "our agent is alive". Pairing the pid with its start time makes the identity
    verifiable. POSIX-only via ``ps``; None on any failure, and callers must treat None as
    "unverifiable", never as "different".
    """
    try:
        proc = subprocess.run(
            ["ps", "-o", "lstart=", "-p", str(pid)],
            capture_output=True, text=True, timeout=5, stdin=subprocess.DEVNULL,
        )
    except (OSError, subprocess.SubprocessError):
        return None
    started = proc.stdout.strip()
    return started or None


def _pid_is_still_ours(rec: RunRecord) -> bool:
    """Whether ``rec.pid`` still names the agent this run started.

    Fails OPEN (True = "assume it is ours, do not reap") whenever identity cannot be established:
    no recorded start time (an older record), or the probe is unavailable. That direction is
    deliberate. Falsely reaping a LIVE run is destructive and silent - the record is stamped
    failed, its pid cleared so it can never be cancelled, and its real outcome is never recorded
    because the terminal stamp is guarded on the status still being running. Failing to reap a
    stale record only leaves it visible as running until someone explicitly calls ``cancel_run``,
    and only then can a wrong process be signalled.
    """
    if rec.pid is None or not _pid_alive(rec.pid):
        return False
    if not rec.pid_start_time:
        return True  # unverifiable (record predates the field) - fail open
    now = _pid_start_time(rec.pid)
    if now is None:
        return True  # probe unavailable (non-POSIX, permission) - fail open
    return now == rec.pid_start_time


def _another_fleet_active(lock_path: Path) -> bool:
    """True when another Marshal Fleet process holds ``base/fleet.lock`` and is still alive."""
    if not lock_path.exists():
        return False
    try:
        data = json.loads(lock_path.read_text(encoding="utf-8"))
        pid = int(data["pid"])
    except (OSError, ValueError, KeyError, json.JSONDecodeError):
        return False  # corrupt/stale lock - treat as inactive; this Fleet will reclaim it
    if pid == os.getpid():
        return False
    return _pid_alive(pid)


def _reap_orphaned_runs(state: FleetState) -> None:
    """Terminal-stamp persisted ``running``/``queued`` runs left by a prior Fleet instance.

    Callers MUST have established that no other live Fleet supervises this repo (see the
    ``fleet.lock`` check in ``Fleet.__init__``) - this function does not re-check.

    A new Fleet's in-process pool starts empty, so any non-terminal record on disk is orphaned
    unless the agent subprocess is still running (per-record ``pid`` probe) or another Fleet in
    THIS process still owns it (config hot-reload). Reaping clears ``pid`` so a later
    ``cancel_run`` can never ``killpg`` a reused pid. Corrupt records are skipped with a warning.
    """
    if not state.dir.exists():
        return
    for path in sorted(state.dir.glob("*.json")):
        try:
            rec = RunRecord.model_validate_json(path.read_text(encoding="utf-8"))
        except (ValidationError, OSError, ValueError) as exc:
            print(f"[marshal] skipping unreadable run record {path.name}: {exc}", file=sys.stderr)
            continue
        if _is_terminal(rec):
            continue
        if _inflight_in_this_process(state.dir, rec.run_id):
            continue  # another Fleet in this process still owns the run (config hot-reload)
        if _pid_is_still_ours(rec):
            continue  # our agent is genuinely still running (MCP died, the child survived)
        try:
            state.update_if(
                rec.run_id,
                lambda r: not _is_terminal(r),
                status=RunStatus.FAILED.value,
                pid=None,
                ended_at=_now(),
                error=_ORPHAN_REAP_ERROR,
            )
        except Exception as exc:  # noqa: BLE001 - startup reaping must never crash Fleet construction
            print(f"[marshal] failed to reap orphaned run {rec.run_id}: {exc}", file=sys.stderr)


def _claim_fleet_lock(lock_path: Path) -> bool:
    """Atomically become this repo's Fleet supervisor. True only if THIS process won the claim.

    The whole decision - read the holder, judge liveness, take over - runs under an advisory
    ``flock`` on a sibling guard file, so it is one critical section rather than three steps other
    processes can interleave with.

    Two earlier attempts were not enough, and both failure modes are worth remembering:
    ``O_CREAT | O_EXCL`` then writing the pid leaves the lock EMPTY for a moment, and a competing
    process reading it in that window saw an unparseable file, concluded "no live holder", and took
    over. Publishing by hard-link fixed that, but the stale-lock path still did unlink-then-create:
    two processes that both found a dead holder could both unlink - the second deleting the FIRST's
    freshly published lock - and both end up believing they won.

    ``flock`` is released by the OS when the process exits, so a crash mid-decision cannot wedge
    it. The lock file itself is never released: a long-lived server keeps it, and a short-lived CLI
    leaves a dead pid the next process takes over.
    """
    try:
        lock_path.parent.mkdir(parents=True, exist_ok=True)
    except OSError as exc:
        print(f"[marshal] failed to create fleet lock dir: {exc}", file=sys.stderr)
        return False

    guard_path = lock_path.with_name(lock_path.name + ".guard")
    try:
        guard = open(guard_path, "a+")  # noqa: SIM115 - closed explicitly below
    except OSError as exc:
        print(f"[marshal] failed to open fleet lock guard: {exc}", file=sys.stderr)
        return False
    try:
        try:
            fcntl.flock(guard.fileno(), fcntl.LOCK_EX | fcntl.LOCK_NB)
        except OSError:
            # Another process is deciding right now. Whoever it is will reconcile; we stand down
            # rather than racing it.
            return False
        if _another_fleet_active(lock_path):
            return False
        try:
            _write_lock_payload(lock_path)
        except OSError as exc:
            print(f"[marshal] failed to write fleet lock: {exc}", file=sys.stderr)
            return False
        return True
    finally:
        guard.close()  # releases the flock


def _write_lock_payload(lock_path: Path) -> None:
    """Write this process's pid to the lock atomically (temp + replace, never half-written)."""
    fd, tmp_str = tempfile.mkstemp(dir=str(lock_path.parent), prefix="fleet.lock.", suffix=".tmp")
    try:
        with os.fdopen(fd, "w", encoding="utf-8") as f:
            f.write(json.dumps({"pid": os.getpid()}))
        os.replace(tmp_str, lock_path)
    except BaseException:
        with contextlib.suppress(OSError):
            os.unlink(tmp_str)
        raise


def _base_branch_drift_warning(rec: RunRecord | None, target: str) -> tuple[bool, str]:
    """Warn when integrate's target differs from the branch the run was spawned from."""
    if rec is None or rec.base_branch is None or rec.base_branch == target:
        return False, ""
    return True, f"warning: run was based on {rec.base_branch!r}, merging into {target!r}"


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
    """A run's work surfaced read-only for the driver to review.

    Uncommitted work lives in ``changed_files`` / ``diff`` (working tree vs HEAD). Commits the
    agent made on the run's branch since the merge-base with the collect target are in
    ``committed_changed_files`` / ``committed_diff`` / ``commit_count`` — both sections are
    reported when both exist.
    """

    run_id: str
    branch: str | None
    worktree: str | None
    changed_files: list[str]
    diff: str
    committed_changed_files: list[str] = []
    committed_diff: str = ""
    commit_count: int = 0


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
    base_branch_drift: bool = False  # True when merge target differs from the run's recorded base


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
    # DRY RUN ONLY: per-candidate `{run_id, unmerged_commits, merged_into}`. The reported blocker
    # was never the filters - it was not knowing which worktrees held work nobody had landed:
    # "I couldn't tell which held unmerged work that wasn't mine", so 84 worktrees accumulated.
    # Computing it costs a git call per candidate, which is fine for a deliberate preview and is
    # why it is not on the every-row `status` listing.
    unmerged: list[dict[str, Any]] = []
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
        budget_gate: EnforceBudgetGate | None = None,
        session_start: datetime | None = None,
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
        # The gate is injectable (like run_gate) so a layer that REBUILDS Fleets over the same
        # ledger - the workspace registry on config hot-reload - can keep ONE gate per repo:
        # in-flight runs on the evicted Fleet still hold slots the replacement consults, and the
        # old Fleet's terminal release frees them for the new one. Default: a private gate,
        # exactly the prior single-Fleet behavior.
        self._budget_gate = budget_gate if budget_gate is not None else EnforceBudgetGate()
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
        # without restating the timestamp. Injectable for the same reason as budget_gate: a
        # rebuilt Fleet must not silently reset every `window: session` budget clock.
        self.session_start: datetime = (
            session_start if session_start is not None else datetime.now(timezone.utc)
        )
        # Reap ONLY as the winner of an atomic claim. Checking liveness and then writing was a
        # TOCTOU: two Fleets could both pass the check and both reap. Winning the claim is the
        # permission to reconcile.
        if _claim_fleet_lock(base / "fleet.lock"):
            _reap_orphaned_runs(self.state)

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
            _unregister_inflight_run(self.state.dir, run_id)
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
            resolved_base = self.worktrees.resolve_base_branch(req.task.base_branch)
            with self._create_lock:
                wt = self.worktrees.create(run_id, base_branch=req.task.base_branch)
            # Pin the sha AFTER creation, from the new worktree's own branch tip. Resolving the ref
            # beforehand was racy: if the base branch moved between the lookup and `worktree add`,
            # the record claimed one commit while the worktree was cut from another, and reviews
            # were then computed against a base the agent never had. The created branch's tip IS
            # what it was cut from, so there is no window to lose.
            resolved_base_commit = self.worktrees.branch_tip(wt.branch) if wt.branch else None
            self.worktrees.setup(wt)
            _register_inflight_run(self.state.dir, run_id)
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
                    base_branch=resolved_base,
                    base_commit=resolved_base_commit,
                    started_at=started,
                )
            )
            self._budget_gate.bind(budget_keys, run_id)
            return run_id, wt, started
        except Exception:
            self._budget_gate.release(budget_keys)
            raise

    @property
    def budget_gate(self) -> EnforceBudgetGate:
        """The enforce-budget concurrency gate this Fleet consults (read-only accessor).

        Exposed so the workspace registry can carry the SAME gate into a replacement Fleet on
        config hot-reload (see workspaces.WorkspaceRuntime) instead of forking it.
        """
        return self._budget_gate

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
            handle = _inflight_handle(self.state.dir, run_id)

            def _record_pid(pid: int) -> None:
                # Stamp the pid together with its start time: the pair is an identity a later
                # process can verify, where a bare pid can be silently reused by the OS.
                self.state.update(run_id, pid=pid, pid_start_time=_pid_start_time(pid))
                if handle is None:
                    return
                pending_cancel = _publish_pid(handle, pid)
                if pending_cancel:
                    # Cancel arrived before the pid existed; apply it now rather than leaving the
                    # agent running behind an already-terminal record.
                    with contextlib.suppress(ProcessLookupError, OSError):
                        os.killpg(pid, signal.SIGTERM)

            def _record_exit() -> None:
                # The child has been reaped, so its pid may now be recycled - never signal it again.
                if handle is not None:
                    with _active_runs_guard:
                        handle.exited = True

            opts = RunOpts(
                cwd=wt.path,
                permission=req.permission,
                model=req.model,
                timeout_s=req.timeout_s,
                on_pid=_record_pid,
                on_exit=_record_exit,
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
            _unregister_inflight_run(self.state.dir, run_id)
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

    def _collect_target(self, rec: RunRecord | None = None) -> str:
        """Merge-base reference for a run's committed work.

        The run's OWN recorded base when we have it - NOT whatever is checked out now. Those are
        different questions: `integrate` merges into the current branch (that is the user's intent
        at merge time), but a *review* has to be computed against the base the agent actually
        started from. Using the current branch means a checkout between spawn and collect silently
        changes the diff: commits inherited from an unrelated branch appear as the agent's work, or
        the agent's own commits vanish because the new target already contains them.

        Falls back to the current branch for records written before `base_branch` existed.
        """
        # Prefer the pinned sha over the branch name: the name may point somewhere else now.
        if rec is not None and rec.base_commit:
            return rec.base_commit
        if rec is not None and rec.base_branch:
            return rec.base_branch
        try:
            return self.worktrees.current_branch()
        except WorktreeError:
            return "HEAD"  # detached checkout: diff against the checked-out commit

    def collect_run(self, run_id: str) -> CollectResult:
        """Surface a run's diff + changed files. Read-only - nothing is merged."""
        wt = self._worktree_for(run_id)
        rec = self.state.get(run_id)
        changed_files = self.worktrees.changed_files(wt)
        diff = self.worktrees.diff(wt)
        committed_changed_files: list[str] = []
        committed_diff = ""
        commit_count = 0
        if wt.branch:
            target = self._collect_target(rec)
            commit_count = self.worktrees.unmerged_commit_count(wt.branch, target)
            if commit_count:
                committed_changed_files = self.worktrees.merged_diff_files(wt.branch, target)
                committed_diff = self.worktrees.merged_diff(wt.branch, target)
        return CollectResult(
            run_id=run_id,
            branch=wt.branch or None,
            worktree=str(wt.path),
            changed_files=changed_files,
            diff=diff,
            committed_changed_files=committed_changed_files,
            committed_diff=committed_diff,
            commit_count=commit_count,
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

    def _unmerged_count(self, rec: RunRecord) -> int | None:
        """Commits on this run's branch that the current branch does not have, or None if unknown.

        None is "cannot tell", never "zero" - the branch is gone, or git could not be asked. A
        driver deciding whether a worktree is safe to drop must be able to distinguish "nothing to
        lose" from "I could not find out", because they justify opposite actions.
        """
        if not rec.branch:
            return None
        try:
            return self.worktrees.unmerged_commit_count(rec.branch, self.worktrees.current_branch())
        except WorktreeError:
            return None

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
                # The safety answer, for the one call where the user is deciding rather than acting.
                result.unmerged.append({
                    "run_id": rec.run_id,
                    "unmerged_commits": self._unmerged_count(rec),
                    "merged_into": rec.merged_into,
                })
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
        cancel_error: str | None = None
        cancel_extra: dict[str, object] = {}
        # Signal only through a live handle for a run THIS process started. A child's pid is held
        # by the OS until its parent reaps it, so the handle's `exited` flag marks exactly when the
        # pid stops being safe to signal. A run owned by another (or dead) process gets no signal:
        # guessing at a pid we do not own risks SIGTERM to an unrelated process group.
        handle = _inflight_handle(self.state.dir, run_id)
        if handle is None:
            cancel_extra["pid"] = None
            cancel_error = (
                "fleet: cancelled without signalling - this run was started by another process, "
                "so its pid cannot be confirmed to still belong to the agent"
            )
        else:
            with _active_runs_guard:
                handle.cancel_requested = True
                pid, exited = handle.pid, handle.exited
            if exited:
                pass  # the agent already finished; the terminal stamp below is all that is left
            elif pid is None:
                # Cancel beat the pid: `_record_pid` applies it the moment the pid is known, so the
                # agent is stopped rather than left running behind a terminal record.
                pass
            else:
                with contextlib.suppress(ProcessLookupError, OSError):
                    os.killpg(pid, signal.SIGTERM)
        stamp: dict[str, object] = {"status": "cancelled", "ended_at": _now(), **cancel_extra}
        if cancel_error:
            stamp["error"] = cancel_error
        # Stamp cancelled ONLY if the run is still running - update_if does the re-check and the
        # write atomically under the per-run lock, so a run that finished (succeeded/failed) between
        # the kill and now is never overwritten with "cancelled".
        return self.state.update_if(
            run_id, lambda r: r.status == RunStatus.RUNNING.value, **stamp
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
        drift, drift_msg = _base_branch_drift_warning(rec, target)
        return IntegrateResult(
            run_id=run_id,
            status="merged",
            branch=wt.branch,
            merged_into=target,
            changed_files=changed,
            commit=commit,
            base_branch_drift=drift,
            message=drift_msg,
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
