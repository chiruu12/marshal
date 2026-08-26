"""Tests for the usage tracker (file IO + aggregation; deterministic, no network)."""

from __future__ import annotations

from concurrent.futures import ThreadPoolExecutor
from datetime import UTC, datetime, timedelta
from pathlib import Path
from typing import Any

import pytest

from marshal_engine import AgentResult, RunStatus, UsageRecord, UsageSource
from marshal_engine.accounting.usage import (
    USAGE_WINDOWS,
    UsageEvent,
    UsageTracker,
    usage_window_since,
)


def _ev(**kw: Any) -> UsageEvent:
    base: dict[str, Any] = {"ts": "2026-06-19T00:00:00Z", "run_id": "r", "backend": "opencode"}
    base.update(kw)
    return UsageEvent(**base)


def test_record_appends_and_summarizes(tmp_path: Path) -> None:
    t = UsageTracker(tmp_path / "usage")
    t.record(_ev(run_id="r1", backend="opencode", cost_usd=0.01, input_tokens=100, output_tokens=10))
    t.record(_ev(run_id="r2", backend="cursor", cost_usd=0.0, source="unavailable"))
    t.record(_ev(run_id="r3", backend="opencode", cost_usd=0.02, input_tokens=200, output_tokens=20))

    assert t.events_path.exists()
    assert len(t.events()) == 3

    s = t.summary()
    assert s.totals.runs == 3
    assert abs(s.totals.cost_usd - 0.03) < 1e-9
    assert s.by_backend["opencode"].runs == 2
    assert abs(s.by_backend["opencode"].cost_usd - 0.03) < 1e-9
    assert s.by_backend["cursor"].runs == 1
    assert s.by_backend["opencode"].input_tokens == 300


def test_from_result_builds_event() -> None:
    res = AgentResult(
        status=RunStatus.EXITED_CLEAN,
        usage=UsageRecord(
            backend="opencode",
            input_tokens=50,
            output_tokens=5,
            cost_usd=0.005,
            source=UsageSource.NATIVE,
        ),
    )
    ev = UsageEvent.from_result(
        res, run_id="r1", backend="opencode", ts="2026-06-19T00:00:00Z", model="opencode-go/glm-5.2"
    )
    assert ev.backend == "opencode"
    assert ev.input_tokens == 50
    assert ev.cost_usd == 0.005
    assert ev.status == "exited_clean"
    assert ev.source == "native"
    assert ev.model == "opencode-go/glm-5.2"


def test_from_result_carries_cache_write_tokens_to_summary(tmp_path: Path) -> None:
    """Stamped cache_write_tokens must survive UsageEvent → events.jsonl → Bucket rollup."""
    res = AgentResult(
        status=RunStatus.EXITED_CLEAN,
        usage=UsageRecord(
            backend="cursor",
            input_tokens=100,
            output_tokens=10,
            cache_read_tokens=40,
            cache_write_tokens=8,
            source=UsageSource.UNAVAILABLE,
        ),
    )
    ev = UsageEvent.from_result(
        res, run_id="cw1", backend="cursor", ts="2026-07-30T00:00:00Z"
    )
    assert ev.cache_read_tokens == 40
    assert ev.cache_write_tokens == 8

    t = UsageTracker(tmp_path / "usage")
    t.record(ev)
    tot = t.summary().totals
    assert tot.cache_read_tokens == 40
    assert tot.cache_write_tokens == 8
    assert t.summary().by_backend["cursor"].cache_write_tokens == 8


