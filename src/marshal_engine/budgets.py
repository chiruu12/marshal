"""Budget tracking: soft-warn by default; optional hard refuse when ``enforce: true``.

Budgets are scoped by client, backend, or globally, over session/week/month windows.
Spend is read from the usage ledger; lookup failures degrade silently for advisory
budgets so a soft-warn never breaks a run or the usage display. Enforced budgets raise
``BudgetExceeded`` instead of spawning when the cap is already met.

``enforce: true`` also serializes matching in-flight spawns (see ``EnforceBudgetGate``):
without a per-run cost reservation, parallel admits against the same ledger snapshot can
overshoot the cap by up to concurrency × per-run cost. The gate admits at most one
in-flight matching spawn per enforce budget until that run finishes and records spend.
"""

from __future__ import annotations

import sys
import threading
from datetime import datetime, timedelta, timezone
from typing import Protocol

from pydantic import BaseModel

from .config import BudgetSpec
from .usage import UsageSummary, UsageTracker


class BudgetExceeded(RuntimeError):
    """Raised when an ``enforce: true`` budget's windowed spend already meets its cap,
    or when another in-flight run already holds that enforce budget (concurrency guard)."""


class BudgetRunScope(Protocol):
    """Minimal run shape for budget scope matching (avoids importing Fleet/RunRequest)."""

    client: str | None
    backend_name: str


def _budget_window_since(window: str, session_start: datetime, now: datetime) -> datetime:
    """Map a budget's window name to the [since, now) start (UTC).

    `session` maps to the Fleet's `session_start` (the long-lived MCP server's wake instant), so
    a driver can ask "what have I spent since the server started?" without restating a timestamp.
    """
    if window == "session":
        return session_start
    if window == "week":
        return now - timedelta(days=7)
    if window == "month":
        return now - timedelta(days=30)
    raise ValueError(f"unknown budget window: {window!r} (use session|week|month)")


def _budget_spend_from_summary(summary: UsageSummary, budget: BudgetSpec) -> float:
    """Cost recorded under a budget's scope within an already-computed summary (no ledger scan).

    A budget is scoped to its own `client`/`backend` (or the whole fleet when neither is set) - the
    spend is what has been recorded under THAT scope, not the scope of any one run. A client/backend
    with no recorded events (or a subscription backend reporting $0) reads 0.0; we never fabricate a
    percentage or "remaining" from a missing cost.
    """
    if budget.client is not None:
        bucket = summary.by_client.get(budget.client)
        return bucket.cost_usd if bucket is not None else 0.0
    if budget.backend is not None:
        bucket = summary.by_backend.get(budget.backend)
        return bucket.cost_usd if bucket is not None else 0.0
    return summary.totals.cost_usd


def _budget_spend_cached(
    cache: dict[str, UsageSummary],
    tracker: UsageTracker,
    session_start: datetime,
    budget: BudgetSpec,
    now: datetime,
) -> float:
    """Windowed spend for a budget's scope, scanning the ledger once per DISTINCT window via `cache`.

    Budgets sharing a window (session/week/month - only three possible) reuse one `summary(since=)`
    scan instead of one per budget, so a run-start check with N budgets does at most 3 ledger reads.
    """
    if budget.window not in cache:
        cache[budget.window] = tracker.summary(
            since=_budget_window_since(budget.window, session_start, now)
        )
    return _budget_spend_from_summary(cache[budget.window], budget)


def _budget_scope_label(budget: BudgetSpec) -> str:
    """Human-readable scope label for a budget (what the warning / display names)."""
    if budget.client is not None:
        return f"client:{budget.client}"
    if budget.backend is not None:
        return f"backend:{budget.backend}"
    return "global"


def _budget_matches(budget: BudgetSpec, req: BudgetRunScope) -> bool:
    if budget.client is not None:
        return req.client == budget.client
    if budget.backend is not None:
        return req.backend_name == budget.backend
    return True


def compute_budget_status(
    tracker: UsageTracker,
    session_start: datetime,
    budgets: list[BudgetSpec],
    now: datetime,
) -> list[BudgetStatus]:
    """Build a `BudgetStatus` per configured budget from the ledger at `now`.

    Lookup failures for an individual budget degrade to spent=0 (same honesty as a scope with
    no events) so the display never crashes the usage surface.
    """
    cache: dict[str, UsageSummary] = {}
    out: list[BudgetStatus] = []
    for b in budgets:
        try:
            spent = _budget_spend_cached(cache, tracker, session_start, b, now)
        except Exception:  # noqa: BLE001 - display never fails a usage query
            spent = 0.0
        out.append(
            BudgetStatus(
                scope=_budget_scope_label(b),
                window=b.window,
                spent_usd=spent,
                limit_usd=b.limit_usd,
                remaining_usd=max(0.0, b.limit_usd - spent),
                enforce=b.enforce,
            )
        )
    return out


