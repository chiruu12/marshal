"""Tests for FleetState persistence (one JSON file per run)."""

from __future__ import annotations

import time
from concurrent.futures import ThreadPoolExecutor
from pathlib import Path

import pytest
from pydantic import ValidationError

import marshal_engine.state as state_mod
from marshal_engine.state import FleetState, RunRecord
from marshal_engine.types import RunStatus

# Sleep inside the locked RMW (between read and write) long enough that a one-shot wave of
# concurrent writers all observe the same pre-update record before any write lands. Without
# flock that yields reliable final-state sibling-field loss; with flock they serialize and
# both fields survive. Production leaves ``_rmw_between_read_write`` unset.
_RMW_RACE_DELAY_S = 0.05


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


@pytest.mark.parametrize(
    "bad_id",
    ["", ".", "..", "../x", "foo/bar", "a\\b", ".hidden", "-lead", "café", "a\x00b", "a" * 129],
)
def test_get_update_refuse_unsafe_run_id(tmp_path: Path, bad_id: str) -> None:
    # The ledger filename is `<run_id>.json`: an unvalidated id is a path-traversal read/write
    # outside the runs dir (and a cross-workspace escape once the id is stat'ed across repos).
    # get/update/update_if all funnel through the one validated _path, which refuses fail-closed.
    st = FleetState(tmp_path / "runs")
    st.add(RunRecord(run_id="r1", task_id="t1", backend="opencode"))
    with pytest.raises(ValueError, match="unsafe run_id"):
        st.get(bad_id)
    with pytest.raises(ValueError, match="unsafe run_id"):
        st.update(bad_id, status="failed")
    with pytest.raises(ValueError, match="unsafe run_id"):
        st.update_if(bad_id, lambda r: True, status="failed")
    # nothing escaped the runs dir, and the real record is untouched
    got = st.get("r1")
    assert got is not None and got.status == "queued"
    assert [p.name for p in tmp_path.rglob("*.json")] == ["r1.json"]


def test_add_refuses_unsafe_run_id(tmp_path: Path) -> None:
    # The write path funnels through the same validated _path, so a poisoned record can never
    # land outside the ledger dir either.
    st = FleetState(tmp_path / "runs")
    with pytest.raises(ValueError, match="unsafe run_id"):
        st.add(RunRecord(run_id="../escape", task_id="t1", backend="opencode"))
    assert not (tmp_path / "escape.json").exists()


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


# --- cross-process run-record integrity (issue #144 / M9) ------------------------------------


class TestCrossProcessRunRecordUpdates:
    """Run-record RMW must serialize across processes, not only threads.

    Two FleetState instances (separate in-process locks) or two real OS processes updating the
    same ``runs/<id>.json`` used to interleave read-modify-write and drop a sibling field
    (e.g. ``merged_into`` clobbered by a concurrent terminal stamp). The ``.json.lock`` flock
    closes that hole.
    """

    def test_separate_lock_holders_do_not_lose_sibling_fields(
        self, tmp_path: Path, monkeypatch: pytest.MonkeyPatch
    ) -> None:
        runs = tmp_path / "runs"
        FleetState(runs).add(
            RunRecord(run_id="r1", task_id="t1", backend="opencode", status="running")
        )
        # Separate FleetState instances => independent threading.Lock maps; only the flock
        # serializes them (same shape as a CLI + MCP server on one repo).
        # One-shot wave + RMW delay: without flock every writer reads the initial record and
        # the last write drops a sibling field; with flock, updates serialize and both survive.
        monkeypatch.setattr(
            state_mod, "_rmw_between_read_write", lambda: time.sleep(_RMW_RACE_DELAY_S)
        )
        writers_per_role = 4

        def stamp_merged() -> None:
            FleetState(runs).update("r1", merged_into="main")

        def stamp_terminal() -> None:
            FleetState(runs).update("r1", status="exited_clean", cost_usd=0.5)

        with ThreadPoolExecutor(max_workers=writers_per_role * 2) as pool:
            futures = [
                pool.submit(stamp_merged) for _ in range(writers_per_role)
            ] + [pool.submit(stamp_terminal) for _ in range(writers_per_role)]
            for fut in futures:
                fut.result()

        got = FleetState(runs).get("r1")
        assert got is not None
        assert got.merged_into == "main", "merged_into was lost to an interleaved terminal stamp"
        assert got.status == "exited_clean", "terminal status was lost to an interleaved merge stamp"
        assert got.cost_usd > 0.0

    def test_concurrent_os_processes_do_not_lose_sibling_fields(self, tmp_path: Path) -> None:
        import subprocess
        import sys

        runs = tmp_path / "runs"
        FleetState(runs).add(
            RunRecord(run_id="r1", task_id="t1", backend="opencode", status="running")
        )
        worker = r"""
import sys
import time
from pathlib import Path

import marshal_engine.state as state_mod
from marshal_engine.state import FleetState

state_mod._rmw_between_read_write = lambda: time.sleep(0.05)

runs = Path(sys.argv[1])
role = sys.argv[2]
st = FleetState(runs)
if role == "merged":
    st.update("r1", merged_into="main")
else:
    st.update("r1", status="exited_clean", cost_usd=0.5)
"""
        # Eight one-shot processes (4 merged + 4 terminal) with an injected RMW delay: without
        # flock, sibling fields are lost in the final record; with flock, both survive.
        roles = ["merged"] * 4 + ["terminal"] * 4
        procs = [
            subprocess.Popen(
                [sys.executable, "-c", worker, str(runs), role],
            )
            for role in roles
        ]
        for proc in procs:
            assert proc.wait(timeout=120) == 0

        got = FleetState(runs).get("r1")
        assert got is not None
        assert got.merged_into == "main", "merged_into was lost across OS-process writers"
        assert got.status == "exited_clean", "terminal status was lost across OS-process writers"
        assert got.cost_usd > 0.0
        # Sidecar must not be mistaken for a run record.
        assert (runs / "r1.json.lock").exists()
        assert {r.run_id for r in FleetState(runs).list()} == {"r1"}


