"""Tests for FleetState persistence (one JSON file per run)."""

from __future__ import annotations

from concurrent.futures import ThreadPoolExecutor
from pathlib import Path

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


def test_persists_across_instances(tmp_path: Path) -> None:
    d = tmp_path / "runs"
    FleetState(d).add(RunRecord(run_id="r1", task_id="t1", backend="cursor"))
    reopened = FleetState(d).get("r1")
    assert reopened is not None and reopened.backend == "cursor"


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
