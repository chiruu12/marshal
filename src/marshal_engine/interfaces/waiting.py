"""Blocking until spawned runs finish, so a driver does not spend a turn per poll.

``spawn`` returns a RUNNING record immediately and nothing closed the loop: a driver fanning out
five runs polled ``status`` on a cadence it had to guess, one tool call and one model turn per tick,
every tick before the last one saying "not yet". Too tight burns tokens; too loose adds dead
wall-clock to every run.

MCP has no server-initiated push, so "notify me when done" can only be a **blocking wait** - the
server holds the call open. This is still a poll loop; the point is that it runs *here*, where a
tick costs a few file reads, instead of in the driver's context, where a tick costs a turn.

Free functions rather than methods, for the same reason as `routing.py`: waiting on a finished run
does not depend on which clients are configured today, and the wait must also span workspaces,
which no single-repo ``MarshalService`` can do. `wait_for_terminal` takes its clock and its sleep
as parameters so the whole thing is testable without spending real seconds.
"""

from __future__ import annotations

import time
from typing import Callable, Iterable, Mapping, Sequence

from pydantic import BaseModel

from ..core.types import is_terminal
from ..runtime.state import RunRecord

#: Fetch the current record for each requested id. ``None`` means no such run - distinct from a
#: record that exists and has not finished, because one can never settle and the other will.
Fetch = Callable[[Sequence[str]], Mapping[str, "RunRecord | None"]]

#: Hard ceiling on a single wait. The real ceiling is usually lower and not ours: MCP clients apply
#: their own request timeout, and a call held past it is killed. That is exactly why expiry returns
#: a partial result instead of raising - the driver re-calls and keeps its progress, so a client
#: with a short timeout degrades to a coarser poll rather than to a broken tool.
MAX_WAIT_S = 600.0

#: Long enough that a tick is cheap next to any real agent run, short enough that a run finishing
#: does not sit unreported. Not adaptive: a fixed interval is one less thing to explain.
DEFAULT_POLL_INTERVAL_S = 1.0


class WaitResult(BaseModel):
    """What the wait observed. Every requested id appears in exactly one of the three lists."""

    settled: list[RunRecord] = []
    pending: list[RunRecord] = []
    #: Ids with no record in any workspace. Returned, never waited on: nothing will create them.
    unknown: list[str] = []
    #: True when the deadline expired with runs still pending. `pending` is then the partial result.
    timed_out: bool = False
    waited_ms: int = 0

    @property
    def all_settled(self) -> bool:
        """Did everything that *could* finish, finish? Unknown ids do not count against this."""
        return not self.pending


def wait_for_terminal(
    fetch: Fetch,
    run_ids: Iterable[str],
    *,
    timeout_s: float,
    poll_interval_s: float = DEFAULT_POLL_INTERVAL_S,
    sleep: Callable[[float], None] = time.sleep,
    monotonic: Callable[[], float] = time.monotonic,
) -> WaitResult:
    """Block until every run in ``run_ids`` is terminal, or until ``timeout_s`` elapses.

    Terminality is `core.types.is_terminal`, the same predicate `routing` uses and the same one
    behind the status a driver reads - there is deliberately no second definition of "done" that
    could disagree with `status` about the run it is waiting on.

    Never raises on expiry. It returns what settled and what did not, so a caller that is cut off
    (by this timeout or by its own client's) can act on the finished runs and re-call for the rest.

    A cancelled run is terminal, so cancelling something being waited on releases the wait on the
    next tick. A run whose supervisor died still reads ``running`` forever, and only the timeout
    ends that wait - `is_terminal` describes the record, not the process.
    """
    ids = list(dict.fromkeys(run_ids))  # de-dup, keep the caller's order for a stable result
    deadline = monotonic() + max(0.0, min(timeout_s, MAX_WAIT_S))
    started = monotonic()

    while True:
        current = fetch(ids)
        unknown = [rid for rid in ids if current.get(rid) is None]
        known = [rec for rid in ids if (rec := current.get(rid)) is not None]
        settled = [rec for rec in known if is_terminal(rec.status)]
        pending = [rec for rec in known if not is_terminal(rec.status)]

        # Checked before the first sleep, so a wait on already-finished runs costs one fetch and
        # returns at once rather than idling out a poll interval.
        if not pending or monotonic() >= deadline:
            return WaitResult(
                settled=settled,
                pending=pending,
                unknown=unknown,
                timed_out=bool(pending),
                waited_ms=int((monotonic() - started) * 1000),
            )

        # Never sleep past the deadline: overshooting would report a wait longer than was asked for.
        sleep(max(0.0, min(poll_interval_s, deadline - monotonic())))
