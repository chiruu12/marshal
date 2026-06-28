"""Edge-case + regression tests for the complex, high-consequence paths.

These target the code an adversarial audit flagged as most likely to harbor bugs: the git merge-back
state machine (integrate), worktree git ops, the per-run state layer under concurrent writers, run
cancellation, and the run/spawn lifecycle. Each "regression" test would FAIL against the code before
its fix; the rest pin correct-but-previously-untested behavior.
"""

from __future__ import annotations

import os
import subprocess
import sys
import threading
import time
from pathlib import Path

import pytest

from marshal_engine import (
    AgentResult,
    Capabilities,
    PermissionMode,
    RunOpts,
    RunStatus,
    TaskSpec,
    UsageRecord,
    UsageSource,
)
from marshal_engine.backends.base import CodingAgentBackend
from marshal_engine.fleet import Fleet, RunRequest
from marshal_engine.state import FleetState, RunRecord
from marshal_engine.worktree import WorktreeError, WorktreeManager


# --- fakes -----------------------------------------------------------------------------------


class _Printer(CodingAgentBackend):
    name = "printer"
    binary = "python"
    capabilities = Capabilities()

    def check_available(self) -> bool:
        return True

    def build_invocation(self, task: TaskSpec, opts: RunOpts) -> list[str]:
        return [sys.executable, "-c", "print('ok')"]

    def map_permission(self, mode: PermissionMode) -> list[str]:
        return []

    def parse_output(self, raw_stdout: str, raw_stderr: str, exit_code: int) -> AgentResult:
        return AgentResult(status=RunStatus.SUCCEEDED, text=raw_stdout.strip(), exit_code=exit_code)


class _PerTaskWriter(CodingAgentBackend):
    """Writes a file named after the task id (disjoint files => parallel runs don't conflict)."""

    name = "writer"
    binary = "python"
    capabilities = Capabilities()

    def check_available(self) -> bool:
        return True

    def build_invocation(self, task: TaskSpec, opts: RunOpts) -> list[str]:
        return [sys.executable, "-c", f"open({task.id!r}+'.txt','w').write('x'); print('done')"]

    def map_permission(self, mode: PermissionMode) -> list[str]:
        return []

    def parse_output(self, raw_stdout: str, raw_stderr: str, exit_code: int) -> AgentResult:
        return AgentResult(
            status=RunStatus.SUCCEEDED if exit_code == 0 else RunStatus.FAILED,
            text=raw_stdout.strip(),
            usage=UsageRecord(backend="writer", cost_usd=0.001, source=UsageSource.NATIVE),
            exit_code=exit_code,
        )


class _Committer(CodingAgentBackend):
    """Self-commits A.txt onto the branch, then leaves B.txt uncommitted (like a multi-commit agent)."""

    name = "committer"
    binary = "python"
    capabilities = Capabilities()

    def check_available(self) -> bool:
        return True

    def build_invocation(self, task: TaskSpec, opts: RunOpts) -> list[str]:
        script = (
            "import subprocess as s; open('A.txt','w').write('a');"
            "s.run(['git','add','A.txt'],check=True);"
            "s.run(['git','commit','--no-verify','-q','-m','agent','A.txt'],check=True);"
            "open('B.txt','w').write('b'); print('done')"
        )
        return [sys.executable, "-c", script]

    def map_permission(self, mode: PermissionMode) -> list[str]:
        return []

    def parse_output(self, raw_stdout: str, raw_stderr: str, exit_code: int) -> AgentResult:
        return AgentResult(
            status=RunStatus.SUCCEEDED if exit_code == 0 else RunStatus.FAILED,
            text=raw_stdout.strip(),
            exit_code=exit_code,
        )


class _NoOp(CodingAgentBackend):
    name = "noop"
    binary = "python"
    capabilities = Capabilities()

    def check_available(self) -> bool:
        return True

    def build_invocation(self, task: TaskSpec, opts: RunOpts) -> list[str]:
        return [sys.executable, "-c", "pass"]

    def map_permission(self, mode: PermissionMode) -> list[str]:
        return []

    def parse_output(self, raw_stdout: str, raw_stderr: str, exit_code: int) -> AgentResult:
        return AgentResult(status=RunStatus.SUCCEEDED, text="", exit_code=exit_code)


