"""Recording a run's outcome, and reading the routing ledger built from it.

Free functions over ``(FleetState, UsageTracker)`` rather than methods on ``MarshalService``,
because ``marshal status`` / ``marshal usage`` work on a repo with **no** ``fleet.config.yaml`` and
these must too - a verdict about a finished run does not depend on which clients are configured
today. ``MarshalService`` delegates here, so the CLI and the MCP tool share one code path instead
of two implementations that can disagree (see the `marshal models` divergence this repo already
paid for).

This module is the seam that keeps ``accounting/ledger.py`` pure: ``runtime`` and ``accounting``
are sibling layers and must not import each other, so the ledger never learns ``RunRecord`` exists.
It receives the join as a plain mapping, built here.
"""

from __future__ import annotations

from datetime import datetime, timezone
from typing import Literal

from pydantic import BaseModel

from ..accounting.ledger import RoutingLedger, summarize_routing
from ..accounting.usage import UsageTracker, UsageWindow, usage_window_since
from ..core.types import RunOutcome, RunStatus
from ..runtime.state import FleetState, RunRecord

#: ``run_id -> outcome``. See `outcome_index` for why absence and ``None`` must stay distinct.
OutcomeIndex = dict[str, str | None]

_INTEGRATED = RunOutcome.INTEGRATED.value
_LEGAL = {o.value for o in RunOutcome}

#: Statuses that mean the run has not finished. There is nothing to judge yet.
_NON_TERMINAL = frozenset({RunStatus.QUEUED.value, RunStatus.RUNNING.value})

#: Cap on the reviewer's note. `text` and `verify_output` already showed what an unbounded field
#: costs: every reader of the record pays for it, and `status --full` dumps it.
MAX_OUTCOME_NOTE_LEN = 2000


class OutcomeResult(BaseModel):
    """What recording a verdict did - including the case where it deliberately did nothing.

    ``status`` is returned rather than raised for `conflict` so a driver can branch on it, the
    same shape `integrate` uses for its outcomes. ``outcome`` is always what the record says
    **now**, so a caller that ignores `status` still cannot misread the stored verdict.
    """

    run_id: str
    status: Literal["recorded", "unchanged", "conflict"]
    outcome: str | None
    previous: str | None
    note: str | None
    message: str


def outcome_index(state: FleetState) -> OutcomeIndex:
    """``run_id -> outcome`` for every run record on disk.

    A present key with a ``None`` value means "this run exists and has no verdict yet"; an
    **absent** key means the record is gone. The ledger is append-only and permanent, run records
    are not, so those two must stay distinguishable - collapsing them would silently reclassify
    pruned history as unjudged.
    """
    return {rec.run_id: rec.outcome for rec in state.list()}


