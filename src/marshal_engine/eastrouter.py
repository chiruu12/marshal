"""Read REAL per-request cost from EastRouter's ``/v1/usage`` and reconcile it to a Marshal run.

EastRouter is an OpenAI-compatible router. Codex routed through it reports tokens but no cost
(``unavailable``), and EastRouter's per-request price varies with prompt caching - so a static price
table would systematically mislead (cache-heavy sessions are cheap, fresh runs are dear). This
module fetches the ACTUAL ``amount_usd`` EastRouter charged, so cost is reported as ``admin-api``
(real), never an estimate.

Attribution. A Marshal run carries no EastRouter ``request_id`` (Codex doesn't surface it), so usage
records are matched by ``(model, created_at within the run's [start, end] window)``. That is exact
when at most one run uses a given EastRouter model at a time - the default fleet pairs each model
with a single client. If two clients drive the SAME EastRouter model concurrently, the window cannot
separate them; the token-reconciliation guard below detects the mismatch (matched prompt tokens won't
equal the run's input tokens) and the run KEEPS its estimated/unavailable cost rather than asserting
a wrong real cost. Honest-or-nothing.

Pagination. ``/v1/usage`` returns the most recent records; a single page can miss a run's records
when the account is busy (e.g. a long run + a concurrent benchmark push them past page 1). So we
paginate (assumed newest-first), accumulating until a page is short (the last page), predates the
window, repeats (the API ignored ``offset`` - a no-progress guard, so we never loop forever), or the
page cap is hit. Without full pagination a long run's real cost would silently fall back to
``unavailable`` even though the provider charged for it.
"""

from __future__ import annotations

import json
import os
import time
import urllib.error
import urllib.request
from collections.abc import Callable
from dataclasses import dataclass
from datetime import datetime, timedelta, timezone

from . import __version__
from .types import UsageSource

DEFAULT_BASE_URL = "https://api.eastrouter.com/v1"

#: Slack on each side of the run window for clock skew + record-propagation lag.
_WINDOW_BUFFER_S = 3.0
#: A run's matched prompt tokens must agree with its reported input tokens within this tolerance,
#: else we assume the window caught the wrong records (concurrency) and decline to claim a cost.
_RECONCILE_REL_TOL = 0.10
_RECONCILE_ABS_TOL = 200

#: /v1/usage pagination: records per page, and a hard cap on pages walked back in time (safety
#: bound so a very busy account can't make one cost lookup page forever).
_PAGE_SIZE = 1000
_MAX_PAGES = 20

#: (url, api_key, timeout_s) -> response body, or None on any transport failure. Injectable for tests.
HttpGetter = Callable[[str, str, float], "str | None"]

#: EastRouter 403s the default `Python-urllib/<ver>` User-Agent, so send an explicit one.
_USER_AGENT = f"marshal/{__version__} (+https://github.com/chiruu12/marshal)"


@dataclass(frozen=True)
class ExternalCost:
    """A real, attributed cost for one run, sourced from a provider usage API."""

    cost_usd: float
    source: UsageSource  # ADMIN_API
    prompt_tokens: int
    completion_tokens: int
    matched_records: int


@dataclass(frozen=True)
class _Rec:
    model: str
    amount: float
    prompt: int
    completion: int
    reasoning: int
    created: datetime | None


def _http_get(url: str, api_key: str, timeout_s: float) -> str | None:
    request = urllib.request.Request(
        url,
        headers={
            "Authorization": f"Bearer {api_key}",
            "User-Agent": _USER_AGENT,
            "Accept": "application/json",
        },
    )
    try:
        with urllib.request.urlopen(request, timeout=timeout_s) as resp:  # noqa: S310 - fixed https API host
            body: bytes = resp.read()
    except (urllib.error.URLError, OSError, ValueError):
        return None
    return body.decode("utf-8", "replace")


def _parse_dt(value: object) -> datetime | None:
    if not isinstance(value, str):
        return None
    try:
        dt = datetime.fromisoformat(value)
    except ValueError:
        return None
    # Normalize to aware UTC: EastRouter may return a naive `created_at` (no offset), and comparing a
    # naive datetime to the aware run window (`_now()` is always aware) raises TypeError - which the
    # caller swallows, silently dropping real-cost attribution. Assume UTC for naive records.
    return dt if dt.tzinfo is not None else dt.replace(tzinfo=timezone.utc)


def _parse_records(raw: str) -> list[_Rec]:
    try:
        data = json.loads(raw)
    except json.JSONDecodeError:
        return []
    rows = data.get("data") if isinstance(data, dict) else data
    if not isinstance(rows, list):
        return []
    out: list[_Rec] = []
    for r in rows:
        if not isinstance(r, dict):
            continue
        out.append(
            _Rec(
                model=str(r.get("model", "")),
                amount=float(r.get("amount_usd", 0.0) or 0.0),
                prompt=int(r.get("prompt_tokens", 0) or 0),
                completion=int(r.get("completion_tokens", 0) or 0),
                reasoning=int(r.get("reasoning_tokens", 0) or 0),
                created=_parse_dt(r.get("created_at")),
            )
        )
    return out


