"""Tests for the EastRouter real-cost reader.

Pure logic only - the HTTP getter is injected, so there is no network. Cover attribution by
(model, window), multi-record (multi-turn) summing, the token-reconciliation guard that declines to
claim a cost when the window is ambiguous, and the graceful no-ops (missing key, transport failure).
"""

from __future__ import annotations

import json
import re

import pytest

from marshal_engine import UsageSource
from marshal_engine.accounting.eastrouter import fetch_run_cost

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


def test_missing_amount_usd_does_not_fabricate_admin_api_zero() -> None:
    # Honesty: a row with no usable amount_usd must not become admin-api $0. Missing/null
    # fields are skipped so token reconciliation fails and the run stays unattributed.
    for row in (
        {  # missing amount_usd entirely
            "model": "z-ai/glm-5.1",
            "prompt_tokens": 7000,
            "completion_tokens": 150,
            "reasoning_tokens": 0,
            "created_at": "2026-06-28T12:00:05+00:00",
        },
        {  # explicit null
            "model": "z-ai/glm-5.1",
            "amount_usd": None,
            "prompt_tokens": 7000,
            "completion_tokens": 150,
            "reasoning_tokens": 0,
            "created_at": "2026-06-28T12:00:05+00:00",
        },
    ):
        ext = fetch_run_cost(
            model="z-ai/glm-5.1", start_iso=_START, end_iso=_END,
            input_tokens=7000,
            api_key="sk-test", attempts=1, http=_getter(_usage(row)),  # type: ignore[arg-type]
        )
        assert ext is None, f"expected no attribution for row={row!r}"


def test_explicit_zero_amount_usd_still_attributes() -> None:
    # An explicitly reported 0.0 is a real free charge — keep admin-api attribution.
    body = _usage(_rec("z-ai/glm-5.1", 0.0, 7000, 150, "2026-06-28T12:00:05+00:00"))
    ext = fetch_run_cost(
        model="z-ai/glm-5.1", start_iso=_START, end_iso=_END,
        input_tokens=7000,
        api_key="sk-test", attempts=1, http=_getter(body),  # type: ignore[arg-type]
    )
    assert ext is not None
    assert ext.cost_usd == 0.0
    assert ext.source is UsageSource.ADMIN_API


def test_happy_path_real_cost() -> None:
    body = _usage(
        _rec("z-ai/glm-5.1", 0.005, 7000, 150, "2026-06-28T12:00:05+00:00"),
        _rec("moonshotai/kimi-k2.7-code", 0.01, 5000, 40, "2026-06-28T12:00:06+00:00"),
    )
    ext = fetch_run_cost(
        model="z-ai/glm-5.1", start_iso=_START, end_iso=_END,
        input_tokens=7000,
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
        input_tokens=7000,
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
        input_tokens=7000,
        api_key="sk-test", attempts=1, http=_getter(body),  # type: ignore[arg-type]
    )
    assert ext is None


def test_out_of_window_is_no_match() -> None:
    body = _usage(_rec("z-ai/glm-5.1", 0.005, 7000, 150, "2026-06-28T12:05:00+00:00"))
    ext = fetch_run_cost(
        model="z-ai/glm-5.1", start_iso=_START, end_iso=_END,
        input_tokens=7000,
        api_key="sk-test", attempts=1, http=_getter(body),  # type: ignore[arg-type]
    )
    assert ext is None


def test_token_mismatch_declines_cost() -> None:
    # window caught a record whose tokens don't match this run (e.g. a concurrent same-model run):
    # the guard refuses to claim a wrong cost.
    body = _usage(_rec("z-ai/glm-5.1", 0.02, 30000, 150, "2026-06-28T12:00:05+00:00"))
    ext = fetch_run_cost(
        model="z-ai/glm-5.1", start_iso=_START, end_iso=_END,
        input_tokens=7000,
        api_key="sk-test", attempts=1, http=_getter(body),  # type: ignore[arg-type]
    )
    assert ext is None


