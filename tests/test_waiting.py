"""`wait_for_terminal` - the loop behind `wait_for_runs`.

Pure: no filesystem, no real clock, no real sleeping. `fetch`, `sleep` and `monotonic` are all
injected, so a "ten minute timeout" test costs microseconds and the timing assertions are exact
rather than flaky.
"""

from __future__ import annotations

from typing import Mapping, Sequence

import pytest

from marshal_engine.interfaces.waiting import (
    MAX_WAIT_S,
    WaitResult,
    wait_for_terminal,
)
from marshal_engine.runtime.state import RunRecord


def _rec(run_id: str, status: str) -> RunRecord:
    return RunRecord(run_id=run_id, task_id="t", backend="opencode", status=status)


class _Clock:
    """A monotonic clock that only advances when something sleeps. No wall-clock dependency."""

    def __init__(self) -> None:
        self.t = 0.0
        self.slept: list[float] = []

    def monotonic(self) -> float:
        return self.t

    def sleep(self, seconds: float) -> None:
        self.slept.append(seconds)
        self.t += seconds


def _fetch_from(*frames: Mapping[str, RunRecord | None]):
    """Return a fetch that yields each frame in turn, then repeats the last one forever."""
    calls = {"n": 0}

    def fetch(ids: Sequence[str]) -> dict[str, RunRecord | None]:
        frame = frames[min(calls["n"], len(frames) - 1)]
        calls["n"] += 1
        return {rid: frame.get(rid) for rid in ids}

    fetch.calls = calls  # type: ignore[attr-defined]
    return fetch


def test_already_terminal_runs_return_without_sleeping() -> None:
    """The status is checked before the first sleep, so a finished run does not idle out a tick."""
    clock = _Clock()
    result = wait_for_terminal(
        _fetch_from({"a": _rec("a", "exited_clean")}),
        ["a"],
        timeout_s=60,
        sleep=clock.sleep,
        monotonic=clock.monotonic,
    )
    assert result.all_settled
    assert [r.run_id for r in result.settled] == ["a"]
    assert result.timed_out is False
    assert clock.slept == []


def test_wait_returns_when_the_last_run_finishes() -> None:
    clock = _Clock()
    running = {"a": _rec("a", "running"), "b": _rec("b", "running")}
    half = {"a": _rec("a", "exited_clean"), "b": _rec("b", "running")}
    done = {"a": _rec("a", "exited_clean"), "b": _rec("b", "failed")}

    result = wait_for_terminal(
        _fetch_from(running, half, done),
        ["a", "b"],
        timeout_s=60,
        sleep=clock.sleep,
        monotonic=clock.monotonic,
    )
    assert result.all_settled
    assert {r.run_id for r in result.settled} == {"a", "b"}
    assert result.pending == []
    assert clock.slept == [1.0, 1.0]
    assert result.waited_ms == 2000


def test_a_failed_run_is_settled_not_pending() -> None:
    """Settled means FINISHED, never succeeded - a waiter that skipped failures would never return.

    The docstring promises the driver branches on `status` exactly as after a poll, so every
    terminal status has to land in `settled`.
    """
    clock = _Clock()
    for status in ("failed", "timed_out", "cancelled", "verify_failed", "empty"):
        result = wait_for_terminal(
            _fetch_from({"a": _rec("a", status)}),
            ["a"],
            timeout_s=60,
            sleep=clock.sleep,
            monotonic=clock.monotonic,
        )
        assert [r.run_id for r in result.settled] == ["a"], f"{status} was not treated as settled"
        assert result.timed_out is False


def test_queued_is_not_settled() -> None:
    """A queued run has not started; returning it as settled would report work that never ran."""
    clock = _Clock()
    result = wait_for_terminal(
        _fetch_from({"a": _rec("a", "queued")}),
        ["a"],
        timeout_s=2,
        sleep=clock.sleep,
        monotonic=clock.monotonic,
    )
    assert result.timed_out is True
    assert [r.run_id for r in result.pending] == ["a"]


def test_expiry_returns_a_partial_result_rather_than_raising() -> None:
    """The whole degradation story rests on this: a cut-off wait must still hand back its progress."""
    clock = _Clock()
    frame = {"a": _rec("a", "exited_clean"), "b": _rec("b", "running")}

    result = wait_for_terminal(
        _fetch_from(frame),
        ["a", "b"],
        timeout_s=3,
        sleep=clock.sleep,
        monotonic=clock.monotonic,
    )
    assert result.timed_out is True
    assert result.all_settled is False
    assert [r.run_id for r in result.settled] == ["a"]
    assert [r.run_id for r in result.pending] == ["b"]
    assert result.waited_ms == 3000


