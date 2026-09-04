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
import time
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


# Marks a start time as rendered under a PINNED locale and timezone, and is therefore comparable
# across processes. `ps -o lstart=` formats through LC_TIME and TZ, so the same live pid renders
# differently to two processes that disagree about either - measured here, 5h30m apart under
# `TZ=UTC` against `TZ=Asia/Kolkata`. That is the deployment Marshal documents: a launchd-spawned
# MCP server and a terminal CLI do not share an environment (`runtime/env.py` recovers PATH for
# the same reason), and a laptop crossing timezones re-renders every pid on its own. Unmarked
# values were stamped by a version that used the ambient rendering; they are NOT comparable, and
# `_identity_verdict` reports them unverifiable rather than different, because "different" means
# "this pid is somebody else's" - the reading that authorises reaping a live run.
# `accounting/budgets.py` keeps its own copy of this marker (sibling layers must not import each
# other); change both together, and see `test_liveness_inflight.py` for the guard that they agree.
_PINNED_IDENTITY_PREFIX = "C/UTC|"

#: How long a lock/claim written WITHOUT a start-time identity may block takeover.
#:
#: Long enough that a transient ``ps`` blip during claim/create does not lose the hold
#: mid-flight; short enough that a recycled-pid wedge (live stranger, null start time)
#: self-heals without manual intervention. Only consulted when identity is unverifiable
#: AND no pinned start time was ever recorded - a stamped identity whose probe fails at
#: read time stays held for as long as the pid lives (stealing a live supervisor's lock
#: is the failure the fail-closed branch exists to prevent).
_UNVERIFIABLE_HOLD_TTL_S = 300.0


def _unverifiable_hold_still_active(payload: dict[str, object]) -> bool:
    """True while an identity-less hold should still block takeover.

    Missing/invalid ``written_at`` counts as still active: pre-TTL writers (and older
    Marshal versions that never stamped a start time) must keep their hold while the pid
    lives, or an upgrade would steal a live supervisor's lock. New writers that could not
    probe a start time stamp ``written_at`` so this bound can eventually lift.
    """
    raw = payload.get("written_at")
    if isinstance(raw, bool) or not isinstance(raw, (int, float, str)):
        return True
    try:
        written_at = float(raw)
    except (TypeError, ValueError):
        return True
    return (time.time() - written_at) < _UNVERIFIABLE_HOLD_TTL_S


def _unverifiable_supervisor_hold(payload: dict[str, object], recorded: str | None) -> bool:
    """Whether an identity-unverifiable lock/claim should still be treated as held.

    Two failure shapes look the same at the call site (``_identity_verdict`` is None) but
    must not share a policy:

    * A *pinned* start time is on disk and the probe just failed: assume still held. The
      writer proved identity once; refusing takeover protects a live supervisor when ``ps``
      is temporarily unavailable - the risk the fail-closed branch exists to prevent.
    * No usable start time was ever recorded (null, missing, or a pre-pinning stamp): the
      hold is bounded by ``written_at``. Honouring that forever is a hold no event can
      lift once the pid is recycled - the wedge this module already documents for bare
      pids. A bounded false hold self-heals; refusing to write the lock at all would
      disable every Fleet on a host without ``ps``.
    """
    if isinstance(recorded, str) and recorded.startswith(
        (_PINNED_IDENTITY_PREFIX, _PROC_IDENTITY_PREFIX)
    ):
        return True
    return _unverifiable_hold_still_active(payload)


#: Marker for a Linux identity read from ``/proc/<pid>/stat`` field 22 - the process's start time
#: in clock ticks SINCE BOOT. A distinct prefix from the ``ps`` one because the two values are not
#: comparable, and `_identity_verdict` must read a cross-prefix mismatch as "cannot check" rather
#: than "different process" - "different" is the reading that authorises reaping a live run.
_PROC_IDENTITY_PREFIX = "proc/starttime|"


def _proc_start_ticks(pid: int) -> str | None:
    """Linux: ``/proc/<pid>/stat`` field 22, the start time in ticks since boot. None elsewhere.

    Preferred over ``ps -o lstart=`` on Linux because it does not move. ``lstart`` is derived from
    the CURRENT boot-time estimate, which shifts whenever the wall clock is stepped (an NTP
    correction is enough), so the same LIVE process rendered a different start time before and
    after - read as "this pid is somebody else's", which is precisely the reading that authorises
    reaping a live agent. A boot-relative counter cannot drift with the clock.
    """
    try:
        with open(f"/proc/{pid}/stat", encoding="utf-8") as fh:
            raw = fh.read()
    except OSError:
        return None
    # The comm field is parenthesised and may itself contain spaces or ')': fields are counted
    # from AFTER the last ')', which is what every correct /proc/stat parser does.
    close = raw.rfind(")")
    if close == -1:
        return None
    fields = raw[close + 2 :].split()
    # After comm, field 3 is state; starttime is field 22 overall, i.e. index 19 here.
    if len(fields) < 20 or not fields[19].isdigit():
        return None
    return _PROC_IDENTITY_PREFIX + fields[19]