def test_old_ledger_lines_without_cache_write_still_load(tmp_path: Path) -> None:
    """Additive field: pre-field events.jsonl lines omit cache_write_tokens and must still parse."""
    events = tmp_path / "usage" / "events.jsonl"
    events.parent.mkdir(parents=True)
    events.write_text(
        '{"ts":"2026-01-01T00:00:00Z","run_id":"old1","backend":"cursor",'
        '"input_tokens":10,"output_tokens":2,"cache_read_tokens":5,'
        '"cost_usd":0.0,"status":"exited_clean","source":"unavailable"}\n'
    )
    loaded = UsageTracker(tmp_path / "usage").events()
    assert len(loaded) == 1
    assert loaded[0].cache_read_tokens == 5
    assert loaded[0].cache_write_tokens == 0  # default for missing field
    assert UsageTracker(tmp_path / "usage").summary().totals.cache_write_tokens == 0


def test_mixed_old_and_new_ledger_summarizes_cache_write(tmp_path: Path) -> None:
    events = tmp_path / "usage" / "events.jsonl"
    events.parent.mkdir(parents=True)
    events.write_text(
        '{"ts":"2026-01-01T00:00:00Z","run_id":"old1","backend":"cursor",'
        '"input_tokens":10,"output_tokens":1,"cache_read_tokens":3,'
        '"cost_usd":0.0,"status":"exited_clean","source":"unavailable"}\n'
        '{"ts":"2026-07-30T00:00:00Z","run_id":"new1","backend":"cursor",'
        '"input_tokens":20,"output_tokens":2,"cache_read_tokens":7,'
        '"cache_write_tokens":9,"cost_usd":0.0,"status":"exited_clean",'
        '"source":"unavailable"}\n'
    )
    tot = UsageTracker(tmp_path / "usage").summary().totals
    assert tot.runs == 2
    assert tot.cache_read_tokens == 10
    assert tot.cache_write_tokens == 9  # old line contributes 0; new contributes 9


def test_concurrent_records_do_not_corrupt_the_log(tmp_path: Path) -> None:
    # Parallel runs each append their own line; the append-only log must not lose or tear records.
    t = UsageTracker(tmp_path / "usage")

    def rec(i: int) -> None:
        t.record(_ev(run_id=f"r{i}", backend="opencode", cost_usd=0.001))

    with ThreadPoolExecutor(max_workers=8) as pool:
        list(pool.map(rec, range(60)))

    assert len(t.events()) == 60
    assert t.summary().totals.runs == 60


def test_empty_tracker(tmp_path: Path) -> None:
    t = UsageTracker(tmp_path / "usage")
    assert t.events() == []
    assert t.summary().totals.runs == 0
    assert t.summary().totals.cost_per_succeeded is None  # no successes -> not claimable


def test_cost_per_outcome_and_source_split(tmp_path: Path) -> None:
    t = UsageTracker(tmp_path / "usage")
    t.record(_ev(run_id="r1", cost_usd=0.02, status="exited_clean", source="native"))
    t.record(_ev(run_id="r2", cost_usd=0.04, status="exited_clean", source="estimated"))
    t.record(_ev(run_id="r3", cost_usd=0.00, status="empty", source="unavailable"))  # cost, no success

    tot = t.summary().totals
    assert tot.runs == 3
    assert tot.succeeded == 2
    assert abs(tot.cost_usd - 0.06) < 1e-9
    assert abs(tot.cost_native - 0.02) < 1e-9
    assert abs(tot.cost_estimated - 0.04) < 1e-9  # legacy ledger spend still attributed
    assert tot.priced_runs == 2          # native + legacy estimated both count as priced
    assert abs(tot.cost_per_run - 0.02) < 1e-9       # 0.06 / 3
    assert abs(tot.cost_per_succeeded - 0.03) < 1e-9  # 0.06 / 2 (failures/empties still cost)