class BudgetStatus(BaseModel):
    """One configured budget's current standing - for `usage` displays + the MCP surface."""

    scope: str           # "client:<name>" | "backend:<name>" | "global"
    window: str          # session | week | month
    spent_usd: float     # windowed cost under this scope (0.0 for a scope with no spend)
    limit_usd: float
    remaining_usd: float # max(0, limit - spent) - the same floor a $0 spend gives a $0 remaining
    enforce: bool = False


def check_budget(
    tracker: UsageTracker,
    session_start: datetime,
    budgets: list[BudgetSpec],
    req: BudgetRunScope,
) -> None:
    """Warn (advisory) or raise ``BudgetExceeded`` (enforce) for matching over-cap budgets.

    For every budget whose scope matches `req` (client match, backend match, or global), the
    windowed spend is recomputed from the usage ledger; if it meets or exceeds the cap:

    * ``enforce=false`` (default): soft-warn on stderr; never raise from this path's own
      lookup failures (a soft budget never breaks a run).
    * ``enforce=true``: raise ``BudgetExceeded`` so the spawn is refused before a worktree is
      created. Lookup failures for an enforced budget also raise (fail closed).

    A subscription / unknown-cost backend reports $0, so a $ budget on it never triggers (and
    shows $0 spent); we don't fabricate a percentage or "remaining" from that.
    """
    if not budgets:
        return
    now = datetime.now(timezone.utc)
    cache: dict[str, UsageSummary] = {}
    for b in budgets:
        if not _budget_matches(b, req):
            continue
        try:
            spent = _budget_spend_cached(cache, tracker, session_start, b, now)
        except Exception as exc:  # noqa: BLE001
            if b.enforce:
                raise BudgetExceeded(
                    f"budget {_budget_scope_label(b)} ({b.window}): spend lookup failed; "
                    f"refusing spawn because enforce=true ({exc})"
                ) from exc
            continue
        if spent < b.limit_usd:
            continue
        msg = (
            f"[marshal] budget: {_budget_scope_label(b)} spent "
            f"${spent:.4f} >= cap ${b.limit_usd:.4f} ({b.window})"
        )
        if b.enforce:
            raise BudgetExceeded(
                f"{msg}; refusing new spawn (enforce=true). "
                "Raise limit_usd, wait for the window to roll, or set enforce: false for soft-warn."
            )
        print(msg, file=sys.stderr)


def _enforce_budget_key(budget: BudgetSpec) -> str:
    """Stable key for an enforce-budget concurrency slot (scope + window + limit)."""
    return f"{_budget_scope_label(budget)}|{budget.window}|{budget.limit_usd}"


class EnforceBudgetGate:
    """Admit at most one in-flight spawn per matching ``enforce: true`` budget.

    Ledger checks alone are TOCTOU under ``run_many`` / concurrent ``spawn``: every thread can
    read the same pre-run spend and pass before any usage is recorded. Holding a per-budget
    slot until the run finishes closes that race without inventing a per-run cost estimate.
    Advisory budgets are unaffected.
    """

    def __init__(self) -> None:
        self._lock = threading.Lock()
        # key -> run_id once bound; empty string while reserved between begin() and bind()
        self._held: dict[str, str] = {}

    def begin(
        self,
        tracker: UsageTracker,
        session_start: datetime,
        budgets: list[BudgetSpec],
        req: BudgetRunScope,
    ) -> list[str]:
        """Check ledger caps, then reserve concurrency slots for matching enforce budgets."""
        with self._lock:
            check_budget(tracker, session_start, budgets, req)
            keys: list[str] = []
            for b in budgets:
                if not b.enforce or not _budget_matches(b, req):
                    continue
                key = _enforce_budget_key(b)
                holder = self._held.get(key)
                if holder is not None:
                    held_by = holder or "starting"
                    raise BudgetExceeded(
                        f"budget {_budget_scope_label(b)} ({b.window}): another in-flight run "
                        f"holds this enforce cap (run {held_by}); refusing concurrent spawn to "
                        "prevent overshoot. Wait for it to finish, or set enforce: false."
                    )
                self._held[key] = ""
                keys.append(key)
            return keys

    def bind(self, keys: list[str], run_id: str) -> None:
        """Attach reserved slots to the concrete run_id after worktree creation."""
        if not keys:
            return
        with self._lock:
            for key in keys:
                if key in self._held:
                    self._held[key] = run_id

    def release(self, keys: list[str]) -> None:
        """Drop slots reserved by ``begin`` when ``_start`` fails before bind."""
        if not keys:
            return
        with self._lock:
            for key in keys:
                self._held.pop(key, None)

    def release_run(self, run_id: str) -> None:
        """Release every slot held by ``run_id`` (terminal path / spawn submit failure)."""
        with self._lock:
            for key, held in list(self._held.items()):
                if held == run_id:
                    del self._held[key]
