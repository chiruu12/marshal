"""Unit tests for EnforceBudgetGate + enforce budget fail-closed / reservation hygiene."""

from __future__ import annotations

import threading
import time
from concurrent.futures import ThreadPoolExecutor
from datetime import datetime, timezone
from pathlib import Path
from types import SimpleNamespace

import pytest

from marshal_engine.budgets import (
    BudgetExceeded,
    EnforceBudgetGate,
    _enforce_budget_key,
    check_budget,
)
from marshal_engine.config import BudgetSpec
from marshal_engine.usage import UsageEvent, UsageTracker


def _scope(*, client: str | None = None, backend_name: str = "opencode") -> SimpleNamespace:
    return SimpleNamespace(client=client, backend_name=backend_name)


def _tracker(tmp_path: Path) -> UsageTracker:
    return UsageTracker(tmp_path / "usage")


def _seed(
    tracker: UsageTracker,
    *,
    backend: str = "opencode",
    client: str | None = "worker",
    cost: float = 0.50,
) -> None:
    tracker.record(
        UsageEvent(
            ts=datetime.now(timezone.utc).isoformat(),
            run_id=f"seed.{backend}.{cost}",
            backend=backend,
            client=client,
            cost_usd=cost,
            status="exited_clean",
            source="native",
        )
    )


SESSION = datetime(2026, 7, 1, tzinfo=timezone.utc)


def test_multi_budget_conflict_releases_earlier_slots(tmp_path: Path) -> None:
    """Reserving budget A then failing on held budget B must pop A's placeholder (#141)."""
    tracker = _tracker(tmp_path)
    global_b = BudgetSpec(window="week", limit_usd=100.0, enforce=True)
    backend_b = BudgetSpec(backend="cursor", window="week", limit_usd=100.0, enforce=True)
    gate = EnforceBudgetGate()
    # Simulate another in-flight run already holding the backend slot.
    gate._held[_enforce_budget_key(backend_b)] = "peer-run"

    with pytest.raises(BudgetExceeded, match="in-flight"):
        gate.begin(tracker, SESSION, [global_b, backend_b], _scope(backend_name="cursor"))

    assert _enforce_budget_key(global_b) not in gate._held
    assert gate._held[_enforce_budget_key(backend_b)] == "peer-run"

    # After the conflict, a spawn matching only the first (global) budget succeeds.
    keys = gate.begin(tracker, SESSION, [global_b], _scope(backend_name="opencode"))
    assert keys == [_enforce_budget_key(global_b)]
    gate.release(keys)
    assert _enforce_budget_key(global_b) not in gate._held


def test_begin_releases_slots_on_every_reservation_failure(tmp_path: Path) -> None:
    """Any raise inside the reservation loop must clear keys reserved so far."""
    tracker = _tracker(tmp_path)
    a = BudgetSpec(window="week", limit_usd=100.0, enforce=True)
    b = BudgetSpec(client="worker", window="week", limit_usd=100.0, enforce=True)
    c = BudgetSpec(backend="opencode", window="week", limit_usd=100.0, enforce=True)
    gate = EnforceBudgetGate()
    gate._held[_enforce_budget_key(c)] = "holder"

    with pytest.raises(BudgetExceeded, match="in-flight"):
        gate.begin(
            tracker, SESSION, [a, b, c], _scope(client="worker", backend_name="opencode")
        )

    assert _enforce_budget_key(a) not in gate._held
    assert _enforce_budget_key(b) not in gate._held
    assert gate._held[_enforce_budget_key(c)] == "holder"


def test_release_and_release_run_clear_slots(tmp_path: Path) -> None:
    """Explicit release paths (``_start`` failure / terminal) drop reserved keys."""
    tracker = _tracker(tmp_path)
    budget = BudgetSpec(backend="opencode", window="week", limit_usd=100.0, enforce=True)
    gate = EnforceBudgetGate()
    keys = gate.begin(tracker, SESSION, [budget], _scope())
    assert keys
    gate.release(keys)  # _start failed before bind
    assert gate._held == {}

    keys = gate.begin(tracker, SESSION, [budget], _scope())
    gate.bind(keys, "run-1")
    gate.release_run("run-1")  # terminal / spawn submit failure
    assert gate._held == {}