def test_admin_api_cost_has_its_own_bucket(tmp_path: Path) -> None:
    # Regression: a real provider admin-api cost (EastRouter) is its own ground-truth bucket and the
    # source buckets sum to the total - including a LEGACY "estimated" line, whose spend must still
    # be attributed or the split stops accounting for cost_usd.
    t = UsageTracker(tmp_path / "usage")
    t.record(_ev(run_id="r1", cost_usd=0.01, status="exited_clean", source="native"))
    t.record(_ev(run_id="r2", cost_usd=0.02, status="exited_clean", source="admin-api"))
    t.record(_ev(run_id="r3", cost_usd=0.04, status="exited_clean", source="estimated"))
    tot = t.summary().totals
    assert abs(tot.cost_admin_api - 0.02) < 1e-9
    assert abs((tot.cost_native + tot.cost_admin_api + tot.cost_estimated) - tot.cost_usd) < 1e-9


def test_historical_estimated_ledger_still_loads_and_counts_priced(tmp_path: Path) -> None:
    events = tmp_path / "usage" / "events.jsonl"
    events.parent.mkdir(parents=True)
    events.write_text(
        '{"ts":"2026-01-01T00:00:00Z","run_id":"h1","backend":"codex","cost_usd":0.05,'
        '"status":"exited_clean","source":"estimated","input_tokens":1000,"output_tokens":200}\n'
    )
    tot = UsageTracker(tmp_path / "usage").summary().totals
    assert tot.runs == 1
    assert abs(tot.cost_usd - 0.05) < 1e-9
    assert abs(tot.cost_estimated - 0.05) < 1e-9
    assert abs((tot.cost_native + tot.cost_admin_api + tot.cost_estimated) - tot.cost_usd) < 1e-9
    assert tot.priced_runs == 1
    assert tot.input_tokens == 1000
    assert tot.output_tokens == 200


def test_empty_run_with_cost_inflates_cost_per_succeeded(tmp_path: Path) -> None:
    t = UsageTracker(tmp_path / "usage")
    t.record(_ev(run_id="s", cost_usd=0.02, status="exited_clean", source="native"))
    t.record(_ev(run_id="e", cost_usd=0.03, status="empty", source="estimated"))  # burned tokens, no success

    tot = t.summary().totals
    assert tot.runs == 2
    assert tot.succeeded == 1
    assert abs(tot.cost_usd - 0.05) < 1e-9            # EMPTY cost is real spend, counted
    assert abs(tot.cost_per_run - 0.025) < 1e-9       # 0.05 / 2
    assert abs(tot.cost_per_succeeded - 0.05) < 1e-9  # 0.05 / 1 - the wasted EMPTY run inflates it


# --- usage window vocabulary (CLI ↔ MCP shared mapping) ---------------------------------------


def test_usage_window_since_resolves_each_window() -> None:
    """Shared mapping: `all` → None; every other window → a concrete UTC `since`."""
    session_start = datetime(2026, 7, 1, 12, 0, 0, tzinfo=UTC)
    now = datetime(2026, 7, 27, 18, 0, 0, tzinfo=UTC)

    assert usage_window_since("all", session_start=session_start, now=now) is None
    assert usage_window_since("session", session_start=session_start, now=now) == session_start
    assert usage_window_since("day", session_start=session_start, now=now) == now - timedelta(hours=24)
    assert usage_window_since("week", session_start=session_start, now=now) == now - timedelta(days=7)
    assert usage_window_since("month", session_start=session_start, now=now) == now - timedelta(days=30)

    with pytest.raises(ValueError, match="unknown usage window"):
        usage_window_since("year", session_start=session_start, now=now)