class _Exploder(CodingAgentBackend):
    name = "boom"
    binary = "python"
    capabilities = Capabilities()

    def check_available(self) -> bool:
        return True

    def build_invocation(self, task: TaskSpec, opts: RunOpts) -> list[str]:
        return [sys.executable, "-c", "print('hi')"]

    def map_permission(self, mode: PermissionMode) -> list[str]:
        return []

    def parse_output(self, raw_stdout: str, raw_stderr: str, exit_code: int) -> AgentResult:
        raise RuntimeError("kaboom")


class _LongSleeper(CodingAgentBackend):
    """A real 30s subprocess so a cancel/SIGTERM has something live to kill."""

    name = "sleeper"
    binary = "python"
    capabilities = Capabilities()

    def check_available(self) -> bool:
        return True

    def build_invocation(self, task: TaskSpec, opts: RunOpts) -> list[str]:
        return [sys.executable, "-c", "import time; time.sleep(30)"]

    def map_permission(self, mode: PermissionMode) -> list[str]:
        return []

    def parse_output(self, raw_stdout: str, raw_stderr: str, exit_code: int) -> AgentResult:
        return AgentResult(
            status=RunStatus.SUCCEEDED if exit_code == 0 else RunStatus.FAILED,
            text=raw_stdout.strip(),
            exit_code=exit_code,
        )


class _PeakCounter(CodingAgentBackend):
    """Tracks peak concurrent runs (run() overridden, no subprocess) to test the concurrency cap."""

    name = "pk"
    binary = "python"
    capabilities = Capabilities()

    def __init__(self) -> None:
        self._lock = threading.Lock()
        self.active = 0
        self.peak = 0

    def check_available(self) -> bool:
        return True

    def build_invocation(self, task: TaskSpec, opts: RunOpts) -> list[str]:
        return [sys.executable, "-c", "pass"]

    def map_permission(self, mode: PermissionMode) -> list[str]:
        return []

    def parse_output(self, raw_stdout: str, raw_stderr: str, exit_code: int) -> AgentResult:
        return AgentResult(status=RunStatus.SUCCEEDED, text="ok", exit_code=exit_code)

    def run(self, task: TaskSpec, opts: RunOpts) -> AgentResult:  # type: ignore[override]
        with self._lock:
            self.active += 1
            self.peak = max(self.peak, self.active)
        time.sleep(0.2)
        with self._lock:
            self.active -= 1
        return AgentResult(status=RunStatus.SUCCEEDED, text="ok", exit_code=0)


def _init_repo(root: Path) -> None:
    def git(*a: str) -> None:
        subprocess.run(["git", "-C", str(root), *a], check=True, capture_output=True, text=True)

    git("init", "-q")
    git("config", "user.email", "t@t")
    git("config", "user.name", "t")
    (root / "README.md").write_text("hi")
    git("add", "-A")
    git("commit", "-q", "-m", "init")


@pytest.fixture
def repo(tmp_path: Path) -> Path:
    r = tmp_path / "repo"
    r.mkdir()
    _init_repo(r)
    return r


def _porcelain(repo: Path) -> str:
    return subprocess.run(
        ["git", "-C", str(repo), "status", "--porcelain"], capture_output=True, text=True
    ).stdout.strip()


# --- state layer: concurrent writers (regressions for the per-run lock + unique temp) --------


def test_state_concurrent_updates_same_run_no_crash(tmp_path: Path) -> None:
    st = FleetState(tmp_path / "runs")
    st.add(RunRecord(run_id="r1", task_id="t", backend="b", status="running"))
    errors: list[Exception] = []

    def worker(n: int) -> None:
        try:
            for _ in range(25):
                st.update("r1", output_tokens=n)
        except Exception as exc:  # noqa: BLE001 - capture so the assert reports it
            errors.append(exc)

    threads = [threading.Thread(target=worker, args=(i,)) for i in range(8)]
    for t in threads:
        t.start()
    for t in threads:
        t.join()
    assert errors == []  # a fixed temp name would have raised FileNotFoundError on os.replace
    assert st.get("r1") is not None  # file intact + readable


