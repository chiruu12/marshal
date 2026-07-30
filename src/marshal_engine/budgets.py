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
from dataclasses import dataclass, field
from datetime import datetime, timedelta, timezone
from typing import Protocol

from pydantic import BaseModel, Field

from .config import BudgetSpec
from .usage import (
    Bucket,
    LedgerCursor,
    UnreadableUsageLedgerError,
    UsageEvent,
    UsageSummary,
    UsageTracker,
    _in_window,
)


class BudgetExceeded(RuntimeError):
    """Raised when an ``enforce: true`` budget's windowed spend already meets its cap,
    or when another in-flight run already holds that enforce budget (concurrency guard)."""


class BudgetRunScope(Protocol):
    """Minimal run shape for budget scope matching (avoids importing Fleet/RunRequest)."""

    client: str | None
    backend_name: str


@dataclass(frozen=True)
class BudgetCheckSnapshot:
    """Outside-lock spend baseline + ledger cursor for O(tail) under-lock revalidation."""

    cursor: LedgerCursor
    enforce_spent: dict[str, float] = field(default_factory=dict)


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


def _budget_bucket_from_summary(summary: UsageSummary, budget: BudgetSpec) -> Bucket | None:
    """The rollup bucket for a budget's scope, or None when the scope has no events."""
    if budget.client is not None:
        return summary.by_client.get(budget.client)
    if budget.backend is not None:
        return summary.by_backend.get(budget.backend)
    return summary.totals


def _budget_spent_known(bucket: Bucket | None) -> bool:
    """False when the scope has runs but none carried a priced cost source."""
    if bucket is None or bucket.runs == 0:
        return True
    return bucket.priced_runs > 0 or bucket.cost_usd > 0


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


