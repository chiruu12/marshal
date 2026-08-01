"""Routing evidence: which client actually produced work you kept, for which kind of task.

Derived on read, never stored - the same two-layer split the rest of accounting uses. The engine
stamps *facts* (cost, tokens, duration, provenance) to an immutable ledger; interpretation lives
here and is recomputed every call, so nothing on disk can go stale or disagree with itself.

**The join.** Answering "which client should I send this to" needs two things that live apart:
`task_kind` is on the usage event, `outcome` is on the run record. They are keyed by `run_id`, and
this module deliberately does not know what a run record *is* - `runtime` and `accounting` are
sibling layers that must not import each other. The caller passes the outcomes in as a plain
mapping (`interfaces/routing.py` builds it), which keeps this file pure and directly testable
without a filesystem.

**What this refuses to do.** It never ranks on cost it did not measure, and it never hides a small
sample. A client with one lucky run can rank first - but its `n` travels with it on every surface,
so the reader can see exactly how much evidence is behind the number.
"""

from __future__ import annotations

import math
from collections.abc import Iterable, Mapping, Sequence
from datetime import datetime
from typing import Final

from pydantic import BaseModel

from ..core.types import RunOutcome, UsageSource
from .usage import UsageEvent, _in_window

#: ``run_id -> outcome``. A present key with ``None`` means "run exists, not judged yet"; an
#: **absent** key means the run record is gone. Those are different facts and must not collapse:
#: the ledger is permanent, run records are not, so treating pruned history as unjudged would
#: quietly move deleted runs into the denominator of every rate.
OutcomeIndex = Mapping[str, str | None]

#: Cost sources that represent a real measurement. Everything else contributes tokens and a run
#: count but never a dollar figure - the same predicate `service.report()` uses to pick `cheapest`.
MEASURED_SOURCES: Final[frozenset[str]] = frozenset(
    {UsageSource.NATIVE.value, UsageSource.ADMIN_API.value}
)

#: Sentinels for the two grouping keys when an event carries neither.
UNTAGGED: Final[str] = "untagged"          # no task_kind was passed at spawn
UNKNOWN_CLIENT: Final[str] = "unknown"     # ad-hoc backend spawn, no named client

_INTEGRATED: Final[str] = RunOutcome.INTEGRATED.value
_REJECTED: Final[str] = RunOutcome.REJECTED.value
_ABANDONED: Final[str] = RunOutcome.ABANDONED.value
_JUDGED: Final[frozenset[str]] = frozenset({_INTEGRATED, _REJECTED, _ABANDONED})


class RoutingCell(BaseModel):
    """One `(task_kind, client)` pair's measured history.

    Every count is reported. A rate without its `n` is a claim without evidence, so `n_runs` and
    `n_judged` are first-class fields rather than something a caller has to derive.
    """

    task_kind: str
    client: str

    n_runs: int = 0          # every event in the window for this pair
    n_judged: int = 0        # a verdict was recorded
    n_integrated: int = 0
    n_rejected: int = 0
    n_abandoned: int = 0
    n_unjudged: int = 0      # run record exists, nobody has judged it
    n_no_record: int = 0     # the run record is gone; counted in n_runs, never in n_judged

    #: Integrated over JUDGED runs, not over all runs. `None` when nothing has been judged - a
    #: rate over an empty denominator is not 0%, it is unknown, and rendering it as 0 would
    #: defame a client nobody has reviewed yet.
    integration_rate: float | None = None

    mean_duration_ms: float | None = None
    duration_runs: int = 0

    #: Measured cost only. `None` - never 0.0 - when no integrated run reported a real cost.
    mean_cost_per_integrated: float | None = None
    priced_integrated_runs: int = 0
    #: Spend across ALL measured runs for this pair, not just the integrated ones. Cost-per-
    #: integrated on its own flatters a client that burns four rejected runs for every keeper.
    measured_cost_all_usd: float = 0.0
    priced_runs: int = 0
    cost_native: float = 0.0
    cost_admin_api: float = 0.0

    rank: int | None = None  # None = unranked (nothing judged), still listed
    cost_ranked: bool = False
    evidence: str = ""
    notes: list[str] = []


class RoutingLedger(BaseModel):
    """The full picture, ranked. `recommended` is `None` rather than a guess when nothing is judged."""

    cells: list[RoutingCell] = []
    recommended: str | None = None
    recommended_task_kind: str | None = None
    total_runs: int = 0
    total_judged: int = 0
    events_without_record: int = 0
    task_kind_filter: str | None = None
    #: Set when nothing has been judged yet: the ledger is not empty, it is unevaluated, and a
    #: caller that cannot tell those apart will read "no data" as "no history".
    caveat: str | None = None