def test_usage_windows_match_across_surfaces(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch, capsys: pytest.CaptureFixture[str]
) -> None:
    """CLI `--window` choices and MCP `usage(window=...)` enum must stay identical (and equal
    USAGE_WINDOWS). This is the lock that keeps the two surfaces from drifting apart again."""
    import asyncio
    import re

    from marshal_engine.interfaces import cli
    from marshal_engine.interfaces.mcp_server import build_app, build_service

    expected = set(USAGE_WINDOWS)
    assert expected == {"session", "day", "week", "month", "all"}

    # CLI: argparse renders `--window {a,b,c,...}` in --help
    with pytest.raises(SystemExit) as ei:
        cli.main(["usage", "--help"])
    assert ei.value.code == 0
    help_text = capsys.readouterr().out
    m = re.search(r"--window\s+\{([^}]+)\}", help_text)
    assert m is not None, f"--window choices missing from help:\n{help_text}"
    cli_windows = {c.strip() for c in m.group(1).split(",")}
    assert cli_windows == expected

    # MCP: tool schema enum
    pytest.importorskip("mcp")
    repo = tmp_path / "repo"
    repo.mkdir()
    monkeypatch.setenv("MARSHAL_REPO", str(repo))
    monkeypatch.delenv("MARSHAL_CONFIG", raising=False)
    app = build_app(build_service())
    tools = {t.name: t for t in asyncio.run(app.list_tools())}
    mcp_windows = set(tools["usage"].input_schema["properties"]["window"]["enum"])
    assert mcp_windows == expected


# --- time-windowed rollups + per-backend/model breakdown --------------------------------------


def test_summary_without_args_is_unchanged_when_unfiltered(tmp_path: Path) -> None:
    # Backward compat: summary() with no args behaves exactly as before (the existing JSON shape
    # test pins this; here we additionally lock down the new by_backend_model breakdown).
    t = UsageTracker(tmp_path / "usage")
    t.record(_ev(run_id="r1", backend="opencode", model="<provider>/<model-a>", cost_usd=0.01,
                 input_tokens=100, output_tokens=10))
    t.record(_ev(run_id="r2", backend="opencode", model="<provider>/<model-b>", cost_usd=0.02,
                 input_tokens=200, output_tokens=20))
    t.record(_ev(run_id="r3", backend="cursor", model="<provider>/<model-c>", cost_usd=0.0,
                 source="unavailable"))

    s = t.summary()
    assert set(s.by_backend_model) == {
        "opencode/<provider>/<model-a>",
        "opencode/<provider>/<model-b>",
        "cursor/<provider>/<model-c>",
    }
    assert s.by_backend_model["opencode/<provider>/<model-a>"].runs == 1
    assert abs(s.by_backend_model["opencode/<provider>/<model-a>"].cost_usd - 0.01) < 1e-9
    assert s.by_backend_model["cursor/<provider>/<model-c>"].input_tokens == 0
    # Tokens from both opencode models rolled up at the backend level
    assert s.by_backend["opencode"].input_tokens == 300
    assert s.by_backend_model["opencode/<provider>/<model-a>"].input_tokens == 100


def test_summary_window_excludes_outside_events(tmp_path: Path) -> None:
    # A `since` filter is the common case (last 7d / 30d). Events outside the window drop out of
    # totals and every breakdown.
    t = UsageTracker(tmp_path / "usage")
    t.record(_ev(run_id="old", ts="2020-01-01T00:00:00Z", backend="opencode", cost_usd=1.00))
    t.record(_ev(run_id="new", ts="2026-06-19T00:00:00Z", backend="opencode", cost_usd=0.05))

    since = datetime(2026, 1, 1, tzinfo=UTC)
    s = t.summary(since=since)
    assert s.totals.runs == 1
    assert s.totals.runs == 1
    assert s.by_backend["opencode"].runs == 1
    assert abs(s.totals.cost_usd - 0.05) < 1e-9
    assert s.by_backend_model["opencode/-"].runs == 1


def test_summary_window_inclusive_bounds_compare_in_utc(tmp_path: Path) -> None:
    # Inclusive [since, until], and naive vs aware datetimes both work (compared in UTC).
    t = UsageTracker(tmp_path / "usage")
    t.record(_ev(run_id="a", ts="2026-06-19T10:00:00+00:00", backend="opencode"))
    t.record(_ev(run_id="b", ts="2026-06-19T12:00:00+00:00", backend="opencode"))
    t.record(_ev(run_id="c", ts="2026-06-19T14:00:00+00:00", backend="opencode"))

    # since=12:00Z, until=12:00Z -> only the 12:00 event is in the window
    s = t.summary(
        since=datetime(2026, 6, 19, 12, tzinfo=UTC),
        until=datetime(2026, 6, 19, 12, tzinfo=UTC),
    )
    assert s.totals.runs == 1

    # Naive since (treated as UTC) vs an aware event still aligns.
    s2 = t.summary(since=datetime(2026, 6, 19, 11))
    assert s2.totals.runs == 2  # 12:00Z and 14:00Z