def test_a_concurrent_run_inside_the_slack_is_not_billed_to_this_run() -> None:
    """The reconciliation slack used to be symmetric, so any concurrent same-model run whose
    prompt fit inside it was folded in - its charge summed and the total stamped `admin-api`,
    i.e. measured. Over-counting cannot happen to a run's own records: those prompt tokens were
    never sent by this run."""
    body = _usage(
        _rec("z-ai/glm-5.1", 0.005, 10000, 100, "2026-06-28T12:00:03+00:00"),  # this run
        _rec("z-ai/glm-5.1", 0.002, 600, 40, "2026-06-28T12:00:05+00:00"),     # somebody else's
    )
    ext = fetch_run_cost(
        model="z-ai/glm-5.1", start_iso=_START, end_iso=_END,
        input_tokens=10000,
        api_key="sk-test", attempts=1, http=_getter(body),  # type: ignore[arg-type]
    )
    assert ext is None, "a concurrent run's charge was billed here as measured cost"


def test_an_intruders_reasoning_tokens_cannot_inflate_the_bill() -> None:
    """The cost an intruder adds is not bounded by the prompt slack that let it in: `completion`
    and `reasoning` are summed and never reconciled, so a record with a tiny prompt and enormous
    reasoning could contribute an unbounded amount."""
    body = _usage(
        _rec("z-ai/glm-5.1", 0.005, 10000, 100, "2026-06-28T12:00:03+00:00"),
        _rec("z-ai/glm-5.1", 0.900, 600, 60000, "2026-06-28T12:00:05+00:00"),
    )
    ext = fetch_run_cost(
        model="z-ai/glm-5.1", start_iso=_START, end_iso=_END,
        input_tokens=10000,
        api_key="sk-test", attempts=1, http=_getter(body),  # type: ignore[arg-type]
    )
    assert ext is None


def test_a_foreign_record_inside_a_small_absolute_slack_is_refused() -> None:
    """The over side gets no absolute allowance at all. Token counts are integers, so nothing
    rounds - a slack here would not absorb noise, it would admit a foreign record small enough to
    fit, and that record arrives with its whole `amount` attached."""
    body = _usage(
        _rec("z-ai/glm-5.1", 0.005, 10000, 100, "2026-06-28T12:00:03+00:00"),
        _rec("z-ai/glm-5.1", 0.400, 150, 9000, "2026-06-28T12:00:05+00:00"),  # 150 < any small tol
    )
    ext = fetch_run_cost(
        model="z-ai/glm-5.1", start_iso=_START, end_iso=_END,
        input_tokens=10000,
        api_key="sk-test", attempts=1, http=_getter(body),  # type: ignore[arg-type]
    )
    assert ext is None


def test_a_short_window_still_attributes() -> None:
    """The under side keeps its slack. A record that has not propagated makes the cost SHORT,
    which understates spend - the safe direction, and the case the tolerance exists for."""
    body = _usage(_rec("z-ai/glm-5.1", 0.004, 9500, 100, "2026-06-28T12:00:03+00:00"))
    ext = fetch_run_cost(
        model="z-ai/glm-5.1", start_iso=_START, end_iso=_END,
        input_tokens=10000,
        api_key="sk-test", attempts=1, http=_getter(body),  # type: ignore[arg-type]
    )
    assert ext is not None
    assert ext.cost_usd == 0.004


def test_cache_reads_explain_a_provider_counting_them_in_prompt() -> None:
    """`input_tokens` excludes cache reads; a provider may bill them inside `prompt`. That
    over-count is explainable by this run alone, so it must not be read as an intruder - a
    flat refusal here would silently turn every cache-hitting run's cost into `unavailable`."""
    body = _usage(_rec("z-ai/glm-5.1", 0.006, 14000, 120, "2026-06-28T12:00:03+00:00"))
    ext = fetch_run_cost(
        model="z-ai/glm-5.1", start_iso=_START, end_iso=_END,
        input_tokens=10000, cache_read_tokens=4000,
        api_key="sk-test", attempts=1, http=_getter(body),  # type: ignore[arg-type]
    )
    assert ext is not None
    assert ext.cost_usd == 0.006


