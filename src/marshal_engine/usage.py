"""Per-provider usage tracking - append-only events log + rolled-up summary.

No database: an `events.jsonl` (one line per run) plus a derived summary, so usage is auditable and
queryable. Every event carries a `source` so unknown costs are never confused with
provider-reported ones. `summary()` returns a typed `UsageSummary` (computed on read, never stored),
optionally filtered to a `[since, until]` time window over each event's `ts`.
"""

from __future__ import annotations

import hashlib
import sys
from dataclasses import dataclass
from datetime import datetime, timedelta, timezone
from pathlib import Path
from typing import Any, Final, Literal

from pydantic import BaseModel, ValidationError, field_validator, Field

from .types import AgentResult, RunStatus, UsageRecord, UsageSource, canonical_status

# Truncated sha256 hex prefix stored on usage events. Long enough to group repeat attempts at the
# same goal; short enough for the ledger. Never store the goal text itself (secrets / proprietary).
GOAL_DIGEST_PREFIX_LEN: Final[int] = 16


def goal_digest(goal: str) -> str:
    """Return a stable sha256 hex prefix of ``goal`` for grouping — never the text itself."""
    return hashlib.sha256(goal.encode("utf-8")).hexdigest()[:GOAL_DIGEST_PREFIX_LEN]


@dataclass(frozen=True)
class LedgerCursor:
    """Byte-offset identity of a usage ledger snapshot (for O(tail) re-reads)."""

    size: int
    inode: int
    mtime_ns: int


class UnreadableUsageLedgerError(Exception):
    """Strict-mode ledger fault: skipped/torn lines or an unexpected rewrite/truncate.

    Reporting paths catch nothing here (they use ``strict=False``). Enforce-budget paths
    translate this into an actionable ``BudgetExceeded``.
    """

    def __init__(
        self,
        path: Path,
        *,
        skipped: int = 0,
        reason: str = "",
    ) -> None:
        self.path = path
        self.skipped = skipped
        self.reason = reason
        if skipped:
            msg = (
                f"usage ledger has {skipped} unreadable event(s) ({path}); "
                "enforced budgets cannot verify spend - repair or remove the torn line"
            )
        elif reason:
            msg = (
                f"usage ledger unreadable ({path}): {reason}; "
                "enforced budgets cannot verify spend - repair or remove the torn line"
            )
        else:
            msg = (
                f"usage ledger unreadable ({path}); "
                "enforced budgets cannot verify spend - repair or remove the torn line"
            )
        super().__init__(msg)

# Canonical usage time-window vocabulary shared by CLI (`marshal usage --window`) and MCP
# (`usage(window=...)`). Both surfaces must accept exactly this set — never diverge.
UsageWindow = Literal["session", "day", "week", "month", "all"]
USAGE_WINDOWS: Final[tuple[UsageWindow, ...]] = ("session", "day", "week", "month", "all")


def usage_window_since(
    window: str,
    *,
    session_start: datetime,
    now: datetime,
) -> datetime | None:
    """Map a usage window name to a UTC ``since`` (``None`` for ``"all"``).

    Shared by the CLI and MCP ``usage`` surfaces so the window vocabulary cannot drift.
    ``session`` means since ``session_start`` (the Fleet's wake for a long-lived MCP server;
    the one-shot CLI passes ``now`` so it honestly means "since this invocation").
    """
    if window == "all":
        return None
    if window == "session":
        return session_start
    if window == "day":
        return now - timedelta(hours=24)
    if window == "week":
        return now - timedelta(days=7)
    if window == "month":
        return now - timedelta(days=30)
    raise ValueError(
        f"unknown usage window: {window!r} (use {'|'.join(USAGE_WINDOWS)})"
    )


