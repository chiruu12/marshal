"""Tests for the usage tracker (file IO + aggregation; deterministic, no network)."""

from __future__ import annotations

from pathlib import Path
from typing import Any

from marshal_engine import AgentResult, RunStatus, UsageRecord, UsageSource
from marshal_engine.usage import UsageEvent, UsageTracker


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
    assert s["totals"]["runs"] == 3
    assert abs(s["totals"]["cost_usd"] - 0.03) < 1e-9
    assert s["by_backend"]["opencode"]["runs"] == 2
    assert abs(s["by_backend"]["opencode"]["cost_usd"] - 0.03) < 1e-9
    assert s["by_backend"]["cursor"]["runs"] == 1
    assert s["by_backend"]["opencode"]["input_tokens"] == 300
    assert t.summary_path.exists()


def test_from_result_builds_event() -> None:
    res = AgentResult(
        status=RunStatus.SUCCEEDED,
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
    assert ev.status == "succeeded"
    assert ev.source == "native"
    assert ev.model == "opencode-go/glm-5.2"


def test_empty_tracker(tmp_path: Path) -> None:
    t = UsageTracker(tmp_path / "usage")
    assert t.events() == []
    assert t.summary()["totals"]["runs"] == 0
    assert t.summary()["totals"]["cost_per_succeeded"] is None  # no successes -> not claimable


def test_cost_per_outcome_and_source_split(tmp_path: Path) -> None:
    t = UsageTracker(tmp_path / "usage")
    t.record(_ev(run_id="r1", cost_usd=0.02, status="succeeded", source="native"))
    t.record(_ev(run_id="r2", cost_usd=0.04, status="succeeded", source="estimated"))
    t.record(_ev(run_id="r3", cost_usd=0.00, status="empty", source="unavailable"))  # cost, no success

    tot = t.summary()["totals"]
    assert tot["runs"] == 3
    assert tot["succeeded"] == 2
    assert abs(tot["cost_usd"] - 0.06) < 1e-9
    assert abs(tot["cost_native"] - 0.02) < 1e-9
    assert abs(tot["cost_estimated"] - 0.04) < 1e-9     # estimate kept separate from native
    assert abs(tot["cost_per_run"] - 0.02) < 1e-9       # 0.06 / 3
    assert abs(tot["cost_per_succeeded"] - 0.03) < 1e-9  # 0.06 / 2 (failures/empties still cost)