def test_list_skips_torn_record_and_warns_with_count(
    tmp_path: Path, capsys: pytest.CaptureFixture[str]
) -> None:
    """Torn/corrupt run JSON is skipped; listing succeeds and stderr names the skipped count."""
    runs = tmp_path / "runs"
    runs.mkdir()
    st = FleetState(runs)
    st.add(RunRecord(run_id="good", task_id="t", backend="b", status="running"))
    (runs / "torn.json").write_text("{not json", encoding="utf-8")
    (runs / "partial.json").write_text(
        '{"run_id":"partial","task_id":"t","backend":"b"', encoding="utf-8"
    )

    assert [r.run_id for r in st.list()] == ["good"]
    err = capsys.readouterr().err
    assert "skipping 2 unreadable run record" in err


def test_list_skips_binary_file_and_warns(
    tmp_path: Path, capsys: pytest.CaptureFixture[str]
) -> None:
    runs = tmp_path / "runs"
    runs.mkdir()
    st = FleetState(runs)
    st.add(RunRecord(run_id="good", task_id="t", backend="b"))
    (runs / "garbage.json").write_bytes(b"\xff\xfe\x00not utf8")

    assert [r.run_id for r in st.list()] == ["good"]
    assert "skipping 1 unreadable run record" in capsys.readouterr().err


def test_structured_field_round_trips_and_legacy_records_load_without_it(tmp_path: Path) -> None:
    """Additive optional field: old ledgers without `structured` load; new values persist."""
    d = tmp_path / "runs"
    st = FleetState(d)
    st.add(RunRecord(run_id="legacy", task_id="t", backend="cursor"))
    got = st.get("legacy")
    assert got is not None and got.structured is None

    st.update("legacy", structured={"score": 4}, status="exited_clean")
    reopened = FleetState(d).get("legacy")
    assert reopened is not None
    assert reopened.structured == {"score": 4}

    # A pre-field on-disk record (no structured key) must still validate.
    raw_path = d / "prefield.json"
    raw_path.write_text(
        '{"run_id":"prefield","task_id":"t","backend":"cursor","status":"exited_clean"}',
        encoding="utf-8",
    )
    loaded = FleetState(d).get("prefield")
    assert loaded is not None and loaded.structured is None


def test_base_commit_sha_loads_and_branch_name_poison_is_stripped(tmp_path: Path) -> None:
    """Valid sha base_commit round-trips; a pre-fix branch-name value loads as None (#173)."""
    d = tmp_path / "runs"
    st = FleetState(d)
    sha = "90edd39921345d5a4262f9c848c304a6007eb890"
    st.add(
        RunRecord(
            run_id="good",
            task_id="t",
            backend="cursor",
            base_commit=sha,
            branch="marshal/good",
        )
    )
    got = st.get("good")
    assert got is not None and got.base_commit == sha

    raw_path = d / "poisoned.json"
    raw_path.write_text(
        '{"run_id":"poisoned","task_id":"t","backend":"cursor",'
        '"status":"exited_clean","branch":"marshal/poisoned",'
        '"base_commit":"marshal/poisoned"}',
        encoding="utf-8",
    )
    poisoned = FleetState(d).get("poisoned")
    assert poisoned is not None
    assert poisoned.base_commit is None  # stripped, record still loads
