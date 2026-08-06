"""The `marshal` CLI - inspect backends, usage, and fleet state."""

from __future__ import annotations

from typing import Any, Sequence

from ...orchestration.fleet import BudgetStatus
from ...core.types import UsageSource
from ...accounting.ledger import RoutingCell, RoutingLedger
from ...accounting.usage import Bucket

def _align_rows(header: Sequence[str], rows: Sequence[Sequence[Any]]) -> list[str]:
    """Render a header + rows of strings as column-aligned lines (no border).

    Each column is sized to the widest cell; values are stringified and left-aligned. Keeps the
    fixed-width f-string convention used in `_cmd_status` / `_cmd_backends` so the existing
    stdlib-only, no-deps posture holds.
    """
    if not header:
        return []
    widths = [len(str(h)) for h in header]
    for row in rows:
        for i, cell in enumerate(row):
            if i < len(widths):
                widths[i] = max(widths[i], len(str(cell)))
    out = ["  ".join(str(h).ljust(widths[i]) for i, h in enumerate(header))]
    for row in rows:
        # Clamp: a row with more cells than the header renders the extras unpadded rather than
        # raising IndexError (this is a general-purpose helper, so guard against future misuse).
        out.append(
            "  ".join(
                str(c).ljust(widths[i]) if i < len(widths) else str(c)
                for i, c in enumerate(row)
            )
        )
    return out


def _format_cost_display(cost_usd: float | None, source: str | None) -> str:
    """Human cost string from amount + provenance; unknown spend is never rendered as $0."""
    if cost_usd is None or source is None or source == UsageSource.UNAVAILABLE.value:
        return "unavailable"
    return f"${cost_usd:.4f}"


def _bucket_cost_is_known(b: Bucket) -> bool:
    if b.runs == 0:
        return True
    return b.priced_runs > 0 or b.cost_usd > 0


def _format_bucket_cost(b: Bucket) -> str:
    if not _bucket_cost_is_known(b):
        return "unavailable"
    return f"${b.cost_usd:.4f}"


def _format_bucket_rate(cost: float, b: Bucket, *, no_successes: bool = False) -> str:
    """Per-run rate, annotated when only some of the bucket's runs have a known cost.

    A rate divides known spend by ALL runs in the bucket, so a bucket mixing priced and
    unpriced runs understates it. Say how many runs the number actually covers rather than
    presenting a partial rate as the whole - the same reason an unpriced run is not $0.
    """
    if no_successes or b.runs == 0:
        return "n/a"
    if not _bucket_cost_is_known(b):
        return "unavailable"
    if b.priced_runs < b.runs:
        return f"${cost:.4f} ({b.priced_runs}/{b.runs} priced)"
    return f"${cost:.4f}"


def _format_budget_amount(amount: float, *, known: bool) -> str:
    if not known and amount == 0.0:
        return "unavailable"
    return f"${amount:.4f}"


def _format_cost_split(b: Bucket) -> str:
    """Compact provenance split; zeros collapsed so the row stays readable.

    ``estimated`` appears only for legacy ledger lines - Marshal no longer produces estimates, but
    the split must still account for every dollar in ``cost_usd`` or it silently disagrees with the
    total beside it (and with the JSON).
    """
    parts: list[str] = []
    for label, val in (
        ("native", b.cost_native),
        ("admin-api", b.cost_admin_api),
        ("estimated (legacy)", b.cost_estimated),
    ):
        if val > 0:
            parts.append(f"{label} ${val:.4f}")
    return " ".join(parts) if parts else "-"


def _print_bucket_table(title: str, buckets: dict[str, Bucket]) -> None:
    if not buckets:
        return
    print(f"\n{title}")
    header = (
        "name", "runs", "succeeded", "cost_usd", "cost split",
        "input_tokens", "output_tokens", "cache_read_tokens", "cache_write_tokens",
    )
    rows = [
        (
            name,
            str(v.runs),
            str(v.succeeded),
            _format_bucket_cost(v),
            _format_cost_split(v),
            str(v.input_tokens),
            str(v.output_tokens),
            str(v.cache_read_tokens),
            str(v.cache_write_tokens),
        )
        for name, v in sorted(buckets.items())
    ]
    for line in _align_rows(header, rows):
        print(f"  {line}")


def _print_budget_table(rows: Sequence[BudgetStatus]) -> None:
    """Print the configured budgets as an aligned table. No-op when no budgets."""
    if not rows:
        return
    print("\nbudgets")
    header = ("scope", "window", "spent", "limit", "remaining", "mode")
    table_rows = [
        (
            r.scope,
            r.window,
            _format_budget_amount(r.spent_usd, known=r.spent_known),
            f"${r.limit_usd:.4f}",
            "unavailable" if not r.spent_known else f"${r.remaining_usd:.4f}",
            "enforce" if r.enforce else "soft-warn",
        )
        for r in rows
    ]
    for line in _align_rows(header, table_rows):
        print(f"  {line}")


def _format_integration_rate(cell: RoutingCell) -> str:
    """The rate with its denominator attached, or an honest "unknown".

    A bare percentage invites a reader to treat 1/1 and 40/40 as the same claim, so the judged
    count travels with it. Nothing judged is `unknown`, never `0%` - an unreviewed client has not
    failed, it has not been looked at.
    """
    if cell.integration_rate is None:
        return f"unknown (0 of {cell.n_runs} judged)"
    return f"{cell.integration_rate * 100:.0f}% ({cell.n_integrated}/{cell.n_judged} judged)"


def _format_cell_cost(cell: RoutingCell) -> str:
    """Mean cost per integrated run - `unavailable` rather than `$0.0000` when unmeasured."""
    if cell.mean_cost_per_integrated is None:
        return "unavailable"
    if cell.priced_integrated_runs < cell.n_integrated:
        return (
            f"${cell.mean_cost_per_integrated:.4f} "
            f"({cell.priced_integrated_runs}/{cell.n_integrated} priced)"
        )
    return f"${cell.mean_cost_per_integrated:.4f}"


def _format_duration(cell: RoutingCell) -> str:
    if cell.mean_duration_ms is None:
        return "-"
    return f"{cell.mean_duration_ms / 1000:.1f}s"


def _print_routing_table(ledger: RoutingLedger) -> None:
    """Ranked routing evidence. Every rate is printed beside the sample size behind it."""
    rows = [
        [
            str(c.rank) if c.rank is not None else "-",
            c.task_kind,
            c.client,
            str(c.n_runs),
            _format_integration_rate(c),
            _format_duration(c),
            _format_cell_cost(c),
            "; ".join(c.notes) or "",
        ]
        for c in ledger.cells
    ]
    for line in _align_rows(
        ("rank", "task_kind", "client", "runs", "integrated", "mean_dur", "$/integrated", "notes"),
        rows,
    ):
        print(line)
