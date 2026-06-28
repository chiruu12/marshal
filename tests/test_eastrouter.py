"""Tests for the EastRouter real-cost reader.

Pure logic only - the HTTP getter is injected, so there is no network. Cover attribution by
(model, window), multi-record (multi-turn) summing, the token-reconciliation guard that declines to
claim a cost when the window is ambiguous, and the graceful no-ops (missing key, transport failure).
"""

from __future__ import annotations

import json

import pytest

from marshal_engine import UsageSource
from marshal_engine.eastrouter import fetch_run_cost

_START = "2026-06-28T12:00:00+00:00"
_END = "2026-06-28T12:00:10+00:00"


def _usage(*records: dict[str, object]) -> str:
    return json.dumps({"data": list(records)})


def _rec(model: str, amount: float, prompt: int, completion: int, when: str) -> dict[str, object]:
    return {
        "request_id": "er_x",
        "model": model,
        "amount_usd": amount,
        "prompt_tokens": prompt,
        "completion_tokens": completion,
        "reasoning_tokens": 0,
        "created_at": when,
        "pool_used": "plan_a",
    }


def _getter(body: str | None) -> object:
    def get(url: str, key: str, timeout_s: float) -> str | None:
        return body

    return get


def test_happy_path_real_cost() -> None:
    body = _usage(
        _rec("z-ai/glm-5.1", 0.005, 7000, 150, "2026-06-28T12:00:05+00:00"),
        _rec("moonshotai/kimi-k2.7-code", 0.01, 5000, 40, "2026-06-28T12:00:06+00:00"),
    )
    ext = fetch_run_cost(
        model="z-ai/glm-5.1", start_iso=_START, end_iso=_END,
        input_tokens=7000, output_tokens=150,
        api_key="sk-test", attempts=1, http=_getter(body),  # type: ignore[arg-type]
    )
    assert ext is not None
    assert ext.cost_usd == 0.005
    assert ext.source is UsageSource.ADMIN_API
    assert ext.matched_records == 1
    assert ext.prompt_tokens == 7000


def test_multi_record_sums_cost_and_tokens() -> None:
    # one run, two EastRouter requests (multi-turn) -> cost + tokens sum
    body = _usage(
        _rec("z-ai/glm-5.1", 0.004, 4000, 100, "2026-06-28T12:00:03+00:00"),
        _rec("z-ai/glm-5.1", 0.003, 3000, 80, "2026-06-28T12:00:07+00:00"),
    )
    ext = fetch_run_cost(
        model="z-ai/glm-5.1", start_iso=_START, end_iso=_END,
        input_tokens=7000, output_tokens=180,
        api_key="sk-test", attempts=1, http=_getter(body),  # type: ignore[arg-type]
    )
    assert ext is not None
    assert ext.cost_usd == 0.007
    assert ext.matched_records == 2
    assert ext.prompt_tokens == 7000


def test_wrong_model_is_no_match() -> None:
    body = _usage(_rec("moonshotai/kimi-k2.7-code", 0.01, 7000, 40, "2026-06-28T12:00:05+00:00"))
    ext = fetch_run_cost(
        model="z-ai/glm-5.1", start_iso=_START, end_iso=_END,
        input_tokens=7000, output_tokens=40,
        api_key="sk-test", attempts=1, http=_getter(body),  # type: ignore[arg-type]
    )
    assert ext is None


def test_out_of_window_is_no_match() -> None:
    body = _usage(_rec("z-ai/glm-5.1", 0.005, 7000, 150, "2026-06-28T12:05:00+00:00"))
    ext = fetch_run_cost(
        model="z-ai/glm-5.1", start_iso=_START, end_iso=_END,
        input_tokens=7000, output_tokens=150,
        api_key="sk-test", attempts=1, http=_getter(body),  # type: ignore[arg-type]
    )
    assert ext is None


def test_token_mismatch_declines_cost() -> None:
    # window caught a record whose tokens don't match this run (e.g. a concurrent same-model run):
    # the guard refuses to claim a wrong cost.
    body = _usage(_rec("z-ai/glm-5.1", 0.02, 30000, 150, "2026-06-28T12:00:05+00:00"))
    ext = fetch_run_cost(
        model="z-ai/glm-5.1", start_iso=_START, end_iso=_END,
        input_tokens=7000, output_tokens=150,
        api_key="sk-test", attempts=1, http=_getter(body),  # type: ignore[arg-type]
    )
    assert ext is None


def test_missing_key_is_none(monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.delenv("EASTROUTER_API_KEY", raising=False)
    ext = fetch_run_cost(
        model="z-ai/glm-5.1", start_iso=_START, end_iso=_END,
        input_tokens=7000, output_tokens=150,
        attempts=1, http=_getter(_usage()),  # type: ignore[arg-type]
    )
    assert ext is None


def test_transport_failure_is_none() -> None:
    ext = fetch_run_cost(
        model="z-ai/glm-5.1", start_iso=_START, end_iso=_END,
        input_tokens=7000, output_tokens=150,
        api_key="sk-test", attempts=1, http=_getter(None),  # type: ignore[arg-type]
    )
    assert ext is None


def test_opencode_provider_prefixed_model_matches() -> None:
    # OpenCode passes `eastrouter/z-ai/glm-5.1`; /v1/usage logs the bare `z-ai/glm-5.1`.
    body = _usage(_rec("z-ai/glm-5.1", 0.005, 7000, 150, "2026-06-28T12:00:05+00:00"))
    ext = fetch_run_cost(
        model="eastrouter/z-ai/glm-5.1", start_iso=_START, end_iso=_END,
        input_tokens=7000, output_tokens=150,
        api_key="sk-test", attempts=1, http=_getter(body),  # type: ignore[arg-type]
    )
    assert ext is not None
    assert ext.cost_usd == 0.005
    assert ext.source is UsageSource.ADMIN_API


def test_retry_picks_up_late_record(monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setattr("marshal_engine.eastrouter.time.sleep", lambda _s: None)
    body = _usage(_rec("z-ai/glm-5.1", 0.005, 7000, 150, "2026-06-28T12:00:05+00:00"))
    calls = {"n": 0}

    def get(url: str, key: str, timeout_s: float) -> str | None:
        calls["n"] += 1
        return None if calls["n"] == 1 else body  # record not propagated on first poll

    ext = fetch_run_cost(
        model="z-ai/glm-5.1", start_iso=_START, end_iso=_END,
        input_tokens=7000, output_tokens=150,
        api_key="sk-test", attempts=2, http=get,  # type: ignore[arg-type]
    )
    assert ext is not None and ext.cost_usd == 0.005
    assert calls["n"] == 2
