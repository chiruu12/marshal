"""Recording a run's verdict — the denominator every routing number is computed over.

Kept separate from test_service.py because `record_outcome` is deliberately config-free: it works
on a repo with no fleet.config.yaml, and these tests exercise it that way.
"""

from __future__ import annotations

from pathlib import Path

import pytest

from marshal_engine.accounting.usage import UsageEvent, UsageTracker
from marshal_engine.interfaces.routing import (
    MAX_OUTCOME_NOTE_LEN,
    build_routing,
    outcome_index,
    record_outcome,
)
from marshal_engine.runtime.state import FleetState, RunRecord


@pytest.fixture
def state(tmp_path: Path) -> FleetState:
    return FleetState(tmp_path / "runs")


def _run(
    state: FleetState, run_id: str = "r1", status: str = "exited_clean", **fields: object
) -> RunRecord:
    rec = RunRecord(run_id=run_id, task_id="t1", backend="echo", status=status, **fields)
    state.add(rec)
    return rec


def test_recording_a_rejection(state: FleetState) -> None:
    _run(state)
    result = record_outcome(state, "r1", "rejected", note="wrong approach")
    assert result.status == "recorded"
    assert result.outcome == "rejected"
    assert result.previous is None
    on_disk = state.get("r1")
    assert on_disk is not None
    assert on_disk.outcome == "rejected"
    assert on_disk.outcome_note == "wrong approach"
    assert on_disk.outcome_at is not None


def test_an_unknown_run_is_an_error_and_creates_nothing(state: FleetState) -> None:
    """The ledger holds facts about runs that happened; a typo must not mint a record."""
    with pytest.raises(ValueError, match="no such run"):
        record_outcome(state, "ghost", "rejected")
    assert state.list() == []


def test_an_invalid_outcome_names_the_legal_set_and_writes_nothing(state: FleetState) -> None:
    _run(state)
    with pytest.raises(ValueError, match="abandoned, advisory, integrated, rejected"):
        record_outcome(state, "r1", "sort-of-ok")
    rec = state.get("r1")
    assert rec is not None and rec.outcome is None


def test_integrated_is_sticky_and_the_record_is_untouched(state: FleetState) -> None:
    """A merge commit is a mechanical fact, not an opinion.

    Returned as `conflict` rather than raised so a driver can branch on it — and the on-disk
    record must be byte-identical afterwards, not merely equal-ish.
    """
    _run(state, outcome="integrated", merged_into="main")
    before = (state.dir / "r1.json").read_bytes()

    result = record_outcome(state, "r1", "rejected", note="changed my mind")
    assert result.status == "conflict"
    assert result.outcome == "integrated"  # what the record says NOW, not what was asked for
    assert result.previous == "integrated"
    assert "merged_into=main" in result.message
    assert (state.dir / "r1.json").read_bytes() == before


def test_re_recording_the_same_verdict_is_idempotent(state: FleetState) -> None:
    _run(state)
    first = record_outcome(state, "r1", "rejected", note="first")
    stamped = state.get("r1")
    assert stamped is not None
    before = (state.dir / "r1.json").read_bytes()

    second = record_outcome(state, "r1", "rejected", note="second")
    assert first.status == "recorded"
    assert second.status == "unchanged"
    # No write at all: the original timestamp and note survive a repeat call.
    assert (state.dir / "r1.json").read_bytes() == before


def test_an_opinion_may_be_revised(state: FleetState) -> None:
    """Only `integrated` is sticky — rejected and abandoned are judgments, and judgments change."""
    _run(state)
    record_outcome(state, "r1", "rejected")
    result = record_outcome(state, "r1", "abandoned")
    assert result.status == "recorded"
    assert result.previous == "rejected"
    assert result.outcome == "abandoned"


