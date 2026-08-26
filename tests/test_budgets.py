"""Unit tests for EnforceBudgetGate + enforce budget fail-closed / reservation hygiene."""

from __future__ import annotations

import contextlib
import json
import os
import threading
import time
from concurrent.futures import ThreadPoolExecutor
from datetime import UTC, datetime
from pathlib import Path
from types import SimpleNamespace

import pytest

import marshal_engine.accounting.budgets as budgets_mod
from marshal_engine.accounting.budgets import _PINNED_IDENTITY_PREFIX as PINNED
from marshal_engine.accounting.budgets import (
    BudgetExceeded,
    BudgetStatus,
    EnforceBudgetGate,
    _enforce_budget_key,
    _recheck_enforce_from_tail,
    check_budget,
    compute_budget_status,
)
from marshal_engine.accounting.usage import (
    UnreadableUsageLedgerError,
    UsageEvent,
    UsageTracker,
)
from marshal_engine.core.config import BudgetSpec


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
            ts=datetime.now(UTC).isoformat(),
            run_id=run_id or f"seed.{backend}.{cost}.{time.time_ns()}",
            backend=backend,
            client=client,
            cost_usd=cost,
            status="exited_clean",
            source="native",
        )
    )


SESSION = datetime(2026, 7, 1, tzinfo=UTC)


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
    # Phrase-bound so "$100.0" / timestamps / paths cannot satisfy the skipped-count claim.
    assert "1 unreadable event" in msg
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
    n = 4
    # Rendezvous instead of a stopwatch: every scan must be inside read_events at the same moment
    # to get past the barrier. Serialized scans never assemble, so the wait breaks. A slow machine
    # only makes assembly slower, never impossible.
    barrier = threading.Barrier(n)

    def slow(*, strict: bool = False) -> object:
        barrier.wait(timeout=30)
        return real(strict=strict)

    tracker.read_events = slow  # type: ignore[method-assign]
    errors: list[BaseException] = []

    def worker() -> None:
        try:
            gate.begin(tracker, SESSION, [budget], _scope())
        except BudgetExceeded as exc:
            errors.append(exc)

    with ThreadPoolExecutor(max_workers=n) as pool:
        list(pool.map(lambda _: worker(), range(n)))

    # `not barrier.broken` is the load-bearing assertion: serialized scans never assemble, the
    # wait times out, and the barrier breaks. Counting refusals is NOT enough - the fail-closed
    # handler turns a BrokenBarrierError into BudgetExceeded, so a serialized run would still
    # produce n errors and look like a pass.
    assert not barrier.broken, "scans serialized under the lock (barrier never assembled)"
    assert len(errors) == n
    assert all("in-flight" not in str(e) and "lookup failed" not in str(e) for e in errors), (
        f"expected over-cap refusals, got {[str(e) for e in errors]}"
    )


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
            ts=datetime.now(UTC).isoformat(),
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


def test_check_budget_enforce_never_reads_last_star_attrs(tmp_path: Path) -> None:
    """Enforce path must consume read_events' return pair — not last_events/last_cursor.

    A concurrent check_budget can interleave between separate attribute loads and pair a
    stale spend baseline with a newer cursor (fail-open). Guard: any last_* *read* raises
    (writes from read_events' stamps still go through ``__setattr__``).
    """

    class _NoLastStarTracker(UsageTracker):
        def __getattribute__(self, name: str) -> object:
            if name in ("last_events", "last_cursor"):
                raise AssertionError(f"check_budget must not read {name}")
            return object.__getattribute__(self, name)

    tracker = _NoLastStarTracker(tmp_path / "usage")
    _seed(tracker, cost=0.40)
    budgets = [
        BudgetSpec(backend="opencode", window="week", limit_usd=1.0, enforce=True),
        BudgetSpec(backend="opencode", window="month", limit_usd=10.0, enforce=True),
    ]
    snap = check_budget(tracker, SESSION, budgets, _scope())
    assert abs(snap.enforce_spent[_enforce_budget_key(budgets[0])] - 0.40) < 1e-9
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


def test_budget_status_lookup_failure_reports_spent_unknown(tmp_path: Path) -> None:
    # Honesty: a summary lookup error means spend is unknown — never claim a known $0.
    tracker = _tracker(tmp_path)

    def boom(*, since=None, until=None, strict=False):  # noqa: ANN001, ARG001
        raise RuntimeError("ledger unreadable")

    tracker.summary = boom  # type: ignore[method-assign]
    now = datetime.now(UTC)
    rows = compute_budget_status(
        tracker, SESSION, [BudgetSpec(window="week", limit_usd=1.0)], now
    )
    assert len(rows) == 1
    assert rows[0].spent_usd == 0.0
    assert rows[0].spent_known is False


def test_budget_status_spent_known_is_serialized() -> None:
    # Machine consumers (MCP / --json) must see spent_known — it is not CLI-only.
    row = BudgetStatus(
        scope="global",
        window="week",
        spent_usd=0.0,
        limit_usd=1.0,
        remaining_usd=1.0,
        enforce=False,
        spent_known=False,
    )
    dumped = row.model_dump(mode="json")
    assert dumped["spent_known"] is False
    assert "spent_known" in row.model_dump_json()


