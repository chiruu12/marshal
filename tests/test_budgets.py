"""Unit tests for ``EnforceBudgetGate`` reservation bookkeeping.

Fleet-level budget behavior (soft-warn, over-cap refuse, one-in-flight admit) lives in
test_fleet.py; this file pins the gate's slot lifecycle: a ``begin()`` that raises must
roll back every slot it reserved (and only those), so a refused spawn can never lock out
a budget for the process lifetime.
"""

from __future__ import annotations

from datetime import datetime, timezone
from pathlib import Path

import pytest

from marshal_engine import TaskSpec
from marshal_engine.budgets import BudgetExceeded, EnforceBudgetGate, _enforce_budget_key
from marshal_engine.config import BudgetSpec
from marshal_engine.fleet import RunRequest
from marshal_engine.usage import UsageEvent, UsageTracker


@pytest.fixture
def tracker(tmp_path: Path) -> UsageTracker:
    return UsageTracker(tmp_path / "usage")


@pytest.fixture
def session_start() -> datetime:
    return datetime.now(timezone.utc)


def _req(*, backend: str = "metered", client: str | None = None) -> RunRequest:
    return RunRequest(backend_name=backend, task=TaskSpec(id="t", goal="x"), client=client)


def _seed_spend(tracker: UsageTracker, *, backend: str, client: str | None, cost: float) -> None:
    """Append one priced event so the next budget check has spend to read."""
    tracker.record(
        UsageEvent(
            ts=datetime.now(timezone.utc).isoformat(),
            run_id=f"seed.{backend}.x",
            backend=backend,
            client=client,
            cost_usd=cost,
            status="exited_clean",
            source="native",
        )
    )


def test_multi_budget_conflict_releases_earlier_slots(
    tracker: UsageTracker, session_start: datetime
) -> None:
    # Regression (#141): a spawn matching 2+ enforce budgets reserves budget A's slot, then
    # conflicts on budget B's held slot. A's placeholder must be rolled back with the raise -
    # otherwise every later spawn matching A is refused ("run starting") for the lifetime of
    # the long-lived MCP server process: a permanent spawn lockout pointing at a phantom holder.
    budget_a = BudgetSpec(client="worker", window="week", limit_usd=100.0, enforce=True)
    budget_b = BudgetSpec(backend="metered", window="week", limit_usd=100.0, enforce=True)
    budgets = [budget_a, budget_b]
    gate = EnforceBudgetGate()

    # An in-flight run holds budget B (client=None matches B only, not A).
    held = gate.begin(tracker, session_start, budgets, _req(client=None))
    gate.bind(held, "run-holder")

    # A spawn matching BOTH budgets conflicts on B - and must not leak A's slot.
    with pytest.raises(BudgetExceeded, match="in-flight"):
        gate.begin(tracker, session_start, budgets, _req(client="worker"))
    assert _enforce_budget_key(budget_a) not in gate._held
    # The pre-existing holder is untouched: only this call's own reservations rolled back.
    assert gate._held[_enforce_budget_key(budget_b)] == "run-holder"

    # The spawn the bug deadlocked: one matching ONLY budget A now succeeds.
    keys = gate.begin(tracker, session_start, budgets, _req(backend="ghost", client="worker"))
    assert keys == [_enforce_budget_key(budget_a)]


