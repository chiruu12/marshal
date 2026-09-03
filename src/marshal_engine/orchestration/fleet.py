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
import shlex
import signal
import sys
import threading
import time
import uuid
from collections.abc import Callable, Mapping
from concurrent.futures import ThreadPoolExecutor
from datetime import UTC, datetime, timedelta
from pathlib import Path

from pydantic import ValidationError

from ..accounting.budgets import BudgetStatus as BudgetStatus
from ..accounting.budgets import EnforceBudgetGate as EnforceBudgetGate
from ..accounting.budgets import check_budget as check_budget
from ..accounting.budgets import compute_budget_status as compute_budget_status
from ..accounting.eastrouter import CostResolver, default_cost_resolvers
from ..accounting.usage import UsageEvent, UsageTracker, goal_digest
from ..backends.base import CodingAgentBackend
from ..core.config import BudgetSpec
from ..core.layout import (
    MARSHAL_DIRNAME,
    artifacts_dir,
    budget_gate_path,
    marshal_dir,
    run_artifacts_dir,
)
from ..core.retry import RetryPolicy, is_transient_failure
from ..core.types import (
    AgentResult,
    PermissionMode,
    ProgressTimeout,
    RunOpts,
    RunOutcome,
    RunStatus,
    TaskSpec,
    UsageRecord,
    UsageSource,
)
from ..runtime.env import merge_user_path, redact_secrets
from ..runtime.git_exclude import try_append_git_exclude
from ..runtime.logs import RunLogStore
from ..runtime.state import FleetState, RunRecord
from ..runtime.worktree import Worktree, WorktreeError, WorktreeManager, is_git_object_id
from .diagnostics import (
    _base_branch_drift_warning,
    _deferred_provision_error,
    _live_agent_message,
    _orphaned_base_diagnosis,
    _worktree_gone_message,
)
from .inflight import (
    _active_runs_guard,
    _clear_creating_claim,
    _creating_claim_held,
    _inflight_handle,
    _inflight_in_this_process,
    _publish_pid,
    _register_inflight_run,
    _unregister_inflight_run,
    _write_creating_claim,
)
from .liveness import (
    _agent_may_still_be_writing,
    _claim_fleet_lock,
    _identity_verdict,
    _is_terminal,
    _now,
    _pid_alive,
    _pid_is_still_ours,
    _pid_is_verifiably_ours,
    _pid_start_time,
    _supervisor_identity,
    with_liveness,
)
from .provisioning import (
    _provision_read_paths,
    _require_context_files,
    harvest_artifacts,
    prepare_artifact_dir,
    provision_run_artifacts,
)
from .reaping import _TMP_REAP_AGE_S, _reap_orphaned_runs
from .results import (
    CleanResult,
    CollectResult,
    CommitResult,
    IntegrateResult,
    RunManyJob,
    RunManyJobResult,
    RunRequest,
)
from .structured import (
    _apply_structured_output,
    _redact_structured,
    _task_with_schema_instruction,
)

logger = logging.getLogger(__name__)

#: Test-only seam: called after cancel snapshots the handle and before it re-checks/signals.
#: Production leaves this ``None``. Tests assign a callback to force the copy→reap window.
_cancel_after_handle_snapshot: Callable[[], None] | None = None

#: Test-only seam: called after the RUNNING record's ``os.replace`` and before the creating claim
#: is cleared. Production leaves this ``None``.
_after_creating_record_published: Callable[[], None] | None = None


#: Cap on the agent's final message as persisted in the run record. `text` is read back by every
#: status/collect call, so an unbounded field costs every reader of the record - but the cut has to
#: be *disclosed*, not silent (see RunRecord.text_truncated). The full stream stays in the run log.
_TEXT_CAP = 16000


