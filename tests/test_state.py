"""Tests for FleetState persistence (one JSON file per run)."""

from __future__ import annotations

from concurrent.futures import ThreadPoolExecutor
from pathlib import Path

import pytest
from pydantic import ValidationError

from marshal_engine.state import FleetState, RunRecord


def test_add_get_update_list(tmp_path: Path) -> None:
    st = FleetState(tmp_path / "runs")
    assert st.list() == []
    st.add(RunRecord(run_id="r1", task_id="t1", backend="opencode", status="running"))

    got = st.get("r1")
    assert got is not None and got.status == "running"

    updated = st.update("r1", status="succeeded", cost_usd=0.02)
    assert updated.status == "succeeded"
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
    st.add(RunRecord(run_id="r1", task_id="t1", backend="opencode", status="succeeded"))
    path = next((tmp_path / "runs").iterdir())
    mtime_before = path.stat().st_mtime

    # Predicate returns False: nothing should be written.
    result = st.update_if("r1", lambda r: False, status="cancelled")
    assert result.status == "succeeded"  # the un-modified record

    mtime_after = path.stat().st_mtime
    assert mtime_before == mtime_after  # file untouched, predicate short-circuited the write

    # And: the file is still readable as the original record
    assert st.get("r1").status == "succeeded"


def test_update_if_predicate_true_writes_and_returns_new(tmp_path: Path) -> None:
    # The happy path: predicate True -> the update is applied and the returned record carries
    # the new field. Locks the contract: update_if returns the new record on success, not None.
    st = FleetState(tmp_path / "runs")
    st.add(RunRecord(run_id="r1", task_id="t1", backend="opencode", status="running"))
    result = st.update_if("r1", lambda r: r.status == "running", status="succeeded", cost_usd=0.5)
    assert result.status == "succeeded"
    assert result.cost_usd == 0.5


# --- state.add: documents the upsert semantics (run_id is the natural key) ------------------


def test_add_with_existing_run_id_clobbers(tmp_path: Path) -> None:
    # state.add is named "add" but behaves as upsert: adding a second record with the same
    # run_id overwrites the first. Fleet._start never collides (run_id includes a uuid hex
    # suffix), so this is a no-op in production; the test pins the behavior so a future
    # refactor that turns add() into a strict create has to make a deliberate decision.
    st = FleetState(tmp_path / "runs")
    st.add(RunRecord(run_id="r1", task_id="t1", backend="opencode", status="running", cost_usd=0.1))
    st.add(RunRecord(run_id="r1", task_id="t1", backend="opencode", status="succeeded", cost_usd=0.9))
    recs = st.list()
    assert len(recs) == 1
    assert recs[0].status == "succeeded"
    assert recs[0].cost_usd == 0.9
