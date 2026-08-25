"""Process identity, liveness probes, and the per-repo Fleet lock.

Everything here answers one of two questions: *is this process still alive*, and *is it still the
one we think it is*. A pid alone answers neither - the OS recycles pids - so a pid is only ever
treated as an identity when paired with the OS-reported start time of that pid.

The two biases are deliberate and opposite, and picking the wrong one is a real bug either way:

  * ``_pid_is_still_ours`` fails **OPEN** (unknown -> "assume ours"). Its callers reap; falsely
    reaping a live run is destructive and silent.
  * ``_pid_is_verifiably_ours`` fails **CLOSED** (unknown -> "not ours"). Its callers name the pid
    in a ``kill`` instruction or refuse to clean a worktree; acting on a recycled pid is worse
    than saying nothing.

Split out of ``fleet.py``: probing a pid needs nothing but a record, so tying it to a Fleet meant a
workspace whose service had not been built yet reported ``null`` for a verifiably live agent.
"""

from __future__ import annotations

import contextlib
import fcntl
import json
import os
import subprocess
import sys
import tempfile
from datetime import UTC, datetime
from pathlib import Path

from ..core.types import RunStatus
from ..runtime.env import DETACHED_STDIO
from ..runtime.state import RunRecord


def _now() -> str:
    return datetime.now(UTC).isoformat()


def _is_terminal(rec: RunRecord) -> bool:
    """True once a run has stopped - i.e. it is neither queued nor still running."""
    return rec.status not in (RunStatus.RUNNING.value, RunStatus.QUEUED.value)


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
            capture_output=True, text=True, timeout=5,
            check=False,
            **DETACHED_STDIO,
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


def _agent_may_still_be_writing(rec: RunRecord) -> bool:
    """Terminal status, but the agent process could still be mid-write.

    Deliberately scoped to ``cancelled``. Every other terminal status is stamped by the supervisor
    AFTER it observed the process exit, so the recorded pid is known-stale - and the pid is never
    cleared, so on a machine that has since recycled it, ``_pid_is_still_ours`` would fail OPEN
    (no recorded start time, or the probe is unavailable) and report a stranger's live process as
    this run's agent. For reaping that direction is the safe one; for a write path it would strand
    a finished run's work behind a permanent refusal the driver has no way to clear.

    ``cancel_run`` is the one path that stamps terminal without having observed an exit: on a run
    the current process did not start it cannot signal the process group at all, so the record
    reads ``cancelled`` while the agent keeps writing. ``teams.py`` draws this same line for the
    same reason (``_UNSTABLE_FOR_REVIEW``).
    """
    return rec.status == RunStatus.CANCELLED.value and _pid_is_still_ours(rec)


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
