"""Tests for FleetState persistence (one JSON file per run)."""

from __future__ import annotations

from concurrent.futures import ThreadPoolExecutor
from pathlib import Path

import pytest
from pydantic import ValidationError

from marshal_engine.state import FleetState, RunRecord
from marshal_engine.types import RunStatus


def test_agent_liveness_is_never_written_to_the_ledger(tmp_path: Path) -> None:
    """`agent_alive` answers "right now", so persisting it would store a value that is wrong the
    moment it lands - the exact class of staleness the field exists to fix."""
    state = FleetState(tmp_path / "runs")
    state.add(
        RunRecord(run_id="r1", task_id="t", backend="b", status="running", agent_alive=True)
    )
    raw = (tmp_path / "runs" / "r1.json").read_text(encoding="utf-8")
    assert "agent_alive" not in raw
    assert state.get("r1").agent_alive is None  # nothing to read back


def test_add_get_update_list(tmp_path: Path) -> None:
    st = FleetState(tmp_path / "runs")
    assert st.list() == []
    st.add(RunRecord(run_id="r1", task_id="t1", backend="opencode", status="running"))

    got = st.get("r1")
    assert got is not None and got.status == "running"

    updated = st.update("r1", status="exited_clean", cost_usd=0.02)
    assert updated.status == "exited_clean"
    assert updated.cost_usd == 0.02
    assert len(st.list()) == 1
    assert st.get("missing") is None


def test_update_validates_and_does_not_corrupt(tmp_path: Path) -> None:
    # A wrong-typed update must raise, not silently write a corrupt record that vanishes on read.
    st = FleetState(tmp_path / "runs")
    st.add(RunRecord(run_id="r1", task_id="t1", backend="opencode"))
    with pytest.raises(ValidationError):
        st.update("r1", cost_usd="not-a-number")
    # the run is still readable and unchanged
    got = st.get("r1")
    assert got is not None and got.cost_usd == 0.0
    assert len(st.list()) == 1


def test_persists_across_instances(tmp_path: Path) -> None:
    d = tmp_path / "runs"
    FleetState(d).add(RunRecord(run_id="r1", task_id="t1", backend="cursor"))
    reopened = FleetState(d).get("r1")
    assert reopened is not None and reopened.backend == "cursor"


def test_verify_fields_round_trip_and_default(tmp_path: Path) -> None:
    d = tmp_path / "runs"
    st = FleetState(d)
    st.add(RunRecord(run_id="r1", task_id="t1", backend="cursor"))
    got = st.get("r1")
    assert got is not None and got.verify_passed is None and got.verify_output == ""  # old ledgers load

    st.update("r1", status="verify_failed", verify_passed=False, verify_output="verify exited 1")
    reopened = FleetState(d).get("r1")
    assert reopened is not None
    assert reopened.status == "verify_failed"
    assert reopened.verify_passed is False
    assert reopened.verify_output == "verify exited 1"


def test_concurrent_adds_do_not_lose_records(tmp_path: Path) -> None:
    # The whole point of per-run files: N runs writing at once never clobber each other (the old
    # single-file read-modify-write would lose records here).
    st = FleetState(tmp_path / "runs")

    def add(i: int) -> None:
        st.add(RunRecord(run_id=f"r{i}", task_id=f"t{i}", backend="opencode"))

    with ThreadPoolExecutor(max_workers=8) as pool:
        list(pool.map(add, range(50)))

    assert len(st.list()) == 50
    assert {r.run_id for r in st.list()} == {f"r{i}" for i in range(50)}


# --- update_if: predicate gates the write ----------------------------------------------------


def test_update_if_predicate_false_does_not_modify_record(tmp_path: Path) -> None:
    # update_if is the only path that respects the cancel-wins invariant: the predicate is the
    # ONLY way to decide whether to overwrite a terminal status. A False predicate must skip
    # the write entirely - not just compute the write and bail, not even re-touch the file.
    # Locks the invariant: a `cancel_run` racing a naturally-finished run must NEVER clobber
    # the natural "succeeded" status with "cancelled" (and vice versa).
    st = FleetState(tmp_path / "runs")
    st.add(RunRecord(run_id="r1", task_id="t1", backend="opencode", status="exited_clean"))
    path = next((tmp_path / "runs").iterdir())
    mtime_before = path.stat().st_mtime

    # Predicate returns False: nothing should be written.
    result = st.update_if("r1", lambda r: False, status="cancelled")
    assert result.status == "exited_clean"  # the un-modified record

    mtime_after = path.stat().st_mtime
    assert mtime_before == mtime_after  # file untouched, predicate short-circuited the write

    # And: the file is still readable as the original record
    assert st.get("r1").status == "exited_clean"


def test_update_if_predicate_true_writes_and_returns_new(tmp_path: Path) -> None:
    # The happy path: predicate True -> the update is applied and the returned record carries
    # the new field. Locks the contract: update_if returns the new record on success, not None.
    st = FleetState(tmp_path / "runs")
    st.add(RunRecord(run_id="r1", task_id="t1", backend="opencode", status="running"))
    result = st.update_if("r1", lambda r: r.status == "running", status="exited_clean", cost_usd=0.5)
    assert result.status == "exited_clean"
    assert result.cost_usd == 0.5


# --- state.add: documents the upsert semantics (run_id is the natural key) ------------------


def test_add_with_existing_run_id_clobbers(tmp_path: Path) -> None:
    # state.add is named "add" but behaves as upsert: adding a second record with the same
    # run_id overwrites the first. Fleet._start never collides (run_id includes a uuid hex
    # suffix), so this is a no-op in production; the test pins the behavior so a future
    # refactor that turns add() into a strict create has to make a deliberate decision.
    st = FleetState(tmp_path / "runs")
    st.add(RunRecord(run_id="r1", task_id="t1", backend="opencode", status="running", cost_usd=0.1))
    st.add(RunRecord(run_id="r1", task_id="t1", backend="opencode", status="exited_clean", cost_usd=0.9))
    recs = st.list()
    assert len(recs) == 1
    assert recs[0].status == "exited_clean"
    assert recs[0].cost_usd == 0.9


def test_a_pre_rename_record_still_loads(tmp_path: Path) -> None:
    """`succeeded` became `exited_clean` because the old word claimed more than it checked - the
    process exited 0, which is not a statement about correctness. Records written before that are
    facts about what happened and are NOT rewritten: they are reinterpreted on read."""
    runs = tmp_path / "runs"
    runs.mkdir()
    (runs / "old.json").write_text(
        '{"run_id":"old","task_id":"t","backend":"b","status":"succeeded"}', encoding="utf-8"
    )
    rec = FleetState(runs).get("old")
    assert rec is not None
    assert rec.status == RunStatus.EXITED_CLEAN.value


def test_migrating_a_status_on_read_does_not_rewrite_the_file(tmp_path: Path) -> None:
    """Reading a ledger must never mutate it. A record is evidence; silently editing history to
    match a new vocabulary is exactly what the two-layer usage split exists to prevent."""
    runs = tmp_path / "runs"
    runs.mkdir()
    path = runs / "old.json"
    original = '{"run_id":"old","task_id":"t","backend":"b","status":"succeeded"}'
    path.write_text(original, encoding="utf-8")
    FleetState(runs).get("old")
    assert path.read_text(encoding="utf-8") == original