def test_summary_window_drops_unparseable_timestamps(tmp_path: Path) -> None:
    # A malformed ts can't be compared; safer to exclude than to misclassify. (The all-time summary
    # includes it because no filter is applied.)
    t = UsageTracker(tmp_path / "usage")
    t.record(_ev(run_id="ok", ts="2026-06-19T00:00:00Z", backend="opencode"))
    t.record(_ev(run_id="bad", ts="not-a-date", backend="opencode"))

    assert t.summary().totals.runs == 2  # all-time: keep both
    s = t.summary(since=datetime(2020, 1, 1, tzinfo=UTC))
    assert s.totals.runs == 1  # windowed: drop the malformed one
    assert s.by_backend["opencode"].runs == 1


def test_by_backend_model_aggregates_in_the_same_loop(tmp_path: Path) -> None:
    # The compound key is '<backend>/<model>' (model='-' when None). The same loop as the other
    # breakdowns drives it, so totals reconcile.
    t = UsageTracker(tmp_path / "usage")
    t.record(_ev(run_id="r1", backend="opencode", model="<provider>/<model-a>",
                 cost_usd=0.01, input_tokens=10, output_tokens=1))
    t.record(_ev(run_id="r2", backend="opencode", model="<provider>/<model-a>",
                 cost_usd=0.02, input_tokens=20, output_tokens=2))
    t.record(_ev(run_id="r3", backend="cursor", cost_usd=0.0))  # no model -> "<backend>/-"

    s = t.summary()
    # The same key aggregates across runs
    a = s.by_backend_model["opencode/<provider>/<model-a>"]
    assert a.runs == 2
    assert abs(a.cost_usd - 0.03) < 1e-9
    assert a.input_tokens == 30
    assert a.output_tokens == 3
    # The model-less event lands under <backend>/- and matches the by_backend view for that backend
    assert s.by_backend_model["cursor/-"].runs == 1
    assert s.by_backend_model["cursor/-"].runs == s.by_backend["cursor"].runs


def test_pre_rename_ledger_events_still_count_as_successes(tmp_path: Path) -> None:
    """The ledger is APPEND-ONLY, so every event written before `succeeded` became `exited_clean`
    still carries the old word. A reader that only knew the new spelling would silently stop
    counting those runs — quietly changing every historical cost-per-succeeded figure. The whole
    point of the ledger is that recorded facts do not move under you."""
    from marshal_engine.accounting.usage import UsageTracker

    u = tmp_path / "usage"
    u.mkdir()
    (u / "events.jsonl").write_text(
        '{"ts":"2026-01-01T00:00:00+00:00","run_id":"old","backend":"cursor",'
        '"cost_usd":0.10,"status":"succeeded","source":"native"}\n',
        encoding="utf-8",
    )
    summary = UsageTracker(u).summary()
    assert summary.totals.runs == 1
    assert summary.totals.succeeded == 1, "a pre-rename success stopped counting"
    assert summary.totals.cost_per_succeeded == 0.1