def test_state_update_if_respects_predicate(tmp_path: Path) -> None:
    st = FleetState(tmp_path / "runs")
    st.add(RunRecord(run_id="r1", task_id="t", backend="b", status="succeeded"))
    out = st.update_if("r1", lambda r: r.status == "running", status="cancelled")
    assert out.status == "succeeded"  # predicate false -> no overwrite of a terminal status
    st.update("r1", status="running")
    out = st.update_if("r1", lambda r: r.status == "running", status="cancelled")
    assert out.status == "cancelled"  # predicate true -> updated


def test_state_list_skips_binary_file(tmp_path: Path) -> None:
    runs = tmp_path / "runs"
    runs.mkdir()
    st = FleetState(runs)
    st.add(RunRecord(run_id="good", task_id="t", backend="b"))
    (runs / "garbage.json").write_bytes(b"\xff\xfe\x00not utf8")  # UnicodeDecodeError on read
    assert [r.run_id for r in st.list()] == ["good"]  # skipped, not a crashed listing


# --- worktree: has_unmerged_commits fails safe (regression) ----------------------------------


def test_has_unmerged_commits_raises_on_git_error(repo: Path) -> None:
    wm = WorktreeManager(repo)
    with pytest.raises(WorktreeError):
        wm.has_unmerged_commits("no-such-branch", "also-missing")


# --- base.run: on_pid failure must not escape / leak (regression) ----------------------------


def test_on_pid_failure_does_not_escape(tmp_path: Path, capsys: pytest.CaptureFixture[str]) -> None:
    def boom(_pid: int) -> None:
        raise RuntimeError("pid record failed")

    res = _Printer().run(TaskSpec(id="t", goal="x"), RunOpts(cwd=tmp_path, on_pid=boom, timeout_s=30))
    assert res.status is RunStatus.SUCCEEDED  # the callback error didn't escape (would skip communicate)
    assert "on_pid callback failed" in capsys.readouterr().err


# --- spawn lifecycle: a dead pool must not strand a RUNNING record (regression) --------------


def test_spawn_after_pool_shutdown_stamps_failed(repo: Path) -> None:
    fleet = Fleet(repo, {"printer": _Printer()})
    fleet._executor()  # build the pool...
    fleet._bg.shutdown(wait=True)  # type: ignore[union-attr]  # ...then kill it without nulling _bg
    with pytest.raises(RuntimeError):
        fleet.spawn(RunRequest(backend_name="printer", task=TaskSpec(id="sp", goal="x")))
    recs = fleet.state.list()
    assert len(recs) == 1 and recs[0].status == "failed"  # not a stranded RUNNING zombie


# --- run_many: one failure must not abort the batch -----------------------------------------


def test_run_many_mixed_batch_survives_a_failure(repo: Path) -> None:
    fleet = Fleet(repo, {"writer": _PerTaskWriter(), "boom": _Exploder()})
    recs = fleet.run_many(
        [
            RunRequest(backend_name="writer", task=TaskSpec(id="ok", goal="x")),
            RunRequest(backend_name="boom", task=TaskSpec(id="bad", goal="x")),
        ]
    )
    by_task = {r.task_id: r for r in recs}
    assert [r.task_id for r in recs] == ["ok", "bad"]  # order preserved
    assert by_task["ok"].status == "succeeded"
    assert by_task["bad"].status == "failed"


def test_run_many_respects_max_concurrency(repo: Path) -> None:
    backend = _PeakCounter()
    fleet = Fleet(repo, {"pk": backend})
    reqs = [RunRequest(backend_name="pk", task=TaskSpec(id=f"j{i}", goal="x")) for i in range(4)]
    fleet.run_many(reqs, max_concurrency=2)
    assert backend.peak <= 2  # the pool cap is actually enforced (OOM guard)


# --- integrate state machine: edge cases + regressions --------------------------------------


def test_integrate_reports_committed_and_uncommitted_files(repo: Path) -> None:
    # Regression: the merged result must list EVERY file the branch lands (a self-committed file +
    # an uncommitted one), not just the last uncommitted delta.
    fleet = Fleet(repo, {"committer": _Committer()})
    rec = fleet.run("committer", TaskSpec(id="c1", goal="x"))
    assert rec.status == "succeeded"
    res = fleet.integrate(rec.run_id)
    assert res.status == "merged"
    assert set(res.changed_files) == {"A.txt", "B.txt"}