class UsageEvent(BaseModel):
    ts: str
    run_id: str
    backend: str
    client: str | None = None
    model: str | None = None
    input_tokens: int = 0
    output_tokens: int = 0
    cache_read_tokens: int = 0
    # Additive (default 0): pre-field ledger lines omit it and still parse.
    cache_write_tokens: int = 0
    cost_usd: float = 0.0
    duration_ms: int = 0
    status: str = ""
    source: str = UsageSource.UNAVAILABLE.value
    # Routing facts (optional / additive): pre-Phase-1 ledger lines omit them and still parse.
    # task_kind = caller tag for the kind of work; goal_digest = sha256 prefix of the goal text
    # (never the text). Judgment about the work is deliberately *not* here: it arrives after this
    # line is written, and is stamped on RunRecord.outcome (see Fleet.integrate) so the ledger line
    # is never rewritten and cost rollups stay one event per run.
    task_kind: str | None = None
    goal_digest: str | None = None

    @field_validator("status", mode="before")
    @classmethod
    def _migrate_status(cls, value: Any) -> Any:
        """Accept a pre-rename spelling from the ledger.

        The ledger is append-only, so every event written before `succeeded` became `exited_clean`
        still carries the old word. Without this, those runs would silently stop counting as
        successes and every historical cost-per-succeeded figure would change - the ledger's whole
        point is that recorded facts do not move under you.
        """
        return canonical_status(value) if isinstance(value, str) else value

    @classmethod
    def from_result(
        cls,
        result: AgentResult,
        *,
        run_id: str,
        backend: str,
        ts: str,
        usage: UsageRecord | None = None,
        client: str | None = None,
        model: str | None = None,
        task_kind: str | None = None,
        goal_digest: str | None = None,
    ) -> UsageEvent:
        # `usage` lets the caller pass a priced/normalized record; default to what the run carried.
        u = usage if usage is not None else result.usage
        return cls(
            ts=ts,
            run_id=run_id,
            backend=backend,
            client=client,
            model=model or (u.model if u else None),
            input_tokens=u.input_tokens if u else 0,
            output_tokens=u.output_tokens if u else 0,
            cache_read_tokens=u.cache_read_tokens if u else 0,
            cache_write_tokens=u.cache_write_tokens if u else 0,
            cost_usd=u.cost_usd if u else 0.0,
            duration_ms=result.duration_ms,  # wall-clock from base.run(), always present
            status=result.status.value,
            source=(u.source.value if u else UsageSource.UNAVAILABLE.value),
            task_kind=task_kind,
            goal_digest=goal_digest,
        )


class Bucket(BaseModel):
    """Rolled-up usage for one grouping (totals, or one backend/client/model)."""

    runs: int = 0
    succeeded: int = 0
    priced_runs: int = 0  # runs with native/admin-api cost (incl. measured $0; legacy "estimated" too)
    cost_usd: float = 0.0
    cost_native: float = 0.0        # cost we know is real (backend-reported)
    cost_admin_api: float = 0.0     # real cost from a provider admin-API (e.g. EastRouter) - also ground truth
    # Tombstone: Marshal no longer PRODUCES estimated cost. The field stays because the ledger is
    # immutable - historical lines still say "estimated", and their spend must land in a component
    # or the split silently stops summing to cost_usd.
    cost_estimated: float = 0.0
    input_tokens: int = 0
    output_tokens: int = 0
    cache_read_tokens: int = 0
    cache_write_tokens: int = 0
    cost_per_run: float = 0.0
    # None (not 0) when there are no successes - a real outcome cost can't be claimed.
    cost_per_succeeded: float | None = None


class UsageSummary(BaseModel):
    """Derived usage rollup: grand totals plus per-backend/client/model breakdowns."""

    totals: Bucket = Field(default_factory=Bucket)
    by_backend: dict[str, Bucket] = {}
    by_client: dict[str, Bucket] = {}
    by_model: dict[str, Bucket] = {}
    by_backend_model: dict[str, Bucket] = {}


