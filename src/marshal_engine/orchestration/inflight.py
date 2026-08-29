"""Runs this process currently owns - the in-memory registry and its on-disk ``.creating`` claim.

Two mechanisms, one job: stop a run that is genuinely in flight from being reaped as an orphan.
They cover different scopes and neither is redundant.

  * ``_active_runs`` is **process-local**. It is keyed by ``<repo>/.marshal`` so a replacement Fleet
    built in the same MCP server process (config hot-reload) shares the map with the evicted Fleet's
    background pool - startup reaping must not touch those runs even before a pid is stamped.
  * the ``.creating`` sidecar is **cross-process**. It is written before ``worktrees.create`` and
    cleared only after the RUNNING record's ``os.replace`` publishes, so a concurrent ``clean`` in
    another process always sees the claim and/or the record. The create->add gap has no record; the
    add->clear handoff must not open a second one.

A ``_RunHandle`` is the cancellation seam: a pid alone is not safe to signal, so the handle pairs
the pid with its start time and tracks whether the child has been reaped yet.
"""

from __future__ import annotations

import contextlib
import json
import os
import tempfile
import threading
import time
from pathlib import Path

from .liveness import (
    _identity_verdict,
    _pid_alive,
    _pid_start_time,
    _unverifiable_supervisor_hold,
)

# Process-wide in-flight run ids keyed by ``<repo>/.marshal`` (resolved). A replacement Fleet
# constructed in the same MCP server process (config hot-reload) shares this map with the evicted
# Fleet's background pool, so startup reaping must not touch those runs even when they have no pid
# yet (e.g. a test backend that overrides run() without spawning).
_active_runs_guard = threading.Lock()
_active_runs: dict[str, dict[str, _RunHandle]] = {}

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


def _register_inflight_run(runs_dir: Path, run_id: str) -> _RunHandle:
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


def _publish_pid(handle: _RunHandle, pid: int) -> bool:
    """Record a newly spawned child's pid on ``handle``; True if a cancel is already pending.

    Clears ``exited``: a published pid means a LIVE child. The handle is reused across retries, so
    an exit recorded by a previous attempt would otherwise make cancel skip signalling the retry.
    Stamps ``pid_start_time`` when the probe works so cancel can refuse a recycled pid if reap
    races the signal.

    A missing start time is still published (``pid`` set, ``pid_start_time`` left None). That is
    deliberate, not a silent degradation: Marshal forked this child moments ago and has not
    reaped it, so the OS still holds the number for us - recycling between fork and this call is
    not a real hazard. Cancel's ``started is None`` branch then signals on that provenance.
    Refusing to stamp the pid would turn a transient ``ps`` failure into a silent cancel no-op
    with the agent still running against a cancelled record.
    """
    started = _pid_start_time(pid)
    with _active_runs_guard:
        handle.pid = pid
        # None = proof unavailable; cancel treats that branch as "signal on fork provenance".
        handle.pid_start_time = started
        handle.exited = False
        return handle.cancel_requested


def _inflight_handle(runs_dir: Path, run_id: str) -> _RunHandle | None:
    key = _marshal_base_key(runs_dir)
    with _active_runs_guard:
        return _active_runs.get(key, {}).get(run_id)


def _inflight_in_this_process(runs_dir: Path, run_id: str) -> bool:
    return _inflight_handle(runs_dir, run_id) is not None


def _creating_claim_path(runs_dir: Path, run_id: str) -> Path:
    return runs_dir / f"{run_id}{_CREATING_SUFFIX}"


def _write_creating_claim(runs_dir: Path, run_id: str) -> None:
    """Durably mark ``run_id`` as mid-create so a cross-process orphan sweep will spare it.

    Same degrade-never-disable rule as ``_write_lock_payload``: when the start-time probe fails
    we still write the claim (a create must stay shielded from a concurrent clean), and stamp
    ``written_at`` so an unverifiable hold can age out instead of shielding a recycled pid
    forever.
    """
    runs_dir.mkdir(parents=True, exist_ok=True)
    path = _creating_claim_path(runs_dir, run_id)
    pid = os.getpid()
    started = _pid_start_time(pid)
    payload_dict: dict[str, object] = {"pid": pid, "pid_start_time": started}
    if started is None:
        payload_dict["written_at"] = time.time()
    payload = json.dumps(payload_dict)
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
    An unverifiable hold is bounded the same way as the fleet lock (see
    ``_unverifiable_supervisor_hold``).
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
    verdict = _identity_verdict(pid, recorded if isinstance(recorded, str) else None)
    if verdict is None:
        return _unverifiable_supervisor_hold(
            data, recorded if isinstance(recorded, str) else None
        )
    return verdict
