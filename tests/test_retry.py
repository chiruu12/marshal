"""Tests for the transient-failure classifier and retry/backoff policy."""

from __future__ import annotations

from marshal_engine.retry import RetryPolicy, is_transient_failure
from marshal_engine.types import AgentResult, RunStatus


def _failed(error: str | None) -> AgentResult:
    return AgentResult(status=RunStatus.FAILED, error=error)


def test_transient_markers_match_case_insensitively() -> None:
    assert is_transient_failure(_failed("opencode: database is locked"))
    assert is_transient_failure(_failed("HTTP 429 Too Many Requests"))
    assert is_transient_failure(_failed("Provider Overloaded, try again"))
    assert is_transient_failure(_failed("connection reset by peer"))


def test_genuine_failure_is_not_transient() -> None:
    assert not is_transient_failure(_failed("AssertionError: expected 2 got 3"))
    assert not is_transient_failure(_failed(None))   # no error text -> not attributable to a cause
    assert not is_transient_failure(_failed(""))


def test_only_failed_status_is_transient() -> None:
    # A timeout carries its own status; retrying it would burn another full timeout window.
    assert not is_transient_failure(
        AgentResult(status=RunStatus.TIMED_OUT, error="rate limit hit before the timeout")
    )
    assert not is_transient_failure(AgentResult(status=RunStatus.SUCCEEDED))


def test_backoff_grows_geometrically() -> None:
    p = RetryPolicy(max_attempts=4, backoff_base_s=1.0, backoff_factor=2.0)
    assert p.delay_for(1) == 1.0
    assert p.delay_for(2) == 2.0
    assert p.delay_for(3) == 4.0