def record_outcome(
    state: FleetState,
    run_id: str,
    outcome: str,
    *,
    note: str | None = None,
    now: datetime | None = None,
) -> OutcomeResult:
    """Stamp a driver's judgment on a finished run.

    Distinct from ``status``, which is process truth (`exited_clean`, `failed`). This is whether
    the work was any good, and it is the denominator every routing number is computed over: with
    no way to record a rejection, a run someone reviewed and threw away is indistinguishable from
    one nobody has looked at yet.

    ``integrated`` is **sticky**. It is a mechanical fact - a merge commit exists - not an opinion,
    so it is never overwritten; the attempt returns `conflict` instead. There is deliberately no
    `force`: undoing an integration is a new fact about a different commit, not a correction of the
    old one, and would want its own `reverted` outcome rather than erasing history.
    """
    if outcome not in _LEGAL:
        raise ValueError(
            f"invalid outcome {outcome!r}; expected one of {', '.join(sorted(_LEGAL))}"
        )
    if note is not None:
        note = note.strip()[:MAX_OUTCOME_NOTE_LEN] or None

    stamped = (now or datetime.now(timezone.utc)).isoformat()
    # Everything the predicate observed UNDER THE LOCK. Classifying from a pre-read instead would
    # report the wrong status whenever a concurrent write lands in between - and the concurrent
    # write this races is `integrate`, which is exactly the transition that must not be misreported.
    seen: dict[str, str | None] = {}

    def _may_record(rec: RunRecord) -> bool:
        seen["previous"] = rec.outcome
        seen["run_status"] = rec.status
        seen["merged_into"] = rec.merged_into
        if rec.status in _NON_TERMINAL:
            # Judging work that has not finished is not a verdict, it is a guess - and `outcome`
            # is what every routing rate is computed over.
            seen["refusal"] = "unfinished"
            return False
        if rec.outcome == outcome:
            seen["refusal"] = "unchanged"  # idempotent: re-recording a verdict is not a write
            return False
        if rec.outcome == _INTEGRATED:
            seen["refusal"] = "conflict"
            return False
        if outcome == _INTEGRATED and not rec.merged_into:
            # `integrated` is written by `integrate`, which has actually merged something. Letting
            # a caller assert it by hand would mint a permanent, un-overwritable verdict for a
            # merge that never happened - and stickiness is justified ONLY by that merge existing.
            seen["refusal"] = "not-merged"
            return False
        return True

    try:
        if state.get(run_id) is None:
            raise KeyError(run_id)
        after = state.update_if(
            run_id, _may_record, outcome=outcome, outcome_at=stamped, outcome_note=note
        )
    except KeyError:
        # Never create a record: the ledger holds facts about runs that actually happened.
        raise ValueError(f"no such run: {run_id!r}") from None

    previous = seen.get("previous")
    refusal = seen.get("refusal")

    def _result(status: str, message: str) -> OutcomeResult:
        return OutcomeResult(
            run_id=run_id,
            status=status,  # type: ignore[arg-type]
            outcome=after.outcome,
            previous=previous,
            note=after.outcome_note,
            message=message,
        )

    if refusal is None:
        return _result("recorded", f"recorded {outcome!r} for {run_id}")
    if refusal == "unchanged":
        return _result(
            "unchanged", f"{run_id} was already recorded {outcome!r}; nothing changed"
        )
    if refusal == "unfinished":
        return _result(
            "conflict",
            f"{run_id} is still {seen.get('run_status')!r}; wait for it to finish before judging "
            "its work (cancel_run if you want it stopped).",
        )
    if refusal == "not-merged":
        return _result(
            "conflict",
            f"refusing to record {_INTEGRATED!r} for {run_id}: nothing has been merged "
            "(`merged_into` is unset). That verdict is written by `integrate` when a merge "
            "actually lands, and it is permanent - asserting it by hand would make a merge that "
            "never happened impossible to correct.",
        )
    merged = f" (merged_into={after.merged_into})" if after.merged_into else ""
    return _result(
        "conflict",
        f"{run_id} is already recorded {_INTEGRATED!r}{merged}; refusing to overwrite it with "
        f"{outcome!r}. An integration is a mechanical fact, not an opinion - to undo it, "
        "revert the merge and record the outcome of that new run.",
    )


def build_routing(
    usage: UsageTracker,
    state: FleetState,
    *,
    window: UsageWindow = "all",
    task_kind: str | None = None,
    session_start: datetime | None = None,
    now: datetime | None = None,
) -> RoutingLedger:
    """Read the ledger and the run records, and join them into routing evidence.

    This is the only place the two stores meet. `accounting/ledger.py` stays pure and never learns
    what a run record is - `runtime` and `accounting` are sibling layers that must not import each
    other, so the outcome mapping is assembled here and handed down.

    Reporting posture, matching every other read path: `strict=False`, so a torn or malformed
    ledger line is skipped with a warning rather than taking the whole report down.
    """
    moment = now or datetime.now(timezone.utc)
    since = usage_window_since(window, session_start=session_start or moment, now=moment)
    return summarize_routing(
        usage.events(),
        outcome_index(state),
        since=since,
        task_kind=task_kind,
    )