def test_ledger_failure_fail_closed_leaves_no_slots(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    """enforce=true fails closed on unreadable ledger; no reservation is left behind."""
    tracker = _tracker(tmp_path)
    budget = BudgetSpec(backend="opencode", window="week", limit_usd=1.0, enforce=True)
    gate = EnforceBudgetGate()

    def boom(**_kw: object) -> object:
        raise RuntimeError("ledger unreadable")

    monkeypatch.setattr(tracker, "summary", boom)
    with pytest.raises(BudgetExceeded, match="spend lookup failed"):
        gate.begin(tracker, SESSION, [budget], _scope())
    assert gate._held == {}


def test_torn_ledger_line_does_not_refuse_enforce_spawn(
    tmp_path: Path, capsys: pytest.CaptureFixture[str]
) -> None:
    """A malformed events.jsonl line is skipped — enforce must not fail closed on it (#142)."""
    tracker = _tracker(tmp_path)
    _seed(tracker, cost=0.10)
    with tracker.events_path.open("a", encoding="utf-8") as f:
        f.write('{"ts":"2026-07-01T00:00:00Z","run_id":"torn","backend":"openco')  # torn

    budget = BudgetSpec(backend="opencode", window="week", limit_usd=100.0, enforce=True)
    gate = EnforceBudgetGate()
    keys = gate.begin(tracker, SESSION, [budget], _scope())
    assert keys
    err = capsys.readouterr().err
    assert "skipping 1 malformed usage event line" in err
    gate.release(keys)


def test_begin_optimistic_ledger_scan_outside_lock(tmp_path: Path) -> None:
    """At least the first summary() of begin() must run while ``_lock`` is free (#145)."""
    tracker = _tracker(tmp_path)
    budget = BudgetSpec(backend="opencode", window="week", limit_usd=100.0, enforce=True)
    gate = EnforceBudgetGate()
    held_during_summary: list[bool] = []
    real = tracker.summary

    def spy(**kw: object) -> object:
        held_during_summary.append(gate._lock.locked())
        return real(**kw)

    tracker.summary = spy  # type: ignore[method-assign]
    keys = gate.begin(tracker, SESSION, [budget], _scope())
    assert keys
    assert held_during_summary, "summary() was never called"
    assert held_during_summary[0] is False, "optimistic scan must not hold the gate lock"
    gate.release(keys)


def test_begin_lock_hold_does_not_serialize_over_cap_scans(tmp_path: Path) -> None:
    """Over-cap refuses happen outside the lock, so concurrent begins parallelize ledger scans."""
    tracker = _tracker(tmp_path)
    _seed(tracker, cost=5.0)
    budget = BudgetSpec(backend="opencode", window="week", limit_usd=1.0, enforce=True)
    gate = EnforceBudgetGate()
    real = tracker.summary
    delay = 0.08

    def slow(**kw: object) -> object:
        time.sleep(delay)
        return real(**kw)

    tracker.summary = slow  # type: ignore[method-assign]
    n = 4
    start = time.perf_counter()
    errors: list[BaseException] = []

    def worker() -> None:
        try:
            gate.begin(tracker, SESSION, [budget], _scope())
        except BudgetExceeded as exc:
            errors.append(exc)

    with ThreadPoolExecutor(max_workers=n) as pool:
        list(pool.map(lambda _: worker(), range(n)))
    elapsed = time.perf_counter() - start

    assert len(errors) == n
    # Serialized under the lock would take ~n*delay; parallel optimistic scans ~1*delay.
    assert elapsed < n * delay * 0.7, f"scans appear serialized under the lock ({elapsed:.3f}s)"


def test_concurrent_begin_admits_one_matching_spawn(tmp_path: Path) -> None:
    """Reservation correctness: exactly one of N concurrent matching begins gets the slot."""
    tracker = _tracker(tmp_path)
    budget = BudgetSpec(backend="opencode", window="week", limit_usd=100.0, enforce=True)
    gate = EnforceBudgetGate()
    winners: list[list[str]] = []
    losers: list[str] = []
    barrier = threading.Barrier(8)

    def worker() -> None:
        barrier.wait()
        try:
            keys = gate.begin(tracker, SESSION, [budget], _scope())
            winners.append(keys)
        except BudgetExceeded as exc:
            losers.append(str(exc))

    threads = [threading.Thread(target=worker) for _ in range(8)]
    for t in threads:
        t.start()
    for t in threads:
        t.join()

    assert len(winners) == 1
    assert len(losers) == 7
    assert all("in-flight" in msg for msg in losers)
    gate.release(winners[0])
    # Slot free again — a follow-up begin succeeds.
    keys = gate.begin(tracker, SESSION, [budget], _scope())
    assert keys
    gate.release(keys)


def test_check_budget_enforce_only_skips_advisory_warn(
    tmp_path: Path, capsys: pytest.CaptureFixture[str]
) -> None:
    tracker = _tracker(tmp_path)
    _seed(tracker, cost=2.0)
    advisory = BudgetSpec(backend="opencode", window="week", limit_usd=1.0, enforce=False)
    check_budget(tracker, SESSION, [advisory], _scope(), enforce_only=True)
    assert capsys.readouterr().err == ""
