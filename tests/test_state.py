"""Tests for FleetState persistence."""

from __future__ import annotations

from pathlib import Path

from marshal_engine.state import FleetState, RunRecord


def test_add_get_update_list(tmp_path: Path) -> None:
    st = FleetState(tmp_path / "fleet.json")
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
    p = tmp_path / "fleet.json"
    FleetState(p).add(RunRecord(run_id="r1", task_id="t1", backend="cursor"))
    reopened = FleetState(p).get("r1")
    assert reopened is not None and reopened.backend == "cursor"
