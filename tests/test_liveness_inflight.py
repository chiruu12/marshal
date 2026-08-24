"""Tests for the failure branches of the code that decides a run is dead.

``liveness`` and ``inflight`` answer one question between them - "is anything still working on this
run?" - and every wrong answer is expensive in one of two directions. Say "dead" about a live run
and its worktree is torn out from under a working agent; say "alive" about a crashed one and it
reads RUNNING until someone intervenes by hand.

The happy paths are covered by ``test_fleet.py`` against real processes. What lives here is the
other half: what these functions do when the probe itself fails - an unreadable ``ps``, a corrupt
claim, a lock directory that cannot be created, a pid that has been recycled underneath us. Each of
those has a *deliberate* direction it errs in, and that direction is the thing being asserted.
"""

from __future__ import annotations

import json
import os
import subprocess
from datetime import UTC, datetime, timedelta
from pathlib import Path

import pytest

from marshal_engine.orchestration import inflight as inflight_mod
from marshal_engine.orchestration import liveness as liveness_mod
from marshal_engine.orchestration.inflight import (
    _creating_claim_held,
    _creating_claim_path,
    _write_creating_claim,
)
from marshal_engine.orchestration.liveness import (
    _another_fleet_active,
    _claim_fleet_lock,
    _pid_alive,
    _pid_start_time,
    _write_lock_payload,
)
from marshal_engine.orchestration.reaping import _started_within_grace
from marshal_engine.runtime.state import RunRecord

# A pid that is live and ours for the duration of any test.
LIVE_PID = os.getpid()


def _dead_pid() -> int:
    """A pid that is reliably not running: spawn a child and wait for it to exit."""
    proc = subprocess.Popen(["true"], stdout=subprocess.DEVNULL, stderr=subprocess.DEVNULL)
    proc.wait()
    return proc.pid


# --------------------------------------------------------------------------------------
# _pid_alive / _pid_start_time - the two primitive probes everything else is built on
# --------------------------------------------------------------------------------------


def test_pid_alive_assumes_alive_when_the_probe_is_denied(monkeypatch: pytest.MonkeyPatch) -> None:
    # A pid owned by another user answers EPERM, not ESRCH - the process EXISTS, we just may not
    # signal it. Reading that ambiguity as "dead" would reap a live run, so the only safe reading
    # is "alive". Only ProcessLookupError means genuinely gone.
    def denied(pid: int, sig: int) -> None:
        raise PermissionError(1, "Operation not permitted")

    monkeypatch.setattr(os, "kill", denied)
    assert _pid_alive(LIVE_PID) is True


def test_pid_alive_is_false_for_a_genuinely_absent_process() -> None:
    # The other half of the pair above: ProcessLookupError is the ONE error that means gone.
    assert _pid_alive(_dead_pid()) is False


def test_pid_start_time_is_none_when_ps_cannot_be_run(monkeypatch: pytest.MonkeyPatch) -> None:
    # No `ps` on PATH, or a fork that fails under load. None means "unverifiable", and every
    # caller is required to treat that as "spare it", never as "different, therefore reapable".
    def no_ps(*args: object, **kwargs: object) -> None:
        raise OSError(2, "No such file or directory: 'ps'")

    monkeypatch.setattr(subprocess, "run", no_ps)
    assert _pid_start_time(LIVE_PID) is None


def test_pid_start_time_is_none_when_ps_times_out(monkeypatch: pytest.MonkeyPatch) -> None:
    def slow_ps(*args: object, **kwargs: object) -> None:
        raise subprocess.TimeoutExpired(cmd="ps", timeout=5)

    monkeypatch.setattr(subprocess, "run", slow_ps)
    assert _pid_start_time(LIVE_PID) is None


def test_pid_start_time_is_none_when_ps_prints_nothing(monkeypatch: pytest.MonkeyPatch) -> None:
    # `ps -p <gone>` exits non-zero with empty stdout. An empty string must not be returned as a
    # start time: two unverifiable pids would then compare EQUAL and impersonate each other.
    completed = subprocess.CompletedProcess(args=["ps"], returncode=1, stdout="  \n", stderr="")
    monkeypatch.setattr(subprocess, "run", lambda *a, **k: completed)
    assert _pid_start_time(LIVE_PID) is None