def summarize_routing(
    events: Iterable[UsageEvent],
    outcomes: OutcomeIndex,
    *,
    since: datetime | None = None,
    until: datetime | None = None,
    task_kind: str | None = None,
) -> RoutingLedger:
    """Roll usage events up per `(task_kind, client)`, joined to their recorded verdicts."""
    cells: dict[tuple[str, str], RoutingCell] = {}
    duration_totals: dict[tuple[str, str], int] = {}
    integrated_cost: dict[tuple[str, str], float] = {}
    wanted = task_kind.strip() if task_kind else None

    for event in events:
        if not _in_window(event, since, until):
            continue
        # Free text from the driver: strip only. Case-folding would merge `Refactor` into
        # `refactor` and hide that the taxonomy is drifting; a typo should be a visible row.
        kind = (event.task_kind or "").strip() or UNTAGGED
        if wanted is not None and kind != wanted:
            continue
        client = event.client or UNKNOWN_CLIENT
        key = (kind, client)
        cell = cells.setdefault(key, RoutingCell(task_kind=kind, client=client))

        cell.n_runs += 1
        measured = event.source in MEASURED_SOURCES
        if measured:
            cell.priced_runs += 1
            cell.measured_cost_all_usd = round(cell.measured_cost_all_usd + event.cost_usd, 6)
            if event.source == UsageSource.NATIVE.value:
                cell.cost_native = round(cell.cost_native + event.cost_usd, 6)
            else:
                cell.cost_admin_api = round(cell.cost_admin_api + event.cost_usd, 6)
        if event.duration_ms > 0:
            cell.duration_runs += 1
            duration_totals[key] = duration_totals.get(key, 0) + event.duration_ms

        if event.run_id not in outcomes:
            cell.n_no_record += 1
            continue
        verdict = outcomes[event.run_id]
        if verdict not in _JUDGED:
            cell.n_unjudged += 1
            continue
        cell.n_judged += 1
        if verdict == _INTEGRATED:
            cell.n_integrated += 1
            if measured:
                cell.priced_integrated_runs += 1
                integrated_cost[key] = round(integrated_cost.get(key, 0.0) + event.cost_usd, 6)
        elif verdict == _REJECTED:
            cell.n_rejected += 1
        else:
            cell.n_abandoned += 1

    for key, cell in cells.items():
        if cell.n_judged:
            cell.integration_rate = round(cell.n_integrated / cell.n_judged, 4)
        if cell.duration_runs:
            cell.mean_duration_ms = round(duration_totals[key] / cell.duration_runs, 1)
        if cell.priced_integrated_runs:
            cell.mean_cost_per_integrated = round(
                integrated_cost[key] / cell.priced_integrated_runs, 6
            )
        cell.cost_ranked = cell.priced_integrated_runs > 0
        cell.evidence = _evidence(cell)
        cell.notes = _notes(cell)

    ranked = rank_cells(list(cells.values()))
    total_judged = sum(c.n_judged for c in ranked)
    top = next((c for c in ranked if c.rank == 1), None)
    return RoutingLedger(
        cells=ranked,
        recommended=top.client if top else None,
        recommended_task_kind=top.task_kind if top else None,
        total_runs=sum(c.n_runs for c in ranked),
        total_judged=total_judged,
        events_without_record=sum(c.n_no_record for c in ranked),
        task_kind_filter=wanted,
        caveat=None if total_judged else (
            "no run has a recorded outcome yet, so nothing can be ranked - record verdicts with "
            "set_outcome (integrate records 'integrated' for you; rejections are the half that "
            "otherwise leaves no trace)"
        ),
    )


def rank_cells(cells: Sequence[RoutingCell]) -> list[RoutingCell]:
    """Order cells best-first and stamp `rank` on the ones that can be ranked at all.

    The key order is the whole policy, so it is worth stating plainly:

    1. Judged or not. A cell with no recorded verdict has no evidence and cannot be ranked; it
       sorts last and keeps `rank=None`. It is still returned, with its counts.
    2. Integration rate, descending. This is the question being asked - did the work get kept.
    3. Mean duration, ascending; unknown duration sorts as infinity, so it can lose a tiebreak
       but never win one.
    4. Mean measured cost per integrated run, ascending; **unmeasured cost sorts as infinity**.
       That is the entire mechanism by which cost cannot rank what it did not measure: treating
       an unmeasured cell as $0 would make the backend that reports nothing the cheapest, and
       therefore the winner. Cost sits below rate and duration on purpose - it breaks ties, it
       does not decide.
    5. `(task_kind, client)` alphabetically, so the order never depends on dict insertion.
    """
    ordered = sorted(
        cells,
        key=lambda c: (
            0 if c.integration_rate is not None else 1,
            -(c.integration_rate or 0.0),
            c.mean_duration_ms if c.duration_runs else math.inf,
            c.mean_cost_per_integrated if c.priced_integrated_runs else math.inf,
            c.task_kind,
            c.client,
        ),
    )
    rank = 0
    for cell in ordered:
        if cell.integration_rate is None:
            cell.rank = None
            continue
        rank += 1
        cell.rank = rank
    return ordered


def _evidence(cell: RoutingCell) -> str:
    """One line a human or an agent can quote, with the sample size attached to the claim."""
    if not cell.n_judged:
        return f"{cell.n_runs} run(s), none judged yet"
    pct = f"{(cell.integration_rate or 0.0) * 100:.0f}%"
    return (
        f"{cell.n_integrated}/{cell.n_judged} judged runs integrated ({pct}, n={cell.n_judged})"
        f" out of {cell.n_runs} run(s)"
    )


def _notes(cell: RoutingCell) -> list[str]:
    """Everything that would make the headline number misleading if read on its own."""
    notes: list[str] = []
    if cell.n_unjudged:
        notes.append(
            f"{cell.n_unjudged} run(s) not judged - excluded from the rate, not counted as failures"
        )
    if cell.n_no_record:
        notes.append(f"{cell.n_no_record} run(s) have no run record (pruned); counted only in n_runs")
    if cell.n_integrated and not cell.priced_integrated_runs:
        notes.append("no measured cost for any integrated run - this client is unranked on price")
    elif cell.priced_integrated_runs and cell.priced_integrated_runs < cell.n_integrated:
        notes.append(
            f"cost measured for {cell.priced_integrated_runs}/{cell.n_integrated} integrated run(s)"
        )
    if cell.n_judged and cell.n_judged < 3:
        notes.append(f"small sample (n={cell.n_judged}) - treat the rate as weak evidence")
    return notes