def test_a_torn_line_does_not_swallow_the_next_event(
    tmp_path: Path, capsys: pytest.CaptureFixture[str]
) -> None:
    """A tear must cost the fragment only, never the complete event written after it.

    `record` appended straight onto whatever was there, so the fragment and the next event fused
    into one invalid line and the parser dropped BOTH. The ledger is append-only and interpreted
    on read, so that run's measured spend was gone from every reporting surface for good - and the
    warning said one line was skipped while two events were missing.
    """
    t = UsageTracker(tmp_path / "usage")
    t.record(_ev(run_id="ok1", cost_usd=0.01, status="exited_clean", source="native"))
    with t.events_path.open("a", encoding="utf-8") as f:
        f.write('{"ts":"2026-06-19T00:00:00Z","run_id":"torn","backend":"openco')  # crash here
    t.record(_ev(run_id="after", cost_usd=5.00, status="exited_clean", source="native"))

    events = t.events()
    assert [e.run_id for e in events] == ["ok1", "after"], "the tear took its neighbour with it"
    assert abs(t.summary().totals.cost_usd - 5.01) < 1e-9
    assert "skipping 1 malformed usage event line" in capsys.readouterr().err


def test_recording_onto_a_closed_ledger_adds_no_blank_line(tmp_path: Path) -> None:
    """The repair must be conditional: a normal append is already well-formed, and padding every
    one of them would rewrite the shape of a file other readers seek through by byte offset."""
    t = UsageTracker(tmp_path / "usage")
    t.record(_ev(run_id="a", cost_usd=0.01, status="exited_clean", source="native"))
    t.record(_ev(run_id="b", cost_usd=0.01, status="exited_clean", source="native"))
    raw = t.events_path.read_text(encoding="utf-8")
    assert "\n\n" not in raw
    assert len(raw.splitlines()) == 2


def test_events_skips_torn_final_line_and_warns(
    tmp_path: Path, capsys: pytest.CaptureFixture[str]
) -> None:
    """Crash-mid-append leaves a partial final line; summary must still succeed (#142)."""
    t = UsageTracker(tmp_path / "usage")
    t.record(_ev(run_id="ok1", cost_usd=0.01, status="exited_clean", source="native"))
    t.record(_ev(run_id="ok2", cost_usd=0.02, status="exited_clean", source="native"))
    with t.events_path.open("a", encoding="utf-8") as f:
        f.write('{"ts":"2026-06-19T00:00:00Z","run_id":"torn","backend":"openco')  # no closing

    events = t.events()
    assert [e.run_id for e in events] == ["ok1", "ok2"]
    err = capsys.readouterr().err
    assert "skipping 1 malformed usage event line" in err

    s = t.summary()
    assert s.totals.runs == 2
    assert abs(s.totals.cost_usd - 0.03) < 1e-9


def test_events_skips_mid_file_malformed_lines(
    tmp_path: Path, capsys: pytest.CaptureFixture[str]
) -> None:
    """A corrupt middle line is skipped with a count; neighbors still roll up."""
    usage = tmp_path / "usage"
    usage.mkdir()
    (usage / "events.jsonl").write_text(
        '{"ts":"2026-06-19T00:00:00Z","run_id":"a","backend":"opencode",'
        '"cost_usd":0.01,"status":"exited_clean","source":"native"}\n'
        "{not valid json\n"
        '{"ts":"2026-06-19T00:00:00Z","run_id":"b","backend":"opencode",'
        '"cost_usd":0.02,"status":"exited_clean","source":"native"}\n'
        "also-not-json\n",
        encoding="utf-8",
    )
    t = UsageTracker(usage)
    assert [e.run_id for e in t.events()] == ["a", "b"]
    assert "skipping 2 malformed usage event line" in capsys.readouterr().err
    assert t.summary().totals.runs == 2


def test_events_file_level_read_failure_still_propagates(tmp_path: Path) -> None:
    """Fail-closed for REAL unreadable ledgers: only malformed *lines* are skipped (#142)."""
    usage = tmp_path / "usage"
    usage.mkdir()
    # events.jsonl as a directory makes read_bytes raise IsADirectoryError (OSError).
    (usage / "events.jsonl").mkdir()
    with pytest.raises(OSError):
        UsageTracker(usage).events()
    with pytest.raises(OSError):
        UsageTracker(usage).summary()


