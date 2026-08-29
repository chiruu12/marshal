"""Tests for driver-facing diagnostic messages in ``diagnostics``."""

from __future__ import annotations

import os

from marshal_engine.orchestration.diagnostics import _live_agent_message
from marshal_engine.orchestration.liveness import _pid_start_time
from marshal_engine.runtime.state import RunRecord

LIVE_PID = os.getpid()


def _cancelled_rec(**overrides: object) -> RunRecord:
    base = dict(
        run_id="diag.writer.x",
        task_id="diag",
        backend="writer",
        status="cancelled",
        pid=LIVE_PID,
    )
    base.update(overrides)
    return RunRecord(**base)  # type: ignore[arg-type]


def test_live_agent_message_confident_when_identity_verified() -> None:
    """When pid identity is provably ours, name the agent - same certainty as ``cancel_run``."""
    started = _pid_start_time(LIVE_PID)
    assert started is not None, "test setup: could not probe a live pid start time"
    msg = _live_agent_message(_cancelled_rec(pid_start_time=started))
    assert "its agent is still alive at pid" in msg
    assert "cannot confirm it is the agent" not in msg
    assert "a cancel from another Marshal process cannot signal it" in msg


def test_live_agent_message_hedged_when_identity_unverifiable() -> None:
    """When identity cannot be verified, hedge - mirroring ``cancel_run`` for the same ambiguity."""
    msg = _live_agent_message(_cancelled_rec(pid_start_time=None))
    assert "something is still alive at pid" in msg
    assert "Marshal cannot confirm it is the agent" in msg
    assert "its agent is still alive at pid" not in msg
    assert "Verify the process before ending it" in msg
    assert "a cancel from another Marshal process cannot signal it" in msg


def test_live_agent_message_confident_timeout_kill_cause_when_verified() -> None:
    """The timeout / failed-kill cause split stays intact when identity is verified."""
    started = _pid_start_time(LIVE_PID)
    assert started is not None
    msg = _live_agent_message(
        _cancelled_rec(status="timed_out", agent_survived_kill=True, pid_start_time=started)
    )
    assert "its agent is still alive at pid" in msg
    assert "the run timed out and the kill did not land" in msg
    assert "a cancel from another Marshal process cannot signal it" not in msg