def test_pid_start_time_reads_the_real_start_time_of_a_live_process() -> None:
    # The identity half of the pid+start-time pair has to actually work, or every comparison
    # below degrades to the "unverifiable" branch and proves nothing.
    started = _pid_start_time(LIVE_PID)
    assert started, "could not read this process's own start time"
    assert _pid_start_time(LIVE_PID) == started, "start time is not stable across probes"


# --------------------------------------------------------------------------------------
# the .creating claim - cross-process "this run is mid-create, do not reap it"
# --------------------------------------------------------------------------------------


def test_absent_claim_is_not_held(tmp_path: Path) -> None:
    assert _creating_claim_held(tmp_path, "r1") is False


def test_corrupt_claim_is_treated_as_absent(tmp_path: Path) -> None:
    # A claim truncated by a crash mid-write must not shield the run forever. Unparseable means
    # "no claim", so a genuine orphan stays reclaimable instead of pinning a dead run at RUNNING.
    _creating_claim_path(tmp_path, "r1").write_text("{not json", encoding="utf-8")
    assert _creating_claim_held(tmp_path, "r1") is False


def test_claim_without_a_pid_is_treated_as_absent(tmp_path: Path) -> None:
    # Valid JSON, no identity in it - there is nothing to probe, so there is nothing to spare.
    _creating_claim_path(tmp_path, "r1").write_text(json.dumps({"note": "hi"}), encoding="utf-8")
    assert _creating_claim_held(tmp_path, "r1") is False


def test_claim_with_a_non_integer_pid_is_treated_as_absent(tmp_path: Path) -> None:
    _creating_claim_path(tmp_path, "r1").write_text(json.dumps({"pid": "abc"}), encoding="utf-8")
    assert _creating_claim_held(tmp_path, "r1") is False


def test_claim_held_by_a_dead_process_is_not_held(tmp_path: Path) -> None:
    # The crash-leftover case: the creator died between writing the claim and publishing the
    # RUNNING record. Nothing is working on this run, so the sweep must be free to reclaim it.
    payload = {"pid": _dead_pid(), "pid_start_time": "irrelevant"}
    _creating_claim_path(tmp_path, "r1").write_text(json.dumps(payload), encoding="utf-8")
    assert _creating_claim_held(tmp_path, "r1") is False


def test_claim_held_by_a_live_matching_process_is_held(tmp_path: Path) -> None:
    _write_creating_claim(tmp_path, "r1")
    assert _creating_claim_held(tmp_path, "r1") is True


def test_recycled_pid_does_not_impersonate_the_claim_holder(tmp_path: Path) -> None:
    # The reason the claim stamps a start time at all. The creator died, the OS handed its pid to
    # an unrelated process, and that process is alive - so a pid-only check would report "held"
    # and shield this dead run from every future sweep. The mismatched start time is what breaks
    # the impersonation.
    payload = {"pid": LIVE_PID, "pid_start_time": "Thu Jan  1 00:00:00 1970"}
    _creating_claim_path(tmp_path, "r1").write_text(json.dumps(payload), encoding="utf-8")
    assert _creating_claim_held(tmp_path, "r1") is False


def test_claim_written_by_an_older_version_is_held_while_its_pid_lives(tmp_path: Path) -> None:
    # No start time recorded, because the writer predates the identity stamp. Upgrading Marshal
    # must not make the new code reap runs the old code is still creating, so a live pid is
    # enough here - the pre-upgrade behaviour, deliberately kept.
    payload = {"pid": LIVE_PID}
    _creating_claim_path(tmp_path, "r1").write_text(json.dumps(payload), encoding="utf-8")
    assert _creating_claim_held(tmp_path, "r1") is True


