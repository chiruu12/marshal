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
import shlex
import signal
import subprocess
import sys
import tempfile
import threading
import time
import uuid
from collections.abc import Callable, Mapping
from concurrent.futures import ThreadPoolExecutor
from datetime import datetime, timedelta, timezone
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
from .provisioning import (
    _provision_read_paths,
    _require_context_files,
    harvest_artifacts,
    prepare_artifact_dir,
    provision_run_artifacts,
)
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

# Process-wide in-flight run ids keyed by ``<repo>/.marshal`` (resolved). A replacement Fleet
# constructed in the same MCP server process (config hot-reload) shares this map with the evicted
# Fleet's background pool, so startup reaping must not touch those runs even when they have no pid
# yet (e.g. a test backend that overrides run() without spawning).
_active_runs_guard = threading.Lock()
_active_runs: dict[str, dict[str, "_RunHandle"]] = {}

#: Test-only seam: called after cancel snapshots the handle and before it re-checks/signals.
#: Production leaves this ``None``. Tests assign a callback to force the copy→reap window.
_cancel_after_handle_snapshot: Callable[[], None] | None = None

#: Test-only seam: called after the RUNNING record's ``os.replace`` and before the creating claim
#: is cleared. Production leaves this ``None``.
_after_creating_record_published: Callable[[], None] | None = None

#: Sidecar written before ``worktrees.create`` and cleared only after the RUNNING record's
#: ``os.replace`` publishes, so a concurrent ``clean`` always sees the claim and/or the record
#: (the create→add gap has no record; the add→clear handoff must not open a second gap).
_CREATING_SUFFIX = ".creating"