def test_events_strict_raises_on_torn_line(tmp_path: Path) -> None:
    """Strict reader (enforce path) refuses when any line is unreadable."""
    from marshal_engine.accounting.usage import UnreadableUsageLedgerError

    t = UsageTracker(tmp_path / "usage")
    t.record(_ev(run_id="ok", cost_usd=0.01))
    with t.events_path.open("a", encoding="utf-8") as f:
        f.write('{"ts":"2026-06-19T00:00:00Z","run_id":"torn","backend":"openco')

    with pytest.raises(UnreadableUsageLedgerError, match="unreadable event") as ei:
        t.events(strict=True)
    assert ei.value.skipped == 1
    assert "events.jsonl" in str(ei.value)
    assert "repair or remove the torn line" in str(ei.value)

    with pytest.raises(UnreadableUsageLedgerError):
        t.summary(strict=True)


def test_events_after_reads_only_appended_tail(tmp_path: Path) -> None:
    t = UsageTracker(tmp_path / "usage")
    t.record(_ev(run_id="a", cost_usd=0.01))
    _events, cursor = t.read_events()
    t.record(_ev(run_id="b", cost_usd=0.02))
    t.record(_ev(run_id="c", cost_usd=0.03))
    tail = t.events_after(cursor)
    assert [e.run_id for e in tail] == ["b", "c"]


def test_events_after_truncated_raises(tmp_path: Path) -> None:
    from marshal_engine.accounting.usage import UnreadableUsageLedgerError

    t = UsageTracker(tmp_path / "usage")
    t.record(_ev(run_id="a", cost_usd=0.01))
    _events, cursor = t.read_events()
    t.events_path.write_bytes(b"")
    with pytest.raises(UnreadableUsageLedgerError, match="truncated"):
        t.events_after(cursor)


def test_events_after_same_size_mtime_rewrite_raises(tmp_path: Path) -> None:
    """Same inode + same byte size + different mtime is an in-place rewrite, not a no-op."""
    import os

    from marshal_engine.accounting.usage import UnreadableUsageLedgerError

    t = UsageTracker(tmp_path / "usage")
    t.record(_ev(run_id="a", cost_usd=0.01))
    _events, cursor = t.read_events()
    raw = t.events_path.read_bytes()
    t.events_path.write_bytes(b"Y" * len(raw))
    os.utime(t.events_path, ns=(cursor.mtime_ns + 1_000_000, cursor.mtime_ns + 1_000_000))
    assert t.events_path.stat().st_size == cursor.size
    assert t.events_path.stat().st_mtime_ns != cursor.mtime_ns
    with pytest.raises(UnreadableUsageLedgerError, match="rewritten in place"):
        t.events_after(cursor)


def test_events_after_same_size_same_mtime_is_noop(tmp_path: Path) -> None:
    t = UsageTracker(tmp_path / "usage")
    t.record(_ev(run_id="a", cost_usd=0.01))
    _events, cursor = t.read_events()
    assert t.events_after(cursor) == []


# --- Phase 1 routing facts (task_kind / goal_digest) ---------------------------------


def test_routing_facts_round_trip_through_ledger_and_summary(tmp_path: Path) -> None:
    """New fields survive UsageEvent → events.jsonl → summary; cost rollup unchanged."""
    from marshal_engine.accounting.usage import goal_digest

    digest = goal_digest("refactor the auth module")
    t = UsageTracker(tmp_path / "usage")
    t.record(
        _ev(
            run_id="r1",
            cost_usd=0.02,
            status="exited_clean",
            source="native",
            input_tokens=100,
            output_tokens=10,
            task_kind="refactor",
            goal_digest=digest,
        )
    )
    events = t.events()
    assert len(events) == 1
    assert events[0].task_kind == "refactor"
    assert events[0].goal_digest == digest
    # File contents carry the fields (not just in-memory defaults).
    raw = t.events_path.read_text(encoding="utf-8")
    assert '"task_kind":"refactor"' in raw
    assert f'"goal_digest":"{digest}"' in raw

    tot = t.summary().totals
    assert tot.runs == 1
    assert tot.succeeded == 1
    assert abs(tot.cost_usd - 0.02) < 1e-9


