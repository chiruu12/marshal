"""Tests for cancel_run - pure unit tests (no real agents spawned)."""

from __future__ import annotations

import os
import signal
from pathlib import Path

import pytest

from marshal_engine.fleet import Fleet
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
    fleet.state.add(
        RunRecord(
            run_id="t.x.a2",
            task_id="t",
            backend="x",
            status="running",
            started_at="2026-01-01T00:00:00Z",
            pid=12345,
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
    def _raise(pgid: int, sig: int) -> None:
        raise ProcessLookupError()

    monkeypatch.setattr(os, "killpg", _raise)

    fleet = Fleet(tmp_path, {})
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
    rec = fleet.cancel_run("t.x.a4")
    assert rec.status == "cancelled"


def test_cancel_running_race_natural_finish_no_overwrite(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    """If the run finishes naturally between the kill and the re-read, do NOT overwrite the
    terminal status with 'cancelled'."""

    def _fake_killpg(pgid: int, sig: int) -> None:
        pass  # pretend we killed it

    monkeypatch.setattr(os, "killpg", _fake_killpg)

    running = RunRecord(
        run_id="t.x.a5",
        task_id="t",
        backend="x",
        status="running",
        started_at="2026-01-01T00:00:00Z",
        pid=12345,
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
    monkeypatch.setattr(fleet.state, "get", _get_override)

    rec = fleet.cancel_run("t.x.a5")
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