def test_integrate_refuses_a_running_run(repo: Path) -> None:
    fleet = Fleet(repo, {"sleeper": _LongSleeper()})
    run_id = fleet.spawn(RunRequest(backend_name="sleeper", task=TaskSpec(id="sl", goal="x")))
    try:
        deadline = time.monotonic() + 10
        while time.monotonic() < deadline:
            r = fleet.state.get(run_id)
            # wait for the pid too, so the finally's cancel can actually kill it (else shutdown
            # would block on the full sleep)
            if r and r.status == "running" and r.worktree and r.pid is not None:
                break
            time.sleep(0.05)
        res = fleet.integrate(run_id)  # must NOT commit the agent's half-written worktree
        assert res.status == "blocked"
        assert "in progress" in res.message
    finally:
        fleet.cancel_run(run_id)
        fleet.shutdown()


def test_integrate_empty_for_a_noop_run(repo: Path) -> None:
    fleet = Fleet(repo, {"noop": _NoOp()})
    rec = fleet.run("noop", TaskSpec(id="n1", goal="x"))
    res = fleet.integrate(rec.run_id)
    assert res.status == "empty"


def test_integrate_cleanup_removes_worktree(repo: Path) -> None:
    fleet = Fleet(repo, {"writer": _PerTaskWriter()})
    rec = fleet.run("writer", TaskSpec(id="w1", goal="x"))
    wt = Path(rec.worktree or "")
    assert wt.exists()
    res = fleet.integrate(rec.run_id, cleanup=True)
    assert res.status == "merged"
    assert not wt.exists()


def test_concurrent_integrates_do_not_corrupt_repo(repo: Path) -> None:
    # The per-Fleet integrate lock must serialize two merges into the shared checkout so they can't
    # race git's index.lock and leave the repo mid-merge.
    fleet = Fleet(repo, {"writer": _PerTaskWriter()})
    r1 = fleet.run("writer", TaskSpec(id="aa", goal="x"))
    r2 = fleet.run("writer", TaskSpec(id="bb", goal="x"))
    results: dict[str, str] = {}

    def integ(run_id: str) -> None:
        results[run_id] = fleet.integrate(run_id).status

    threads = [threading.Thread(target=integ, args=(r.run_id,)) for r in (r1, r2)]
    for t in threads:
        t.start()
    for t in threads:
        t.join()
    assert results[r1.run_id] == "merged" and results[r2.run_id] == "merged"
    # the corruption the lock prevents: a checkout left mid-merge
    merge_head = subprocess.run(
        ["git", "-C", str(repo), "rev-parse", "-q", "--verify", "MERGE_HEAD"],
        capture_output=True, text=True,
    )
    assert merge_head.returncode != 0, "repo left mid-merge"
    # no conflicted/modified TRACKED files (ignore the engine's own untracked .marshal/ state dir)
    dirty = [ln for ln in _porcelain(repo).splitlines() if ".marshal" not in ln]
    assert dirty == [], dirty
    assert (repo / "aa.txt").exists() and (repo / "bb.txt").exists()  # both runs landed


# --- cancel_run: end-to-end against a real process ------------------------------------------


def test_cancel_real_spawned_run_kills_the_process(repo: Path) -> None:
    fleet = Fleet(repo, {"sleeper": _LongSleeper()})
    run_id = fleet.spawn(RunRequest(backend_name="sleeper", task=TaskSpec(id="sl", goal="x")))
    try:
        deadline = time.monotonic() + 10
        pid = None
        while time.monotonic() < deadline:
            r = fleet.state.get(run_id)
            if r and r.pid is not None and r.status == "running":
                pid = r.pid
                break
            time.sleep(0.05)
        assert pid is not None, "spawned run never recorded a pid"

        rec = fleet.cancel_run(run_id)
        assert rec.status in {"cancelled", "failed", "timed_out"}  # cancellation took effect

        # the real OS process group must actually die (SIGTERM), not linger
        gone_by = time.monotonic() + 10
        while time.monotonic() < gone_by:
            try:
                os.kill(pid, 0)
                time.sleep(0.05)
            except ProcessLookupError:
                break
        else:
            pytest.fail(f"process {pid} survived cancel_run")
    finally:
        fleet.shutdown()
