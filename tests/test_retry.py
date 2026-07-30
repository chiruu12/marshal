"""Tests for the transient-failure classifier and retry/backoff policy."""

from __future__ import annotations

import pytest

from marshal_engine.retry import RetryPolicy, is_transient_failure
from marshal_engine.types import AgentResult, RunStatus


def _failed(error: str | None) -> AgentResult:
    return AgentResult(status=RunStatus.FAILED, error=error)


def test_transient_markers_match_case_insensitively() -> None:
    assert is_transient_failure(_failed("opencode: database is locked"))
    assert is_transient_failure(_failed("HTTP 429 Too Many Requests"))
    assert is_transient_failure(_failed("Provider Overloaded, try again"))
    assert is_transient_failure(_failed("connection reset by peer"))


@pytest.mark.parametrize(
    "error",
    [
        # Claude / Anthropic-shaped rate limits
        "API Error: 429 rate_limit_error — request too large",
        "anthropic: Error code: 429 - rate_limit_error",
        "overloaded_error: The model is overloaded, please try again",
        # OpenAI-shaped
        "Error code: 429 - {'error': {'message': 'Rate limit reached for gpt-4'}}",
        "openai: Rate limit exceeded. Please try again later.",
        # Generic provider / HTTP transport shapes
        "HTTP/1.1 503 Service Unavailable",
        "upstream status 502",
        "cursor-agent: HTTP 429",
        "502 Bad Gateway from provider",
        "504 Gateway Timeout",
        "connection refused while dialing api",
        "ECONNRESET",
        "opencode: database is locked",
    ],
)
def test_genuine_provider_failures_are_transient(error: str) -> None:
    assert is_transient_failure(_failed(error))


@pytest.mark.parametrize(
    "error",
    [
        # Task output that merely mentions HTTP status digits must NOT burn a retry.
        "AssertionError: expected status_code 200, docs mention 429 for clients",
        "failed tests: retry helper should map 503 to backoff (see comments)",
        "implement handlers for 429 and 504 in the client",
        "fixture response body includes code 502 for the mock server",
        "print('status codes: 200, 429, 500')",
        # Framer must not match inside exception names / compound words / digit runs.
        "AssertionError: 429 was returned",
        "ValueError: 503 unexpected",
        "TypeError: 502",
        "status 4294",
        "encode: 429",
        "teststatus 503 ok",
        "fixed the 502 bug",
        "error code 4299",
    ],
)
def test_bare_status_code_mentions_are_not_transient(error: str) -> None:
    assert not is_transient_failure(_failed(error))


@pytest.mark.parametrize(
    "error",
    [
        "HTTP 429",
        "Error code: 429",
        "429 Too Many Requests",
        "502 Bad Gateway",
        "cursor-agent: HTTP 429",
        # HTTPError alone would not match the anchored error framer; phrase markers still do.
        "HTTPError: 429 Client Error: Too Many Requests",
    ],
)
def test_framed_status_codes_and_reason_phrases_are_transient(error: str) -> None:
    assert is_transient_failure(_failed(error))


def test_genuine_failure_is_not_transient() -> None:
    assert not is_transient_failure(_failed("AssertionError: expected 2 got 3"))
    assert not is_transient_failure(_failed(None))   # no error text -> not attributable to a cause
    assert not is_transient_failure(_failed(""))


def test_only_failed_status_is_transient() -> None:
    # A timeout carries its own status; retrying it would burn another full timeout window.
    assert not is_transient_failure(
        AgentResult(status=RunStatus.TIMED_OUT, error="rate limit hit before the timeout")
    )
    assert not is_transient_failure(AgentResult(status=RunStatus.EXITED_CLEAN))


def test_backoff_grows_geometrically() -> None:
    p = RetryPolicy(max_attempts=4, backoff_base_s=1.0, backoff_factor=2.0)
    assert p.delay_for(1) == 1.0
    assert p.delay_for(2) == 2.0
    assert p.delay_for(3) == 4.0
