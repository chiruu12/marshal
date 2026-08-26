"""Declaring abandoned runs dead - the orphan sweep a Fleet performs at startup.

A run record on disk says RUNNING; the process that was supervising it is gone. Nothing will ever
write that run's outcome, so it must be stamped terminal or it reads as live forever.

"The supervisor is gone" is answered from the supervisor's own recorded identity
(``supervisor_pid`` + start time), not inferred from the agent's. Those are different questions,
and the difference is a window minutes wide: after the agent exits, its supervisor still has
pricing, a usage-API backfill, the ``verify:`` gate and artifact harvest to do, and the record
reads RUNNING throughout. Reading the dead agent pid as an absent supervisor stamped live runs
``failed`` and discarded the outcome they were about to write - and it is reachable, because
``fleet.lock`` gates *reaping*, not ``run``, so a second Marshal process on the same repo sweeps
while the first is mid-finalization. Records predating those fields fall back to the old
inference. Every inference here is biased against the destructive answer.

``_is_reapable`` is the single place the decision is made, and it is deliberately called twice per
record: once to scan, and again inside ``update_if`` under the run's own lock. Two callers, one
rule - a reap can never be authorized by one test and committed against another.
"""

from __future__ import annotations

import sys
from datetime import UTC, datetime
from pathlib import Path

from pydantic import ValidationError

from ..core.types import RunStatus
from ..runtime.state import FleetState, RunRecord
from .inflight import _inflight_in_this_process
from .liveness import _is_terminal, _now, _pid_is_still_ours, _supervisor_is_gone

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
        started = started.replace(tzinfo=UTC)
    reference = now or datetime.now(UTC)
    return (reference - started).total_seconds() < _REAP_GRACE_S


def _is_reapable(rec: RunRecord, runs_dir: Path) -> bool:
    """Whether ``rec`` can be declared orphaned right now. The ONE place that decision is made.

    Used twice per record on purpose: once to scan, and again inside ``update_if`` under the run's
    own lock. Two callers, one rule - a reap can never be authorized by one test and committed
    against another.

    Both processes must be accounted for. A live SUPERVISOR means the outcome is still coming, so
    there is nothing to declare. A live AGENT means the worktree still has a writer, so declaring
    it dead would be a record at odds with what is happening on disk. Only when neither is there
    is the run genuinely abandoned.
    """
    if _inflight_in_this_process(runs_dir, rec.run_id):
        return False  # a Fleet in this process still owns it (config hot-reload)
    if _started_within_grace(rec):
        return False  # no pid stamped yet and too young to judge
    if not _supervisor_is_gone(rec):
        return False  # someone is still going to write this run's outcome
    return not _pid_is_still_ours(rec)


def _reap_orphaned_runs(state: FleetState) -> bool:
    """Terminal-stamp persisted ``running``/``queued`` runs left by a prior Fleet instance.

    Returns True when at least one record was left undecided on evidence that will go stale - it
    was inside the pid-less grace window, or its supervisor or agent was still alive at this
    instant. All are snapshots, not verdicts, so the caller must run this again later (see
    ``Fleet.reconcile_orphans``); otherwise such a record reads RUNNING for the whole life of a
    server that never constructs another Fleet.

    A record with no parseable ``started_at`` and no pid is the one case nothing here can decide:
    there is no evidence either way, and inventing an outcome is worse than showing an honest
    stale record. It is reported as deferred, so the re-check keeps looking at it, but only a
    ``cancel_run`` (or a hand edit) will actually settle it.

    Callers MUST have established that no other live Fleet supervises this repo (see the
    ``fleet.lock`` check in ``Fleet.__init__``) - this function does not re-check.

    A new Fleet's in-process pool starts empty, so any non-terminal record on disk is orphaned
    unless the process that will write its outcome is still alive (``supervisor_pid`` + start
    time), the agent subprocess is still running (per-record ``pid`` probe), or another Fleet in
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