class UsageTracker:
    """Append-only usage events; the rollup is derived on read.

    `record` only appends one line to `events.jsonl` - a small write under O_APPEND, which is atomic
    for concurrent writers, so parallel runs never corrupt the log or race a shared rewrite. The
    summary is computed from the log on demand (`summary()`), never maintained on the hot path.
    """

    def __init__(self, usage_dir: Path | str) -> None:
        self.dir = Path(usage_dir)
        self.events_path = self.dir / "events.jsonl"
        # Snapshot from the most recent successful read_events/summary (budget gate recheck).
        self.last_cursor: LedgerCursor = LedgerCursor(size=0, inode=0, mtime_ns=0)
        self.last_events: list[UsageEvent] = []

    def record(self, event: UsageEvent) -> None:
        self.dir.mkdir(parents=True, exist_ok=True)
        with self.events_path.open("a", encoding="utf-8") as f:
            f.write(event.model_dump_json() + "\n")

    def read_events(self, *, strict: bool = False) -> tuple[list[UsageEvent], LedgerCursor]:
        """Read the ledger once; return events plus a cursor for later O(tail) re-reads.

        ``strict=False`` (reporting): skip malformed lines, stderr-warn with the count.
        ``strict=True`` (enforce): any skipped line raises ``UnreadableUsageLedgerError``.
        File-level faults (OSError, whole-file decode failure) always propagate.

        Also stamps ``last_events`` / ``last_cursor`` so a caller (e.g. ``check_budget``) can
        window multiple budget scopes against the SAME ledger instant after one read.
        """
        if not self.events_path.exists():
            cursor = LedgerCursor(size=0, inode=0, mtime_ns=0)
            self.last_cursor = cursor
            self.last_events = []
            return [], cursor
        st = self.events_path.stat()
        data = self.events_path.read_bytes()
        cursor = LedgerCursor(size=len(data), inode=st.st_ino, mtime_ns=st.st_mtime_ns)
        text = data.decode("utf-8")  # UnicodeDecodeError propagates (file-level fault)
        events = _parse_event_lines(text, path=self.events_path, strict=strict)
        self.last_cursor = cursor
        self.last_events = events
        return events, cursor

    def events(self, *, strict: bool = False) -> list[UsageEvent]:
        events, _ = self.read_events(strict=strict)
        return events

    def events_after(self, cursor: LedgerCursor, *, strict: bool = False) -> list[UsageEvent]:
        """Parse only bytes appended since ``cursor`` (O(new events), normally O(1)).

        Identity is ``(inode, size, mtime_ns)``:
        * same inode + larger size → append-only fast path (parse the tail);
        * same inode + same size + same mtime → no-op ``[]``;
        * same inode + same size + **different mtime** → in-place rewrite, fail closed;
        * inode change / truncate / disappear → fail closed.

        Empty baseline (``cursor`` all zeros) + file appears: read from offset 0 through the
        same parse path (not ``read_events``/``summary``). That is O(file) but only when the
        ledger did not exist at the outside-lock snapshot — bounded by what one spawn window
        could have written, and keeps the under-lock call graph on ``events_after`` alone.
        """
        if not self.events_path.exists():
            if cursor.size == 0:
                return []
            raise UnreadableUsageLedgerError(
                self.events_path, reason="ledger disappeared after the baseline read"
            )
        st = self.events_path.stat()
        if cursor.size == 0 and cursor.inode == 0:
            # Empty baseline → file appeared. Read bytes from offset 0 as the "tail".
            with self.events_path.open("rb") as f:
                data = f.read()
            return _parse_event_lines(
                data.decode("utf-8"), path=self.events_path, strict=strict
            )
        if st.st_ino != cursor.inode:
            raise UnreadableUsageLedgerError(
                self.events_path, reason="ledger was rewritten"
            )
        if st.st_size < cursor.size:
            raise UnreadableUsageLedgerError(
                self.events_path, reason="ledger was truncated"
            )
        if st.st_size == cursor.size:
            if st.st_mtime_ns != cursor.mtime_ns:
                raise UnreadableUsageLedgerError(
                    self.events_path, reason="ledger was rewritten in place"
                )
            return []
        # size > cursor.size: append-only fast path (mtime advancing is expected).
        with self.events_path.open("rb") as f:
            f.seek(cursor.size)
            tail = f.read()
        text = tail.decode("utf-8")
        return _parse_event_lines(text, path=self.events_path, strict=strict)

    def summary(
        self,
        since: datetime | None = None,
        until: datetime | None = None,
        *,
        strict: bool = False,
    ) -> UsageSummary:
        """Roll up events; optionally restrict to a [since, until] window over each event's `ts`.

        Both bounds are compared in UTC against the event's ISO-8601 timestamp. Either bound may
        be None (open-ended). No args = aggregate over the whole ledger (unchanged behavior).
        ``strict`` is forwarded to the ledger reader (default lenient for reporting surfaces).
        """
        events, _ = self.read_events(strict=strict)
        return summarize_events(events, since=since, until=until)


