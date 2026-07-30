"""Unit tests for EnforceBudgetGate + enforce budget fail-closed / reservation hygiene."""

from __future__ import annotations

import threading
import time
from concurrent.futures import ThreadPoolExecutor
from datetime import datetime, timezone
from pathlib import Path
from types import SimpleNamespace

import pytest

import marshal_engine.budgets as budgets_mod
from marshal_engine.budgets import (
    BudgetExceeded,
    EnforceBudgetGate,
    _enforce_budget_key,
    _recheck_enforce_from_tail,
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
    run_id: str | None = None,
) -> None:
    tracker.record(
        UsageEvent(
            ts=datetime.now(timezone.utc).isoformat(),
            run_id=run_id or f"seed.{backend}.{cost}.{time.time_ns()}",
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

    def boom(*, strict: bool = False) -> object:
        raise RuntimeError("ledger unreadable")

    monkeypatch.setattr(tracker, "read_events", boom)
    with pytest.raises(BudgetExceeded, match="spend lookup failed"):
        gate.begin(tracker, SESSION, [budget], _scope())
    assert gate._held == {}


def test_torn_line_enforce_fails_closed_actionable(
    tmp_path: Path, capsys: pytest.CaptureFixture[str]
) -> None:
    """Torn spend line → enforce refuses with file + skipped count + repair step."""
    tracker = _tracker(tmp_path)
    _seed(tracker, cost=0.10)
    with tracker.events_path.open("a", encoding="utf-8") as f:
        f.write('{"ts":"2026-07-01T00:00:00Z","run_id":"torn","backend":"openco')  # torn

    budget = BudgetSpec(backend="opencode", window="week", limit_usd=100.0, enforce=True)
    gate = EnforceBudgetGate()
    with pytest.raises(BudgetExceeded, match="unreadable event") as ei:
        gate.begin(tracker, SESSION, [budget], _scope())
    msg = str(ei.value)
    assert "events.jsonl" in msg
    assert "1" in msg
    assert "repair or remove the torn line" in msg
    assert gate._held == {}
    # Strict path must not emit the lenient reporting warn.
    assert "skipping" not in capsys.readouterr().err


def test_torn_line_advisory_admits_with_warn(
    tmp_path: Path, capsys: pytest.CaptureFixture[str]
) -> None:
    """Advisory budgets stay lenient: torn line → warn + admit."""
    tracker = _tracker(tmp_path)
    _seed(tracker, cost=0.10)
    with tracker.events_path.open("a", encoding="utf-8") as f:
        f.write('{"ts":"2026-07-01T00:00:00Z","run_id":"torn","backend":"openco')

    budget = BudgetSpec(backend="opencode", window="week", limit_usd=100.0, enforce=False)
    gate = EnforceBudgetGate()
    keys = gate.begin(tracker, SESSION, [budget], _scope())
    assert keys == []  # advisory: no concurrency slots
    assert "skipping 1 malformed usage event line" in capsys.readouterr().err


def test_begin_admit_path_no_full_scan_under_lock(tmp_path: Path) -> None:
    """Admit path: under the lock only events_after (tail), never full read_events/summary/events."""
    tracker = _tracker(tmp_path)
    budget = BudgetSpec(backend="opencode", window="week", limit_usd=100.0, enforce=True)
    gate = EnforceBudgetGate()

    full_scans_under_lock: list[str] = []
    real_read = tracker.read_events
    real_summary = tracker.summary
    real_events = tracker.events
    real_after = tracker.events_after
    after_under_lock = 0

    def spy_read(*, strict: bool = False) -> object:
        if gate._lock.locked():
            full_scans_under_lock.append("read_events")
        return real_read(strict=strict)

    def spy_summary(*_a: object, **_k: object) -> object:
        if gate._lock.locked():
            full_scans_under_lock.append("summary")
        return real_summary(*_a, **_k)

    def spy_events(*, strict: bool = False) -> object:
        if gate._lock.locked():
            full_scans_under_lock.append("events")
        return real_events(strict=strict)

    def spy_after(cursor: object, *, strict: bool = False) -> object:
        nonlocal after_under_lock
        if gate._lock.locked():
            after_under_lock += 1
        return real_after(cursor, strict=strict)  # type: ignore[arg-type]

    tracker.read_events = spy_read  # type: ignore[method-assign]
    tracker.summary = spy_summary  # type: ignore[method-assign]
    tracker.events = spy_events  # type: ignore[method-assign]
    tracker.events_after = spy_after  # type: ignore[method-assign]

    keys = gate.begin(tracker, SESSION, [budget], _scope())
    assert keys
    assert full_scans_under_lock == [], f"full scan under lock: {full_scans_under_lock}"
    assert after_under_lock == 1  # tail revalidation ran under the lock
    gate.release(keys)


def test_begin_lock_hold_does_not_serialize_over_cap_scans(tmp_path: Path) -> None:
    """Over-cap refuses happen outside the lock, so concurrent begins parallelize ledger scans."""
    tracker = _tracker(tmp_path)
    _seed(tracker, cost=5.0)
    budget = BudgetSpec(backend="opencode", window="week", limit_usd=1.0, enforce=True)
    gate = EnforceBudgetGate()
    real = tracker.read_events
    delay = 0.08

    def slow(*, strict: bool = False) -> object:
        time.sleep(delay)
        return real(strict=strict)

    tracker.read_events = slow  # type: ignore[method-assign]
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
    keys = gate.begin(tracker, SESSION, [budget], _scope())
    assert keys
    gate.release(keys)


def test_tail_append_between_check_and_lock_is_accounted(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    """Spend appended after the outside read is visible via the under-lock tail recheck."""
    tracker = _tracker(tmp_path)
    _seed(tracker, cost=0.50)
    budget = BudgetSpec(backend="opencode", window="week", limit_usd=1.0, enforce=True)
    gate = EnforceBudgetGate()
    real_check = budgets_mod.check_budget

    def check_then_append(*args: object, **kwargs: object) -> object:
        snap = real_check(*args, **kwargs)  # type: ignore[arg-type]
        _seed(tracker, cost=0.60)  # pushes total to $1.10 >= $1 cap
        return snap

    monkeypatch.setattr(budgets_mod, "check_budget", check_then_append)
    with pytest.raises(BudgetExceeded, match=r"\$1\.1000 >= cap \$1\.0000"):
        gate.begin(tracker, SESSION, [budget], _scope())
    assert gate._held == {}


def test_shrunk_ledger_under_enforce_fails_closed(tmp_path: Path) -> None:
    tracker = _tracker(tmp_path)
    _seed(tracker, cost=0.25)
    budget = BudgetSpec(backend="opencode", window="week", limit_usd=100.0, enforce=True)
    snap = check_budget(tracker, SESSION, [budget], _scope())
    assert snap.enforce_spent
    # Truncate in place (same inode, smaller size).
    tracker.events_path.write_bytes(b"")
    with pytest.raises(BudgetExceeded, match="truncated|unreadable"):
        _recheck_enforce_from_tail(tracker, snap, SESSION, [budget], _scope())


def test_rewritten_ledger_under_enforce_fails_closed(tmp_path: Path) -> None:
    tracker = _tracker(tmp_path)
    _seed(tracker, cost=0.25)
    budget = BudgetSpec(backend="opencode", window="week", limit_usd=100.0, enforce=True)
    snap = check_budget(tracker, SESSION, [budget], _scope())
    # Replace the file to force a new inode when possible.
    tracker.events_path.unlink()
    tracker.record(
        UsageEvent(
            ts=datetime.now(timezone.utc).isoformat(),
            run_id="replacement",
            backend="opencode",
            client="worker",
            cost_usd=0.01,
            status="exited_clean",
            source="native",
        )
    )
    with pytest.raises(BudgetExceeded, match="rewritten|unreadable"):
        _recheck_enforce_from_tail(tracker, snap, SESSION, [budget], _scope())


def test_torn_tail_under_enforce_fails_closed(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    """A torn line appended after the baseline read fails closed on the enforce tail path."""
    tracker = _tracker(tmp_path)
    _seed(tracker, cost=0.10)
    budget = BudgetSpec(backend="opencode", window="week", limit_usd=100.0, enforce=True)
    gate = EnforceBudgetGate()
    real_check = budgets_mod.check_budget

    def check_then_tear(*args: object, **kwargs: object) -> object:
        snap = real_check(*args, **kwargs)  # type: ignore[arg-type]
        with tracker.events_path.open("a", encoding="utf-8") as f:
            f.write('{"ts":"x","run_id":"torn","backend":"openco')
        return snap

    monkeypatch.setattr(budgets_mod, "check_budget", check_then_tear)
    with pytest.raises(BudgetExceeded, match="unreadable event"):
        gate.begin(tracker, SESSION, [budget], _scope())
    assert gate._held == {}


def test_check_budget_enforce_only_skips_advisory_warn(
    tmp_path: Path, capsys: pytest.CaptureFixture[str]
) -> None:
    tracker = _tracker(tmp_path)
    _seed(tracker, cost=2.0)
    advisory = BudgetSpec(backend="opencode", window="week", limit_usd=1.0, enforce=False)
    check_budget(tracker, SESSION, [advisory], _scope(), enforce_only=True)
    assert capsys.readouterr().err == ""


def test_begin_optimistic_ledger_scan_outside_lock(tmp_path: Path) -> None:
    """Full read_events of begin() must run while ``_lock`` is free (#145)."""
    tracker = _tracker(tmp_path)
    budget = BudgetSpec(backend="opencode", window="week", limit_usd=100.0, enforce=True)
    gate = EnforceBudgetGate()
    held_during_read: list[bool] = []
    real = tracker.read_events

    def spy(*, strict: bool = False) -> object:
        held_during_read.append(gate._lock.locked())
        return real(strict=strict)

    tracker.read_events = spy  # type: ignore[method-assign]
    keys = gate.begin(tracker, SESSION, [budget], _scope())
    assert keys
    assert held_during_read, "read_events() was never called"
    assert held_during_read[0] is False, "optimistic scan must not hold the gate lock"
    gate.release(keys)


def test_multi_window_one_strict_ledger_read(tmp_path: Path) -> None:
    """Hole A guard: week+month enforce shares ONE read_events; no per-window rescan."""
    tracker = _tracker(tmp_path)
    _seed(tracker, cost=0.40)
    budgets = [
        BudgetSpec(backend="opencode", window="week", limit_usd=1.0, enforce=True),
        BudgetSpec(backend="opencode", window="month", limit_usd=10.0, enforce=True),
    ]
    reads: list[bool] = []
    real = tracker.read_events

    def spy(*, strict: bool = False) -> object:
        reads.append(strict)
        return real(strict=strict)

    tracker.read_events = spy  # type: ignore[method-assign]
    snap = check_budget(tracker, SESSION, budgets, _scope())
    assert reads == [True], f"expected one strict read, got {reads}"
    assert abs(snap.enforce_spent[_enforce_budget_key(budgets[0])] - 0.40) < 1e-9
    assert abs(snap.enforce_spent[_enforce_budget_key(budgets[1])] - 0.40) < 1e-9
    assert snap.cursor.size > 0


def test_multi_window_peer_append_between_reads_refuses(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    """Hole A: peer append after the (single) ledger read must still refuse the week cap.

    Simulates the old multi-summary race by appending on the first summary() return; with a
    single-read design the append lands after the snapshot cursor and the under-lock tail
    recheck counts it for every enforce window.
    """
    tracker = _tracker(tmp_path)
    _seed(tracker, cost=0.40)
    budgets = [
        BudgetSpec(backend="opencode", window="week", limit_usd=1.0, enforce=True),
        BudgetSpec(backend="opencode", window="month", limit_usd=10.0, enforce=True),
    ]
    gate = EnforceBudgetGate()
    real_summary = tracker.summary
    calls = {"n": 0}

    def summary_then_peer_append(*args: object, **kwargs: object) -> object:
        result = real_summary(*args, **kwargs)
        calls["n"] += 1
        if calls["n"] == 1:
            _seed(tracker, cost=0.70)  # peer append mid-check
        return result

    monkeypatch.setattr(tracker, "summary", summary_then_peer_append)
    with pytest.raises(BudgetExceeded, match=r"\$1\.1000 >= cap \$1\.0000"):
        gate.begin(tracker, SESSION, budgets, _scope())
    assert calls["n"] == 1, "multi-window must not re-enter summary per window"
    assert gate._held == {}


def test_multi_window_admit_under_caps(tmp_path: Path) -> None:
    """Normal multi-window path: under both caps → admit; unchanged size → tail is no-op."""
    tracker = _tracker(tmp_path)
    _seed(tracker, cost=0.40)
    budgets = [
        BudgetSpec(backend="opencode", window="week", limit_usd=1.0, enforce=True),
        BudgetSpec(backend="opencode", window="month", limit_usd=10.0, enforce=True),
    ]
    gate = EnforceBudgetGate()
    after_calls: list[int] = []
    real_after = tracker.events_after

    def spy_after(cursor: object, *, strict: bool = False) -> object:
        st = tracker.events_path.stat()
        after_calls.append(st.st_size - cursor.size)  # type: ignore[attr-defined]
        return real_after(cursor, strict=strict)  # type: ignore[arg-type]

    tracker.events_after = spy_after  # type: ignore[method-assign]
    keys = gate.begin(tracker, SESSION, budgets, _scope())
    assert len(keys) == 2
    assert after_calls == [0], "unchanged ledger → events_after reads zero new bytes"
    gate.release(keys)


def test_same_size_inplace_rewrite_fails_closed(tmp_path: Path) -> None:
    """Hole B: same inode + same size + different mtime must not trust the stale snapshot."""
    import os

    tracker = _tracker(tmp_path)
    _seed(tracker, cost=0.25)
    budget = BudgetSpec(backend="opencode", window="week", limit_usd=100.0, enforce=True)
    snap = check_budget(tracker, SESSION, [budget], _scope())
    raw = tracker.events_path.read_bytes()
    # Same-size overwrite (preserves inode on typical filesystems); force mtime forward.
    tracker.events_path.write_bytes(b"X" * len(raw))
    os.utime(tracker.events_path, ns=(snap.cursor.mtime_ns + 1_000_000, snap.cursor.mtime_ns + 1_000_000))
    assert tracker.events_path.stat().st_size == snap.cursor.size
    assert tracker.events_path.stat().st_mtime_ns != snap.cursor.mtime_ns
    with pytest.raises(BudgetExceeded, match="rewritten in place|unreadable"):
        _recheck_enforce_from_tail(tracker, snap, SESSION, [budget], _scope())


def test_same_inode_append_still_fast_paths(tmp_path: Path) -> None:
    """Hole B companion: same-inode growth still parses the tail (not a rewrite)."""
    tracker = _tracker(tmp_path)
    _seed(tracker, cost=0.10)
    budget = BudgetSpec(backend="opencode", window="week", limit_usd=100.0, enforce=True)
    snap = check_budget(tracker, SESSION, [budget], _scope())
    _seed(tracker, cost=0.05)
    # Must not raise — append is the happy path.
    _recheck_enforce_from_tail(tracker, snap, SESSION, [budget], _scope())
