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
    BudgetStatus,
    EnforceBudgetGate,
    _enforce_budget_key,
    _recheck_enforce_from_tail,
    check_budget,
    compute_budget_status,
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
    now = datetime.now(timezone.utc)
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
    import json

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
                        "pid_start_time": "Thu Jan  1 00:00:00 1970",
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
    rows = compute_budget_status(tracker, SESSION, [advisory], datetime.now(timezone.utc))
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

    worker = r"""
import sys
import time
from datetime import datetime, timezone
from pathlib import Path
from types import SimpleNamespace

from marshal_engine.budgets import BudgetExceeded, EnforceBudgetGate
from marshal_engine.config import BudgetSpec
from marshal_engine.usage import UsageTracker

usage = Path(sys.argv[1])
gate_path = Path(sys.argv[2])
out = Path(sys.argv[3])
# Stagger slightly so both processes contend on flock + reservation, not just startup.
time.sleep(float(sys.argv[4]))
tracker = UsageTracker(usage)
budget = BudgetSpec(backend="opencode", window="week", limit_usd=100.0, enforce=True)
gate = EnforceBudgetGate(path=gate_path)
session = datetime(2026, 7, 1, tzinfo=timezone.utc)
req = SimpleNamespace(client="worker", backend_name="opencode")
try:
    keys = gate.begin(tracker, session, [budget], req)
except BudgetExceeded:
    out.write_text("refused", encoding="utf-8")
    sys.exit(0)
# Hold the slot long enough for the peer to observe it.
time.sleep(0.8)
gate.release(keys)
out.write_text("admitted", encoding="utf-8")
"""
    outs = [result_dir / f"p{i}.txt" for i in range(2)]
    procs = [
        subprocess.Popen(
            [sys.executable, "-c", worker, str(usage), str(gate_path), str(outs[i]), str(i * 0.05)],
        )
        for i in range(2)
    ]
    for proc in procs:
        assert proc.wait(timeout=120) == 0

    outcomes = [p.read_text(encoding="utf-8") for p in outs if p.exists()]
    assert outcomes.count("admitted") == 1, outcomes
    assert outcomes.count("refused") == 1, outcomes