def _parse_event_lines(
    text: str, *, path: Path, strict: bool
) -> list[UsageEvent]:
    """Parse JSONL event lines; lenient skips + warns, strict raises on any bad line."""
    out: list[UsageEvent] = []
    skipped = 0
    for line in text.splitlines():
        line = line.strip()
        if not line:
            continue
        try:
            out.append(UsageEvent.model_validate_json(line))
        except (ValidationError, ValueError):
            skipped += 1
            continue
    if skipped:
        if strict:
            raise UnreadableUsageLedgerError(path, skipped=skipped)
        print(
            f"[marshal] skipping {skipped} malformed usage event line(s)",
            file=sys.stderr,
        )
    return out


def summarize_events(
    events: list[UsageEvent],
    *,
    since: datetime | None = None,
    until: datetime | None = None,
) -> UsageSummary:
    """Roll up an already-loaded event list (shared by summary() and budget checks)."""
    by_backend: dict[str, Bucket] = {}
    by_client: dict[str, Bucket] = {}
    by_model: dict[str, Bucket] = {}
    by_backend_model: dict[str, Bucket] = {}
    totals = Bucket()
    for e in events:
        if not _in_window(e, since, until):
            continue
        _add(totals, e)
        _add(by_backend.setdefault(e.backend, Bucket()), e)
        _add(by_client.setdefault(e.client or "-", Bucket()), e)
        _add(by_model.setdefault(e.model or "-", Bucket()), e)
        bm_key = f"{e.backend}/{e.model or '-'}"
        _add(by_backend_model.setdefault(bm_key, Bucket()), e)
    for bucket in (
        totals,
        *by_backend.values(),
        *by_client.values(),
        *by_model.values(),
        *by_backend_model.values(),
    ):
        _finalize(bucket)
    return UsageSummary(
        totals=totals,
        by_backend=by_backend,
        by_client=by_client,
        by_model=by_model,
        by_backend_model=by_backend_model,
    )


def _to_utc(ts: datetime) -> datetime:
    """Coerce a datetime to a UTC-aware value for comparison (naive datetimes are treated as UTC)."""
    if ts.tzinfo is None:
        return ts.replace(tzinfo=timezone.utc)
    return ts.astimezone(timezone.utc)


def _parse_event_ts(raw: str) -> datetime | None:
    """Parse a UsageEvent.ts (ISO-8601); return None when it's unparseable.

    Unparseable events are dropped from the windowed rollup: a malformed timestamp can't be
    compared, so it's safer to exclude than to misclassify. (All-time summaries still include them
    because they don't consult `_in_window`.)
    """
    try:
        return _to_utc(datetime.fromisoformat(raw))
    except ValueError:
        return None


def _in_window(e: UsageEvent, since: datetime | None, until: datetime | None) -> bool:
    """True when `e` falls in [since, until] (either bound may be None for open-ended)."""
    if since is None and until is None:
        return True
    ts = _parse_event_ts(e.ts)
    if ts is None:
        return False
    if since is not None and ts < _to_utc(since):
        return False
    if until is not None and ts > _to_utc(until):
        return False
    return True


def _add(bucket: Bucket, e: UsageEvent) -> None:
    bucket.runs += 1
    if e.status == RunStatus.EXITED_CLEAN.value:
        bucket.succeeded += 1
    bucket.cost_usd = round(bucket.cost_usd + e.cost_usd, 6)
    if e.source in (
        UsageSource.NATIVE.value,
        UsageSource.ADMIN_API.value,
        "estimated",  # legacy ledger only — still counts as priced when replayed
        "scraped",
    ):
        bucket.priced_runs += 1
    if e.source == UsageSource.NATIVE.value:
        bucket.cost_native = round(bucket.cost_native + e.cost_usd, 6)
    elif e.source == UsageSource.ADMIN_API.value:
        bucket.cost_admin_api = round(bucket.cost_admin_api + e.cost_usd, 6)
    elif e.source in ("estimated", "scraped"):
        bucket.cost_estimated = round(bucket.cost_estimated + e.cost_usd, 6)
    bucket.input_tokens += e.input_tokens
    bucket.output_tokens += e.output_tokens
    bucket.cache_read_tokens += e.cache_read_tokens
    bucket.cache_write_tokens += e.cache_write_tokens


def _finalize(bucket: Bucket) -> None:
    """Add derived cost-per-outcome (report layer, computed on read - never stored on the ledger)."""
    runs = bucket.runs
    succeeded = bucket.succeeded
    bucket.cost_per_run = round(bucket.cost_usd / runs, 6) if runs else 0.0
    bucket.cost_per_succeeded = round(bucket.cost_usd / succeeded, 6) if succeeded else None