def _rec_key(r: _Rec) -> tuple[str, str, float, int, int]:
    """A dedup key so a record seen on two pages (or a repeated page) is counted once."""
    return (r.model, r.created.isoformat() if r.created else "", r.amount, r.prompt, r.completion)


def _collect_window_records(
    getter: HttpGetter,
    base: str,
    key: str,
    timeout_s: float,
    lo: datetime,
    *,
    page_size: int,
    max_pages: int,
) -> list[_Rec] | None:
    """Paginate ``/v1/usage`` (assumed newest-first), collecting records back to the window start.

    Stops when a page is short (last page), is entirely older than ``lo`` (paged past the window),
    repeats records (the API ignored ``offset`` - no-progress guard), is empty, or the page cap is
    hit. Returns None ONLY when the FIRST page's request fails, so the caller can retry; a later-page
    failure returns what was gathered so far.
    """
    out: list[_Rec] = []
    seen: set[tuple[str, str, float, int, int]] = set()
    for page in range(max_pages):
        url = f"{base}/usage?limit={page_size}&offset={page * page_size}"
        raw = getter(url, key, timeout_s)
        if raw is None:
            return None if page == 0 else out
        recs = _parse_records(raw)
        if not recs:
            break
        fresh = [r for r in recs if _rec_key(r) not in seen]
        if not fresh:
            break  # the API returned no new records (e.g. ignored offset) - stop, never loop forever
        for r in fresh:
            seen.add(_rec_key(r))
            out.append(r)
        if len(recs) < page_size:
            break  # a short page is the last page
        newest = max((r.created for r in recs if r.created is not None), default=None)
        if newest is not None and newest < lo:
            break  # the whole page is older than the window - no point paging further back
    return out


def _reconciles(matched_prompt: int, input_tokens: int) -> bool:
    """True if the matched records' prompt tokens agree with the run's input tokens."""
    if input_tokens <= 0:
        return False
    return abs(matched_prompt - input_tokens) <= max(_RECONCILE_ABS_TOL, _RECONCILE_REL_TOL * input_tokens)


def fetch_run_cost(
    *,
    model: str | None,
    start_iso: str,
    end_iso: str,
    input_tokens: int,
    output_tokens: int,
    api_key: str | None = None,
    base_url: str | None = None,
    timeout_s: float = 8.0,
    attempts: int = 2,
    http: HttpGetter | None = None,
    page_size: int = _PAGE_SIZE,
    max_pages: int = _MAX_PAGES,
) -> ExternalCost | None:
    """Real cost for one run from EastRouter ``/v1/usage``, or None if it can't be attributed.

    Paginates ``/v1/usage`` so a long run's records aren't missed when they fall past the first page.
    Returns None (caller keeps its estimate/unavailable cost) on any of: missing key/model, no
    matching records, a usage record not yet propagated, or a token mismatch (concurrent same-model
    runs). Never raises - cost reconciliation must never break a completed run.
    """
    key = api_key or os.environ.get("EASTROUTER_API_KEY")
    if not key or not model or input_tokens <= 0:
        return None
    # OpenCode references an EastRouter model as `eastrouter/<id>` (provider-prefixed); Codex passes
    # the bare `<id>`. `/v1/usage` always logs the bare id, so strip the provider prefix to match.
    target_model = model.removeprefix("eastrouter/")
    start = _parse_dt(start_iso)
    end = _parse_dt(end_iso)
    if start is None or end is None:
        return None
    lo = start - timedelta(seconds=_WINDOW_BUFFER_S)
    hi = end + timedelta(seconds=_WINDOW_BUFFER_S)
    base = base_url or os.environ.get("EASTROUTER_BASE_URL") or DEFAULT_BASE_URL
    getter = http or _http_get

    tries = max(1, attempts)
    for attempt in range(tries):
        records = _collect_window_records(
            getter, base, key, timeout_s, lo, page_size=page_size, max_pages=max_pages
        )
        if records is not None:
            matched = [
                r
                for r in records
                if r.model == target_model and r.created is not None and lo <= r.created <= hi
            ]
            if matched:
                matched_prompt = sum(r.prompt for r in matched)
                if _reconciles(matched_prompt, input_tokens):
                    cost = round(sum(r.amount for r in matched), 6)
                    completion = sum(r.completion + r.reasoning for r in matched)
                    return ExternalCost(
                        cost_usd=cost,
                        source=UsageSource.ADMIN_API,
                        prompt_tokens=matched_prompt,
                        completion_tokens=completion,
                        matched_records=len(matched),
                    )
        if attempt + 1 < tries:
            time.sleep(1.0)  # the last request's record may not have landed yet; brief retry
    return None


#: (model, start_iso, end_iso, input_tokens, output_tokens) -> ExternalCost | None. Keyword-called.
CostResolver = Callable[..., "ExternalCost | None"]


def default_cost_resolvers() -> dict[str, CostResolver]:
    """The built-in provider usage-API resolvers, keyed by a client's ``usage_api`` value."""
    return {"eastrouter": fetch_run_cost}