def test_a_long_note_is_truncated(state: FleetState) -> None:
    """`text` and `verify_output` already showed what an unbounded field costs every reader."""
    _run(state)
    record_outcome(state, "r1", "rejected", note="x" * (MAX_OUTCOME_NOTE_LEN + 500))
    rec = state.get("r1")
    assert rec is not None and rec.outcome_note is not None
    assert len(rec.outcome_note) == MAX_OUTCOME_NOTE_LEN


def test_outcome_index_distinguishes_unjudged_from_pruned(state: FleetState) -> None:
    """A present-but-None value and an absent key must never collapse into each other.

    The ledger is append-only and permanent; run records are not. If a pruned record read as
    "unjudged", deleted history would quietly enter the denominator of every routing rate.
    """
    _run(state, run_id="judged", outcome="integrated")
    _run(state, run_id="unjudged")
    index = outcome_index(state)
    assert index["judged"] == "integrated"
    assert index["unjudged"] is None      # exists, no verdict yet
    assert "pruned" not in index          # record gone — not the same thing


def test_integrated_cannot_be_asserted_without_a_merge(state: FleetState) -> None:
    """The whole justification for stickiness is that a merge commit exists.

    Letting a caller assert `integrated` by hand would mint a permanent, un-overwritable verdict
    for a merge that never happened — and because it is sticky, nothing could ever correct it.
    """
    _run(state)
    result = record_outcome(state, "r1", "integrated")
    assert result.status == "conflict"
    assert "nothing has been merged" in result.message
    rec = state.get("r1")
    assert rec is not None and rec.outcome is None


def test_integrated_may_be_re_affirmed_when_a_merge_really_landed(state: FleetState) -> None:
    """`integrate` stamps it; re-recording the true verdict stays idempotent rather than erroring."""
    _run(state, outcome="integrated", merged_into="main")
    assert record_outcome(state, "r1", "integrated").status == "unchanged"


def test_an_unfinished_run_cannot_be_judged(state: FleetState) -> None:
    """A verdict on work that has not happened is a guess, and rates are computed over verdicts."""
    _run(state, run_id="r2", status="running")
    result = record_outcome(state, "r2", "rejected")
    assert result.status == "conflict"
    assert "still 'running'" in result.message
    rec = state.get("r2")
    assert rec is not None and rec.outcome is None


def test_a_queued_run_cannot_be_judged_either(state: FleetState) -> None:
    _run(state, run_id="r3", status="queued")
    assert record_outcome(state, "r3", "abandoned").status == "conflict"


def test_a_failed_run_can_be_judged(state: FleetState) -> None:
    """`failed` is terminal: abandoning a run that broke is a legitimate, useful verdict."""
    _run(state, run_id="r4", status="failed")
    assert record_outcome(state, "r4", "abandoned").status == "recorded"


def test_the_reported_status_comes_from_the_locked_read(state: FleetState) -> None:
    """`previous` must reflect what the predicate saw under the lock, not a pre-read.

    Classifying from a read taken before `update_if` would misreport whenever a concurrent
    `integrate` lands in between — and that is the one transition that must not be misreported.
    """
    _run(state, outcome="rejected")
    result = record_outcome(state, "r1", "abandoned")
    assert result.previous == "rejected"
    assert result.outcome == "abandoned"
    assert result.status == "recorded"


# --- build_routing: partial ledger must not recommend ----------------------------------------


def _usage_event(**fields: object) -> UsageEvent:
    base = {
        "ts": "2026-06-19T00:00:00+00:00",
        "run_id": "r-ok",
        "backend": "echo",
        "client": "worker",
        "task_kind": "refactor",
        "cost_usd": 0.5,
        "source": "native",
        "duration_ms": 1000,
        "status": "exited_clean",
    }
    base.update(fields)
    return UsageEvent(**base)