def _pid_start_time(pid: int) -> str | None:
    """The OS-reported start time of ``pid``, or None when it cannot be determined.

    A pid alone is not an identity: the OS reuses pids, so "something is alive at pid 4242" does
    not mean "our agent is alive". Pairing the pid with its start time makes the identity
    verifiable. POSIX-only via ``ps``; None on any failure, and callers must treat None as
    "unverifiable", never as "different".

    Rendered under a pinned ``LC_ALL``/``TZ`` and returned prefixed, so the value means the same
    thing to whoever reads it next - see ``_PINNED_IDENTITY_PREFIX``. ``worktree.py`` pins
    ``LC_ALL`` for the same reason.
    """
    ticks = _proc_start_ticks(pid)
    if ticks is not None:
        return ticks
    try:
        proc = subprocess.run(
            ["ps", "-o", "lstart=", "-p", str(pid)],
            capture_output=True, text=True, timeout=5,
            check=False,
            env={**os.environ, "LC_ALL": "C", "TZ": "UTC"},
            **DETACHED_STDIO,
        )
    except (OSError, subprocess.SubprocessError):
        return None
    started = proc.stdout.strip()
    return _PINNED_IDENTITY_PREFIX + started if started else None


def _identity_verdict(pid: int, recorded: str | None) -> bool | None:
    """Whether ``pid`` is still the process ``recorded`` named. None = cannot be established.

    Three-valued on purpose. Every caller already had to distinguish "proven different" from "I
    could not check" - the probe can be unavailable - and each picks the safe direction for what
    it is about to do. This adds one more way to be unable to check: a stamp written before start
    times were pinned, which cannot be compared against a pinned one without reading a rendering
    difference as a different process.

    The cost of that is a wider conservative window during an upgrade: an unmarked stamp whose pid
    has since been recycled by an unrelated process now holds its lock / claim / reservation
    instead of being reclaimed. It is bounded, not permanent - every caller establishes
    ``_pid_alive`` before asking, so the recycled process exiting reclaims the artifact and the
    next write stamps it marked. And the thing being held onto is the tolerable failure by this
    module's own rule: reaping is suppressed until then, where the direction it replaces reaps a
    live agent.
    """
    if not recorded or not recorded.startswith(
        (_PINNED_IDENTITY_PREFIX, _PROC_IDENTITY_PREFIX)
    ):
        return None  # never stamped, or stamped with the un-comparable ambient rendering
    now = _pid_start_time(pid)
    if now is None:
        return None  # probe unavailable (non-POSIX, permission)
    if now.split("|", 1)[0] != recorded.split("|", 1)[0]:
        # Two different clocks (an upgrade that changed the source, or a stamp copied between
        # hosts). Not comparable, so "cannot check" - never "different process".
        return None
    return now == recorded


def _pid_is_still_ours(rec: RunRecord) -> bool:
    """Whether ``rec.pid`` still names the agent this run started.

    Fails OPEN (True = "assume it is ours, do not reap") whenever identity cannot be established:
    no recorded start time (an older record), one written before start times were pinned, or the
    probe is unavailable. That direction is deliberate. Falsely reaping a LIVE run is destructive and silent - the record is stamped
    failed, its pid cleared so it can never be cancelled, and its real outcome is never recorded
    because the terminal stamp is guarded on the status still being running. Failing to reap a
    stale record only leaves it visible as running until someone explicitly calls ``cancel_run``,
    and only then can a wrong process be signalled.
    """
    if rec.pid is None or not _pid_alive(rec.pid):
        return False
    verdict = _identity_verdict(rec.pid, rec.pid_start_time)
    if verdict is None:
        # Unverifiable: no recorded start time, an un-comparable pre-pinning stamp, or the probe
        # is unavailable. Fail open - assume it is ours.
        return True
    return verdict


def _supervisor_identity() -> tuple[int | None, str | None]:
    """This process's ``(pid, start_time)``, for stamping onto a run it is about to supervise.

    Both or neither. A pid whose start time could not be probed is worse than no pid at all:
    `_supervisor_is_gone` would have to trust it on liveness alone, and once a reboot recycles the
    number that reads as a live supervisor for good - permanently unreapable, where the rule this
    replaced would have reaped it.
    """
    pid = os.getpid()
    started = _pid_start_time(pid)
    return (pid, started) if started is not None else (None, None)