# --- cross-process enforce gate (issue #182) -------------------------------------------------


def test_soft_warn_begin_is_lock_free(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> None:
    """Advisory budgets must not open the reservation flock or write budget_gate.json."""
    tracker = _tracker(tmp_path)
    budget = BudgetSpec(backend="opencode", window="week", limit_usd=100.0, enforce=False)
    gate = EnforceBudgetGate()
    flock_calls: list[object] = []

    def boom_flock(*_a: object, **_k: object) -> None:
        flock_calls.append(True)
        raise AssertionError("advisory begin must not flock")

    monkeypatch.setattr(budgets_mod.fcntl, "flock", boom_flock)
    keys = gate.begin(tracker, SESSION, [budget], _scope())
    assert keys == []
    assert flock_calls == []
    assert not (tmp_path / "budget_gate.json").exists()


def test_release_on_failure_path_frees_slot_for_peer(tmp_path: Path) -> None:
    """release(keys) after a failed _start must free the disk slot, not only success/release_run."""
    tracker = _tracker(tmp_path)
    budget = BudgetSpec(backend="opencode", window="week", limit_usd=100.0, enforce=True)
    gate_a = EnforceBudgetGate()
    keys = gate_a.begin(tracker, SESSION, [budget], _scope())
    assert keys
    # Simulate _start failure before bind (separate gate instance = other "process" view).
    gate_a.release(keys)
    gate_b = EnforceBudgetGate(path=tmp_path / "budget_gate.json")
    keys_b = gate_b.begin(tracker, SESSION, [budget], _scope())
    assert keys_b
    gate_b.release(keys_b)


def test_dead_process_reservation_is_reclaimed(tmp_path: Path) -> None:
    """A reservation whose holder pid is dead must not block forever."""
    tracker = _tracker(tmp_path)
    budget = BudgetSpec(backend="opencode", window="week", limit_usd=100.0, enforce=True)
    path = tmp_path / "budget_gate.json"
    key = _enforce_budget_key(budget)
    path.write_text(
        json.dumps(
            {
                "held": {
                    key: {
                        "run_id": "orphan-run",
                        "pid": 2_000_000_000,  # not a live pid on any sane host
                        "pid_start_time": f"{PINNED}Thu Jan  1 00:00:00 1970",
                        "token": "orphan-token",
                        "reserved_at": time.time(),
                    }
                }
            }
        ),
        encoding="utf-8",
    )
    gate = EnforceBudgetGate(path=path)
    keys = gate.begin(tracker, SESSION, [budget], _scope())
    assert keys == [key]
    gate.release(keys)


def test_bind_failure_with_stuck_release_lock_does_not_poison_cap(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    """Bind fails and release cannot take the flock → later matching spawn still admits.

    Reproduces the self-inflicted lockout: durable unbound placeholder stays on disk with a
    live holder pid after best-effort cleanup gives up; a later begin must reclaim it via the
    unbound TTL (not depend on the failing process finishing cleanup).
    """
    tracker = _tracker(tmp_path)
    budget = BudgetSpec(backend="opencode", window="week", limit_usd=100.0, enforce=True)
    path = tmp_path / "budget_gate.json"
    gate = EnforceBudgetGate(path=path)
    keys = gate.begin(tracker, SESSION, [budget], _scope())
    assert keys

    real_flock = budgets_mod._flock_exclusive

    @contextlib.contextmanager
    def flock_fail(*_a: object, **_k: object):  # noqa: ANN001
        raise BudgetExceeded(
            "budget gate lock timed out after 5.0s (test); refusing spawn because enforce=true"
        )
        yield  # pragma: no cover

    monkeypatch.setattr(budgets_mod, "_flock_exclusive", flock_fail)
    with pytest.raises(BudgetExceeded, match="lock timed out"):
        gate.bind(keys, "run-poison")
    # release clears in-memory slots; disk drop also needs the flock and silently gives up.
    gate.release(keys)
    monkeypatch.setattr(budgets_mod, "_flock_exclusive", real_flock)

    # Poison on disk: empty run_id, this process's live pid (release could not clear it).
    disk = json.loads(path.read_text(encoding="utf-8"))
    entry = disk["held"][keys[0]]
    assert entry.get("run_id") in ("", None)
    assert entry["pid"] == os.getpid()
    # Age the placeholder past the unbound TTL so reclaim does not wait on wall clock.
    entry["reserved_at"] = time.time() - budgets_mod._UNBOUND_RESERVATION_TTL_S - 1.0
    path.write_text(json.dumps(disk), encoding="utf-8")

    gate2 = EnforceBudgetGate(path=path)
    keys2 = gate2.begin(tracker, SESSION, [budget], _scope())
    assert keys2 == keys
    gate2.release(keys2)


def test_fresh_unbound_reservation_still_blocks_peer(tmp_path: Path) -> None:
    """Genuinely in-flight unbound (within TTL, live pid) must still refuse — no fail-open."""
    tracker = _tracker(tmp_path)
    budget = BudgetSpec(backend="opencode", window="week", limit_usd=100.0, enforce=True)
    path = tmp_path / "budget_gate.json"
    key = _enforce_budget_key(budget)
    path.write_text(
        json.dumps(
            {
                "held": {
                    key: {
                        "run_id": "",
                        "pid": os.getpid(),
                        "pid_start_time": budgets_mod._pid_start_time(os.getpid()),
                        "reserved_at": time.time(),
                        "token": "fresh-unbound-token",
                    }
                }
            }
        ),
        encoding="utf-8",
    )
    gate = EnforceBudgetGate(path=path)
    with pytest.raises(BudgetExceeded, match="in-flight"):
        gate.begin(tracker, SESSION, [budget], _scope())
    assert gate._held == {}


def test_bound_reservation_ignores_unbound_ttl(tmp_path: Path) -> None:
    """A bound in-flight run stays held even when reserved_at is ancient (no fail-open)."""
    tracker = _tracker(tmp_path)
    budget = BudgetSpec(backend="opencode", window="week", limit_usd=100.0, enforce=True)
    path = tmp_path / "budget_gate.json"
    key = _enforce_budget_key(budget)
    path.write_text(
        json.dumps(
            {
                "held": {
                    key: {
                        "run_id": "still-running",
                        "pid": os.getpid(),
                        "pid_start_time": budgets_mod._pid_start_time(os.getpid()),
                        "reserved_at": time.time() - 10_000.0,
                        "token": "bound-token",
                    }
                }
            }
        ),
        encoding="utf-8",
    )
    gate = EnforceBudgetGate(path=path)
    with pytest.raises(BudgetExceeded, match="in-flight"):
        gate.begin(tracker, SESSION, [budget], _scope())
    assert gate._held == {}


def test_slow_unbound_holder_does_not_double_admit_after_ttl(
    tmp_path: Path,
) -> None:
    """Live holder past unbound TTL: peer reclaim + bind ownership → at most one admit.

    Drives timing via ``reserved_at`` (no sleep). Gate A keeps an in-memory unbound slot
    while disk ages out; gate B reclaims; A's bind must refuse rather than steal B's entry.
    """
    tracker = _tracker(tmp_path)
    budget = BudgetSpec(backend="opencode", window="week", limit_usd=100.0, enforce=True)
    path = tmp_path / "budget_gate.json"
    gate_a = EnforceBudgetGate(path=path)
    keys_a = gate_a.begin(tracker, SESSION, [budget], _scope())
    assert keys_a

    disk = json.loads(path.read_text(encoding="utf-8"))
    disk["held"][keys_a[0]]["reserved_at"] = (
        time.time() - budgets_mod._UNBOUND_RESERVATION_TTL_S - 1.0
    )
    path.write_text(json.dumps(disk), encoding="utf-8")

    gate_b = EnforceBudgetGate(path=path)
    keys_b = gate_b.begin(tracker, SESSION, [budget], _scope())
    assert keys_b == keys_a

    with pytest.raises(BudgetExceeded, match="reclaimed before bind"):
        gate_a.bind(keys_a, "run-a-slow")
    assert keys_a[0] not in gate_a._held

    gate_b.bind(keys_b, "run-b")
    disk_after = json.loads(path.read_text(encoding="utf-8"))
    assert disk_after["held"][keys_b[0]]["run_id"] == "run-b"
    assert disk_after["held"][keys_b[0]]["pid"] == os.getpid()
    # Cap of one: only B's bound entry remains.
    assert list(disk_after["held"]) == keys_b
    gate_b.release_run("run-b")


def test_bind_refuses_when_slot_held_by_peer(tmp_path: Path) -> None:
    """bind detects a disk entry owned by another holder and refuses (no steal / re-create)."""
    tracker = _tracker(tmp_path)
    budget = BudgetSpec(backend="opencode", window="week", limit_usd=100.0, enforce=True)
    path = tmp_path / "budget_gate.json"
    key = _enforce_budget_key(budget)
    gate = EnforceBudgetGate(path=path)
    keys = gate.begin(tracker, SESSION, [budget], _scope())
    assert keys == [key]

    # Peer replaced our unbound placeholder while we still believe we hold it in memory.
    path.write_text(
        json.dumps(
            {
                "held": {
                    key: {
                        "run_id": "peer-run",
                        "pid": 2_000_000_000,
                        "pid_start_time": f"{PINNED}Thu Jan  1 00:00:00 1970",
                        "reserved_at": time.time(),
                        "token": "peer-token",
                    }
                }
            }
        ),
        encoding="utf-8",
    )
    with pytest.raises(BudgetExceeded, match="reclaimed before bind") as ei:
        gate.bind(keys, "run-late")
    assert "peer-run" in str(ei.value)
    assert key not in gate._held
    disk = json.loads(path.read_text(encoding="utf-8"))
    assert disk["held"][key]["run_id"] == "peer-run"


def _live_peer_entry(run_id: str, token: str) -> dict[str, object]:
    pid = os.getpid()
    return {
        "run_id": run_id,
        "pid": pid,
        "pid_start_time": budgets_mod._pid_start_time(pid),
        "reserved_at": time.time(),
        "token": token,
    }


def test_bind_loss_release_does_not_delete_peer_reservation(tmp_path: Path) -> None:
    """Fleet's release after bind loses the slot must not reopen a peer's cap.

    Mirror of the lockout bug: cleanup must not remove a disk entry it no longer owns.
    Cap stays closed — a third admit is refused while the peer holds.
    """
    tracker = _tracker(tmp_path)
    budget = BudgetSpec(backend="opencode", window="week", limit_usd=100.0, enforce=True)
    path = tmp_path / "budget_gate.json"
    key = _enforce_budget_key(budget)
    gate_a = EnforceBudgetGate(path=path)
    keys_a = gate_a.begin(tracker, SESSION, [budget], _scope())
    assert keys_a

    disk = json.loads(path.read_text(encoding="utf-8"))
    disk["held"][key]["reserved_at"] = (
        time.time() - budgets_mod._UNBOUND_RESERVATION_TTL_S - 1.0
    )
    path.write_text(json.dumps(disk), encoding="utf-8")

    gate_b = EnforceBudgetGate(path=path)
    keys_b = gate_b.begin(tracker, SESSION, [budget], _scope())
    assert keys_b == keys_a
    gate_b.bind(keys_b, "run-b")
    peer_token = json.loads(path.read_text(encoding="utf-8"))["held"][key]["token"]

    with pytest.raises(BudgetExceeded, match="reclaimed before bind"):
        gate_a.bind(keys_a, "run-a")
    # Same failure path as Fleet._start: release after bind ownership loss.
    gate_a.release(keys_a)

    disk_after = json.loads(path.read_text(encoding="utf-8"))
    assert disk_after["held"][key]["run_id"] == "run-b"
    assert disk_after["held"][key]["token"] == peer_token

    gate_c = EnforceBudgetGate(path=path)
    with pytest.raises(BudgetExceeded, match="in-flight"):
        gate_c.begin(tracker, SESSION, [budget], _scope())
    gate_b.release_run("run-b")


def test_release_stale_token_does_not_delete_peer_reservation(tmp_path: Path) -> None:
    """release with a stale local token must leave a peer's disk entry intact."""
    tracker = _tracker(tmp_path)
    budget = BudgetSpec(backend="opencode", window="week", limit_usd=100.0, enforce=True)
    path = tmp_path / "budget_gate.json"
    key = _enforce_budget_key(budget)
    gate = EnforceBudgetGate(path=path)
    keys = gate.begin(tracker, SESSION, [budget], _scope())
    assert keys
    assert key in gate._tokens

    path.write_text(
        json.dumps({"held": {key: _live_peer_entry("peer-run", "peer-token")}}),
        encoding="utf-8",
    )
    gate.release(keys)
    disk = json.loads(path.read_text(encoding="utf-8"))
    assert disk["held"][key]["token"] == "peer-token"
    assert disk["held"][key]["run_id"] == "peer-run"
    assert gate._held == {}

    with pytest.raises(BudgetExceeded, match="in-flight"):
        EnforceBudgetGate(path=path).begin(tracker, SESSION, [budget], _scope())


def test_release_still_frees_own_disk_slot(tmp_path: Path) -> None:
    """Ownership check must not over-tighten — a holder can still free its own slot."""
    tracker = _tracker(tmp_path)
    budget = BudgetSpec(backend="opencode", window="week", limit_usd=100.0, enforce=True)
    path = tmp_path / "budget_gate.json"
    gate = EnforceBudgetGate(path=path)
    keys = gate.begin(tracker, SESSION, [budget], _scope())
    assert keys
    token = gate._tokens[keys[0]]
    disk_before = json.loads(path.read_text(encoding="utf-8"))
    assert disk_before["held"][keys[0]]["token"] == token

    gate.release(keys)
    disk_after = json.loads(path.read_text(encoding="utf-8"))
    assert keys[0] not in disk_after.get("held", {})
    assert gate._held == {}

    # Slot is free for a peer.
    gate_b = EnforceBudgetGate(path=path)
    keys_b = gate_b.begin(tracker, SESSION, [budget], _scope())
    assert keys_b == keys
    gate_b.release(keys_b)


def test_release_run_does_not_delete_peer_reservation(tmp_path: Path) -> None:
    """release_run must only remove disk entries whose token still matches ours."""
    tracker = _tracker(tmp_path)
    budget = BudgetSpec(backend="opencode", window="week", limit_usd=100.0, enforce=True)
    path = tmp_path / "budget_gate.json"
    key = _enforce_budget_key(budget)
    gate = EnforceBudgetGate(path=path)
    keys = gate.begin(tracker, SESSION, [budget], _scope())
    gate.bind(keys, "run-a")

    path.write_text(
        json.dumps({"held": {key: _live_peer_entry("peer-run", "peer-token")}}),
        encoding="utf-8",
    )
    gate.release_run("run-a")
    disk = json.loads(path.read_text(encoding="utf-8"))
    assert disk["held"][key]["token"] == "peer-token"
    assert disk["held"][key]["run_id"] == "peer-run"
    assert gate._held == {}

    with pytest.raises(BudgetExceeded, match="in-flight"):
        EnforceBudgetGate(path=path).begin(tracker, SESSION, [budget], _scope())


def test_begin_rollback_does_not_delete_peer_owned_entry(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    """Partial-failure rollback drops only entries still carrying our begin token."""
    tracker = _tracker(tmp_path)
    budget = BudgetSpec(backend="opencode", window="week", limit_usd=100.0, enforce=True)
    path = tmp_path / "budget_gate.json"
    key = _enforce_budget_key(budget)
    gate = EnforceBudgetGate(path=path)
    real_write = budgets_mod._write_reservations
    calls = {"n": 0}

    def write_swap_peer_then_fail_once(
        p: Path, held: dict[str, dict[str, object]]
    ) -> None:
        calls["n"] += 1
        if calls["n"] == 1:
            # Simulate the in-memory slot being taken over before the write lands.
            held[key] = _live_peer_entry("peer-run", "peer-token")
            raise OSError("simulated reservation write failure")
        real_write(p, held)

    monkeypatch.setattr(budgets_mod, "_write_reservations", write_swap_peer_then_fail_once)
    with pytest.raises(BudgetExceeded, match="reservation write failed"):
        gate.begin(tracker, SESSION, [budget], _scope())
    assert gate._held == {}
    # Rollback write (2nd call) persisted the peer entry — we must not have popped it.
    assert calls["n"] == 2
    disk = json.loads(path.read_text(encoding="utf-8"))
    assert disk["held"][key]["token"] == "peer-token"

    with pytest.raises(BudgetExceeded, match="in-flight"):
        EnforceBudgetGate(path=path).begin(tracker, SESSION, [budget], _scope())


def test_renew_keeps_unbound_slot_past_wall_ttl(tmp_path: Path) -> None:
    """renew bumps reserved_at so a live unbound holder is not treated as abandoned."""
    tracker = _tracker(tmp_path)
    budget = BudgetSpec(backend="opencode", window="week", limit_usd=100.0, enforce=True)
    path = tmp_path / "budget_gate.json"
    gate = EnforceBudgetGate(path=path)
    keys = gate.begin(tracker, SESSION, [budget], _scope())
    assert keys

    disk = json.loads(path.read_text(encoding="utf-8"))
    disk["held"][keys[0]]["reserved_at"] = (
        time.time() - budgets_mod._UNBOUND_RESERVATION_TTL_S - 1.0
    )
    path.write_text(json.dumps(disk), encoding="utf-8")

    gate.renew(keys)
    disk_after = json.loads(path.read_text(encoding="utf-8"))
    assert (
        time.time() - float(disk_after["held"][keys[0]]["reserved_at"])
        < budgets_mod._UNBOUND_RESERVATION_TTL_S
    )

    peer = EnforceBudgetGate(path=path)
    with pytest.raises(BudgetExceeded, match="in-flight"):
        peer.begin(tracker, SESSION, [budget], _scope())
    gate.release(keys)


def test_malformed_held_entry_enforce_refuses(tmp_path: Path) -> None:
    """Present but unparseable held entry → enforce refuses (same as unreadable file)."""
    tracker = _tracker(tmp_path)
    budget = BudgetSpec(backend="opencode", window="week", limit_usd=100.0, enforce=True)
    path = tmp_path / "budget_gate.json"
    key = _enforce_budget_key(budget)
    path.write_text(
        json.dumps({"held": {key: "not-an-object"}}),
        encoding="utf-8",
    )
    gate = EnforceBudgetGate(path=path)
    with pytest.raises(BudgetExceeded, match="reservation file unreadable") as ei:
        gate.begin(tracker, SESSION, [budget], _scope())
    msg = str(ei.value)
    assert str(path) in msg
    assert "Delete the file" in msg
    assert gate._held == {}


def test_malformed_held_entry_missing_pid_enforce_refuses(tmp_path: Path) -> None:
    """Truncated held object (no pid) is unknown state under enforce — not silent reclaim."""
    tracker = _tracker(tmp_path)
    budget = BudgetSpec(backend="opencode", window="week", limit_usd=100.0, enforce=True)
    path = tmp_path / "budget_gate.json"
    key = _enforce_budget_key(budget)
    path.write_text(
        json.dumps({"held": {key: {"run_id": "", "reserved_at": time.time()}}}),
        encoding="utf-8",
    )
    gate = EnforceBudgetGate(path=path)
    with pytest.raises(BudgetExceeded, match="reservation file unreadable"):
        gate.begin(tracker, SESSION, [budget], _scope())
    assert gate._held == {}


def test_malformed_held_entry_reporting_stays_lenient(tmp_path: Path) -> None:
    """Advisory begin + compute_budget_status ignore a malformed held entry (never load it)."""
    tracker = _tracker(tmp_path)
    path = tmp_path / "budget_gate.json"
    path.write_text(
        json.dumps({"held": {"global|week|100.0": "not-an-object"}}),
        encoding="utf-8",
    )
    advisory = BudgetSpec(backend="opencode", window="week", limit_usd=100.0, enforce=False)
    gate = EnforceBudgetGate(path=path)
    keys = gate.begin(tracker, SESSION, [advisory], _scope())
    assert keys == []
    rows = compute_budget_status(tracker, SESSION, [advisory], datetime.now(UTC))
    assert len(rows) == 1
    assert rows[0].spent_known is True


def test_corrupt_reservation_file_enforce_refuses(tmp_path: Path) -> None:
    """Present-but-garbage budget_gate.json must fail closed under enforce (not admit)."""
    tracker = _tracker(tmp_path)
    budget = BudgetSpec(backend="opencode", window="week", limit_usd=100.0, enforce=True)
    path = tmp_path / "budget_gate.json"
    path.write_text("{this is not valid json@@@@", encoding="utf-8")
    gate = EnforceBudgetGate(path=path)
    with pytest.raises(BudgetExceeded, match="reservation file unreadable") as ei:
        gate.begin(tracker, SESSION, [budget], _scope())
    msg = str(ei.value)
    assert str(path) in msg
    assert "Delete the file" in msg
    assert gate._held == {}


def test_absent_reservation_file_still_admits(tmp_path: Path) -> None:
    """No reservation file means nobody holds a slot — admit as before."""
    tracker = _tracker(tmp_path)
    budget = BudgetSpec(backend="opencode", window="week", limit_usd=100.0, enforce=True)
    path = tmp_path / "budget_gate.json"
    assert not path.exists()
    gate = EnforceBudgetGate(path=path)
    keys = gate.begin(tracker, SESSION, [budget], _scope())
    assert keys == [_enforce_budget_key(budget)]
    gate.release(keys)


def test_corrupt_reservation_file_soft_warn_and_reporting_stay_lenient(
    tmp_path: Path,
) -> None:
    """Advisory begin + compute_budget_status must not raise on a corrupt gate file."""
    tracker = _tracker(tmp_path)
    path = tmp_path / "budget_gate.json"
    path.write_text("{this is not valid json@@@@", encoding="utf-8")
    advisory = BudgetSpec(backend="opencode", window="week", limit_usd=100.0, enforce=False)
    gate = EnforceBudgetGate(path=path)
    keys = gate.begin(tracker, SESSION, [advisory], _scope())
    assert keys == []
    rows = compute_budget_status(tracker, SESSION, [advisory], datetime.now(UTC))
    assert len(rows) == 1
    assert rows[0].spent_known is True


def test_cross_process_enforce_admits_at_most_one(tmp_path: Path) -> None:
    """Two OS processes racing the same enforce cap: total admitted must not exceed one.

    Follows ``tests/test_state.py`` cross-process flock pattern (real subprocesses).
    """
    import subprocess
    import sys

    usage = tmp_path / "usage"
    usage.mkdir()
    gate_path = tmp_path / "budget_gate.json"
    result_dir = tmp_path / "results"
    result_dir.mkdir()

    # Filesystem rendezvous, not sleeps: the holder announces its slot and waits to be told to
    # release, so the peer's attempt provably happens WHILE the slot is held. Sleep-based staggering
    # is a race - interpreter startup under load easily exceeds any stagger, and then neither
    # process contends and the assertion means nothing.
    worker = r"""
import sys
import time
from datetime import datetime, timezone
from pathlib import Path
from types import SimpleNamespace

from marshal_engine.accounting.budgets import BudgetExceeded, EnforceBudgetGate
from marshal_engine.core.config import BudgetSpec
from marshal_engine.accounting.usage import UsageTracker

usage = Path(sys.argv[1])
gate_path = Path(sys.argv[2])
out = Path(sys.argv[3])
role = sys.argv[4]                 # "holder" | "peer"
held = gate_path.parent / "held"   # holder announces here
go = gate_path.parent / "go"       # test releases the holder here

def wait_for(path, timeout=60.0):
    deadline = time.monotonic() + timeout
    while time.monotonic() < deadline:
        if path.exists():
            return True
        time.sleep(0.01)
    return False

tracker = UsageTracker(usage)
budget = BudgetSpec(backend="opencode", window="week", limit_usd=100.0, enforce=True)
gate = EnforceBudgetGate(path=gate_path)
session = datetime(2026, 7, 1, tzinfo=timezone.utc)
req = SimpleNamespace(client="worker", backend_name="opencode")

if role == "peer" and not wait_for(held):
    out.write_text("holder-never-started", encoding="utf-8")
    sys.exit(0)

try:
    keys = gate.begin(tracker, session, [budget], req)
except BudgetExceeded:
    out.write_text("refused", encoding="utf-8")
    sys.exit(0)

if role == "holder":
    held.write_text("1", encoding="utf-8")
    wait_for(go)                   # hold until the peer has had its turn
gate.release(keys)
out.write_text("admitted", encoding="utf-8")
"""
    outs = [result_dir / f"p{i}.txt" for i in range(2)]
    roles = ["holder", "peer"]
    procs = [
        subprocess.Popen(
            [sys.executable, "-c", worker, str(usage), str(gate_path), str(outs[i]), roles[i]],
        )
        for i in range(2)
    ]
    # Let the peer finish contending, then release the holder.
    deadline = time.monotonic() + 60
    while time.monotonic() < deadline and not outs[1].exists():
        time.sleep(0.01)
    (tmp_path / "go").write_text("1", encoding="utf-8")
    for proc in procs:
        assert proc.wait(timeout=120) == 0

    outcomes = [p.read_text(encoding="utf-8") for p in outs if p.exists()]
    assert outcomes.count("admitted") == 1, outcomes
    assert outcomes.count("refused") == 1, outcomes


# --- two limits: dollars govern measured spend, runs govern what dollars cannot see -------------


def _seed_unmeasured(tracker: UsageTracker, *, client: str | None = "worker", n: int = 1) -> None:
    """Runs a subscription backend produced: real work, no cost anyone reported."""
    for i in range(n):
        tracker.record(
            UsageEvent(
                ts=datetime.now(UTC).isoformat(),
                run_id=f"unmeasured.{i}.{time.time_ns()}",
                backend="cursor",
                client=client,
                cost_usd=0.0,
                status="exited_clean",
                source="unavailable",
            )
        )


def test_a_dollar_cap_alone_does_not_govern_unmeasurable_runs(tmp_path: Path) -> None:
    """The hole the second limit exists to close.

    A subscription client reports no cost, so a dollar cap sees $0 spent however many runs it
    burns - "within budget" is then a statement about what Marshal can measure, not about what was
    consumed. The dollar cap is not made to guess a price; a run cap is added that governs exactly
    the runs the dollar cap cannot see.
    """
    tracker = _tracker(tmp_path)
    _seed_unmeasured(tracker, n=25)
    usd_only = BudgetSpec(window="month", limit_usd=10.0, enforce=True)

    # Not refused: no measured spend exists to exceed the cap, and inventing one would be a lie.
    check_budget(tracker, SESSION, [usd_only], _scope(client="worker"))

    status = compute_budget_status(tracker, SESSION, [usd_only], datetime.now(UTC))[0]
    assert status.spent_usd == 0.0
    assert status.runs_unmeasured == 0  # not counted: this budget declares no run cap to count for


def test_a_run_cap_governs_runs_whose_cost_was_never_measured(tmp_path: Path) -> None:
    tracker = _tracker(tmp_path)
    _seed_unmeasured(tracker, n=3)
    budget = BudgetSpec(window="month", limit_runs=3, enforce=True)

    with pytest.raises(BudgetExceeded) as exc:
        check_budget(tracker, SESSION, [budget], _scope(client="worker"))

    message = str(exc.value)
    assert "unmeasured cost" in message
    assert "limit_runs" in message  # names the knob, like the dollar path does


def test_each_limit_only_counts_what_it_can_see(tmp_path: Path) -> None:
    """The two limits partition the window's runs; neither stands in for the other."""
    tracker = _tracker(tmp_path)
    _seed(tracker, cost=2.0)          # measured
    _seed(tracker, cost=3.0)          # measured
    _seed_unmeasured(tracker, n=4)    # unmeasured
    budget = BudgetSpec(window="month", limit_usd=100.0, limit_runs=100)

    status = compute_budget_status(tracker, SESSION, [budget], datetime.now(UTC))[0]

    assert status.spent_usd == pytest.approx(5.0)   # only the priced runs
    assert status.runs_unmeasured == 4              # only the unpriced ones
    assert status.remaining_usd == pytest.approx(95.0)
    assert status.remaining_runs == 96


def test_a_run_cap_is_not_tripped_by_measured_runs(tmp_path: Path) -> None:
    """A measured run is already governed by the dollar cap; counting it twice would double-charge."""
    tracker = _tracker(tmp_path)
    _seed(tracker, cost=0.01)
    _seed(tracker, cost=0.01)
    _seed(tracker, cost=0.01)
    budget = BudgetSpec(window="month", limit_runs=2, enforce=True)

    check_budget(tracker, SESSION, [budget], _scope(client="worker"))  # must not raise


def test_a_budget_that_caps_nothing_is_refused_at_config_load() -> None:
    """A budget with neither limit would sit in `usage` looking like a control that is in force."""
    from marshal_engine.core.config import ConfigError, _parse_budgets

    with pytest.raises(ConfigError, match="must set limit_usd, limit_runs, or both"):
        _parse_budgets([{"window": "month"}])


def test_two_budgets_differing_only_in_run_cap_get_separate_slots() -> None:
    """Enforced budgets reserve a concurrency slot by key; the key must tell them apart.

    Two budgets identical in scope, window and dollar cap but with different `limit_runs` are
    different controls. Sharing a slot would let one's in-flight spawn make the other look already
    held, refusing a run that was admissible under it.
    """
    from marshal_engine.accounting.budgets import _enforce_budget_key

    a = BudgetSpec(window="month", limit_usd=10.0, limit_runs=5, enforce=True)
    b = BudgetSpec(window="month", limit_usd=10.0, limit_runs=50, enforce=True)

    assert _enforce_budget_key(a) != _enforce_budget_key(b)


def test_a_runs_only_budget_renders_without_a_dollar_limit() -> None:
    """The human `usage` table must not assume every budget has a dollar cap.

    A runs-only budget is the normal shape for a subscription fleet - the case this whole feature
    exists for - so formatting it must not be the thing that crashes the display.
    """
    from marshal_engine.interfaces.cli.formatting import _print_budget_table

    status = BudgetStatus(
        scope="backend:cursor", window="week", spent_usd=0.0,
        limit_usd=None, remaining_usd=None,
        runs_unmeasured=7, limit_runs=50, remaining_runs=43,
        enforce=True, spent_known=True,
    )

    _print_budget_table([status])  # must not raise


def test_the_example_config_budgets_actually_load() -> None:
    """The shipped example is the first thing a user copies; a bad window there fails at load."""
    import yaml

    from marshal_engine.core.config import _parse_budgets

    text = Path("fleet.config.example.yaml").read_text(encoding="utf-8")
    lines = text.splitlines()
    start = next(i for i, line in enumerate(lines) if line.startswith("# budgets:"))
    block = [lines[start][2:]]
    for line in lines[start + 1:]:
        if not line.startswith("#   "):
            break                       # end of the budgets block; other commented sections follow
        block.append(line[2:])

    parsed = yaml.safe_load("\n".join(block))
    _parse_budgets(parsed["budgets"])  # raises ConfigError on a bad window / missing limits
# --- #164: an unreadable ledger is not an empty one ---------------------------------------------


def test_an_unstattable_ledger_does_not_read_as_no_spend(tmp_path: Path, monkeypatch) -> None:
    """The fail-open that mattered: `exists()` said False for EACCES as well as for "not yet".

    An enforced budget then saw $0 spent and admitted the spawn - the cap failing open in the one
    mode whose whole job is to refuse. Only "no ledger written yet" may read as empty.
    """
    tracker = _tracker(tmp_path)
    _seed(tracker, cost=99.0)

    real_stat = Path.stat

    def denied(self, *a, **kw):
        if self.name == "events.jsonl":
            raise PermissionError(13, "Permission denied")
        return real_stat(self, *a, **kw)

    monkeypatch.setattr(Path, "stat", denied)

    with pytest.raises(PermissionError):
        tracker.read_events(strict=True)


def test_a_cold_ledger_still_reads_as_empty(tmp_path: Path) -> None:
    """The legitimate case must survive the fix, or every first run fails closed on nothing."""
    tracker = _tracker(tmp_path)
    events, cursor = tracker.read_events(strict=True)
    assert events == []
    assert cursor.size == 0


def test_a_tear_inside_a_multibyte_character_still_reports(tmp_path: Path) -> None:
    """Torn-line tolerance held for ASCII ledgers only.

    A `client` name can be non-ASCII and is not escaped on write, so a crash can tear the final
    line INSIDE a character. Decoding the whole file strictly made that a hard failure for every
    reader, including `marshal usage`, which is supposed to degrade rather than die.
    """
    tracker = _tracker(tmp_path)
    _seed(tracker, cost=1.0, client="wörker")
    with tracker.events_path.open("ab") as f:
        # The tear has to land INSIDE a character to exercise this: "ö" is two bytes in UTF-8, so
        # keeping only its first byte leaves a sequence no strict decode can read.
        f.write('{"client": "wö'.encode()[:-1])

    events = tracker.events(strict=False)  # must not raise
    assert len(events) == 1
    assert events[0].cost_usd == pytest.approx(1.0)


def test_enforce_refuses_a_ledger_it_cannot_decode(tmp_path: Path) -> None:
    """Reporting may patch over a tear; enforce may not - a doubtful ledger caps nothing."""
    tracker = _tracker(tmp_path)
    _seed(tracker, cost=1.0, client="wörker")
    with tracker.events_path.open("ab") as f:
        f.write('{"client": "wö'.encode()[:-1])

    with pytest.raises((UnicodeDecodeError, UnreadableUsageLedgerError)):
        tracker.read_events(strict=True)


def _budgets_probe_in_a_child(pid: int, **env_overrides: str) -> str:
    """``budgets._pid_start_time(pid)`` as rendered by a SEPARATE process with its own env."""
    import subprocess
    import sys

    proc = subprocess.run(
        [
            sys.executable,
            "-c",
            (
                "import sys\n"
                "from marshal_engine.accounting.budgets import _pid_start_time\n"
                "print(_pid_start_time(int(sys.argv[1])))\n"
            ),
            str(pid),
        ],
        capture_output=True,
        text=True,
        env={**os.environ, **env_overrides},
        check=True,
    )
    return proc.stdout.strip()


def test_the_reservation_probe_reads_one_identity_across_timezones() -> None:
    """`accounting` cannot import `orchestration`, so the pid identity probe exists twice. The
    copies are never compared against each other, which is exactly why one can be fixed and the
    other left behind - and a reservation holder that reads as a stranger lets an enforce-mode cap
    admit past a live one. Same property, asserted separately, because the marker alone does not
    prove the environment was pinned."""
    utc = _budgets_probe_in_a_child(os.getpid(), TZ="UTC", LC_ALL="C")
    kolkata = _budgets_probe_in_a_child(os.getpid(), TZ="Asia/Kolkata", LC_ALL="C")

    assert utc not in ("", "None"), "the probe could not read a live pid at all"
    assert utc.startswith(PINNED)
    assert utc == kolkata, "the same live pid rendered as two identities"