def test_the_wait_never_sleeps_past_the_deadline() -> None:
    """Overshooting would report a wait longer than was asked for - and hold the call open for it."""
    clock = _Clock()
    result = wait_for_terminal(
        _fetch_from({"a": _rec("a", "running")}),
        ["a"],
        timeout_s=2.5,
        poll_interval_s=1.0,
        sleep=clock.sleep,
        monotonic=clock.monotonic,
    )
    assert clock.slept == [1.0, 1.0, 0.5]
    assert result.waited_ms == 2500


def test_timeout_is_capped_at_the_module_ceiling() -> None:
    clock = _Clock()
    result = wait_for_terminal(
        _fetch_from({"a": _rec("a", "running")}),
        ["a"],
        timeout_s=10_000,
        poll_interval_s=100,
        sleep=clock.sleep,
        monotonic=clock.monotonic,
    )
    assert result.waited_ms == int(MAX_WAIT_S * 1000)


def test_unknown_ids_are_reported_at_once_and_never_waited_on() -> None:
    """Nothing will ever create a record for an unknown id, so waiting on one only burns the clock."""
    clock = _Clock()
    result = wait_for_terminal(
        _fetch_from({"a": _rec("a", "exited_clean")}),
        ["a", "ghost"],
        timeout_s=60,
        sleep=clock.sleep,
        monotonic=clock.monotonic,
    )
    assert result.unknown == ["ghost"]
    assert [r.run_id for r in result.settled] == ["a"]
    assert result.all_settled is True, "an unknown id must not count as pending work"
    assert result.timed_out is False
    assert clock.slept == []


def test_all_unknown_returns_immediately() -> None:
    clock = _Clock()
    result = wait_for_terminal(
        _fetch_from({}), ["x", "y"], timeout_s=600,
        sleep=clock.sleep, monotonic=clock.monotonic,
    )
    assert result.unknown == ["x", "y"]
    assert result.settled == [] and result.pending == []
    assert clock.slept == []


def test_cancelling_a_waited_on_run_releases_the_wait() -> None:
    clock = _Clock()
    result = wait_for_terminal(
        _fetch_from({"a": _rec("a", "running")}, {"a": _rec("a", "cancelled")}),
        ["a"],
        timeout_s=600,
        sleep=clock.sleep,
        monotonic=clock.monotonic,
    )
    assert result.all_settled
    assert result.settled[0].status == "cancelled"
    assert result.waited_ms == 1000, "it should not have waited out the 600s timeout"


def test_duplicate_ids_are_collapsed() -> None:
    fetch = _fetch_from({"a": _rec("a", "exited_clean")})
    clock = _Clock()
    result = wait_for_terminal(
        fetch, ["a", "a", "a"], timeout_s=60, sleep=clock.sleep, monotonic=clock.monotonic
    )
    assert [r.run_id for r in result.settled] == ["a"]


def test_every_requested_id_appears_exactly_once_across_the_three_lists() -> None:
    """The result's contract: nothing requested is silently dropped, nothing is double-counted."""
    clock = _Clock()
    ids = ["done", "busy", "ghost"]
    result = wait_for_terminal(
        _fetch_from({"done": _rec("done", "exited_clean"), "busy": _rec("busy", "running")}),
        ids,
        timeout_s=1,
        sleep=clock.sleep,
        monotonic=clock.monotonic,
    )
    seen = [r.run_id for r in result.settled] + [r.run_id for r in result.pending] + result.unknown
    assert sorted(seen) == sorted(ids)


def test_a_zero_timeout_is_a_single_non_blocking_check() -> None:
    clock = _Clock()
    result = wait_for_terminal(
        _fetch_from({"a": _rec("a", "running")}),
        ["a"],
        timeout_s=0.0,
        sleep=clock.sleep,
        monotonic=clock.monotonic,
    )
    assert clock.slept == []
    assert result.timed_out is True
    assert [r.run_id for r in result.pending] == ["a"]


def test_all_settled_is_false_while_anything_is_pending() -> None:
    assert WaitResult(pending=[_rec("a", "running")]).all_settled is False
    assert WaitResult(settled=[_rec("a", "failed")]).all_settled is True


@pytest.mark.parametrize("status", ["exited_clean", "failed", "cancelled"])
def test_a_run_that_finishes_between_frames_is_not_missed(status: str) -> None:
    clock = _Clock()
    result = wait_for_terminal(
        _fetch_from({"a": _rec("a", "running")}, {"a": _rec("a", status)}),
        ["a"],
        timeout_s=60,
        sleep=clock.sleep,
        monotonic=clock.monotonic,
    )
    assert result.settled[0].status == status