def test_claim_with_an_unprobeable_start_time_is_held(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    # The pid is alive but `ps` will not answer, so we cannot tell holder from impersonator.
    # Unverifiable errs toward sparing: deleting a possibly-live create is the worse mistake.
    payload = {"pid": LIVE_PID, "pid_start_time": "Thu Jan  1 00:00:00 1970"}
    _creating_claim_path(tmp_path, "r1").write_text(json.dumps(payload), encoding="utf-8")
    monkeypatch.setattr(inflight_mod, "_pid_start_time", lambda pid: None)
    assert _creating_claim_held(tmp_path, "r1") is True


def test_write_creating_claim_leaves_no_temp_file_when_publishing_fails(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    # The claim publishes by temp+replace. If the replace fails, the temp file must not survive:
    # `runs/` is swept by name, and abandoned `.creating.*.tmp` files would accumulate silently
    # in a directory whose contents are read as fleet state.
    def boom(src: object, dst: object) -> None:
        raise OSError(28, "No space left on device")

    monkeypatch.setattr(os, "replace", boom)
    with pytest.raises(OSError):
        _write_creating_claim(tmp_path, "r1")

    assert not list(tmp_path.glob("*.tmp")), "a temp claim file was left behind"
    assert not _creating_claim_path(tmp_path, "r1").exists()


# --------------------------------------------------------------------------------------
# the fleet lock - "another Fleet is supervising this repo"
# --------------------------------------------------------------------------------------


def test_corrupt_lock_is_treated_as_inactive(tmp_path: Path) -> None:
    # A lock file half-written by a crash would otherwise wedge the repo permanently: no Fleet
    # could ever claim supervision again. Unparseable means reclaimable.
    lock = tmp_path / "fleet.lock"
    lock.write_text("{", encoding="utf-8")
    assert _another_fleet_active(lock) is False


def test_lock_held_by_this_process_is_not_another_fleet(tmp_path: Path) -> None:
    lock = tmp_path / "fleet.lock"
    _write_lock_payload(lock)
    assert _another_fleet_active(lock) is False, "this process mistook its own lock for a rival's"


def test_live_holder_with_an_unprobeable_start_time_is_assumed_held(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    # Mirrors the claim rule, in the same direction: when `ps` cannot confirm identity we assume
    # the holder is real rather than stealing supervision from a possibly-live Fleet.
    # pid 1 is live and is not this process, so the identity check is actually reached rather
    # than short-circuiting on the "that's my own lock" branch.
    lock = tmp_path / "fleet.lock"
    payload = {"pid": 1, "pid_start_time": "Thu Jan  1 00:00:00 1970"}
    lock.write_text(json.dumps(payload), encoding="utf-8")
    monkeypatch.setattr(liveness_mod, "_pid_start_time", lambda pid: None)
    assert _another_fleet_active(lock) is True


def test_recycled_pid_does_not_impersonate_the_lock_holder(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    # The lock's counterpart to the claim's recycled-pid test, and the reason the lock payload
    # stamps a start time at all. Without the equality check, a supervisor that died and had its
    # pid handed to an unrelated live process would look like a live supervisor forever: no later
    # Fleet would ever take over, and every stale run under it would read RUNNING until that
    # unrelated process happened to exit.
    lock = tmp_path / "fleet.lock"
    payload = {"pid": 1, "pid_start_time": "Thu Jan  1 00:00:00 1970"}
    lock.write_text(json.dumps(payload), encoding="utf-8")
    monkeypatch.setattr(liveness_mod, "_pid_start_time", lambda pid: "Fri Jan  2 00:00:00 1970")
    assert _another_fleet_active(lock) is False


def test_claim_fails_when_the_lock_directory_cannot_be_created(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    # Read-only home, full disk, bad permissions. Failing to claim must return False - a Fleet
    # that cannot record supervision must not behave as though it holds it.
    def no_mkdir(self: Path, *args: object, **kwargs: object) -> None:
        raise OSError(13, "Permission denied")

    monkeypatch.setattr(Path, "mkdir", no_mkdir)
    # The parent EXISTS. A missing one would make the guard `open` fail too, and the same False
    # would come back whether or not the mkdir handler was there at all - so the test would pass
    # against code with that handler deleted. An existing parent leaves mkdir as the only way to
    # reach False, which is what makes this a pin rather than a coincidence.
    assert _claim_fleet_lock(tmp_path / "fleet.lock") is False


def test_claim_fails_when_the_guard_file_cannot_be_opened(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    lock = tmp_path / "fleet.lock"
    real_open = open

    def selective_open(path: object, *args: object, **kwargs: object) -> object:
        if str(path).endswith(".guard"):
            raise OSError(13, "Permission denied")
        return real_open(path, *args, **kwargs)  # type: ignore[arg-type]

    monkeypatch.setattr("builtins.open", selective_open)
    assert _claim_fleet_lock(lock) is False


def test_claim_stands_down_when_another_process_holds_the_guard(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    # The whole read-judge-take-over decision runs under one flock. If we cannot get it, another
    # process is mid-decision; standing down is correct, because racing it is exactly the bug
    # the guard was introduced to fix.
    import fcntl

    lock = tmp_path / "fleet.lock"

    def contended(fd: int, op: int) -> None:
        raise OSError(35, "Resource temporarily unavailable")

    monkeypatch.setattr(fcntl, "flock", contended)
    assert _claim_fleet_lock(lock) is False
    assert not lock.exists(), "stood down but still published a lock"


def test_claim_fails_when_the_lock_payload_cannot_be_written(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    # Won the guard, judged the holder dead, then could not publish. Returning True here would be
    # the worst outcome available: this Fleet would supervise while the on-disk lock says nobody
    # does, so a second Fleet would claim it too.
    lock = tmp_path / "fleet.lock"

    def boom(path: Path) -> None:
        raise OSError(28, "No space left on device")

    monkeypatch.setattr(liveness_mod, "_write_lock_payload", boom)
    assert _claim_fleet_lock(lock) is False


def test_claim_succeeds_and_records_a_verifiable_identity(tmp_path: Path) -> None:
    # The positive control for the tests above: a clean claim publishes a lock carrying BOTH
    # halves of the identity, which is what makes every recycled-pid check downstream possible.
    lock = tmp_path / "fleet.lock"
    assert _claim_fleet_lock(lock) is True

    data = json.loads(lock.read_text(encoding="utf-8"))
    assert data["pid"] == LIVE_PID
    assert data["pid_start_time"] == _pid_start_time(LIVE_PID)


def test_write_lock_payload_leaves_no_temp_file_when_publishing_fails(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    # Same reasoning as the claim's temp file, in the directory that holds fleet state.
    lock = tmp_path / "fleet.lock"

    def boom(src: object, dst: object) -> None:
        raise OSError(28, "No space left on device")

    monkeypatch.setattr(os, "replace", boom)
    with pytest.raises(OSError):
        _write_lock_payload(lock)

    assert not list(tmp_path.glob("*.tmp")), "a temp lock file was left behind"
    assert not lock.exists()


# --------------------------------------------------------------------------------------
# the reap grace window - "too young to judge"
# --------------------------------------------------------------------------------------


def _record(**kw: object) -> RunRecord:
    base = {"run_id": "r1", "task_id": "t1", "backend": "fake", "status": "running"}
    base.update(kw)
    return RunRecord(**base)  # type: ignore[arg-type]


def test_a_run_with_an_unreadable_start_time_is_treated_as_young() -> None:
    # An unparseable timestamp is not evidence that the run is dead - it is the absence of
    # evidence. Reaping on it would turn one corrupt field into a killed run, so the grace
    # window swallows the ambiguity instead.
    assert _started_within_grace(_record(pid=None, started_at="not-a-timestamp")) is True


def test_a_run_with_no_start_time_is_treated_as_young() -> None:
    assert _started_within_grace(_record(pid=None, started_at=None)) is True


def test_a_naive_start_time_is_compared_as_utc() -> None:
    # Records written before the timestamps carried a zone are still on disk. Comparing a naive
    # value against an aware `now` raises, so it is read as UTC - which makes an OLD naive record
    # correctly fall outside the grace window rather than crashing the sweep.
    old = (datetime.now(UTC) - timedelta(hours=6)).replace(tzinfo=None).isoformat()
    assert _started_within_grace(_record(pid=None, started_at=old)) is False

    fresh = datetime.now(UTC).replace(tzinfo=None).isoformat()
    assert _started_within_grace(_record(pid=None, started_at=fresh)) is True


def test_a_stamped_pid_skips_the_grace_window() -> None:
    # Grace exists only for records too young to have a pid. Once there IS one, liveness can be
    # probed directly, and deferring would just leave a decidable run undecided.
    fresh = datetime.now(UTC).isoformat()
    assert _started_within_grace(_record(pid=LIVE_PID, started_at=fresh)) is False