def test_missing_key_is_none(monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.delenv("EASTROUTER_API_KEY", raising=False)
    ext = fetch_run_cost(
        model="z-ai/glm-5.1", start_iso=_START, end_iso=_END,
        input_tokens=7000,
        attempts=1, http=_getter(_usage()),  # type: ignore[arg-type]
    )
    assert ext is None


def test_transport_failure_is_none() -> None:
    ext = fetch_run_cost(
        model="z-ai/glm-5.1", start_iso=_START, end_iso=_END,
        input_tokens=7000,
        api_key="sk-test", attempts=1, http=_getter(None),  # type: ignore[arg-type]
    )
    assert ext is None


def test_opencode_provider_prefixed_model_matches() -> None:
    # OpenCode passes `eastrouter/z-ai/glm-5.1`; /v1/usage logs the bare `z-ai/glm-5.1`.
    body = _usage(_rec("z-ai/glm-5.1", 0.005, 7000, 150, "2026-06-28T12:00:05+00:00"))
    ext = fetch_run_cost(
        model="eastrouter/z-ai/glm-5.1", start_iso=_START, end_iso=_END,
        input_tokens=7000,
        api_key="sk-test", attempts=1, http=_getter(body),  # type: ignore[arg-type]
    )
    assert ext is not None
    assert ext.cost_usd == 0.005
    assert ext.source is UsageSource.ADMIN_API


def _paged_getter(pages: dict[int, str]) -> object:
    def get(url: str, key: str, timeout_s: float) -> str | None:
        m = re.search(r"offset=(\d+)", url)
        return pages.get(int(m.group(1)) if m else 0)

    return get


def test_pagination_finds_records_beyond_the_first_page() -> None:
    # busy account: page 0 (full at page_size=2) is unrelated records; the run's record is on page 1.
    page0 = _usage(
        _rec("other/model", 0.01, 100, 10, "2026-06-28T12:00:01+00:00"),
        _rec("other/model", 0.01, 100, 10, "2026-06-28T12:00:02+00:00"),
    )
    page1 = _usage(_rec("z-ai/glm-5.1", 0.005, 7000, 150, "2026-06-28T12:00:05+00:00"))
    ext = fetch_run_cost(
        model="z-ai/glm-5.1", start_iso=_START, end_iso=_END,
        input_tokens=7000,
        api_key="sk-test", attempts=1, http=_paged_getter({0: page0, 2: page1}), page_size=2,  # type: ignore[arg-type]
    )
    assert ext is not None and ext.cost_usd == 0.005  # found despite being past the first page


def test_pagination_does_not_stop_on_a_page_of_unusable_rows() -> None:
    # A FULL page whose rows all lack a usable `amount_usd` is skipped for attribution, but it is
    # not the last page: terminating on the usable count would read it as empty/short and never
    # reach page 1, silently losing a real charge.
    page0 = _usage(
        {"model": "other/model", "prompt_tokens": 100, "completion_tokens": 10,
         "created_at": "2026-06-28T12:00:01+00:00"},                      # no amount_usd
        {"model": "other/model", "amount_usd": None, "prompt_tokens": 100,
         "completion_tokens": 10, "created_at": "2026-06-28T12:00:02+00:00"},  # null amount_usd
    )
    page1 = _usage(_rec("z-ai/glm-5.1", 0.005, 7000, 150, "2026-06-28T12:00:05+00:00"))
    ext = fetch_run_cost(
        model="z-ai/glm-5.1", start_iso=_START, end_iso=_END,
        input_tokens=7000,
        api_key="sk-test", attempts=1, http=_paged_getter({0: page0, 2: page1}), page_size=2,  # type: ignore[arg-type]
    )
    assert ext is not None and ext.cost_usd == 0.005


def test_pagination_terminates_when_offset_ignored() -> None:
    # the API ignores `offset` and returns the same FULL page forever; the no-progress guard must
    # stop the walk (not hang) and still attribute the in-window records on that page.
    full = _usage(
        _rec("z-ai/glm-5.1", 0.004, 4000, 100, "2026-06-28T12:00:03+00:00"),
        _rec("z-ai/glm-5.1", 0.003, 3000, 80, "2026-06-28T12:00:07+00:00"),
    )

    def get(url: str, key: str, timeout_s: float) -> str | None:
        return full

    ext = fetch_run_cost(
        model="z-ai/glm-5.1", start_iso=_START, end_iso=_END,
        input_tokens=7000,
        api_key="sk-test", attempts=1, http=get, page_size=2,  # type: ignore[arg-type]
    )
    assert ext is not None and ext.cost_usd == 0.007


def test_naive_created_at_is_treated_as_utc() -> None:
    # Regression: EastRouter may return a created_at with no offset; comparing it to the aware run
    # window used to raise TypeError (silently dropping the real cost). It must be treated as UTC.
    body = _usage(_rec("z-ai/glm-5.1", 0.005, 7000, 150, "2026-06-28T12:00:05"))  # naive, no +00:00
    ext = fetch_run_cost(
        model="z-ai/glm-5.1", start_iso=_START, end_iso=_END,
        input_tokens=7000,
        api_key="sk-test", attempts=1, http=_getter(body),  # type: ignore[arg-type]
    )
    assert ext is not None and ext.cost_usd == 0.005


def test_retry_picks_up_late_record(monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setattr("marshal_engine.accounting.eastrouter.time.sleep", lambda _s: None)
    body = _usage(_rec("z-ai/glm-5.1", 0.005, 7000, 150, "2026-06-28T12:00:05+00:00"))
    calls = {"n": 0}

    def get(url: str, key: str, timeout_s: float) -> str | None:
        calls["n"] += 1
        return None if calls["n"] == 1 else body  # record not propagated on first poll

    ext = fetch_run_cost(
        model="z-ai/glm-5.1", start_iso=_START, end_iso=_END,
        input_tokens=7000,
        api_key="sk-test", attempts=2, http=get,  # type: ignore[arg-type]
    )
    assert ext is not None and ext.cost_usd == 0.005
    assert calls["n"] == 2


# --- a partial window must never be reported as a measured cost ---------------------------------


def _sequential_pages(pages: list[str | None]):
    """Serve one body per offset, so pagination behaviour can be driven exactly."""
    def get(url: str, key: str, timeout_s: float) -> str | None:
        offset = int(re.search(r"offset=(\d+)", url).group(1))
        limit = int(re.search(r"limit=(\d+)", url).group(1))
        index = offset // limit
        return pages[index] if index < len(pages) else _usage()
    return get


def test_a_page_cap_hit_declines_to_attribute_cost() -> None:
    """Running out of pages means the window was never fully seen.

    Reconciliation does NOT catch this on its own: it tolerates 10% of prompt tokens going missing,
    so a dropped record under that threshold reconciles while its charge is simply absent - and the
    result would be tagged `admin-api`, i.e. measured. Short-and-measured is the failure mode the
    cost invariant exists to prevent, so an unseen remainder means no claim at all.
    """
    # Distinct records per page, so the walk really is stopped by the CAP - identical rows would
    # dedup and end the loop as "the API is repeating itself", which is a different case.
    def page(n: int) -> str:
        return _usage(
            _rec("m", 0.01, 100, 10, f"2026-06-28T12:00:0{n}+00:00"),
            _rec("m", 0.01, 100, 10, f"2026-06-28T12:00:0{n + 1}+00:00"),
        )

    getter = _sequential_pages([page(1), page(3), page(5)])

    cost = fetch_run_cost(
        model="m", start_iso=_START, end_iso=_END, input_tokens=200,
        api_key="k", http=getter, page_size=2, max_pages=2, attempts=1,
    )
    assert cost is None


def test_a_later_page_failing_declines_to_attribute_cost() -> None:
    """A transport failure part-way through leaves a set we know is incomplete."""
    full_page = _usage(*[_rec("m", 0.01, 100, 10, _START) for _ in range(2)])
    getter = _sequential_pages([full_page, None])

    cost = fetch_run_cost(
        model="m", start_iso=_START, end_iso=_END, input_tokens=200,
        api_key="k", http=getter, page_size=2, max_pages=5, attempts=1,
    )
    assert cost is None


def test_a_window_that_ends_naturally_still_attributes() -> None:
    """The guard must not swallow the normal case: a short page IS the end of the window."""
    getter = _sequential_pages([_usage(_rec("m", 0.01, 200, 10, _START))])

    cost = fetch_run_cost(
        model="m", start_iso=_START, end_iso=_END, input_tokens=200,
        api_key="k", http=getter, page_size=2, max_pages=5, attempts=1,
    )
    assert cost is not None
    assert cost.source is UsageSource.ADMIN_API
    assert cost.cost_usd == pytest.approx(0.01)