def _event_matches_budget_scope(event: UsageEvent, budget: BudgetSpec) -> bool:
    if budget.client is not None:
        return event.client == budget.client
    if budget.backend is not None:
        return event.backend == budget.backend
    return True


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
    no events) so the display never crashes the usage surface. Always lenient (reporting path).
    """
    cache: dict[str, UsageSummary] = {}
    out: list[BudgetStatus] = []
    for b in budgets:
        try:
            summary = cache.get(b.window)
            if summary is None:
                summary = tracker.summary(
                    since=_budget_window_since(b.window, session_start, now),
                    strict=False,
                )
                cache[b.window] = summary
            spent = _budget_spend_from_summary(summary, b)
            bucket = _budget_bucket_from_summary(summary, b)
            spent_known = _budget_spent_known(bucket)
        except Exception:  # noqa: BLE001 - display never fails a usage query
            spent = 0.0
            spent_known = True
        out.append(
            BudgetStatus(
                scope=_budget_scope_label(b),
                window=b.window,
                spent_usd=spent,
                limit_usd=b.limit_usd,
                remaining_usd=max(0.0, b.limit_usd - spent),
                enforce=b.enforce,
                spent_known=spent_known,
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
    spent_known: bool = Field(
        default=True,
        exclude=True,
        description="CLI-only: False when scope has runs but no priced cost source",
    )


def check_budget(
    tracker: UsageTracker,
    session_start: datetime,
    budgets: list[BudgetSpec],
    req: BudgetRunScope,
    *,
    enforce_only: bool = False,
) -> BudgetCheckSnapshot:
    """Warn (advisory) or raise ``BudgetExceeded`` (enforce) for matching over-cap budgets.

    For every budget whose scope matches `req` (client match, backend match, or global), the
    windowed spend is recomputed from the usage ledger; if it meets or exceeds the cap:

    * ``enforce=false`` (default): soft-warn on stderr; never raise from this path's own
      lookup failures (a soft budget never breaks a run). Lenient ledger reads (torn lines
      skipped + warned).
    * ``enforce=true``: raise ``BudgetExceeded`` so the spawn is refused before a worktree is
      created. Lookup failures and any skipped/torn ledger lines also raise (fail closed) with
      an actionable repair message.

    ``enforce_only=True`` skips advisory budgets (no soft-warn). Returns a
    ``BudgetCheckSnapshot`` (ledger cursor + per-enforce-budget spend) so
    ``EnforceBudgetGate.begin`` can revalidate from the appended tail under its lock without a
    full O(ledger) rescan.

    A subscription / unknown-cost backend reports $0, so a $ budget on it never triggers (and
    shows $0 spent); we don't fabricate a percentage or "remaining" from that.
    """
    empty = BudgetCheckSnapshot(cursor=LedgerCursor(size=0, inode=0, mtime_ns=0))
    if not budgets:
        return empty
    now = datetime.now(timezone.utc)
    matching = [
        b
        for b in budgets
        if _budget_matches(b, req) and (not enforce_only or b.enforce)
    ]
    if not matching:
        return empty

    enforce_budgets = [b for b in matching if b.enforce]
    advisory_budgets = [b for b in matching if not b.enforce]

    cursor = LedgerCursor(size=0, inode=0, mtime_ns=0)
    enforce_spent: dict[str, float] = {}
    # Windowed summaries keyed by window name; at most one ledger read per distinct window.
    enforce_cache: dict[str, UsageSummary] = {}
    advisory_cache: dict[str, UsageSummary] = {}

    if enforce_budgets:
        try:
            # Go through summary() so a monkeypatched summary still fails closed (fleet tests /
            # diagnostics), and so last_cursor is stamped for the gate's O(tail) recheck.
            for b in enforce_budgets:
                if b.window not in enforce_cache:
                    enforce_cache[b.window] = tracker.summary(
                        since=_budget_window_since(b.window, session_start, now),
                        strict=True,
                    )
                spent = _budget_spend_from_summary(enforce_cache[b.window], b)
                enforce_spent[_enforce_budget_key(b)] = spent
                if spent < b.limit_usd:
                    continue
                raise BudgetExceeded(
                    f"[marshal] budget: {_budget_scope_label(b)} spent "
                    f"${spent:.4f} >= cap ${b.limit_usd:.4f} ({b.window}); "
                    "refusing new spawn (enforce=true). "
                    "Raise limit_usd, wait for the window to roll, or set enforce: false for soft-warn."
                )
            cursor = tracker.last_cursor
        except BudgetExceeded:
            raise
        except UnreadableUsageLedgerError as exc:
            raise BudgetExceeded(str(exc)) from exc
        except Exception as exc:  # noqa: BLE001 - OSError / decode / unexpected
            raise BudgetExceeded(
                f"budget {_budget_scope_label(enforce_budgets[0])} "
                f"({enforce_budgets[0].window}): spend lookup failed; "
                f"refusing spawn because enforce=true ({exc})"
            ) from exc

    if advisory_budgets and not enforce_only:
        for b in advisory_budgets:
            try:
                # Reuse a strict window summary when present (no skipped lines); else lenient.
                if b.window in enforce_cache:
                    spent = _budget_spend_from_summary(enforce_cache[b.window], b)
                else:
                    if b.window not in advisory_cache:
                        advisory_cache[b.window] = tracker.summary(
                            since=_budget_window_since(b.window, session_start, now),
                            strict=False,
                        )
                    spent = _budget_spend_from_summary(advisory_cache[b.window], b)
                    cursor = tracker.last_cursor
            except Exception:  # noqa: BLE001 - soft budget never breaks a run
                continue
            if spent < b.limit_usd:
                continue
            print(
                f"[marshal] budget: {_budget_scope_label(b)} spent "
                f"${spent:.4f} >= cap ${b.limit_usd:.4f} ({b.window})",
                file=sys.stderr,
            )

    return BudgetCheckSnapshot(cursor=cursor, enforce_spent=enforce_spent)


def _enforce_budget_key(budget: BudgetSpec) -> str:
    """Stable key for an enforce-budget concurrency slot (scope + window + limit)."""
    return f"{_budget_scope_label(budget)}|{budget.window}|{budget.limit_usd}"


def _recheck_enforce_from_tail(
    tracker: UsageTracker,
    snap: BudgetCheckSnapshot,
    session_start: datetime,
    budgets: list[BudgetSpec],
    req: BudgetRunScope,
) -> None:
    """Under-lock revalidation: apply only appended ledger bytes to the outside spend baseline.

    Lock-body work is O(new events), normally O(1). A rewritten/truncated/torn tail fails closed.
    """
    if not snap.enforce_spent:
        return
    try:
        new_events = tracker.events_after(snap.cursor, strict=True)
    except UnreadableUsageLedgerError as exc:
        raise BudgetExceeded(str(exc)) from exc
    except Exception as exc:  # noqa: BLE001
        raise BudgetExceeded(
            f"usage ledger unreadable ({tracker.events_path}): {exc}; "
            "enforced budgets cannot verify spend - repair or remove the torn line"
        ) from exc

    now = datetime.now(timezone.utc)
    spent = dict(snap.enforce_spent)
    matching = [b for b in budgets if b.enforce and _budget_matches(b, req)]
    for e in new_events:
        for b in matching:
            since = _budget_window_since(b.window, session_start, now)
            if not _in_window(e, since, None):
                continue
            if not _event_matches_budget_scope(e, b):
                continue
            key = _enforce_budget_key(b)
            spent[key] = spent.get(key, 0.0) + e.cost_usd

    for b in matching:
        key = _enforce_budget_key(b)
        cur = spent.get(key, 0.0)
        if cur < b.limit_usd:
            continue
        raise BudgetExceeded(
            f"[marshal] budget: {_budget_scope_label(b)} spent "
            f"${cur:.4f} >= cap ${b.limit_usd:.4f} ({b.window}); "
            "refusing new spawn (enforce=true). "
            "Raise limit_usd, wait for the window to roll, or set enforce: false for soft-warn."
        )


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
        """Check ledger caps, then reserve concurrency slots for matching enforce budgets.

        Full ledger spend is computed *before* acquiring ``_lock`` so fleet-wide spawns do not
        serialize behind O(ledger) scans. Under the lock we revalidate from only the appended
        tail (O(new events), normally O(1)), then reserve slots. If reservation fails partway
        through a multi-budget match, every key reserved so far is released before re-raising.
        """
        # Optimistic check outside the lock (advisory soft-warn + enforce refuse + cursor).
        snap = check_budget(tracker, session_start, budgets, req)
        with self._lock:
            # Tail-only enforce re-check — never a full summary()/events() rescan under the lock.
            _recheck_enforce_from_tail(tracker, snap, session_start, budgets, req)
            keys: list[str] = []
            try:
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
            except Exception:
                for key in keys:
                    self._held.pop(key, None)
                raise

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