def test_begin_rolls_back_every_prior_reservation_on_conflict(
    tracker: UsageTracker, session_start: datetime
) -> None:
    # N matching enforce budgets where the k-th raises: all k-1 earlier reservations from
    # THIS call are popped, and slots other runs legitimately hold are left alone.
    budget_global = BudgetSpec(window="week", limit_usd=100.0, enforce=True)
    budget_client = BudgetSpec(client="worker", window="week", limit_usd=100.0, enforce=True)
    budget_backend = BudgetSpec(backend="metered", window="week", limit_usd=100.0, enforce=True)
    budgets = [budget_global, budget_client, budget_backend]
    gate = EnforceBudgetGate()

    # A prior run holds ONLY the backend budget (reserved through its own begin call).
    held = gate.begin(tracker, session_start, [budget_backend], _req(client=None))
    gate.bind(held, "run-holder")

    # The new spawn matches all three; the first two reserve, the third conflicts.
    with pytest.raises(BudgetExceeded, match="in-flight"):
        gate.begin(tracker, session_start, budgets, _req(client="worker"))
    assert gate._held == {_enforce_budget_key(budget_backend): "run-holder"}

    # With only the real holder left, a spawn matching the first two budgets is admitted.
    keys = gate.begin(tracker, session_start, budgets, _req(backend="ghost", client="worker"))
    assert keys == [_enforce_budget_key(budget_global), _enforce_budget_key(budget_client)]


def test_begin_over_cap_enforce_raises_before_any_reservation(
    tracker: UsageTracker, session_start: datetime
) -> None:
    # check_budget runs before the reservation loop: an over-cap enforce budget refuses the
    # spawn with the ledger message and reserves nothing (the loop is never entered).
    budget_a = BudgetSpec(client="worker", window="week", limit_usd=1.0, enforce=True)
    budget_b = BudgetSpec(backend="metered", window="week", limit_usd=100.0, enforce=True)
    gate = EnforceBudgetGate()
    _seed_spend(tracker, backend="metered", client="worker", cost=5.0)
    with pytest.raises(BudgetExceeded, match="refusing new spawn"):
        gate.begin(tracker, session_start, [budget_a, budget_b], _req(client="worker"))
    assert gate._held == {}


def test_begin_ledger_failure_fails_closed_and_holds_nothing(
    tracker: UsageTracker, session_start: datetime, monkeypatch: pytest.MonkeyPatch
) -> None:
    # Fail-closed contract (check_budget): an enforce budget whose spend lookup errors refuses
    # the spawn. The refusal must not hold a slot, so a spawn is admitted once the ledger
    # reads again - a transient ledger hiccup is not a permanent lockout either.
    budget = BudgetSpec(backend="metered", window="week", limit_usd=100.0, enforce=True)
    gate = EnforceBudgetGate()

    def boom(**_kw: object) -> object:
        raise RuntimeError("ledger corrupt")

    monkeypatch.setattr(tracker, "summary", boom)  # type: ignore[method-assign]
    with pytest.raises(BudgetExceeded, match="spend lookup failed"):
        gate.begin(tracker, session_start, [budget], _req())
    assert gate._held == {}

    monkeypatch.undo()
    assert gate.begin(tracker, session_start, [budget], _req()) == [_enforce_budget_key(budget)]


def test_single_enforce_budget_reserve_bind_release_cycle(
    tracker: UsageTracker, session_start: datetime
) -> None:
    # Happy path unchanged: reserve ("" placeholder) -> bind(run_id) -> a matching peer is
    # refused while held -> release frees the slot for the next matching spawn.
    budget = BudgetSpec(backend="metered", window="week", limit_usd=100.0, enforce=True)
    key = _enforce_budget_key(budget)
    gate = EnforceBudgetGate()

    keys = gate.begin(tracker, session_start, [budget], _req())
    assert keys == [key]
    assert gate._held[key] == ""  # reserved, not yet bound to a run

    gate.bind(keys, "run-1")
    assert gate._held[key] == "run-1"
    with pytest.raises(BudgetExceeded, match="in-flight"):
        gate.begin(tracker, session_start, [budget], _req())

    gate.release_run("run-1")
    assert gate._held == {}

    # The _start-failure path: release() drops a not-yet-bound reservation.
    keys = gate.begin(tracker, session_start, [budget], _req())
    gate.release(keys)
    assert gate._held == {}
    assert gate.begin(tracker, session_start, [budget], _req()) == [key]