def _cap_text(text: str) -> tuple[str, bool, int | None]:
    """Cap `text` for persistence. Returns (capped, was_truncated, full_len_if_truncated).

    `full_len` is None when nothing was cut, so the field means "this many characters existed"
    rather than doubling as a length readout that a caller could mistake for the stored length.
    """
    if len(text) <= _TEXT_CAP:
        return text, False, None
    return text[:_TEXT_CAP], True, len(text)


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
        ended = ended.replace(tzinfo=UTC)
    return ended <= cutoff



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
        allow_external_read_paths: bool = False,
        retries: RetryPolicy | None = None,
        cost_resolvers: Mapping[str, CostResolver] | None = None,
        run_gate: threading.Semaphore | None = None,
        budgets: list[BudgetSpec] | None = None,
        budget_gate: EnforceBudgetGate | None = None,
        session_start: datetime | None = None,
        progress_timeout: ProgressTimeout | None = None,
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
        # False keeps `read_paths` inside this workspace's repo. The name denylist cannot cover a
        # host; scope can - and it is what stops a read_path reaching another workspace's ledger.
        self.allow_external_read_paths = allow_external_read_paths
        base = Path(base_dir) if base_dir is not None else marshal_dir(self.repo_root)
        # Run directories do NOT follow `base_dir`. The ledger, logs and run records under
        # `<repo>/.marshal/` are Marshal-written and belong with the repo; the run's working tree is
        # the one directory an agent writes, and it lives outside the repo so a relative path from
        # the agent's cwd cannot climb into the checkout or the ledger (#175). Only an explicit
        # `worktree_base` overrides that - one rule, so there is no second path where the boundary
        # silently does not hold.
        self.worktrees = WorktreeManager(
            self.repo_root,
            worktree_base,
            setup_cmd=worktree_setup,
            verify_cmd=verify,
            allow_unsafe_commands=allow_unsafe_commands,
            integrate_run_hooks=integrate_run_hooks,
        )
        # Keep Marshal's own state out of the user's `git status`. Best-effort and local-only
        # (`.git/info/exclude`, never their tracked `.gitignore`): this is tidiness, so a directory
        # that is not a repo - or a `.git` we cannot write - must not fail the run that follows.
        # Placed here rather than in `marshal init` because a driver reaching Marshal over MCP
        # never runs init, and that is the common case.
        try_append_git_exclude(self.repo_root, f"{MARSHAL_DIRNAME}/")
        self.state = FleetState(base / "runs")
        self.usage = UsageTracker(base / "usage")
        self.logs = RunLogStore(base / "logs")
        self.backends: dict[str, CodingAgentBackend] = dict(backends)
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
        # Opt-in progress-aware timeout. Default (None / disabled) keeps the plain `timeout_s`
        # wall clock, so a bare Fleet behaves exactly as before; the service turns it on from
        # config, the same way `retries` works.
        self.progress_timeout = progress_timeout or ProgressTimeout()
        # The gate is injectable (like run_gate) so a layer that REBUILDS Fleets over the same
        # ledger - the workspace registry on config hot-reload - can keep ONE gate per repo:
        # in-flight runs on the evicted Fleet still hold slots the replacement consults, and the
        # old Fleet's terminal release frees them for the new one. Default: a private gate on
        # ``.marshal/budget_gate.json`` (cross-process flock; see layout.budget_gate_path).
        if budget_gate is not None:
            self._budget_gate = budget_gate
        else:
            gate_path = (
                budget_gate_path(self.repo_root)
                if base_dir is None
                else Path(base) / "budget_gate.json"
            )
            self._budget_gate = EnforceBudgetGate(path=gate_path)
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
            session_start if session_start is not None else datetime.now(UTC)
        )
        # Reap ONLY as the winner of an atomic claim. Checking liveness and then writing was a
        # TOCTOU: two Fleets could both pass the check and both reap. Winning the claim is the
        # permission to reconcile.
        self._reap_lock = threading.Lock()
        self._fleet_lock_path = base / "fleet.lock"
        self._owns_fleet_lock = _claim_fleet_lock(self._fleet_lock_path)
        with self._reap_lock:  # same mutual exclusion the later re-checks use
            if self._owns_fleet_lock:
                _reap_orphaned_runs(self.state)

    def reconcile_orphans(self) -> None:
        """Re-run reconciliation. Read surfaces call this - exactly when a stale record is believed.

        Deliberately NOT gated on a "something was deferred" flag. Such a flag can only be set at
        construction, so once cleared it stays cleared: an orphan created afterwards - by a process
        that started a run and died - would never be looked at again for the life of this Fleet.
        Rescanning costs one pass over the ledger, which every caller of this is doing anyway.

        A denied lock claim is retried rather than remembered. Ownership can be denied simply
        because a short-lived CLI held the guard for the moment we asked; treating that as
        permanent left a long-lived server that never reconciles again.
        """
        with self._reap_lock:
            if not self._owns_fleet_lock:
                self._owns_fleet_lock = _claim_fleet_lock(self._fleet_lock_path)
            if self._owns_fleet_lock:
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

        The run is recorded RUNNING after ``git worktree add`` but BEFORE provisioning
        (``read_paths`` / ``setup_cmd``, which can take up to ``setup_timeout_s``). ``RUNNING``
        therefore spans provisioning — ``pid`` / ``agent_alive`` may be null until setup or the
        agent publishes one. Provisioning and the agent then run on a persistent pool that
        outlives this call, so the driver can poll ``get_run`` / ``status`` and ``cancel_run``
        during setup.

        Cancel-during-setup guarantees: when a setup pid is published, ``cancel_run`` killpg's
        that process group; otherwise cancel is cooperative (pure-Python provisioning such as
        ``read_paths`` copies is not killable — only ``setup_cmd``'s timeout bounds that phase).
        The record is stamped ``cancelled`` immediately; the background task discards the
        worktree when it reaches the next cancel checkpoint; ``clean`` will not reap while the
        task is still in-flight in this process.
        """
        run_id, wt, started = self._start(request, ts, defer_provisioning=True)
        try:
            self._executor().submit(
                self._execute_bg, request, run_id, wt, started, deferred_provisioning=True
            )
        except RuntimeError as exc:
            # The pool was shut down between _start and submit; don't strand a RUNNING record
            # or an enforce-budget concurrency slot.
            self._budget_gate.release_run(run_id)
            with contextlib.suppress(WorktreeError):
                self.worktrees.discard(str(wt.path), wt.branch)
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

    def _start(
        self, req: RunRequest, ts: str | None, *, defer_provisioning: bool = False
    ) -> tuple[str, Worktree, str]:
        """Synchronous prefix: validate, create the worktree, record RUNNING -> (run_id, wt, ts).

        When ``defer_provisioning`` is False (``run`` / ``run_many``), context files, ``read_paths``,
        and ``setup_cmd`` run here before the RUNNING record is written — a provision failure then
        leaves no record (M2). When True (``spawn``), only ``git worktree add`` is synchronous; the
        RUNNING record is written immediately so the driver can poll/cancel, and provisioning runs
        inside the background ``_execute`` task (failures terminal-stamp the record there).
        """
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
            # Claim BEFORE create: the create→add gap has a directory on disk but no run record, so
            # a concurrent cross-process `clean` would otherwise treat it as an orphan (#181).
            _write_creating_claim(self.state.dir, run_id)
            try:
                # Serialize only `git worktree add` (it races across threads but is milliseconds). Provision
                # the worktree (`setup`, e.g. `uv sync`) OUTSIDE the lock so a fan-out runs N setups in
                # parallel instead of one-at-a-time behind the lock.
                resolved_base = self.worktrees.resolve_base_branch(req.task.base_branch)
                # Renew the unbound placeholder while `git worktree add` runs so a slow-but-alive
                # holder is not TTL-reclaimed mid-create; bind still verifies ownership on disk.
                wt = None
                try:
                    with self._budget_gate.keep_alive(budget_keys):
                        with self._create_lock:
                            wt = self.worktrees.create(run_id, base_branch=req.task.base_branch)
                except Exception:
                    if wt is not None:
                        with contextlib.suppress(WorktreeError):
                            self.worktrees.discard(str(wt.path), wt.branch)
                    raise
                assert wt is not None  # create either returned or raised
                # Pin the sha AFTER creation, from the new worktree's own branch tip. Resolving the ref
                # beforehand was racy: if the base branch moved between the lookup and `worktree add`,
                # the record claimed one commit while the worktree was cut from another, and reviews
                # were then computed against a base the agent never had. The created branch's tip IS
                # what it was cut from, so there is no window to lose.
                resolved_base_commit = self.worktrees.branch_tip(wt.branch) if wt.branch else None
                # Bind immediately after worktree create (before provision) so the durable
                # reservation carries a real run_id for the long setup window. Bind before the
                # RUNNING record so a reservation I/O / ownership failure is failure-atomic
                # (discard worktree/branch, then re-raise; outer release frees the slot).
                try:
                    self._budget_gate.bind(budget_keys, run_id)
                except Exception:
                    with contextlib.suppress(WorktreeError):
                        self.worktrees.discard(str(wt.path), wt.branch)
                    raise
                if not defer_provisioning:
                    # Sync path (run_agent): provision before recording so a failure leaves no RUNNING
                    # zombie and no orphan worktree (M2). setup() tears down + raises on failure.
                    try:
                        self._provision_worktree(wt, req)
                    except Exception:
                        with contextlib.suppress(WorktreeError):
                            self.worktrees.discard(str(wt.path), wt.branch)
                        raise
                _register_inflight_run(self.state.dir, run_id)
                _sup_pid, _sup_started = _supervisor_identity()
                # Publish the record FIRST (state.add → os.replace). Clear the claim only after
                # that replace: reverse order opens a gap where a concurrent sweep sees neither
                # claim nor record and discards a live worktree. Between replace and clear both
                # shields are up, so there is no unprotected window.
                self.state.add(
                    RunRecord(
                        run_id=run_id,
                        task_id=req.task.id,
                        backend=req.backend_name,
                        client=req.client,
                        model=req.model,
                        status=RunStatus.RUNNING.value,
                        # Who is supervising this run. Stamped at creation, before the agent
                        # even starts, so the record is never briefly reapable-by-default. Both
                        # halves or neither: a pid without a verifiable start time would be
                        # trusted on bare liveness alone, and after a reboot recycles the number
                        # that reads as a live supervisor forever - permanently unreapable, where
                        # the rule this replaced would have reaped it. `None` falls back to that
                        # rule, which is the honest answer when identity cannot be established.
                        supervisor_pid=_sup_pid,
                        supervisor_start_time=_sup_started,
                        worktree=str(wt.path),
                        branch=wt.branch,
                        base_branch=resolved_base,
                        base_commit=resolved_base_commit,
                        # What this worktree's environment came from. `None` means it was provisioned
                        # by nothing - a bare checkout - which is the sharpest form of the delta.
                        # shlex.join, not " ".join: the scaffolded form is `sh -c "cd sub && uv sync"`,
                        # and a plain join renders that as `sh -c cd sub && uv sync` - a DIFFERENT
                        # command. Provenance that misdescribes what ran is worse than none, since the
                        # whole point of this field is letting a driver trust where a number came from.
                        # For deferred spawn this is the *configured* command (not yet run); on setup
                        # failure the terminal error names the phase.
                        worktree_setup=(
                            shlex.join(self.worktrees.setup_cmd) if self.worktrees.setup_cmd else None
                        ),
                        read_paths=list(req.task.read_paths),
                        started_at=started,
                    )
                )
                if _after_creating_record_published is not None:
                    _after_creating_record_published()
                return run_id, wt, started
            finally:
                # Always release the claim: after a successful publish the record shields the dir;
                # on any failure (including a raise between publish and here) a stuck claim would
                # lock out orphan reclaim for the life of this process.
                _clear_creating_claim(self.state.dir, run_id)
        except Exception:
            self._budget_gate.release(budget_keys)
            raise

    def _provision_worktree(
        self,
        wt: Worktree,
        req: RunRequest,
        *,
        run_id: str | None = None,
    ) -> None:
        """Context files + read_paths + setup_cmd. Caller handles discard on pre-setup failure.

        When ``run_id`` is set (spawn's deferred path), ``setup`` publishes its pid on the in-flight
        handle so ``cancel_run`` can SIGTERM the setup process group.
        """
        _require_context_files(wt, req.task.context_files)
        _provision_read_paths(
                wt, self.repo_root, req.task.read_paths,
                allow_external=self.allow_external_read_paths,
            )
        provision_run_artifacts(wt, artifacts_dir(self.repo_root), req.task.artifacts_from)
        prepare_artifact_dir(wt)
        on_pid: Callable[[int], None] | None = None
        on_exit: Callable[[], None] | None = None
        # Holds the setup process's pid so it can be unstamped once setup is done. A list because
        # the hook that learns it is a closure running before this frame resumes.
        setup_pid_ref: list[int] = []
        if run_id is not None:
            handle = _inflight_handle(self.state.dir, run_id)
            if handle is not None:

                def _on_pid(pid: int) -> None:
                    setup_pid_ref.append(pid)
                    self.state.update_if(
                        run_id,
                        lambda r: not _is_terminal(r),
                        pid=pid,
                        pid_start_time=_pid_start_time(pid),
                    )
                    if _publish_pid(handle, pid):
                        with contextlib.suppress(ProcessLookupError, OSError):
                            os.killpg(pid, signal.SIGTERM)

                def _on_exit() -> None:
                    with _active_runs_guard:
                        handle.exited = True

                on_pid = _on_pid
                on_exit = _on_exit

        try:
            # Only pass cancel hooks when wired — keeps `setup(wt)` call shape for spies/tests.
            if on_pid is not None or on_exit is not None:
                self.worktrees.setup(wt, on_pid=on_pid, on_exit=on_exit)
            else:
                self.worktrees.setup(wt)
        finally:
            # Setup is over - it returned, failed, or was killed by a cancel - so its pid names a
            # process that no longer exists. Leaving it stamped means the record keeps naming a
            # corpse, and once the OS recycles the number, an unrelated live one: `agent_alive`,
            # the orphan reaper's "a stamped pid is decidable now" rule, and `clean`'s refusal to
            # reclaim a worktree whose pid looks alive all read it.
            #
            # In a `finally`, and with no terminal-status check, because the cancel path needs
            # both: `cancel_run` stamps `cancelled` while setup is still running and setup then
            # raises, so a clear placed after the call - or predicated on the record being
            # non-terminal - would skip exactly the case that strands a pid on a terminal record.
            #
            # On the task's own thread rather than in `_on_exit`, which runs on the callback
            # thread and contended with the concurrent terminal stamp for this run's lock.
            #
            # Matching the exact setup pid is the whole guard: it means this is our process and it
            # is gone, and it can never clobber a pid the agent has already stamped.
            if run_id is not None and setup_pid_ref:
                self.state.update_if(
                    run_id,
                    lambda r: r.pid == setup_pid_ref[-1],
                    pid=None,
                    pid_start_time=None,
                )

    def _run_deferred_provisioning(
        self, req: RunRequest, run_id: str, wt: Worktree
    ) -> RunRecord | None:
        """Spawn-path provisioning. Returns a terminal record if the run ended; else None.

        Guarantees: no zombie RUNNING — every failure/cancel path terminal-stamps. Setup/provision
        failures discard the worktree. Cancel intent always wins the *final* stamp: if
        ``cancel_requested`` is set when setup exits non-zero, this stamps ``cancelled`` (not
        ``failed``), even when the except path races ahead of ``cancel_run``'s own update_if.
        Pre-pid cancel is cooperative — see ``spawn`` docstring.
        """
        if self._cancel_requested(run_id):
            with contextlib.suppress(WorktreeError):
                self.worktrees.discard(str(wt.path), wt.branch)
            return self.state.update_if(
                run_id,
                _still_running,
                status=RunStatus.CANCELLED.value,
                ended_at=_now(),
                error="fleet: cancelled during setup",
            )
        try:
            self._provision_worktree(wt, req, run_id=run_id)
        except Exception as exc:  # noqa: BLE001 - stamp terminal; never leave RUNNING
            with contextlib.suppress(WorktreeError):
                if wt.path.exists():
                    self.worktrees.discard(str(wt.path), wt.branch)
            # Cancel intent wins even when killpg caused this exception and cancel_run's
            # RUNNING→cancelled update_if has not landed yet (Trace 1).
            if self._cancel_requested(run_id):
                return self.state.update_if(
                    run_id,
                    _still_running,
                    status=RunStatus.CANCELLED.value,
                    ended_at=_now(),
                    error=(
                        f"fleet: cancelled during setup "
                        f"({_deferred_provision_error(exc)})"
                    ),
                )
            err = _deferred_provision_error(exc)
            return self.state.update_if(
                run_id,
                _still_running,
                status=RunStatus.FAILED.value,
                ended_at=_now(),
                error=err,
            )
        if self._cancel_requested(run_id):
            # Setup/provision finished (or was a no-op) but cancel won before the agent —
            # discard the worktree and do not launch the backend. Discard here too: cancel may
            # have arrived mid-provision (pre-pid) after the opening checkpoint, so the start-of-
            # method discard never ran.
            with contextlib.suppress(WorktreeError):
                self.worktrees.discard(str(wt.path), wt.branch)
            return self.state.update_if(
                run_id,
                _still_running,
                status=RunStatus.CANCELLED.value,
                ended_at=_now(),
                error="fleet: cancelled during setup",
            )
        return None

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
            self.usage, self.session_start, self.budgets, now or datetime.now(UTC),
        )

    def _execute(
        self,
        req: RunRequest,
        run_id: str,
        wt: Worktree,
        ts: str,
        *,
        cleanup: bool = False,
        deferred_provisioning: bool = False,
    ) -> RunRecord:
        """Execute suffix: run the backend, price + classify, persist the terminal record."""
        backend = self.backends[req.backend_name]
        result: AgentResult | None = None
        # Every attempt the retry loop threw away. Bound before the try so the `finally` log write
        # can reach it even when the run died before the loop ran.
        abandoned: list[AgentResult] = []
        record: RunRecord | None = None
        # Whether this run's line already reached the ledger, so the failure path can add one
        # when it did not - and never a second one when it did.
        usage_recorded = False
        # Bound before the try because the failure path reads it: `extract_usage` is a backend
        # seam and can raise, and an unbound name there would replace the real exception with an
        # UnboundLocalError inside the except block - skipping the terminal stamp and leaving the
        # record `running` forever.
        usage: UsageRecord | None = None
        try:
            if deferred_provisioning:
                early = self._run_deferred_provisioning(req, run_id, wt)
                if early is not None:
                    return early
            handle = _inflight_handle(self.state.dir, run_id)

            def _record_pid(pid: int) -> None:
                # Stamp the pid together with its start time: the pair is an identity a later
                # process can verify, where a bare pid can be silently reused by the OS.
                # `update_if` because the record may already have been terminal-stamped (e.g.
                # reaped by another process): writing a live pid onto a `failed` record produces a
                # record that claims a running process for a run it says is dead.
                self.state.update_if(
                    run_id,
                    lambda r: not _is_terminal(r),
                    pid=pid,
                    pid_start_time=_pid_start_time(pid),
                )
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
                client_env=req.client_env,
                on_pid=_record_pid,
                on_exit=_record_exit,
                progress=self.progress_timeout if self.progress_timeout.enabled else None,
            )
            # Hold a slot for the agent run (the heavy, memory-hungry part) - including any transient
            # retry backoff, since the run is still in flight. Worktree creation/provision in _start
            # already happened outside the slot; a no-op context when ungated.
            gate = self._run_gate if self._run_gate is not None else contextlib.nullcontext()
            with gate:
                # Prompt-level schema instruction only (no backend-contract change). Validation
                # runs AFTER retries so a schema-invalid reply is never treated as transient.
                run_task = _task_with_schema_instruction(req.task)
                result, attempts, abandoned = self._run_with_retries(
                    backend, run_task, opts, run_id, wt
                )
                result = _apply_structured_output(req.task, result)
            usage = backend.extract_usage(result)    # the seam (default: result.usage)
            self._price_usage(usage, req.model)      # normalize cost + source (unavailable unless native)
            # Fold in what the retried-away attempts spent, so the run's line is the run's total.
            usage = self._fold_abandoned_attempts(usage, abandoned, req.model, backend)
            self._apply_external_cost(usage, req, start_iso=ts)  # backfill REAL cost if a usage_api is set
            status = self._authoritative_status(result, wt)
            # The workspace's optional verify gate: only a would-be-SUCCEEDED run that actually
            # CHANGED FILES is gated (the EMPTY downgrade already happened above; a text-only
            # reply can't have broken the repo, so don't burn a full test run on an unchanged
            # tree). "Changed files" means committed ones too - see `_worktree_produced_files`.
            # A failed gate demotes to VERIFY_FAILED; the worktree is kept for review.
            verify_passed: bool | None = None
            verify_output = ""
            if (
                status is RunStatus.EXITED_CLEAN
                and self.worktrees.verify_cmd
                and self._worktree_produced_files(wt)
            ):
                verify_passed, verify_output = self.worktrees.verify(wt)
                if not verify_passed:
                    status = RunStatus.VERIFY_FAILED
            event = UsageEvent.from_result(
                result, run_id=run_id, backend=req.backend_name, ts=ts, usage=usage,
                client=req.client, model=req.model,
                task_kind=req.task.task_kind,
                # Digest of the goal text the agent received (worker preamble included; schema
                # suffix is applied later on a copy). Never the raw text — ledger is long-lived.
                goal_digest=goal_digest(req.task.goal),
            )
            event.status = status.value              # report the authoritative process status (incl. EMPTY)
            self.usage.record(event)
            usage_recorded = True
            # Harvest BEFORE the terminal stamp so the record names its artifacts in the same write
            # that makes it terminal. Landing them afterwards would publish a finished run whose
            # artifact list is still empty, and a driver polling for terminal-then-reading would
            # see a run that produced nothing.
            artifacts = self._harvest_artifacts(wt, run_id)
            # Same ordering rule as the harvest above, and for the same reason.
            self._persist_run_log(run_id, req, abandoned, result)
            # Stamp the terminal record ONLY if the run is still running, so a `cancel_run` that
            # already marked it `cancelled` (the common cancel-wins-first race) is preserved rather
            # than clobbered by this thread returning from the SIGTERM-killed subprocess. The usage
            # event above is the immutable spend record regardless; this is the lifecycle status.
            # Redact first (value-based scrub needs the whole secret present), then cap - and
            # record that the cap fired. A driver reading `text` as a finished product cannot see
            # the cut from the string alone; see RunRecord.text_truncated.
            _redacted_text = redact_secrets(result.text)
            _capped_text, _text_truncated, _text_full_len = _cap_text(_redacted_text)
            # Release the enforce slot BEFORE publishing a record that says the run is over.
            # Stamping first left a window where a run read as terminal while still holding its
            # cap, so a driver following the documented loop - poll until terminal, then dispatch -
            # could be refused with "wait for it to finish" naming a run that had finished (#278).
            # Safe against overshoot: `usage.record` above already put this run's spend in the
            # ledger, which is what the next spawn re-checks. The `finally` below still calls this
            # as the backstop for paths that never reach here; `release_run` is idempotent.
            self._budget_gate.release_run(run_id)
            record = self.state.update_if(
                run_id,
                _still_running,
                status=status.value,
                artifacts=artifacts,
                cost_usd=event.cost_usd,
                # From the usage RECORD, not the ledger event: the event defaults absent counts to
                # 0 (it is the facts ledger, and its `source` column carries the provenance), while
                # the run record must be able to say "nobody counted". No usage record at all means
                # the backend reported nothing; a record with 0 in it is a measured zero.
                input_tokens=usage.input_tokens if usage is not None else None,
                output_tokens=usage.output_tokens if usage is not None else None,
                duration_ms=result.duration_ms,
                source=event.source,
                # Redact before the 16KB cut: value-based scrub needs the whole secret present.
                text=_capped_text,
                text_truncated=_text_truncated,
                text_full_len=_text_full_len,
                structured=_redact_structured(result.structured),
                ended_at=_now(),
                error=redact_secrets(result.error) if result.error else result.error,
                attempts=attempts,
                verify_passed=verify_passed,
                verify_output=verify_output,
                agent_survived_kill=result.agent_survived_kill,
            )
            self._ensure_artifacts_recorded(run_id, record, artifacts)
        except Exception as exc:  # noqa: BLE001 - never leave a run stranded as RUNNING
            # The agent's spend belongs in the ledger even when the bookkeeping AFTER it failed.
            # Everything between `backend.run` and `usage.record` - the verify gate, the commit
            # count, the event build - can raise, and the run then reached `failed` with no ledger
            # line at all: real tokens and real dollars, spent and invisible to every cost, budget
            # and routing figure. The ledger's contract is one line per run; a run that broke is
            # still a run. Guarded by the flag so a failure AFTER the record cannot double-count.
            if result is not None and not usage_recorded:
                self._record_usage_best_effort(run_id, req, result, ts, usage)
            # Terminal-stamp the record before re-raising, so one failure can't leave a zombie - but
            # only if still running, so a concurrent cancel's terminal status wins.
            # Harvest here too: a run that died partway may still have written the findings that
            # explain why, and those are exactly what the next round needs.
            failed_artifacts = self._harvest_artifacts(wt, run_id)
            if result is not None:
                self._persist_run_log(run_id, req, abandoned, result)
            # Same ordering as the success path (#278): never publish a terminal record while the
            # enforce slot is still held. A run that failed before `usage.record` has no spend to
            # overshoot with; one that failed after has already recorded it.
            self._budget_gate.release_run(run_id)
            failed_rec = self.state.update_if(
                run_id, _still_running, status=RunStatus.FAILED.value, ended_at=_now(),
                error=f"fleet: {exc}", artifacts=failed_artifacts,
            )
            self._ensure_artifacts_recorded(run_id, failed_rec, failed_artifacts)
            # The record above is the real one for this run. Carry its id out with the exception
            # so a batch caller can hand the driver an addressable handle instead of a synthetic
            # id that resolves to nothing.
            exc._marshal_run_id = run_id  # type: ignore[attr-defined]
            raise
        finally:
            # Backstop release. Both terminal-stamp paths above release first, so that a record
            # never says "finished" while its cap is still held (#278); this covers the paths that
            # reach neither - and is a no-op when they did, since `release_run` is idempotent.
            self._budget_gate.release_run(run_id)
            _unregister_inflight_run(self.state.dir, run_id)
            # Persist the FULL raw stdout/stderr for every terminal run (success OR failure) so a
            # driver can inspect what the agent actually did after the fact. Best-effort: a logging
            # failure (disk full, permission, ...) must never break a finished run; stderr the cause
            # for visibility. Skipped when no AgentResult was produced (e.g. the backend crashed
            # before parse_output returned - there is nothing to log).
            #
            # EVERY attempt is written, not just the last. A record showing `attempts: 3` used to
            # pair with a log of attempt 3 alone, so a driver diagnosing a flaky backend read a
            # clean log and concluded the failures were phantom - the retried-away attempts are
            # exactly the evidence being looked for. Same reasoning as the cost ledger, which
            # already folds in what the abandoned attempts spent.
            if result is not None:
                self._persist_run_log(run_id, req, abandoned, result)

        if cleanup and _agent_may_still_be_writing(record):
            # Deleting a worktree out from under a live writer destroys the partial work AND the
            # branch it is on, and unlike commit/integrate there is nothing to retry afterwards.
            # `Fleet.clean` refuses these records already; this inline path skipped the check and
            # so undid, destructively, the protection the flag exists to give.
            msg = (
                "cleanup skipped: the agent is still alive and writing to this worktree "
                "(see `error`); removing it now would delete work in progress. Clean it "
                "once the process is gone."
            )
            existing = self.state.get(run_id)
            if existing is not None and existing.error:
                msg = f"{existing.error}; {msg}"
            self.state.update(run_id, error=msg)
            refreshed = self.state.get(run_id)
            if refreshed is not None:
                record = refreshed
        elif cleanup:
            try:
                self.worktrees.remove(wt)
            except Exception as exc:  # noqa: BLE001 - terminal stamp already landed
                # remove() raising AFTER the terminal stamp contradicts the record (and is
                # silently swallowed in `_execute_bg`). Stamp a warning on the record instead;
                # the run's outcome stands, the worktree simply remains for a later clean.
                msg = f"cleanup warning: failed to remove worktree: {exc}"
                existing = self.state.get(run_id)
                if existing is not None and existing.error:
                    msg = f"{existing.error}; {msg}"
                self.state.update(run_id, error=msg)
                refreshed = self.state.get(run_id)
                if refreshed is not None:
                    record = refreshed
        return record

    def with_liveness(self, rec: RunRecord) -> RunRecord:
        """Instance-side alias for ``with_liveness`` - see the module-level function."""
        return with_liveness(rec)

    def _cancel_requested(self, run_id: str) -> bool:
        """Whether a cancel has been asked for - intent, not confirmation.

        The in-process handle's ``cancel_requested`` flag is set as soon as ``cancel_run`` accepts
        the request, including when identity could not be confirmed (record stays ``running``; no
        signal). The terminal record covers a cancel another process stamped. Either form of intent
        must stop further work (retries, setup) - confirmation (``status=cancelled``) is a separate
        fact about whether Marshal could honestly claim the agent stopped.
        """
        handle = _inflight_handle(self.state.dir, run_id)
        if handle is not None:
            with _active_runs_guard:
                if handle.cancel_requested:
                    return True
        rec = self.state.get(run_id)
        return rec is not None and _is_terminal(rec)

    def _run_with_retries(
        self,
        backend: CodingAgentBackend,
        task: TaskSpec,
        opts: RunOpts,
        run_id: str,
        wt: Worktree,
    ) -> tuple[AgentResult, int, list[AgentResult]]:
        """Run the backend, retrying only on a transient (infra/transport) failure with backoff.

        Returns the final result, the number of attempts made, and every ABANDONED attempt's result.

        The abandoned ones are returned rather than dropped because a provider can charge for an
        attempt it then failed - a rate limit hit part-way through leaves real tokens spent, and the
        backend reports them. Keeping only the last result meant the ledger stated a three-attempt
        run cost what its final attempt cost, which is an undercount presented as a measurement. A
        genuine task failure or a timeout is returned as-is - never retried.

        The worktree is reused across attempts, which is safe only while the tree is untouched.
        Those markers usually DO arrive at startup or transport time, before the agent has written
        anything - but not always: a rate limit or a dropped connection can land mid-run, and
        `base.run()` fills `error` from the output tail for any backend killed part-way, so a
        transient-shaped error is not proof that nothing was written. Retrying into a tree the
        agent has already edited restarts the task on top of its own half-finished work, and the
        duplicated result is recorded `exited_clean` like any other success. So the tree is
        checked before each retry and one that has been written to ends the loop: one honest
        failure the driver can re-run beats a success built on work nobody asked for twice.

        The check has to be `_worktree_produced_files`, not `_worktree_has_changes`. A backend
        that commits as it goes leaves an uncommitted tree that is clean, so asking only `git
        status` reads a partially-committed run as untouched and retries straight into it - the
        same blind spot that skipped the verify gate (#294) and truncated the review subject.

        Cancel *intent* ends the loop - including an unconfirmed cancel that left the record
        `running`. SIGTERM can surface as a transport-shaped error, so without this check a
        cancelled run would sleep and spawn a WHOLE new attempt - backend setup and all - which
        the pending cancel then kills on arrival. That put a second writer in the worktree after
        cancel was already requested.
        """
        attempt = 1
        abandoned: list[AgentResult] = []
        while True:
            result = backend.run(task, opts)
            if attempt >= self.retries.max_attempts or not is_transient_failure(result):
                return result, attempt, abandoned
            if self._cancel_requested(run_id):
                return result, attempt, abandoned
            if self._worktree_produced_files(wt):
                result.error = (
                    f"{result.error or 'transient failure'} - not retried: the agent had already "
                    f"written to its worktree when this arrived, so another attempt would restart "
                    f"the task on top of its own partial work. Re-run it if you want a clean try."
                )
                return result, attempt, abandoned
            delay = self.retries.delay_for(attempt)
            print(
                f"[marshal] {run_id}: transient failure (attempt {attempt}/"
                f"{self.retries.max_attempts}), retrying in {delay:.1f}s: {result.error}",
                file=sys.stderr,
            )
            time.sleep(delay)
            # Re-check AFTER the sleep too. The backoff is the widest window in the whole loop, so
            # a cancel is most likely to arrive exactly here; checking only before the sleep would
            # let the loop wake up and spawn a fresh agent into the worktree - and bill for it -
            # after cancel intent was already recorded (possibly without a `cancelled` stamp).
            if self._cancel_requested(run_id):
                return result, attempt, abandoned
            # Recorded only once the loop commits to REPLACING this result - so the list holds
            # exactly the attempts whose usage no other code path will ever see.
            abandoned.append(result)
            attempt += 1

    def _execute_bg(
        self,
        req: RunRequest,
        run_id: str,
        wt: Worktree,
        ts: str,
        *,
        deferred_provisioning: bool = False,
    ) -> None:
        """Background variant: the outcome (incl. failure) is already persisted; never propagate."""
        try:
            self._execute(
                req, run_id, wt, ts, deferred_provisioning=deferred_provisioning
            )
        except Exception:  # noqa: BLE001, S110 - _execute already terminal-stamped; driver polls status()
            pass

    def run_many(
        self,
        jobs: list[RunManyJob],
        *,
        max_concurrency: int = 4,
        stagger_s: float = 0.1,
    ) -> list[RunManyJobResult]:
        """Run a batch of jobs concurrently; block until every chain finishes.

        Each job runs its primary request, then — when the job carries a ``then`` follow-up — runs
        that follow-up in the **same worker** as soon as the primary reaches a terminal state. A
        follow-up does **not** wait for sibling primaries (unlike a second barrier).

        ``max_concurrency`` caps thread-pool workers. Each worker runs one chain at a time (primary,
        then optionally ``then`` back-to-back), so at most ``max_concurrency`` agent processes run
        concurrently. Submissions are spaced by ``stagger_s`` to ease the Cursor concurrent-launch
        file-lock race. A single run's failure is captured as a FAILED record and never aborts the
        batch. Returns one ``RunManyJobResult`` per input job, in input order; ``then`` is absent
        (with ``then_skipped`` set) when the primary failed, has no branch, the primary's branch
        has no commits beyond its base, or ``commit_run`` could not freeze the primary's work
        (status ``blocked`` / ``error``).
        """
        results: list[RunManyJobResult | None] = [None] * len(jobs)
        with ThreadPoolExecutor(max_workers=max(1, max_concurrency)) as pool:
            futures = {}
            for i, job in enumerate(jobs):
                if stagger_s and i:
                    time.sleep(stagger_s)
                futures[pool.submit(self._run_many_chain, job)] = i
            for fut in futures:
                results[futures[fut]] = fut.result()  # _run_many_chain never raises
        return [r for r in results if r is not None]

    def _run_many_chain(self, job: RunManyJob) -> RunManyJobResult:
        """Run one primary request and its optional ``then`` follow-up in this worker."""
        primary = self._run_request(job.request)
        if job.then is None:
            return RunManyJobResult(primary=primary)
        skip = self._then_skip_reason(primary)
        if skip:
            return RunManyJobResult(primary=primary, then_skipped=skip)
        commit = self.commit_run(primary.run_id)
        if commit.status in ("blocked", "error"):
            msg = commit.message or commit.status
            return RunManyJobResult(primary=primary, then_skipped=f"commit_run: {msg}")
        # Decide from branch tip vs spawn base — not commit_run's status word.
        # ``clean`` means "no new commit needed" (tree already clean), NOT "branch empty";
        # an agent that self-committed leaves a clean tree with real commits beyond base.
        branch = primary.branch
        # `_then_skip_reason` already rejected a branchless primary; re-check rather than assert,
        # since `python -O` strips asserts and would leave branch_tip() taking None.
        if branch is None:
            return RunManyJobResult(primary=primary, then_skipped="primary run has no branch")
        try:
            tip = self.worktrees.branch_tip(branch)
        except WorktreeError:
            # Cannot prove the branch is empty of new commits — prefer the follow-up (same
            # rationale as missing base_commit). Never invent a "no diff" skip from a tip failure.
            tip = None
        # Missing / non-sha tip or base is not evidence of no diff — prefer the follow-up.
        if (
            primary.base_commit is not None
            and tip is not None
            and is_git_object_id(tip)
            and is_git_object_id(primary.base_commit)
            and tip == primary.base_commit
        ):
            return RunManyJobResult(
                primary=primary,
                then_skipped=(
                    "primary produced no diff to review "
                    "(branch has no commits beyond its base)"
                ),
            )
        then_task = job.then.task.model_copy(update={"base_branch": primary.branch})
        then_req = job.then.model_copy(update={"task": then_task})
        then_rec = self._run_request(then_req)
        return RunManyJobResult(primary=primary, then=then_rec)

    def _then_skip_reason(self, primary: RunRecord) -> str | None:
        if primary.status != RunStatus.EXITED_CLEAN.value:
            return f"primary run did not succeed ({primary.status})"
        if not primary.branch:
            return "primary run has no branch"
        return None



    def _record_usage_best_effort(
        self,
        run_id: str,
        req: RunRequest,
        result: AgentResult,
        ts: str,
        usage: UsageRecord | None,
    ) -> None:
        """Write this run's ledger line on the failure path. Never raises.

        Best-effort by construction: this runs while an exception is already unwinding, and a
        second failure here must not replace the original one the driver needs to see.
        """
        try:
            event = UsageEvent.from_result(
                result, run_id=run_id, backend=req.backend_name, ts=ts, usage=usage,
                client=req.client, model=req.model,
                task_kind=req.task.task_kind,
                goal_digest=goal_digest(req.task.goal),
            )
            event.status = RunStatus.FAILED.value
            self.usage.record(event)
        except Exception as exc:  # noqa: BLE001 - the original failure must survive this
            print(
                f"[marshal] {run_id}: failed to record usage for a failed run: {exc}",
                file=sys.stderr,
            )

    def _persist_run_log(
        self,
        run_id: str,
        req: RunRequest,
        abandoned: list[AgentResult],
        result: AgentResult,
    ) -> None:
        """Write the run's full stdout/stderr. Idempotent, so it is safe to call more than once.

        Called BEFORE the terminal stamp, for the same reason artifacts are harvested before it:
        a driver polling for terminal-then-reading must never see a finished run whose diagnostics
        are not on disk yet. `get_run_log` returning null is documented to mean "the backend
        crashed before producing one", so the gap between the stamp and this write turned a
        transient ordering into a durable wrong conclusion. The `finally` call remains as the
        backstop for paths that never reach the stamp at all.
        """
        try:
            self.logs.write_attempts(
                run_id,
                [(r.raw_stdout or "", r.raw_stderr or "") for r in (*abandoned, result)],
                # Same client env: the child received — scrub values that never appear in
                # os.environ (e.g. PROVIDER_AUTH under a non-secret-shaped key name).
                extra_values=req.client_env or None,
            )
        except Exception as exc:  # noqa: BLE001 - log persistence is best-effort, never breaks a run
            print(f"[marshal] {run_id}: failed to persist run log: {exc}", file=sys.stderr)

    def _run_request(self, req: RunRequest) -> RunRecord:
        """run_request one request, capturing any failure as a FAILED record so a batch survives it."""
        try:
            return self.run_request(req)
        except Exception as exc:  # noqa: BLE001 - one job's failure must not abort the batch
            # If the run got far enough to be stamped, hand back the REAL record. Synthesizing
            # one here minted `<task>.<backend>` - an id with no uuid suffix, matching nothing in
            # the ledger - so a driver given it as `primary.run_id` could not `get_run`,
            # `collect_run` or `set_outcome` the failure, while the genuine failed record sat
            # unreachable under the id it was actually written with.
            stamped_id = getattr(exc, "_marshal_run_id", None)
            if isinstance(stamped_id, str):
                stamped = self.state.get(stamped_id)
                if stamped is not None:
                    return stamped
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
        """Normalize cost + source in place: keep native cost, else unavailable (tokens kept)."""
        if usage is None:
            return
        if usage.source is UsageSource.NATIVE:
            return  # backend authoritatively reported the cost (a real $0 included) - never override
        usage.cost_usd = 0.0
        usage.source = UsageSource.UNAVAILABLE

    def _fold_abandoned_attempts(
        self,
        usage: UsageRecord | None,
        abandoned: list[AgentResult],
        model: str | None,
        backend: CodingAgentBackend,
    ) -> UsageRecord | None:
        """Add the retried-away attempts' usage to the run's record, or say the total is unknown.

        Tokens are summed unconditionally - they are facts each attempt reported, and a run that
        burned three attempts really did consume all of them.

        Cost is stricter, because cost here is native-or-nothing (see `_price_usage`: there is no
        price table, so an attempt is either a provider-reported figure or unmeasured). Summing only
        the attempts that happened to report cost would produce a number that looks measured and is
        short by however much the silent attempts cost. So the total is only stated when EVERY
        attempt that reported tokens also reported cost; otherwise the run's cost goes back to
        `unavailable`, which the report layer already knows means "unknown", not "$0".

        Losing a measured figure in that mixed case is deliberate. An undercount presented as a
        measurement is the failure this codebase keeps guarding against; an honest "unknown" is not.
        """
        if not abandoned:
            return usage
        priors: list[UsageRecord] = []
        for result in abandoned:
            prior = backend.extract_usage(result)  # same seam the final attempt goes through
            if prior is None:
                continue
            self._price_usage(prior, model)
            priors.append(prior)
        if not priors:
            return usage
        if usage is None:
            usage = priors.pop(0)
        measured = usage.source is UsageSource.NATIVE
        for prior in priors:
            usage.input_tokens += prior.input_tokens
            usage.output_tokens += prior.output_tokens
            usage.cache_read_tokens += prior.cache_read_tokens
            usage.cache_write_tokens += prior.cache_write_tokens
            if prior.source is UsageSource.NATIVE:
                usage.cost_usd += prior.cost_usd
            elif prior.input_tokens or prior.output_tokens:
                # This attempt burned tokens nobody priced, so no total can be stated.
                measured = False
        if not measured:
            usage.cost_usd = 0.0
            usage.source = UsageSource.UNAVAILABLE
        return usage

    def _apply_external_cost(self, usage: UsageRecord | None, req: RunRequest, *, start_iso: str) -> None:
        """Override cost with the REAL charge from a provider usage-API, when the client opts in.

        Runs after `_price_usage`: if the client declares a `usage_api` (e.g. "eastrouter") and the
        provider can attribute an actual cost to this run, replace unavailable with that real cost
        (`source = admin-api`). A failure or an unattributable run is a no-op - unavailable stands.
        This must NEVER raise: a completed run is done, cost reconciliation is best-effort.
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
                cache_read_tokens=usage.cache_read_tokens,
            )
        except Exception:  # noqa: BLE001 - external cost lookup must never break a finished run
            return
        # Only fill unavailable. Native (and any other already-priced source) is ground truth
        # and must not be overwritten by a usage_api backfill.
        if ext is not None and usage.source is UsageSource.UNAVAILABLE:
            usage.cost_usd = ext.cost_usd
            usage.source = ext.source

    def _worktree_has_changes(self, wt: Worktree) -> bool:
        """Whether the worktree holds uncommitted changes.

        Can't tell (a git failure) counts as changed: a wasted gate run beats a missed regression.
        """
        try:
            return bool(self.worktrees.changed_files(wt))
        except WorktreeError:
            return True

    def _worktree_produced_files(self, wt: Worktree) -> bool:
        """Whether the run left file changes at all - the verify gate's trigger.

        Uncommitted changes are only half the question. An agent that COMMITS its own work leaves
        a clean tree behind it, so gating on `changed_files` alone skipped the gate entirely for
        those backends - and then stamped `verify_passed = None`, which `RunRecord` documents as
        "no file changes to gate". A driver could not tell a passing gate from one that never ran
        on work that was about to be integrated. `_authoritative_status` learned this same lesson
        at #250; the gate is the same question asked one step earlier.

        Can't tell counts as produced, matching `_authoritative_status`'s fail-open direction.
        """
        if self._worktree_has_changes(wt):
            return True
        # `agent_commit_count` documents "None when git failed", but a git TIMEOUT raises
        # `WorktreeError` straight through it (and a dead cwd raises OSError from Popen). The
        # sibling `changed_files` call is guarded; this one was not, so a slow git turned a
        # clean, expensive run into `failed` - and, because this runs before `usage.record`,
        # dropped its ledger line entirely.
        try:
            committed = self.worktrees.agent_commit_count(wt)
        except (WorktreeError, OSError):
            return True  # cannot tell -> assume the agent wrote, the safe direction
        return committed is None or committed > 0

    def _authoritative_status(self, result: AgentResult, wt: Worktree) -> RunStatus:
        """A clean exit that produced no work (no text, no file changes) is EMPTY, not success."""
        if result.status is not RunStatus.EXITED_CLEAN:
            return result.status
        if result.text.strip():
            return RunStatus.EXITED_CLEAN
        try:
            changed = self.worktrees.changed_files(wt)
        except WorktreeError:
            return RunStatus.EXITED_CLEAN  # can't tell -> don't mislabel a success as empty
        if changed:
            return RunStatus.EXITED_CLEAN
        # A clean tree is not proof of an idle agent: one that COMMITS its own work leaves nothing
        # uncommitted behind it. Stamping EMPTY there put the record at odds with `collect_run`,
        # which reports those commits - and a driver polling status would discard work that is
        # sitting on the branch (#250). None = could not tell, which must not read as zero.
        try:
            committed = self.worktrees.agent_commit_count(wt)
        except (WorktreeError, OSError):
            return RunStatus.EXITED_CLEAN  # same "can't tell -> don't mislabel" rule as above
        if committed is None or committed > 0:
            return RunStatus.EXITED_CLEAN
        return RunStatus.EMPTY

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
        """Surface a run's diff + changed files. Read-only - nothing is merged.

        A setup-failed (or otherwise torn-down) run has no worktree: returns
        ``produced="unavailable"`` with ``text`` set to the record's error so the driver sees why,
        not a crash. A worktree that vanishes mid-op (provisioning discard racing collect) is the
        same structured path — never an uncaught ``WorktreeError``.

        ``unavailable`` is deliberately NOT ``nothing``: the work could not be read, which is not
        evidence the run produced none. Reporting both as ``nothing`` let a driver reject a run
        that had actually succeeded, and ``routing`` then held that rejection against its client.
        """
        rec = self.state.get(run_id)
        if rec is None:
            raise ValueError(f"no such run: {run_id!r}")

        def _gone(detail: str | None = None) -> CollectResult:
            text = _worktree_gone_message(rec)
            if detail and not rec.error:
                text = detail
            return CollectResult(
                run_id=run_id,
                branch=rec.branch or None,
                worktree=rec.worktree or None,
                changed_files=[],
                diff="",
                produced="unavailable",
                unavailable_reason=text,
                # commit_count stays None: nothing was counted, and 0 would read as "the agent
                # made no commits" for a run whose commits simply could not be reached.
                text=text,
                text_truncated=rec.text_truncated,
                text_full_len=rec.text_full_len,
                structured=rec.structured,
            )

        try:
            wt = self._worktree_for(run_id)
        except ValueError:
            return _gone()
        try:
            changed_files = self.worktrees.changed_files(wt)
            diff = self.worktrees.diff(wt)
            committed_changed_files: list[str] = []
            committed_diff = ""
            # None, not 0, until the count is actually taken - a run with no branch was never
            # counted, and 0 would assert the agent committed nothing.
            commit_count: int | None = None
            if wt.branch:
                # The run works in its own clone, so commits the AGENT made are not in the driver's
                # repo yet - and every branch read below happens there. Without this, self-committed
                # work reads as "no changes" instead of as the work it is.
                self.worktrees.publish(wt)
                target = self._collect_target(rec)
                commit_count = self.worktrees.unmerged_commit_count(wt.branch, target)
                if commit_count:
                    committed_changed_files = self.worktrees.merged_diff_files(wt.branch, target)
                    committed_diff = self.worktrees.merged_diff(wt.branch, target)
        except WorktreeError as exc:
            return _gone(str(exc))
        # A run with no files changed is not necessarily a run that did nothing: a research or
        # review agent's artifact is its final message, and the engine already treats text alone as
        # SUCCEEDED. Returning an empty diff and stopping there made `collect_run` - the tool a
        # driver reaches for first - report silence for a run that had said something.
        has_diff = bool(changed_files or committed_changed_files)
        final_text = "" if has_diff else (rec.text if rec else "")
        produced = "diff" if has_diff else ("text" if final_text.strip() else "nothing")
        return CollectResult(
            run_id=run_id,
            branch=wt.branch or None,
            worktree=str(wt.path),
            changed_files=changed_files,
            diff=diff,
            produced=produced,
            text=final_text,
            # Only meaningful alongside the text we actually returned; a diff-producing run gets
            # the honest default rather than a flag about a message it is not carrying.
            text_truncated=rec.text_truncated if final_text else False,
            text_full_len=rec.text_full_len if final_text else None,
            committed_changed_files=committed_changed_files,
            committed_diff=committed_diff,
            commit_count=commit_count,
            structured=rec.structured,
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
        if _agent_may_still_be_writing(rec):
            # A terminal STATUS is not proof the process stopped - a cancel that could not signal
            # it, or a timeout whose kill did not land. See `_agent_may_still_be_writing`. `clean`
            # already refuses these records for exactly this reason; this path writes a commit, so
            # it needs the check at least as much.
            return CommitResult(
                run_id=run_id,
                status="blocked",
                branch=rec.branch,
                message=_live_agent_message(rec),
            )
        try:
            wt = self._worktree_for(run_id)
        except ValueError:
            return CommitResult(
                run_id=run_id,
                status="error",
                branch=rec.branch,
                message=_worktree_gone_message(rec),
            )
        if not wt.branch:
            # A status, not an exception. `CommitResult` has an `error` status built for exactly
            # this, and every other failure on this path already returns one - a lone raise made
            # "this run has no branch" arrive in a different shape from "the worktree is gone",
            # so a driver handling one still crashed on the other.
            return CommitResult(
                run_id=run_id,
                status="error",
                branch=None,
                message=f"run {run_id!r} has no branch to commit",
            )
        try:
            sha = self.worktrees.commit_all(wt, message or f"marshal: {run_id}")
            tip = self.worktrees.branch_tip(wt.branch)
        except WorktreeError as exc:
            # Mid-op vanish (discard racing commit) or ordinary git failure — structured error.
            msg = _worktree_gone_message(rec) if not Path(rec.worktree or "").exists() else str(exc)
            return CommitResult(run_id=run_id, status="error", branch=wt.branch, message=msg)
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
        ORPHANS - dirs whose run record is missing or unreadable (hand-pruned, or torn). A live
        create writes a ``.creating`` claim before the worktree exists and clears it only after
        the RUNNING record's ``os.replace`` publishes, so neither the create→add gap nor the
        add→clear handoff is mistaken for an orphan (#181). Reported under ``orphans_removed``;
        ``older_than_hours`` does not apply (an orphan has no trustworthy end timestamp).
        """
        result = CleanResult(dry_run=dry_run)
        if run_ids is not None:
            targets: list[RunRecord] = []
            for rid in run_ids:
                try:
                    rec = self.state.get(rid)
                except ValueError as exc:  # unsafe id: refused, reported like any non-target
                    result.skipped.append({"run_id": rid, "reason": str(exc)})
                    continue
                if rec is None:
                    result.skipped.append({"run_id": rid, "reason": "no such run"})
                elif not _is_terminal(rec):
                    result.skipped.append(
                        {"run_id": rid, "reason": f"not finished (status={rec.status})"}
                    )
                else:
                    targets.append(rec)
        else:
            cutoff = datetime.now(UTC) - timedelta(hours=older_than_hours) \
                if older_than_hours is not None else None
            targets = [
                r for r in self.state.list()
                if _in_clean_scope(r, scope) and _ended_before(r, cutoff)
            ]
        for rec in targets:
            # A terminal record does not always mean a finished process. Mirror `_is_reapable`:
            # a background task still registered in this process (e.g. cancel-before-pid during
            # deferred provisioning) may still be using the worktree even though the record already
            # reads `cancelled`/`failed`. Reaping under that writer loses work / confuses git.
            if _inflight_in_this_process(self.state.dir, rec.run_id):
                result.skipped.append(
                    {
                        "run_id": rec.run_id,
                        "reason": "background task still in flight in this process",
                    }
                )
                continue
            # `cancel_run` on a run this process did not start stamps `cancelled` without being
            # able to signal, so the agent can still be writing this worktree. Fail OPEN here,
            # unlike the `kill` instruction on the record: naming an unverified pid could send an
            # operator after an unrelated process, but refusing to delete a worktree that MIGHT
            # still have a writer only leaves a directory behind.
            if _pid_is_still_ours(rec):
                result.skipped.append(
                    {"run_id": rec.run_id, "reason": f"agent may still be running at pid {rec.pid}"}
                )
                continue
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
            # to Marshal's own base_dir, so foreign worktrees are never touched. The create→add
            # gap has a directory but no record yet - a live ``.creating`` claim spares it (#181).
            # An explicit run_ids clean targets exactly those runs, so no sweep.
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
                if _creating_claim_held(self.state.dir, rid):
                    result.skipped.append(
                        {"run_id": rid, "reason": "worktree creation in progress"}
                    )
                    continue
                # Re-read the record: the two checks above are separate reads, and create clears
                # its claim right AFTER publishing the record. A sweep that read "no record" just
                # before the publish and "no claim" just after it would span the whole handoff and
                # discard a LIVE worktree. Re-checking closes that window - by here the record is
                # published or the run is genuinely absent.
                try:
                    if self.state.get(rid) is not None:
                        result.skipped.append(
                            {"run_id": rid, "reason": "worktree creation in progress"}
                        )
                        continue
                except (ValidationError, OSError, ValueError):
                    pass  # still unreadable: garbage, as decided above
                if dry_run:
                    result.orphans_removed.append(rid)
                    continue
                try:
                    self.worktrees.discard(child, f"{self.worktrees.branch_prefix}/{rid}")
                    self.logs.remove(rid)
                    _clear_creating_claim(self.state.dir, rid)  # stale claim from a dead holder
                    result.orphans_removed.append(rid)
                except WorktreeError as exc:
                    result.errors.append({"run_id": rid, "error": str(exc)})
        # Reap orphaned atomic-write temps (a crash between mkstemp and os.replace in state/logs
        # leaves `*.tmp` nothing else collects). Age-gated so a concurrent clean cannot unlink a
        # LIVE temp mid-write. Best-effort; never fails the clean.
        if not dry_run:
            now = time.time()
            for tmp_dir in (self.state.dir, self.logs.dir):
                if not tmp_dir.exists():
                    continue
                for tmp in tmp_dir.glob("*.tmp"):
                    try:
                        if now - tmp.stat().st_mtime < _TMP_REAP_AGE_S:
                            continue
                        tmp.unlink()
                    except OSError:
                        pass
        return result

    def cancel_run(self, run_id: str) -> RunRecord:
        """Cancel a running run: SIGTERM its process group, then mark cancelled.

        Spawn provisioning: when a setup pid is published, that process group is signalled; when
        cancel arrives before any pid, the record is stamped ``cancelled`` immediately and the
        background task cooperatively stops at its next checkpoint (discards the worktree, does
        not launch the agent). Pure-Python provisioning (e.g. ``read_paths`` copies) is not
        killable — only ``setup_cmd`` has an external timeout/kill. Cancel intent still wins the
        final stamp if setup exits non-zero after killpg (see ``_run_deferred_provisioning``).

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
        # When identity is unprovable for a live in-process child, do not signal AND do not stamp
        # cancelled — either alone is wrong (blind killpg / lie about a still-running agent).
        cancel_unconfirmed = False
        if handle is None:
            # Three cases, and the pid is kept in two of them. Clearing it is what once left an
            # operator with no handle on a process that was still writing, and left `clean` with no
            # reason to spare that worktree - so the pid only goes when the process is gone.
            if _pid_is_verifiably_ours(rec):
                # Alive, and provably our agent: safe to name in an instruction to a human.
                cancel_error = (
                    f"fleet: cancelled the record only - the agent is STILL RUNNING at pid "
                    f"{rec.pid} and was started by another process, so Marshal cannot signal it "
                    f"safely. Its worktree may still be written. End it with: kill -TERM -{rec.pid}"
                )
            elif _pid_is_still_ours(rec):
                # Alive, but the identity could not be confirmed - it may be a recycled pid. Keep
                # it as evidence (so `clean` still spares the worktree) without telling anyone to
                # kill it, which could target an unrelated process.
                cancel_error = (
                    f"fleet: cancelled the record only - this run was started by another process "
                    f"and something is still alive at pid {rec.pid}, but Marshal cannot confirm it "
                    f"is the agent. Its worktree may still be written; verify the process before "
                    f"ending it."
                )
            else:
                cancel_extra["pid"] = None
                cancel_error = (
                    "fleet: cancelled without signalling - this run was started by another "
                    "process, so its pid cannot be confirmed to still belong to the agent"
                )
        else:
            with _active_runs_guard:
                handle.cancel_requested = True
            # Test seam: force the window between snapshot and re-check (production: None).
            if _cancel_after_handle_snapshot is not None:
                _cancel_after_handle_snapshot()
            # Re-check under the lock immediately before signalling so a reap that landed after
            # cancel_requested cannot let us killpg a recycled pid (#183). When we have a start
            # time, require a successful probe that still matches — a failed probe is not a
            # mismatch: signalling risks a stranger, and stamping cancelled would lie.
            with _active_runs_guard:
                if handle.exited or handle.pid is None:
                    pass  # finished, or cancel beat the pid (applied when published)
                else:
                    pid = handle.pid
                    started = handle.pid_start_time
                    if started is not None:
                        verdict = _identity_verdict(pid, started)
                        if verdict is None:
                            # Probe failed or process gone. Distinguish: an alive pid whose
                            # identity we cannot read must not be claimed cancelled.
                            if _pid_alive(pid):
                                cancel_unconfirmed = True
                                cancel_error = (
                                    "fleet: cancel not confirmed - the agent's process identity "
                                    "could not be verified, so Marshal did not signal it. The "
                                    "run may still be running; re-check with get_run before "
                                    "assuming it stopped."
                                )
                            # else: dead — stamp cancelled without signalling
                        elif verdict:
                            with contextlib.suppress(ProcessLookupError, OSError):
                                os.killpg(pid, signal.SIGTERM)
                        # else: recycled pid - do not signal a stranger
                    else:
                        with contextlib.suppress(ProcessLookupError, OSError):
                            os.killpg(pid, signal.SIGTERM)
        if cancel_unconfirmed:
            # Keep status running; only record the uncertainty. update_if so a natural finish
            # that landed mid-cancel is not clobbered with a stale warning.
            return self.state.update_if(
                run_id,
                lambda r: r.status == RunStatus.RUNNING.value,
                error=cancel_error,
            )
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
            # Includes the spawn provisioning window (RUNNING before setup finishes).
            return IntegrateResult(
                run_id=run_id,
                status="blocked",
                branch=rec.branch,
                message="run is still in progress; wait for it to finish before integrating",
            )
        if rec is not None and _agent_may_still_be_writing(rec):
            # The status says the run is over; the process says otherwise. Refuse rather than
            # merge a tree that still has a writer - this is the one path that reaches the user's
            # branch.
            return IntegrateResult(
                run_id=run_id,
                status="blocked",
                branch=rec.branch,
                message=_live_agent_message(rec),
            )
        if rec is not None:
            try:
                wt = self._worktree_for(run_id)
            except (ValueError, WorktreeError):
                # Setup-failed / discarded / mid-op vanish: structured refusal, not a crash.
                return IntegrateResult(
                    run_id=run_id,
                    status="error",
                    branch=rec.branch,
                    message=_worktree_gone_message(rec),
                )
        else:
            try:
                wt = self._worktree_for(run_id)
            except (ValueError, WorktreeError) as exc:
                return IntegrateResult(
                    run_id=run_id, status="error", branch=None, message=str(exc)
                )
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
            # Mid-op vanish under a racing discard, or a git op we can't classify as recoverable.
            if rec is not None and not Path(rec.worktree or "").exists():
                return IntegrateResult(
                    run_id=run_id,
                    status="error",
                    branch=wt.branch,
                    message=_worktree_gone_message(rec),
                )
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
                # A conflict used to come back with no message at all, so a conflict caused by an
                # orphaned base looked identical to a genuine overlap - and the file list pointed
                # away from the cause. Say which one it is when we can tell.
                message=_orphaned_base_diagnosis(self.worktrees, rec, target),
            )

        # Judgment lands on the run record (not a second usage event): events.jsonl is one line
        # per run for cost rollups, and rewriting that line would break ledger immutability.
        # `outcome_note` is cleared because it explains the verdict being replaced: a run that was
        # rejected with a reason and then integrated anyway would otherwise read as an integration
        # annotated with why it was refused.
        self.state.update(
            run_id,
            merged_into=target,
            outcome=RunOutcome.INTEGRATED.value,
            outcome_at=datetime.now(UTC).isoformat(),
            outcome_note=None,
        )
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

    def _ensure_artifacts_recorded(self, run_id: str, record: RunRecord, artifacts: list[str]) -> None:
        """Land harvested artifact names even when the terminal stamp was lost to a racing cancel.

        The terminal write is guarded on the run still being RUNNING so a `cancel_run` that already
        stamped `cancelled` wins. That guard is right for the *verdict* and wrong for this: the
        files are already on disk, so dropping their names leaves a record claiming the run produced
        nothing while `.marshal/artifacts/<run_id>/` says otherwise. What a run WROTE is not a
        lifecycle opinion that cancelling can overrule, so it is written unconditionally.
        """
        if not artifacts or record.artifacts == artifacts:
            return
        try:
            self.state.update(run_id, artifacts=artifacts)
        except Exception as exc:  # noqa: BLE001 - the run is over; never raise on bookkeeping
            print(f"[marshal] {run_id}: failed to record artifacts: {exc}", file=sys.stderr)

    def _harvest_artifacts(self, wt: Worktree, run_id: str) -> list[str]:
        """Copy this run's `.marshal-artifacts/` into durable storage. Never raises.

        Best-effort in the same sense as run-log persistence: the work is already done and its
        record is being stamped, so a full disk here must not turn a finished run into a failed
        one. A harvest failure costs the next round its input; a raise would cost the run itself.
        """
        try:
            return harvest_artifacts(wt, run_artifacts_dir(self.repo_root, run_id))
        except Exception as exc:  # noqa: BLE001 - harvesting must never break a finished run
            print(f"[marshal] {run_id}: failed to harvest artifacts: {exc}", file=sys.stderr)
            return []

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