def test_a_torn_ledger_line_withholds_recommendations(tmp_path: Path) -> None:
    """A skipped event must not inflate integration_rate and mint a headline recommendation."""
    usage_dir = tmp_path / "usage"
    usage_dir.mkdir()
    events_path = usage_dir / "events.jsonl"
    events_path.write_text(
        _usage_event(run_id="r-ok").model_dump_json() + "\n"
        '{"ts":"2026-06-19T00:00:00+00:00","run_id":"r-bad","backend":"echo","clie'  # torn
        + "\n",
        encoding="utf-8",
    )
    state = FleetState(tmp_path / "runs")
    state.add(
        RunRecord(
            run_id="r-ok",
            task_id="t1",
            backend="echo",
            client="worker",
            status="exited_clean",
            outcome="integrated",
        )
    )
    state.add(
        RunRecord(
            run_id="r-bad",
            task_id="t2",
            backend="echo",
            client="worker",
            status="exited_clean",
            outcome="rejected",
        )
    )

    ledger = build_routing(UsageTracker(usage_dir), state)

    assert ledger.cells[0].n_judged == 1
    assert ledger.cells[0].integration_rate == 1.0
    assert ledger.recommended is None
    assert ledger.recommended_by_task_kind == {}
    assert ledger.caveat is not None
    assert "unreadable event" in ledger.caveat


def test_a_complete_ledger_still_recommends_normally(tmp_path: Path) -> None:
    """Partiality caveats apply only when the reader actually skipped lines."""
    usage_dir = tmp_path / "usage"
    usage_dir.mkdir()
    (usage_dir / "events.jsonl").write_text(
        _usage_event(run_id="r-ok").model_dump_json() + "\n"
        + _usage_event(run_id="r-bad", cost_usd=0.2).model_dump_json()
        + "\n",
        encoding="utf-8",
    )
    state = FleetState(tmp_path / "runs")
    state.add(
        RunRecord(
            run_id="r-ok",
            task_id="t1",
            backend="echo",
            client="worker",
            status="exited_clean",
            outcome="integrated",
        )
    )
    state.add(
        RunRecord(
            run_id="r-bad",
            task_id="t2",
            backend="echo",
            client="worker",
            status="exited_clean",
            outcome="rejected",
        )
    )

    ledger = build_routing(UsageTracker(usage_dir), state)

    assert ledger.cells[0].n_judged == 2
    assert ledger.cells[0].integration_rate == 0.5
    assert ledger.recommended == "worker"
    assert ledger.caveat is None


def test_a_ledger_torn_mid_character_does_not_crash_routing(tmp_path: Path) -> None:
    """REGRESSION: the strict pass decodes as UTF-8 and raises UnicodeDecodeError on a ledger a
    crash tore mid-character. Only UnreadableUsageLedgerError was caught, so `routing` - a
    REPORTING path whose documented posture is to degrade, not fail - died on a tear the lenient
    read handles fine. Partiality is still reported; only the count is unavailable.
    """
    usage_dir = tmp_path / "usage"
    usage_dir.mkdir()
    good = _usage_event(run_id="r-ok").model_dump_json().encode("utf-8")
    # A multibyte client name cut mid-character, exactly as an interrupted append leaves it.
    # Ends on the FIRST byte of a 2-byte 'ö': a lone 0xC3 is not decodable UTF-8, which is what
    # an append interrupted mid-character leaves behind. (Cutting an ASCII byte would still
    # decode cleanly and only exercise the line-level tear the lenient path already handled.)
    torn = '{"ts":"2026-06-19T00:00:00+00:00","run_id":"r-bad","client":"wö'.encode("utf-8")[:-1]
    (usage_dir / "events.jsonl").write_bytes(good + b"\n" + torn + b"\n")

    state = FleetState(tmp_path / "runs")
    state.add(
        RunRecord(
            run_id="r-ok", task_id="t1", backend="echo", client="worker",
            status="exited_clean", outcome="integrated",
        )
    )

    ledger = build_routing(UsageTracker(usage_dir), state)  # must not raise

    assert ledger.recommended is None
    assert ledger.caveat is not None
    assert "could not be counted" in ledger.caveat