def test_legacy_and_phase1_lines_mix_in_summary(tmp_path: Path) -> None:
    """Pre-Phase-1 lines lack the new fields; mixed rollup must match pre-Phase-1 math."""
    events = tmp_path / "usage" / "events.jsonl"
    events.parent.mkdir(parents=True)
    # Legacy line: no task_kind / goal_digest (and no cache_write_tokens).
    legacy = (
        '{"ts":"2026-01-01T00:00:00Z","run_id":"old1","backend":"cursor",'
        '"input_tokens":10,"output_tokens":2,"cache_read_tokens":5,'
        '"cost_usd":0.01,"status":"exited_clean","source":"native"}\n'
    )
    new = (
        '{"ts":"2026-07-30T00:00:00Z","run_id":"new1","backend":"opencode",'
        '"input_tokens":20,"output_tokens":4,"cache_read_tokens":0,'
        '"cache_write_tokens":0,"cost_usd":0.02,"status":"exited_clean",'
        '"source":"native","task_kind":"bugfix","goal_digest":"abcd1234abcd1234"}\n'
    )
    events.write_text(legacy + new, encoding="utf-8")

    loaded = UsageTracker(tmp_path / "usage").events()
    assert len(loaded) == 2
    assert loaded[0].task_kind is None
    assert loaded[0].goal_digest is None
    assert loaded[1].task_kind == "bugfix"
    assert loaded[1].goal_digest == "abcd1234abcd1234"

    tot = UsageTracker(tmp_path / "usage").summary().totals
    assert tot.runs == 2
    assert tot.succeeded == 2
    assert abs(tot.cost_usd - 0.03) < 1e-9
    assert tot.input_tokens == 30
    assert tot.output_tokens == 6


def test_goal_digest_stable_distinct_and_raw_goal_absent_from_ledger(tmp_path: Path) -> None:
    """Digest is stable for identical goals, differs across goals, and raw text never hits the file."""
    from marshal_engine.accounting.usage import GOAL_DIGEST_PREFIX_LEN, goal_digest

    secret_goal = "ROTATE_SECRET_KEY=super-secret-value-do-not-leak"
    other = "unrelated goal text"
    d1 = goal_digest(secret_goal)
    d2 = goal_digest(secret_goal)
    d3 = goal_digest(other)
    assert d1 == d2
    assert d1 != d3
    assert len(d1) == GOAL_DIGEST_PREFIX_LEN
    assert secret_goal not in d1

    res = AgentResult(status=RunStatus.EXITED_CLEAN)
    ev = UsageEvent.from_result(
        res,
        run_id="g1",
        backend="opencode",
        ts="2026-07-30T00:00:00Z",
        task_kind="refactor",
        goal_digest=goal_digest(secret_goal),
    )
    t = UsageTracker(tmp_path / "usage")
    t.record(ev)
    raw = t.events_path.read_text(encoding="utf-8")
    assert secret_goal not in raw
    assert "super-secret-value-do-not-leak" not in raw
    assert f'"goal_digest":"{d1}"' in raw
    assert "ROTATE_SECRET_KEY" not in raw


def test_from_result_carries_routing_facts() -> None:
    res = AgentResult(
        status=RunStatus.EXITED_CLEAN,
        usage=UsageRecord(backend="opencode", cost_usd=0.01, source=UsageSource.NATIVE),
    )
    ev = UsageEvent.from_result(
        res,
        run_id="r1",
        backend="opencode",
        ts="2026-07-30T00:00:00Z",
        task_kind="docs",
        goal_digest="deadbeefdeadbeef",
    )
    assert ev.task_kind == "docs"
    assert ev.goal_digest == "deadbeefdeadbeef"
