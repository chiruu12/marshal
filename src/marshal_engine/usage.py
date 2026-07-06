"""Per-provider usage tracking - append-only events log + rolled-up summary.

No database: an `events.jsonl` (one line per run) plus a derived summary, so usage is auditable and
queryable. Every event carries a `source` so estimated/scraped costs are never confused with
provider-reported ones. `summary()` returns a typed `UsageSummary` (computed on read, never stored).
"""

from __future__ import annotations

from pathlib import Path

from pydantic import BaseModel, Field

from .types import AgentResult, RunStatus, UsageRecord, UsageSource


class UsageEvent(BaseModel):
    ts: str
    run_id: str
    backend: str
    client: str | None = None
    model: str | None = None
    input_tokens: int = 0
    output_tokens: int = 0
    cache_read_tokens: int = 0
    cost_usd: float = 0.0
    duration_ms: int = 0
    status: str = ""
    source: str = UsageSource.UNAVAILABLE.value

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
            cost_usd=u.cost_usd if u else 0.0,
            duration_ms=result.duration_ms,  # wall-clock from base.run(), always present
            status=result.status.value,
            source=(u.source.value if u else UsageSource.UNAVAILABLE.value),
        )


class Bucket(BaseModel):
    """Rolled-up usage for one grouping (totals, or one backend/client/model)."""

    runs: int = 0
    succeeded: int = 0
    cost_usd: float = 0.0
    cost_native: float = 0.0        # cost we know is real (backend-reported)
    cost_admin_api: float = 0.0     # real cost from a provider admin-API (e.g. EastRouter) - also ground truth
    cost_estimated: float = 0.0     # cost derived from a price table - not ground truth
    input_tokens: int = 0
    output_tokens: int = 0
    cache_read_tokens: int = 0
    cost_per_run: float = 0.0
    # None (not 0) when there are no successes - a real outcome cost can't be claimed.
    cost_per_succeeded: float | None = None


class UsageSummary(BaseModel):
    """Derived usage rollup: grand totals plus per-backend/client/model breakdowns."""

    totals: Bucket = Field(default_factory=Bucket)
    by_backend: dict[str, Bucket] = {}
    by_client: dict[str, Bucket] = {}
    by_model: dict[str, Bucket] = {}


class UsageTracker:
    """Append-only usage events; the rollup is derived on read.

    `record` only appends one line to `events.jsonl` - a small write under O_APPEND, which is atomic
    for concurrent writers, so parallel runs never corrupt the log or race a shared rewrite. The
    summary is computed from the log on demand (`summary()`), never maintained on the hot path.
    """

    def __init__(self, usage_dir: Path | str) -> None:
        self.dir = Path(usage_dir)
        self.events_path = self.dir / "events.jsonl"

    def record(self, event: UsageEvent) -> None:
        self.dir.mkdir(parents=True, exist_ok=True)
        with self.events_path.open("a", encoding="utf-8") as f:
            f.write(event.model_dump_json() + "\n")

    def events(self) -> list[UsageEvent]:
        if not self.events_path.exists():
            return []
        out: list[UsageEvent] = []
        for line in self.events_path.read_text(encoding="utf-8").splitlines():
            line = line.strip()
            if not line:
                continue
            out.append(UsageEvent.model_validate_json(line))
        return out

    def summary(self) -> UsageSummary:
        by_backend: dict[str, Bucket] = {}
        by_client: dict[str, Bucket] = {}
        by_model: dict[str, Bucket] = {}
        totals = Bucket()
        for e in self.events():
            _add(totals, e)
            _add(by_backend.setdefault(e.backend, Bucket()), e)
            _add(by_client.setdefault(e.client or "-", Bucket()), e)
            _add(by_model.setdefault(e.model or "-", Bucket()), e)
        for bucket in (totals, *by_backend.values(), *by_client.values(), *by_model.values()):
            _finalize(bucket)
        return UsageSummary(
            totals=totals, by_backend=by_backend, by_client=by_client, by_model=by_model
        )


def _add(bucket: Bucket, e: UsageEvent) -> None:
    bucket.runs += 1
    if e.status == RunStatus.SUCCEEDED.value:
        bucket.succeeded += 1
    bucket.cost_usd = round(bucket.cost_usd + e.cost_usd, 6)
    if e.source == UsageSource.NATIVE.value:
        bucket.cost_native = round(bucket.cost_native + e.cost_usd, 6)
    elif e.source == UsageSource.ADMIN_API.value:
        bucket.cost_admin_api = round(bucket.cost_admin_api + e.cost_usd, 6)
    elif e.source == UsageSource.ESTIMATED.value:
        bucket.cost_estimated = round(bucket.cost_estimated + e.cost_usd, 6)
    bucket.input_tokens += e.input_tokens
    bucket.output_tokens += e.output_tokens
    bucket.cache_read_tokens += e.cache_read_tokens


def _finalize(bucket: Bucket) -> None:
    """Add derived cost-per-outcome (report layer, computed on read - never stored on the ledger)."""
    runs = bucket.runs
    succeeded = bucket.succeeded
    bucket.cost_per_run = round(bucket.cost_usd / runs, 6) if runs else 0.0
    bucket.cost_per_succeeded = round(bucket.cost_usd / succeeded, 6) if succeeded else None