class _RunHandle:
    """Live state for a run started by THIS process, used to cancel it safely.

    A pid alone is not safe to signal: the OS recycles pids. A child's pid is held until its parent
    reaps it, so signalling is safe exactly while the run loop is between spawn and reap - which is
    what ``exited`` tracks. ``pid_start_time`` pairs with ``pid`` so a reaped-then-recycled number
    is not signalled if ``exited`` has not been set yet. ``cancel_requested`` covers the other end:
    a cancel that arrives before the pid is known is applied as soon as it is.
    """

    __slots__ = ("pid", "pid_start_time", "exited", "cancel_requested")

    def __init__(self) -> None:
        self.pid: int | None = None
        self.pid_start_time: str | None = None
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
    Stamps ``pid_start_time`` so cancel can refuse a recycled pid if reap races the signal.
    """
    started = _pid_start_time(pid)
    with _active_runs_guard:
        handle.pid = pid
        handle.pid_start_time = started
        handle.exited = False
        return handle.cancel_requested


def _inflight_handle(runs_dir: Path, run_id: str) -> "_RunHandle | None":
    key = _marshal_base_key(runs_dir)
    with _active_runs_guard:
        return _active_runs.get(key, {}).get(run_id)


def _inflight_in_this_process(runs_dir: Path, run_id: str) -> bool:
    return _inflight_handle(runs_dir, run_id) is not None


def _creating_claim_path(runs_dir: Path, run_id: str) -> Path:
    return runs_dir / f"{run_id}{_CREATING_SUFFIX}"


def _write_creating_claim(runs_dir: Path, run_id: str) -> None:
    """Durably mark ``run_id`` as mid-create so a cross-process orphan sweep will spare it."""
    runs_dir.mkdir(parents=True, exist_ok=True)
    path = _creating_claim_path(runs_dir, run_id)
    payload = json.dumps({"pid": os.getpid(), "pid_start_time": _pid_start_time(os.getpid())})
    fd, tmp_str = tempfile.mkstemp(
        dir=str(runs_dir), prefix=f"{run_id}.creating.", suffix=".tmp"
    )
    try:
        with os.fdopen(fd, "w", encoding="utf-8") as f:
            f.write(payload)
        os.replace(tmp_str, path)
    except BaseException:
        with contextlib.suppress(OSError):
            os.unlink(tmp_str)
        raise


def _clear_creating_claim(runs_dir: Path, run_id: str) -> None:
    with contextlib.suppress(OSError):
        _creating_claim_path(runs_dir, run_id).unlink()


def _creating_claim_held(runs_dir: Path, run_id: str) -> bool:
    """True when a live process holds a creating claim for ``run_id`` (cross-process).

    Same pid + start-time identity as ``fleet.lock``. A dead/reused holder does not shield a
    crash leftover; a corrupt claim is treated as absent so genuine orphans stay reclaimable.
    """
    path = _creating_claim_path(runs_dir, run_id)
    if not path.exists():
        return False
    try:
        data = json.loads(path.read_text(encoding="utf-8"))
        pid = int(data["pid"])
    except (OSError, ValueError, KeyError, json.JSONDecodeError, TypeError):
        return False
    if not _pid_alive(pid):
        return False
    recorded = data.get("pid_start_time")
    if not isinstance(recorded, str) or not recorded:
        return True  # older/unreadable stamp: assume held while the pid lives
    now = _pid_start_time(pid)
    if now is None:
        return True  # cannot probe: spare rather than delete a possibly-live create
    return now == recorded


def _now() -> str:
    return datetime.now(timezone.utc).isoformat()


def with_liveness(rec: RunRecord) -> RunRecord:
    """Return ``rec`` with ``agent_alive`` filled in for the moment of this call.

    Answers the one question a status read cannot: is the agent still working, or has it finished
    without the outcome being written yet? Terminal records get None - the run is over, so liveness
    is not meaningful - as does a record whose pid identity cannot be established, because "some
    process exists at that number" is not evidence our agent does.

    A module-level function, not a Fleet method, because probing a pid needs nothing but the record.
    Tying it to a Fleet meant a workspace whose service had not been built yet - a fresh server, or
    any workspace not touched this session - reported `null` for a verifiably live agent, which is
    exactly the "cannot tell active work from a finished one" the field is for. Reconciliation is
    different: it MUTATES the ledger, so it is rightly gated on owning the fleet lock.
    """
    if _is_terminal(rec):
        return rec
    if rec.pid is None or not rec.pid_start_time:
        return rec  # nothing to probe, or nothing to verify a probe against
    return rec.model_copy(update={"agent_alive": _pid_is_verifiably_ours(rec)})


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


#: How long a PID-LESS non-terminal record is protected from reaping. A run is persisted RUNNING a
#: moment before its pid is stamped, so a short-lived CLI can otherwise reap a long-lived server's
#: just-started run, which has no pid yet to protect it. Observed in practice: two live agents
#: stamped ``failed`` seconds after spawning, one still running when the record said it had died.
#: A record that DOES carry a pid is never graced - `_pid_is_still_ours` answers definitively, so
#: waiting would only delay the truth. A graced record is re-examined later (see
#: `Fleet.reconcile_orphans`), never skipped permanently.
_REAP_GRACE_S = 180.0

#: Age gate for reaping orphaned ``*.tmp`` files left by a crash between ``mkstemp`` and
#: ``os.replace`` in state/logs writers. A concurrent ``clean`` must not unlink a LIVE temp
#: mid-write (that loses terminal state/logs); only temps older than this are abandoned.
_TMP_REAP_AGE_S = 300.0


#: Stale non-terminal runs reaped at Fleet startup are stamped ``failed``: the supervising process
#: vanished before Marshal recorded an outcome, so we cannot honestly claim success, cancellation,
#: or timeout. ``error`` carries the reap reason; ``pid`` is cleared so ``cancel_run`` can never
#: signal a reused OS pid.
_ORPHAN_REAP_ERROR = (
    "fleet: run orphaned at startup (supervising process exited before run completed)"
)


def _started_within_grace(rec: RunRecord, *, now: datetime | None = None) -> bool:
    """True when ``rec`` has no pid yet and started too recently to be judged orphaned.

    A record with no parseable ``started_at`` is treated as young (do not reap): an unreadable
    timestamp is not evidence that the run is dead.
    """
    if rec.pid is not None:
        return False  # a stamped pid is decidable now; grace would only defer the answer
    if not rec.started_at:
        return True
    try:
        started = datetime.fromisoformat(rec.started_at)
    except ValueError:
        return True
    if started.tzinfo is None:
        started = started.replace(tzinfo=timezone.utc)
    reference = now or datetime.now(timezone.utc)
    return (reference - started).total_seconds() < _REAP_GRACE_S


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
            check=False,
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
    """True when another Marshal Fleet process holds ``base/fleet.lock`` and is still alive.

    Checks the pid AND its recorded start time, the same identity the run records use. A bare pid
    would let a recycled one impersonate a dead holder forever: every later Fleet would see "a live
    supervisor" and decline to reap, so stale runs would read RUNNING until that unrelated process
    happened to exit. A holder written by an older version has no start time recorded - it is
    treated as held while alive, so an upgrade never causes a takeover it should not make.
    """
    if not lock_path.exists():
        return False
    try:
        data = json.loads(lock_path.read_text(encoding="utf-8"))
        pid = int(data["pid"])
    except (OSError, ValueError, KeyError, json.JSONDecodeError):
        return False  # corrupt/stale lock - treat as inactive; this Fleet will reclaim it
    if pid == os.getpid():
        return False
    if not _pid_alive(pid):
        return False
    recorded = data.get("pid_start_time")
    if not isinstance(recorded, str) or not recorded:
        return True  # older lock, or start time unreadable when written: assume still held
    now = _pid_start_time(pid)
    if now is None:
        return True  # cannot probe: assume held rather than steal a live supervisor's lock
    return now == recorded


def _pid_is_verifiably_ours(rec: RunRecord) -> bool:
    """Like ``_pid_is_still_ours`` but fails CLOSED: True only on proof, never on ambiguity.

    ``_pid_is_still_ours`` assumes ours when identity cannot be checked, because there the wrong
    answer reaps a live run. The opposite bias is needed wherever we act on the pid as an identity -
    naming it in a `kill` instruction, or refusing to clean a worktree because of it. An
    unverifiable pid may be a recycled one belonging to something else entirely, and pointing a
    human at it would be worse than saying nothing.
    """
    # Compares the start times DIRECTLY rather than delegating to `_pid_is_still_ours`: that helper
    # returns True when the probe is unavailable, which is the fail-open answer. Inheriting it here
    # would let an unprobeable pid count as "verified" and put a recycled process group into a
    # `kill` instruction - the exact outcome this function exists to prevent.
    if not rec.pid or not rec.pid_start_time:
        return False
    return _pid_start_time(rec.pid) == rec.pid_start_time


def _is_reapable(rec: RunRecord, runs_dir: Path) -> bool:
    """Whether ``rec`` can be declared orphaned right now. The ONE place that decision is made.

    Used twice per record on purpose: once to scan, and again inside ``update_if`` under the run's
    own lock. Two callers, one rule - a reap can never be authorized by one test and committed
    against another.
    """
    if _inflight_in_this_process(runs_dir, rec.run_id):
        return False  # a Fleet in this process still owns it (config hot-reload)
    if _started_within_grace(rec):
        return False  # no pid stamped yet and too young to judge
    return not _pid_is_still_ours(rec)


def _reap_orphaned_runs(state: FleetState) -> bool:
    """Terminal-stamp persisted ``running``/``queued`` runs left by a prior Fleet instance.

    Returns True when at least one record was left undecided on evidence that will go stale - it
    was inside the pid-less grace window, or its agent was still alive at this instant. Both are
    snapshots, not verdicts, so the caller must run this again later (see
    ``Fleet.reconcile_orphans``); otherwise such a record reads RUNNING for the whole life of a
    server that never constructs another Fleet.

    A record with no parseable ``started_at`` and no pid is the one case nothing here can decide:
    there is no evidence either way, and inventing an outcome is worse than showing an honest
    stale record. It is reported as deferred, so the re-check keeps looking at it, but only a
    ``cancel_run`` (or a hand edit) will actually settle it.

    Callers MUST have established that no other live Fleet supervises this repo (see the
    ``fleet.lock`` check in ``Fleet.__init__``) - this function does not re-check.

    A new Fleet's in-process pool starts empty, so any non-terminal record on disk is orphaned
    unless the agent subprocess is still running (per-record ``pid`` probe) or another Fleet in
    THIS process still owns it (config hot-reload). Reaping clears ``pid`` so a later
    ``cancel_run`` can never ``killpg`` a reused pid. Corrupt records are skipped with a warning.
    """
    deferred = False
    if not state.dir.exists():
        return deferred
    for path in sorted(state.dir.glob("*.json")):
        try:
            rec = RunRecord.model_validate_json(path.read_text(encoding="utf-8"))
        except (ValidationError, OSError, ValueError) as exc:
            print(f"[marshal] skipping unreadable run record {path.name}: {exc}", file=sys.stderr)
            continue
        if _is_terminal(rec):
            continue
        if not _is_reapable(rec, state.dir):
            # Left undecided on evidence that goes stale - inside the grace window, or its agent
            # was alive at this instant. Both are snapshots, so keep it on the re-check list.
            deferred = True
            continue
        try:
            # Re-decide inside update_if, not just "still non-terminal". The scan above read the
            # record without a lock, so between deciding and committing a pid can be stamped, or a
            # pid-less record can be the one another process just started. Re-running the SAME
            # predicate under the per-run lock is what makes the decision and the write atomic.
            state.update_if(
                rec.run_id,
                lambda r: not _is_terminal(r) and _is_reapable(r, state.dir),
                status=RunStatus.FAILED.value,
                pid=None,
                ended_at=_now(),
                error=_ORPHAN_REAP_ERROR,
            )
        except Exception as exc:  # noqa: BLE001 - startup reaping must never crash Fleet construction
            print(f"[marshal] failed to reap orphaned run {rec.run_id}: {exc}", file=sys.stderr)
    return deferred


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
            # Stamp the start time alongside the pid: a bare pid is not an identity here either,
            # and a recycled one would make every later Fleet believe a live supervisor exists.
            f.write(json.dumps({"pid": os.getpid(), "pid_start_time": _pid_start_time(os.getpid())}))
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


def _orphaned_base_diagnosis(
    worktrees: WorktreeManager, rec: RunRecord | None, target: str
) -> str:
    """Explain a conflict whose real cause is that the run's base is reachable from nothing.

    Rewriting history (amend, squash, soft-reset-and-recommit) while an agent works leaves its
    branch hanging off a commit no longer reachable from anything. Every file then reads as changed
    on both sides, so git reports conflicts in files the agent never touched - and the conflict list
    actively misleads, because the real cause is not in it.

    Two conditions, and BOTH are required:

    1. the base is not reachable from the merge target - otherwise there is no base problem at all;
    2. no surviving ref reaches it either. A run spawned with `base_branch` onto another branch
       also fails (1) while being entirely healthy, so testing only (1) would announce a problem
       for a supported flow and misdirect the very conflict this exists to explain.

    The message REPORTS the observation and OFFERS the likely causes rather than asserting one.
    `base_branch` is passed through verbatim, so it may be a tag, a raw sha, or a branch since
    deleted - all of which reach this state with no rewrite involved. Naming a rewrite as fact
    would put a confident wrong cause where a vague right one belongs, which is the failure this
    whole diagnosis exists to remove. What is *measured* - nothing reaches this base - holds in
    every one of those cases, and so does the remedy, which is why the message leads with it.

    Reachability, not existence: the reflog keeps an orphaned commit alive as an object for a good
    while, so an existence check answers "fine" exactly when the diagnosis is most needed.
    """
    if rec is None or not rec.base_commit:
        return ""
    try:
        if worktrees.is_ancestor(rec.base_commit, target):
            return ""
        if worktrees.any_user_ref_contains(rec.base_commit):
            return ""
    except WorktreeError:
        return ""
    return (
        f"the commit this run was based on ({rec.base_commit[:12]}) is reachable from no branch or "
        "tag, so git is merging against a base that is no longer in history and the conflicting "
        "files above are probably not the real cause. Usually this means history was rewritten "
        "(amend / squash / reset) while the run was in flight; a deleted base branch, or a "
        "`base_branch` naming a commit that was never on one, do it too. Re-run the task on the "
        "current branch, or cherry-pick the run's own commits onto it."
    )


def _deferred_provision_error(exc: BaseException) -> str:
    """Phase-named error for a spawn-path provision/setup failure (never a bare str(exc))."""
    msg = str(exc)
    if isinstance(exc, WorktreeError) and "worktree setup" in msg:
        return f"fleet: setup: {exc}"
    return f"fleet: provision: {exc}"


def _worktree_gone_message(rec: RunRecord) -> str:
    """Driver-facing reason when collect/integrate/commit hit a torn-down worktree."""
    if rec.error:
        return rec.error
    path = rec.worktree or ""
    return f"worktree for run {rec.run_id!r} no longer exists: {path}"


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
            session_start if session_start is not None else datetime.now(timezone.utc)
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
        if run_id is not None:
            handle = _inflight_handle(self.state.dir, run_id)
            if handle is not None:

                def _on_pid(pid: int) -> None:
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

        # Only pass cancel hooks when wired — keeps `setup(wt)` call shape for spies/tests.
        if on_pid is not None or on_exit is not None:
            self.worktrees.setup(wt, on_pid=on_pid, on_exit=on_exit)
        else:
            self.worktrees.setup(wt)

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
            self.usage, self.session_start, self.budgets, now or datetime.now(timezone.utc),
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
        record: RunRecord | None = None
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
                    backend, run_task, opts, run_id
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
            # tree). A failed gate demotes to VERIFY_FAILED; the worktree is kept for review.
            verify_passed: bool | None = None
            verify_output = ""
            if (
                status is RunStatus.EXITED_CLEAN
                and self.worktrees.verify_cmd
                and self._worktree_has_changes(wt)
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
            # Harvest BEFORE the terminal stamp so the record names its artifacts in the same write
            # that makes it terminal. Landing them afterwards would publish a finished run whose
            # artifact list is still empty, and a driver polling for terminal-then-reading would
            # see a run that produced nothing.
            artifacts = self._harvest_artifacts(wt, run_id)
            # Stamp the terminal record ONLY if the run is still running, so a `cancel_run` that
            # already marked it `cancelled` (the common cancel-wins-first race) is preserved rather
            # than clobbered by this thread returning from the SIGTERM-killed subprocess. The usage
            # event above is the immutable spend record regardless; this is the lifecycle status.
            record = self.state.update_if(
                run_id,
                _still_running,
                status=status.value,
                artifacts=artifacts,
                cost_usd=event.cost_usd,
                input_tokens=event.input_tokens,
                output_tokens=event.output_tokens,
                duration_ms=result.duration_ms,
                source=event.source,
                # Redact before the 16KB cut: value-based scrub needs the whole secret present.
                text=redact_secrets(result.text)[:16000],
                structured=_redact_structured(result.structured),
                ended_at=_now(),
                error=redact_secrets(result.error) if result.error else result.error,
                attempts=attempts,
                verify_passed=verify_passed,
                verify_output=verify_output,
            )
            self._ensure_artifacts_recorded(run_id, record, artifacts)
        except Exception as exc:  # noqa: BLE001 - never leave a run stranded as RUNNING
            # Terminal-stamp the record before re-raising, so one failure can't leave a zombie - but
            # only if still running, so a concurrent cancel's terminal status wins.
            # Harvest here too: a run that died partway may still have written the findings that
            # explain why, and those are exactly what the next round needs.
            failed_artifacts = self._harvest_artifacts(wt, run_id)
            failed_rec = self.state.update_if(
                run_id, _still_running, status=RunStatus.FAILED.value, ended_at=_now(),
                error=f"fleet: {exc}", artifacts=failed_artifacts,
            )
            self._ensure_artifacts_recorded(run_id, failed_rec, failed_artifacts)
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
        self, backend: CodingAgentBackend, task: TaskSpec, opts: RunOpts, run_id: str
    ) -> tuple[AgentResult, int, list[AgentResult]]:
        """Run the backend, retrying only on a transient (infra/transport) failure with backoff.

        Returns the final result, the number of attempts made, and every ABANDONED attempt's result.

        The abandoned ones are returned rather than dropped because a provider can charge for an
        attempt it then failed - a rate limit hit part-way through leaves real tokens spent, and the
        backend reports them. Keeping only the last result meant the ledger stated a three-attempt
        run cost what its final attempt cost, which is an undercount presented as a measurement. The worktree is reused across
        attempts: the markers we retry on (DB lock, rate limit, 5xx, connection errors) happen at
        startup/transport time, before an agent writes anything, so there is nothing to reset. A
        genuine task failure or a timeout is returned as-is - never retried.

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
        except Exception:  # noqa: BLE001 - _execute already terminal-stamped; the driver polls status()
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
            )
        except Exception:  # noqa: BLE001 - external cost lookup must never break a finished run
            return
        # Only fill unavailable. Native (and any other already-priced source) is ground truth
        # and must not be overwritten by a usage_api backfill.
        if ext is not None and usage.source is UsageSource.UNAVAILABLE:
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
        committed = self.worktrees.agent_commit_count(wt)
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

        A setup-failed (or otherwise torn-down) run has no worktree: returns ``produced="nothing"``
        with ``text`` set to the record's error so the driver sees why, not a crash. A worktree
        that vanishes mid-op (provisioning discard racing collect) is the same structured path —
        never an uncaught ``WorktreeError``.
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
                produced="nothing",
                text=text,
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
            commit_count = 0
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
            raise ValueError(f"run {run_id!r} has no branch to commit")
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
            cutoff = datetime.now(timezone.utc) - timedelta(hours=older_than_hours) \
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
                        live_started = _pid_start_time(pid)
                        if live_started is None:
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
                        elif live_started != started:
                            pass  # recycled: do not signal a stranger
                        else:
                            with contextlib.suppress(ProcessLookupError, OSError):
                                os.killpg(pid, signal.SIGTERM)
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
            outcome_at=datetime.now(timezone.utc).isoformat(),
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
