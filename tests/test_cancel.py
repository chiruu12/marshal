"""Tests for cancel_run - pure unit tests (no real agents spawned)."""

from __future__ import annotations

import os
import signal
from pathlib import Path

import pytest

from marshal_engine.fleet import (
    Fleet,
    _publish_pid,
    _register_inflight_run,
    _unregister_inflight_run,
)
from marshal_engine.state import RunRecord


def test_cancel_unknown_run_raises(tmp_path: Path) -> None:
    fleet = Fleet(tmp_path, {})
    with pytest.raises(ValueError, match="no such run"):
        fleet.cancel_run("nope")


def test_cancel_non_running_is_noop(tmp_path: Path) -> None:
    fleet = Fleet(tmp_path, {})
    fleet.state.add(
        RunRecord(
            run_id="t.x.a1",
            task_id="t",
            backend="x",
            status="succeeded",
            started_at="2026-01-01T00:00:00Z",
            ended_at="2026-01-01T00:01:00Z",
        )
    )
    rec = fleet.cancel_run("t.x.a1")
    assert rec.status == "succeeded"


def test_cancel_running_with_pid_kills_and_marks_cancelled(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    killed: list[tuple[int, int]] = []

    def _fake_killpg(pgid: int, sig: int) -> None:
        killed.append((pgid, sig))

    monkeypatch.setattr(os, "killpg", _fake_killpg)

    fleet = Fleet(tmp_path, {})
    # Cancel signals only through a live handle for a run THIS process started.
    _register_inflight_run(fleet.state.dir, "t.x.a2").pid = 12345
    fleet.state.add(
        RunRecord(
            run_id="t.x.a2",
            task_id="t",
            backend="x",
            status="running",
            started_at="2026-01-01T00:00:00Z",
            pid=12345,
            pid_start_time="Mon Jan  1 00:00:00 2026",
        )
    )
    rec = fleet.cancel_run("t.x.a2")
    assert rec.status == "cancelled"
    assert rec.ended_at is not None
    assert killed == [(12345, signal.SIGTERM)]


def test_cancel_running_no_pid_just_marks_cancelled(tmp_path: Path) -> None:
    fleet = Fleet(tmp_path, {})
    fleet.state.add(
        RunRecord(
            run_id="t.x.a3",
            task_id="t",
            backend="x",
            status="running",
            started_at="2026-01-01T00:00:00Z",
            pid=None,
        )
    )
    rec = fleet.cancel_run("t.x.a3")
    assert rec.status == "cancelled"
    assert rec.ended_at is not None


def test_cancel_running_kill_race_still_marks_cancelled(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    """The agent exits between the decision to signal and the signal itself: `killpg` raises
    ProcessLookupError and the run must still be stamped `cancelled`.

    The handle registration is load-bearing. Cancel signals only through an in-process handle, so
    without one this never reaches `killpg` at all and would pass on the `update_if` alone -
    covering the race in name only."""
    called: list[int] = []

    def _raise(pgid: int, sig: int) -> None:
        called.append(pgid)
        raise ProcessLookupError()

    monkeypatch.setattr(os, "killpg", _raise)

    fleet = Fleet(tmp_path, {})
    handle = _register_inflight_run(fleet.state.dir, "t.x.a4")
    _publish_pid(handle, 99999)
    fleet.state.add(
        RunRecord(
            run_id="t.x.a4",
            task_id="t",
            backend="x",
            status="running",
            started_at="2026-01-01T00:00:00Z",
            pid=99999,
        )
    )
    try:
        rec = fleet.cancel_run("t.x.a4")
    finally:
        _unregister_inflight_run(fleet.state.dir, "t.x.a4")
    assert called == [99999], "the kill path was never entered, so the race is untested"
    assert rec.status == "cancelled"


def test_cancel_running_race_natural_finish_no_overwrite(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    """If the run finishes naturally between the kill and the re-read, do NOT overwrite the
    terminal status with 'cancelled'.

    Signalling goes through an in-process handle, so one is registered; the `_pid_start_time` /
    `_pid_alive` stubs this test used to carry were left over from the older identity-checked
    cancel and no longer decide anything."""
    killed: list[int] = []

    def _fake_killpg(pgid: int, sig: int) -> None:
        killed.append(pgid)

    monkeypatch.setattr(os, "killpg", _fake_killpg)

    running = RunRecord(
        run_id="t.x.a5",
        task_id="t",
        backend="x",
        status="running",
        started_at="2026-01-01T00:00:00Z",
        pid=12345,
        pid_start_time="Mon Jan  1 00:00:00 2026",
    )
    finished = RunRecord(
        run_id="t.x.a5",
        task_id="t",
        backend="x",
        status="succeeded",
        started_at="2026-01-01T00:00:00Z",
        ended_at="2026-01-01T00:00:30Z",
        pid=12345,
    )

    call_count: int = 0

    def _get_override(run_id: str) -> RunRecord | None:
        nonlocal call_count
        call_count += 1
        return running if call_count == 1 else finished

    fleet = Fleet(tmp_path, {})
    handle = _register_inflight_run(fleet.state.dir, "t.x.a5")
    _publish_pid(handle, 12345)
    monkeypatch.setattr(fleet.state, "get", _get_override)

    try:
        rec = fleet.cancel_run("t.x.a5")
    finally:
        _unregister_inflight_run(fleet.state.dir, "t.x.a5")
    assert killed == [12345], "the kill was never attempted, so there was no race to lose"
    assert rec.status == "succeeded"  # NOT overwritten to cancelled
    assert call_count == 2  # exactly two reads: before and after the kill attempt


def test_build_app_registers_cancel_run_tool(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    pytest.importorskip("mcp")
    import asyncio

    from marshal_engine.mcp_server import build_app, build_service

    repo = tmp_path / "repo"
    repo.mkdir()
    monkeypatch.setenv("MARSHAL_REPO", str(repo))
    monkeypatch.delenv("MARSHAL_CONFIG", raising=False)
    app = build_app(build_service())
    names = {t.name for t in asyncio.run(app.list_tools())}
    assert "cancel_run" in names
