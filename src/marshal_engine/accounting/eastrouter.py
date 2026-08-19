"""Read REAL per-request cost from EastRouter's ``/v1/usage`` and reconcile it to a Marshal run.

EastRouter is an OpenAI-compatible router. Codex routed through it reports tokens but no cost
(``unavailable``). This module fetches the ACTUAL ``amount_usd`` EastRouter charged, so cost is
reported as ``admin-api`` (real).

Attribution. A Marshal run carries no EastRouter ``request_id`` (Codex doesn't surface it), so usage
records are matched by ``(model, created_at within the run's [start, end] window)``. That is exact
when at most one run uses a given EastRouter model at a time - the default fleet pairs each model
with a single client. If two clients drive the SAME EastRouter model concurrently, the window cannot
separate them; the token-reconciliation guard below detects the mismatch (matched prompt tokens won't
equal the run's input tokens) and the run KEEPS unavailable cost rather than asserting
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
from datetime import UTC, datetime, timedelta

from ..core._version import __version__
from ..core.types import UsageSource

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
    return dt if dt.tzinfo is not None else dt.replace(tzinfo=UTC)


def _parse_amount_usd(value: object) -> float | None:
    """A usable numeric ``amount_usd``, or None when absent/null/unparseable.

    Explicit ``0`` / ``0.0`` is a real reported charge (keep it). Missing or null must NOT
    coerce to ``0.0`` — that would fabricate an ``admin-api`` $0 when the provider reported
    no charge at all.
    """
    if value is None or isinstance(value, bool):
        return None
    if isinstance(value, (int, float)):
        return float(value)
    if isinstance(value, str):
        try:
            return float(value)
        except ValueError:
            return None
    return None


def _parse_records(raw: str) -> tuple[list[_Rec], int]:
    """Usable records plus the RAW row count the page carried.

    Pagination must terminate on the raw count, not on the usable ones: a full page whose rows
    were all skipped (no usable ``amount_usd``) would otherwise read as empty/short and stop the
    walk before reaching later pages that do hold charges.
    """
    try:
        data = json.loads(raw)
    except json.JSONDecodeError:
        return [], 0
    rows = data.get("data") if isinstance(data, dict) else data
    if not isinstance(rows, list):
        return [], 0
    out: list[_Rec] = []
    for r in rows:
        if not isinstance(r, dict):
            continue
        # Row without a usable amount_usd is unusable for cost attribution — skip it so the
        # run stays unavailable rather than claiming a fake admin-api $0.
        if "amount_usd" not in r:
            continue
        amount = _parse_amount_usd(r.get("amount_usd"))
        if amount is None:
            continue
        out.append(
            _Rec(
                model=str(r.get("model", "")),
                amount=amount,
                prompt=int(r.get("prompt_tokens", 0) or 0),
                completion=int(r.get("completion_tokens", 0) or 0),
                reasoning=int(r.get("reasoning_tokens", 0) or 0),
                created=_parse_dt(r.get("created_at")),
            )
        )
    return out, len(rows)


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
) -> _Window | None:
    """Paginate ``/v1/usage`` (assumed newest-first), collecting records back to the window start.

    Stops when a page is short (last page), is entirely older than ``lo`` (paged past the window),
    repeats records (the API ignored ``offset`` - no-progress guard), is empty, or the page cap is
    hit. Returns None ONLY when the FIRST page's request fails, so the caller can retry; a later-page
    failure returns what was gathered so far.
    """
    out: list[_Rec] = []
    seen: set[tuple[str, str, float, int, int]] = set()
    truncated = True  # cleared by whichever break proves we reached the end of the window
    for page in range(max_pages):
        url = f"{base}/usage?limit={page_size}&offset={page * page_size}"
        raw = getter(url, key, timeout_s)
        if raw is None:
            # A later page failing leaves a set we KNOW is incomplete; the caller must not read it
            # as the whole window.
            return None if page == 0 else _Window(out, truncated=True)
        recs, raw_rows = _parse_records(raw)
        if raw_rows == 0:
            truncated = False
            break
        fresh = [r for r in recs if _rec_key(r) not in seen]
        if not fresh and recs:
            # The API returned no new records - it ignored `offset` and is serving the same page.
            # Stop rather than loop forever. NOT counted as truncation: this is everything the API
            # will ever hand back, so paging further is not a remainder we gave up on, and the
            # reconciliation check still has to agree the run's own records add up before any cost
            # is claimed. Truncation is reserved for a walk cut short by OUR page cap or a transport
            # error - cases where more data demonstrably exists and we chose not to, or could not,
            # read it.
            truncated = False
            break
        for r in fresh:
            seen.add(_rec_key(r))
            out.append(r)
        if raw_rows < page_size:
            truncated = False
            break  # a short page is the last page
        newest = max((r.created for r in recs if r.created is not None), default=None)
        if newest is not None and newest < lo:
            truncated = False
            break  # the whole page is older than the window - no point paging further back
    return _Window(out, truncated=truncated)


@dataclass(frozen=True)
class _Window:
    """The records gathered for a time window, and whether they are all of them.

    `truncated` is the honest half. Cost attributed from a partial window is short by whatever the
    unseen records charged, and reconciliation does not catch it: that check tolerates 10% of the
    prompt tokens going missing, so a dropped record under that threshold reconciles fine while its
    charge is simply absent. A number that short would still be tagged `admin-api` - measured.
    """

    records: list[_Rec]
    truncated: bool


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
        window = _collect_window_records(
            getter, base, key, timeout_s, lo, page_size=page_size, max_pages=max_pages
        )
        # A truncated window cannot support a cost claim: the run's own records may be complete, but
        # we cannot know that, and "unavailable" (the caller keeps it) is the honest answer where
        # "measured but short" is not.
        if window is not None and not window.truncated:
            records = window.records
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


#: (model, start_iso, end_iso, input_tokens) -> ExternalCost | None. Keyword-called.
CostResolver = Callable[..., "ExternalCost | None"]


def default_cost_resolvers() -> dict[str, CostResolver]:
    """The built-in provider usage-API resolvers, keyed by a client's ``usage_api`` value."""
    return {"eastrouter": fetch_run_cost}