def _supervisor_is_gone(rec: RunRecord) -> bool:
    """Whether the process that would write this run's outcome has died.

    The orphan sweep's real question. It used to be inferred from the AGENT's pid, which answers a
    different one: a supervisor between the agent exiting and the terminal stamp still has pricing,
    a usage-API backfill, the `verify:` gate and artifact harvest ahead of it - minutes, during
    which the agent pid is already dead and the record still reads `running`. Reading that as
    "orphaned" stamps a live run `failed` and drops the outcome it was about to write.

    Fails CLOSED (False = "assume someone is still there, do not reap") whenever a live
    supervisor pid cannot be told apart from a reused one, matching `_reservation_holder_live`.
    Returns True when no supervisor was recorded at all - a record predating the fields, or one
    written on a host where the start-time probe was unavailable (`_supervisor_identity` stamps
    both halves or neither). Treating those as unreapable would leave them reading `running`
    forever.

    Callers MUST have ruled out in-process ownership first (`_inflight_in_this_process`); the
    own-pid branch below reads "not in flight here" from that having already been checked.
    """
    if rec.supervisor_pid is None:
        return True  # legacy record - fall back to the agent-pid inference in `_is_reapable`
    if rec.supervisor_pid == os.getpid():
        # The supervisor is THIS process, and the caller has already established the run is not in
        # its in-flight pool - so nothing here is going to write that outcome either. Without this
        # a long-lived server would protect its own abandoned runs for as long as it stays up, and
        # a record that used to self-heal would become permanently unreapable. `_another_fleet_active`
        # carves out its own pid for the same reason: "me" is not "someone else who will finish it".
        return True
    if not _pid_alive(rec.supervisor_pid):
        return True
    verdict = _identity_verdict(rec.supervisor_pid, rec.supervisor_start_time)
    if verdict is None:
        return False  # unverifiable - assume it is the supervisor
    return not verdict  # False = a different process reused the pid


def _agent_may_still_be_writing(rec: RunRecord) -> bool:
    """Terminal status, but the agent process could still be mid-write.

    Deliberately scoped to ``cancelled``. Every other terminal status is stamped by the supervisor
    AFTER it observed the process exit, so the recorded pid is known-stale - and the pid is never
    cleared, so on a machine that has since recycled it, ``_pid_is_still_ours`` would fail OPEN
    (no recorded start time, or the probe is unavailable) and report a stranger's live process as
    this run's agent. For reaping that direction is the safe one; for a write path it would strand
    a finished run's work behind a permanent refusal the driver has no way to clear.

    Two paths stamp terminal without having observed an exit. ``cancel_run`` is one: on a run the
    current process did not start it cannot signal the process group at all, so the record reads
    ``cancelled`` while the agent keeps writing. The other is a timeout whose kill did not land -
    ``base.run()`` signals the group, then polls, and a leader still alive after SIGKILL is stamped
    ``timed_out`` with ``agent_survived_kill`` set. That flag is the whole difference: an ordinary
    timeout DID observe the exit, and widening this to the status alone would strand every one of
    them behind a refusal no driver can clear. ``teams.py`` draws this same line for the same
    reason (``_review_instability``).
    """
    if rec.status == RunStatus.CANCELLED.value:
        return _pid_is_still_ours(rec)
    if rec.status == RunStatus.TIMED_OUT.value and rec.agent_survived_kill:
        return _pid_is_still_ours(rec)
    return False


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
    if not rec.pid:
        return False
    return _identity_verdict(rec.pid, rec.pid_start_time) is True


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
    verdict = _identity_verdict(pid, recorded if isinstance(recorded, str) else None)
    if verdict is None:
        return _unverifiable_supervisor_hold(
            data, recorded if isinstance(recorded, str) else None
        )
    return verdict


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
    """Write this process's pid to the lock atomically (temp + replace, never half-written).

    Degrades like ``_supervisor_identity``: when the start-time probe fails we still publish
    (a Fleet that cannot record supervision must not be disabled outright), but we stamp
    ``written_at`` so readers can age out an unverifiable hold. Persisting a bare pid with a
    permanent "assume held" reader is a hold no event can lift once that pid is recycled;
    refusing to write is a refusal no event can lift on a host without ``ps``. The bounded
    hold preserves both properties the forever-hold and the raise-on-probe-failure each lose.
    """
    fd, tmp_str = tempfile.mkstemp(dir=str(lock_path.parent), prefix="fleet.lock.", suffix=".tmp")
    try:
        with os.fdopen(fd, "w", encoding="utf-8") as f:
            pid = os.getpid()
            started = _pid_start_time(pid)
            payload: dict[str, object] = {"pid": pid, "pid_start_time": started}
            if started is None:
                payload["written_at"] = time.time()
            f.write(json.dumps(payload))
        os.replace(tmp_str, lock_path)
    except BaseException:
        with contextlib.suppress(OSError):
            os.unlink(tmp_str)
        raise
