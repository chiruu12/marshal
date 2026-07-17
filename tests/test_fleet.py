"""Integration test for the Fleet orchestrator using a dummy file-writing backend (no network)."""

from __future__ import annotations

import subprocess
import sys
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
from marshal_engine.config import BudgetSpec
from marshal_engine.eastrouter import ExternalCost
from marshal_engine.fleet import Fleet, RunRequest
from marshal_engine.pricing import ModelPrice, PriceTable
from marshal_engine.retry import RetryPolicy
from marshal_engine.worktree import WorktreeError


class _Writer(CodingAgentBackend):
    name = "writer"
    binary = "python"
    capabilities = Capabilities()

    def check_available(self) -> bool:
        return True

    def build_invocation(self, task: TaskSpec, opts: RunOpts) -> list[str]:
        return [sys.executable, "-c", "open('out.txt','w').write('hi'); print('done')"]

    def map_permission(self, mode: PermissionMode) -> list[str]:
        return []

    def parse_output(self, raw_stdout: str, raw_stderr: str, exit_code: int) -> AgentResult:
        return AgentResult(
            status=RunStatus.SUCCEEDED if exit_code == 0 else RunStatus.FAILED,
            text=raw_stdout.strip(),
            usage=UsageRecord(
                backend="writer",
                input_tokens=5,
                output_tokens=1,
                cost_usd=0.001,
                source=UsageSource.NATIVE,
            ),
            exit_code=exit_code,
        )


class _Patcher(CodingAgentBackend):
    """Rewrites a tracked file with task-specific content - used to force merge conflicts."""

    name = "patcher"
    binary = "python"
    capabilities = Capabilities()

    def check_available(self) -> bool:
        return True

    def build_invocation(self, task: TaskSpec, opts: RunOpts) -> list[str]:
        return [sys.executable, "-c", f"open('README.md','w').write({task.id!r})"]

    def map_permission(self, mode: PermissionMode) -> list[str]:
        return []

    def parse_output(self, raw_stdout: str, raw_stderr: str, exit_code: int) -> AgentResult:
        return AgentResult(
            status=RunStatus.SUCCEEDED if exit_code == 0 else RunStatus.FAILED,
            exit_code=exit_code,
        )


class _Sleeper(CodingAgentBackend):
    """Sleeps then prints - used to prove run_many actually runs concurrently."""

    name = "sleeper"
    binary = "python"
    capabilities = Capabilities()

    def check_available(self) -> bool:
        return True

    def build_invocation(self, task: TaskSpec, opts: RunOpts) -> list[str]:
        return [sys.executable, "-c", "import time; time.sleep(0.5); print('done')"]

    def map_permission(self, mode: PermissionMode) -> list[str]:
        return []

    def parse_output(self, raw_stdout: str, raw_stderr: str, exit_code: int) -> AgentResult:
        return AgentResult(
            status=RunStatus.SUCCEEDED if exit_code == 0 else RunStatus.FAILED,
            text=raw_stdout.strip(),
            exit_code=exit_code,
        )


class _NoOp(CodingAgentBackend):
    """Exits 0 but writes nothing and prints nothing - should be recorded as EMPTY, not success."""

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
        return AgentResult(
            status=RunStatus.SUCCEEDED if exit_code == 0 else RunStatus.FAILED,
            text=raw_stdout.strip(),
            exit_code=exit_code,
        )


class _Tokened(CodingAgentBackend):
    """Reports tokens but no cost (like Codex) - the engine must price it via the table."""

    name = "tok"
    binary = "python"
    capabilities = Capabilities()

    def check_available(self) -> bool:
        return True

    def build_invocation(self, task: TaskSpec, opts: RunOpts) -> list[str]:
        return [sys.executable, "-c", "print('done')"]

    def map_permission(self, mode: PermissionMode) -> list[str]:
        return []

    def parse_output(self, raw_stdout: str, raw_stderr: str, exit_code: int) -> AgentResult:
        return AgentResult(
            status=RunStatus.SUCCEEDED,
            text=raw_stdout.strip(),
            usage=UsageRecord(
                backend="tok",
                model="m",
                input_tokens=1_000_000,
                output_tokens=0,
                source=UsageSource.UNAVAILABLE,  # tokens known, cost not - engine prices it
            ),
            exit_code=exit_code,
        )


class _SilentWriter(CodingAgentBackend):
    """Writes a file but returns empty text - a write-only success that must NOT be marked EMPTY."""

    name = "silent"
    binary = "python"
    capabilities = Capabilities()

    def check_available(self) -> bool:
        return True

    def build_invocation(self, task: TaskSpec, opts: RunOpts) -> list[str]:
        return [sys.executable, "-c", "open('made.txt','w').write('x')"]  # writes, prints nothing

    def map_permission(self, mode: PermissionMode) -> list[str]:
        return []

    def parse_output(self, raw_stdout: str, raw_stderr: str, exit_code: int) -> AgentResult:
        return AgentResult(
            status=RunStatus.SUCCEEDED if exit_code == 0 else RunStatus.FAILED,
            text=raw_stdout.strip(),  # empty - the only success signal is the file it wrote
            exit_code=exit_code,
        )


class _NativeZero(CodingAgentBackend):
    """Backend that authoritatively reports a $0 cost with tokens (e.g. a free/local model)."""

    name = "nz"
    binary = "python"
    capabilities = Capabilities()

    def check_available(self) -> bool:
        return True

    def build_invocation(self, task: TaskSpec, opts: RunOpts) -> list[str]:
        return [sys.executable, "-c", "print('done')"]

    def map_permission(self, mode: PermissionMode) -> list[str]:
        return []

    def parse_output(self, raw_stdout: str, raw_stderr: str, exit_code: int) -> AgentResult:
        return AgentResult(
            status=RunStatus.SUCCEEDED,
            text=raw_stdout.strip(),
            usage=UsageRecord(
                backend="nz",
                model="m",
                input_tokens=1_000_000,
                output_tokens=0,
                cost_usd=0.0,
                source=UsageSource.NATIVE,  # the backend really did report $0
            ),
            exit_code=exit_code,
        )


class _LimitedPerms(CodingAgentBackend):
    """Declares safe-edit + yolo only - used to prove permission preflight before worktree create."""

    name = "limited"
    binary = "python"
    capabilities = Capabilities(
        permission_modes=frozenset({PermissionMode.SAFE_EDIT, PermissionMode.YOLO}),
    )

    def check_available(self) -> bool:
        return True

    def build_invocation(self, task: TaskSpec, opts: RunOpts) -> list[str]:
        return [sys.executable, "-c", "open('out.txt','w').write('hi')"]

    def map_permission(self, mode: PermissionMode) -> list[str]:
        if mode not in self.capabilities.permission_modes:
            raise ValueError(f"limited: unsupported permission mode {mode!r}")
        return []

    def parse_output(self, raw_stdout: str, raw_stderr: str, exit_code: int) -> AgentResult:
        return AgentResult(
            status=RunStatus.SUCCEEDED if exit_code == 0 else RunStatus.FAILED,
            exit_code=exit_code,
        )


class _Exploder(CodingAgentBackend):
    """parse_output raises (propagates out of base.run) - the run loop must terminal-stamp it."""

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


class _Loudy(CodingAgentBackend):
    """Returns canned raw_stdout/raw_stderr on its AgentResult - the durably-persisted run log.

    `run()` is overridden, so no subprocess is spawned. The loudy streams are sized to prove no
    truncation (well past the 16KB cap on the run record's `text`).
    """

    name = "loudy"
    binary = "python"
    capabilities = Capabilities()

    def __init__(self, stdout: str = "", stderr: str = "", fail: bool = False) -> None:
        self._stdout = stdout
        self._stderr = stderr
        self._fail = fail

    def check_available(self) -> bool:
        return True

    def build_invocation(self, task: TaskSpec, opts: RunOpts) -> list[str]:
        return [sys.executable, "-c", "pass"]  # any no-op; run() is overridden below

    def map_permission(self, mode: PermissionMode) -> list[str]:
        return []

    def parse_output(self, raw_stdout: str, raw_stderr: str, exit_code: int) -> AgentResult:
        return AgentResult(
            status=RunStatus.SUCCEEDED if not self._fail else RunStatus.FAILED,
            text="short",
            raw_stdout=self._stdout,
            raw_stderr=self._stderr,
            error="forced failure" if self._fail else None,
        )

    def run(self, task: TaskSpec, opts: RunOpts) -> AgentResult:
        # Skip the subprocess: return the canned AgentResult directly. The full base.run() path
        # is exercised by the other dummy backends; this one exists purely to feed the log writer.
        return self.parse_output("", "", 0)


class _Flaky(CodingAgentBackend):
    """Returns canned results per call to drive the transient-retry loop deterministically.

    Each entry in `errors` is the error string for that attempt (None => succeed, writing a file so
    the run is a real SUCCEEDED, not EMPTY). `run()` is overridden, so no subprocess is spawned.
    """

    name = "flaky"
    binary = "python"
    capabilities = Capabilities()

    def __init__(self, errors: list[str | None]) -> None:
        self._errors = errors
        self.calls = 0

    def check_available(self) -> bool:
        return True

    def build_invocation(self, task: TaskSpec, opts: RunOpts) -> list[str]:
        return []

    def map_permission(self, mode: PermissionMode) -> list[str]:
        return []

    def parse_output(self, raw_stdout: str, raw_stderr: str, exit_code: int) -> AgentResult:
        return AgentResult(status=RunStatus.SUCCEEDED)

    def run(self, task: TaskSpec, opts: RunOpts) -> AgentResult:
        err = self._errors[self.calls] if self.calls < len(self._errors) else None
        self.calls += 1
        if err is None:
            (opts.cwd / "ok.txt").write_text("ok")  # a real change -> SUCCEEDED, not EMPTY
            return AgentResult(
                status=RunStatus.SUCCEEDED,
                text="ok",
                usage=UsageRecord(
                    backend="flaky", input_tokens=1, output_tokens=1, source=UsageSource.NATIVE
                ),
            )
        return AgentResult(status=RunStatus.FAILED, error=err)


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


def test_fleet_run_records_state_usage_and_writes(repo: Path) -> None:
    fleet = Fleet(repo, {"writer": _Writer()})
    rec = fleet.run(
        "writer",
        TaskSpec(id="t1", goal="x"),
        permission=PermissionMode.SAFE_EDIT,
        ts="2026-06-19T00:00:00Z",
    )
    assert rec.status == "succeeded"
    assert rec.cost_usd == 0.001
    assert rec.text == "done"  # the agent's final message is persisted for review
    assert rec.run_id.startswith("t1.writer.")  # task.backend.<uuid> - globally unique

    wt = Path(rec.worktree or "")
    assert wt.exists()  # kept by default for later collect/integrate
    assert (wt / "out.txt").read_text() == "hi"

    assert fleet.state.get(rec.run_id) is not None
    s = fleet.usage.summary()
    assert s.totals.runs == 1
    assert s.by_backend["writer"].runs == 1


# --- the verify gate: succeeded means the workspace's gate passed too -------------------------


def test_verify_pass_keeps_succeeded(repo: Path) -> None:
    fleet = Fleet(repo, {"writer": _Writer()}, verify=[sys.executable, "-c", "print('gate ok')"])
    rec = fleet.run("writer", TaskSpec(id="v1", goal="x"))
    assert rec.status == "succeeded"
    assert rec.verify_passed is True
    assert "gate ok" in rec.verify_output


def test_verify_fail_marks_verify_failed_and_keeps_worktree(repo: Path) -> None:
    fleet = Fleet(
        repo,
        {"writer": _Writer()},
        verify=[sys.executable, "-c", "import sys; print('regression here'); sys.exit(2)"],
    )
    rec = fleet.run("writer", TaskSpec(id="v2", goal="x"))
    assert rec.status == "verify_failed"
    assert rec.verify_passed is False
    assert "regression here" in rec.verify_output
    assert Path(rec.worktree or "").exists()  # the diff survives for review
    assert (Path(rec.worktree or "") / "out.txt").exists()
    # the usage event records the authoritative outcome (spend happened; run did not succeed)
    events = fleet.usage.events()
    assert [e.status for e in events if e.run_id == rec.run_id] == ["verify_failed"]


def test_verify_skipped_for_empty_run(repo: Path) -> None:
    # An EMPTY run never reaches the gate: nothing to verify, no wasted gate command.
    fleet = Fleet(
        repo, {"noop": _NoOp()}, verify=[sys.executable, "-c", "import sys; sys.exit(1)"]
    )
    rec = fleet.run("noop", TaskSpec(id="v3", goal="x"))
    assert rec.status == "empty"
    assert rec.verify_passed is None
    assert rec.verify_output == ""


def test_verify_timeout_marks_verify_failed(repo: Path) -> None:
    fleet = Fleet(
        repo, {"writer": _Writer()}, verify=[sys.executable, "-c", "import time; time.sleep(30)"]
    )
    fleet.worktrees.setup_timeout_s = 1  # verify reuses the setup timeout knob
    rec = fleet.run("writer", TaskSpec(id="v4", goal="x"))
    assert rec.status == "verify_failed"
    assert "timed out" in rec.verify_output


def test_clean_finished_reclaims_verify_failed(repo: Path) -> None:
    fleet = Fleet(
        repo, {"writer": _Writer()}, verify=[sys.executable, "-c", "import sys; sys.exit(1)"]
    )
    rec = fleet.run("writer", TaskSpec(id="v5", goal="x"))
    assert rec.status == "verify_failed"
    result = fleet.clean()  # scope="finished" - a post-review action
    assert rec.run_id in result.removed
    assert not Path(rec.worktree or "").exists()


def test_fleet_unknown_backend(repo: Path) -> None:
    fleet = Fleet(repo, {})
    with pytest.raises(ValueError):
        fleet.run("nope", TaskSpec(id="t", goal="x"))


def test_transient_failure_is_retried_then_succeeds(repo: Path) -> None:
    backend = _Flaky(["opencode: database is locked"])  # fail once (transient), then succeed
    fleet = Fleet(repo, {"flaky": backend}, retries=RetryPolicy(max_attempts=3, backoff_base_s=0.0))
    rec = fleet.run("flaky", TaskSpec(id="t", goal="x"))
    assert rec.status == "succeeded"
    assert rec.attempts == 2          # one retry was needed
    assert backend.calls == 2


def test_non_transient_failure_is_not_retried(repo: Path) -> None:
    backend = _Flaky(["AssertionError: expected 2 got 3"])  # a genuine task failure, not transient
    fleet = Fleet(repo, {"flaky": backend}, retries=RetryPolicy(max_attempts=3, backoff_base_s=0.0))
    rec = fleet.run("flaky", TaskSpec(id="t", goal="x"))
    assert rec.status == "failed"
    assert rec.attempts == 1          # no retry for a real failure
    assert backend.calls == 1


def test_transient_retries_are_bounded(repo: Path) -> None:
    backend = _Flaky(["rate limit", "rate limit", "rate limit", "rate limit"])  # never recovers
    fleet = Fleet(repo, {"flaky": backend}, retries=RetryPolicy(max_attempts=3, backoff_base_s=0.0))
    rec = fleet.run("flaky", TaskSpec(id="t", goal="x"))
    assert rec.status == "failed"
    assert rec.attempts == 3          # capped at max_attempts, then gives up
    assert backend.calls == 3


def test_default_fleet_does_not_retry(repo: Path) -> None:
    # A bare Fleet (no retries arg) preserves prior behavior: even a transient failure is not retried.
    backend = _Flaky(["database is locked"])
    fleet = Fleet(repo, {"flaky": backend})
    rec = fleet.run("flaky", TaskSpec(id="t", goal="x"))
    assert rec.status == "failed"
    assert rec.attempts == 1


def test_worktree_setup_runs_outside_the_create_lock(repo: Path) -> None:
    # The perf fix: only `git worktree add` is serialized; worktree provisioning (setup) runs
    # OUTSIDE the create lock so a fan-out provisions in parallel. Prove it by checking the lock is
    # acquirable WHILE setup runs (it would be held if setup still ran inside the lock).
    fleet = Fleet(repo, {"writer": _Writer()})
    observed: dict[str, bool] = {}
    real_setup = fleet.worktrees.setup

    def spy_setup(wt: object) -> None:
        got = fleet._create_lock.acquire(blocking=False)
        observed["lock_free_during_setup"] = got
        if got:
            fleet._create_lock.release()
        real_setup(wt)  # type: ignore[arg-type]

    fleet.worktrees.setup = spy_setup  # type: ignore[method-assign]
    fleet.run("writer", TaskSpec(id="t", goal="x"))
    assert observed["lock_free_during_setup"] is True


def test_run_loop_stamps_failed_on_exception(repo: Path) -> None:
    fleet = Fleet(repo, {"boom": _Exploder()})
    with pytest.raises(RuntimeError):
        fleet.run("boom", TaskSpec(id="x1", goal="x"))
    runs = fleet.state.list()
    assert len(runs) == 1
    assert runs[0].status == "failed"  # not left stranded as RUNNING
    assert runs[0].error and "kaboom" in runs[0].error


# --- per-run log storage: full raw stdout/stderr persisted for every terminal run -------------


def test_fleet_persists_full_raw_log_on_success(repo: Path) -> None:
    # A succeeded run gets its full raw_stdout + raw_stderr written to <base>/logs/<run_id>.log.
    # The 16KB-truncated `text` on the run record is the agent's *final message*; the log file
    # preserves the *full* streams so a driver can inspect what the agent actually did.
    loud = "OUT-" + ("x" * 50_000)
    err = "ERR-" + ("y" * 50_000)
    fleet = Fleet(repo, {"loudy": _Loudy(stdout=loud, stderr=err)})
    rec = fleet.run("loudy", TaskSpec(id="lg1", goal="x"))
    log_path = fleet.logs.path(rec.run_id)
    assert log_path.exists()
    text = log_path.read_text(encoding="utf-8")
    assert f"=== run {rec.run_id} ===" in text
    assert "--- stdout ---" in text
    assert "--- stderr ---" in text
    assert loud in text  # full, untruncated
    assert err in text
    # the read API agrees with the file on disk
    assert fleet.logs.read(rec.run_id) == text


def test_fleet_persists_full_raw_log_on_failure(repo: Path) -> None:
    # A FAILED run (parse_output returned FAILED) still gets its log persisted - the whole point
    # of durable logs is to debug failures, not just celebrate successes.
    loud = "OUT-yep"
    err = "ERR-yep"
    fleet = Fleet(repo, {"loudy": _Loudy(stdout=loud, stderr=err, fail=True)})
    rec = fleet.run("loudy", TaskSpec(id="lg2", goal="x"))
    assert rec.status == "failed"
    log_path = fleet.logs.path(rec.run_id)
    assert log_path.exists()
    text = log_path.read_text(encoding="utf-8")
    assert loud in text and err in text


def test_run_with_no_result_writes_no_log(repo: Path) -> None:
    # When the backend raises before producing an AgentResult, there is nothing to log - the run is
    # stamped FAILED but no log file is written (the documented no-log case).
    fleet = Fleet(repo, {"boom": _Exploder()})
    with pytest.raises(RuntimeError):
        fleet.run("boom", TaskSpec(id="nolog1", goal="x"))
    run_id = fleet.state.list()[0].run_id
    assert fleet.logs.read(run_id) is None
    assert not fleet.logs.path(run_id).exists()


def test_clean_removes_run_log(repo: Path) -> None:
    # clean() reclaims the (disk-heavy, untruncated) run log alongside the worktree.
    fleet = Fleet(repo, {"loudy": _Loudy(stdout="OUT-z", stderr="ERR-z", fail=True)})
    rec = fleet.run("loudy", TaskSpec(id="cllog", goal="x"))
    assert rec.status == "failed"
    assert fleet.logs.path(rec.run_id).exists()  # log written
    result = fleet.clean()  # finished scope reclaims failed runs
    assert rec.run_id in result.removed
    assert not fleet.logs.path(rec.run_id).exists()  # log reclaimed too


def test_fleet_log_write_failure_does_not_break_run(
    repo: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    # A logging failure (disk full, permission, ...) must NEVER crash the run - the spec
    # guards the write defensively. Pin the contract: a run that would otherwise succeed
    # still reports succeeded when the log write raises.
    fleet = Fleet(repo, {"writer": _Writer()})

    def _boom(run_id: str, stdout: str, stderr: str) -> None:
        raise OSError("disk full")

    monkeypatch.setattr(fleet.logs, "write", _boom)
    rec = fleet.run("writer", TaskSpec(id="lg3", goal="x"))
    assert rec.status == "succeeded"  # run succeeded, log write swallowed


def test_run_many_runs_all_in_isolated_worktrees(repo: Path) -> None:
    fleet = Fleet(repo, {"writer": _Writer()})
    reqs = [RunRequest(backend_name="writer", task=TaskSpec(id=f"m{i}", goal="x")) for i in range(6)]
    records = fleet.run_many(reqs, max_concurrency=4, stagger_s=0)

    assert [r.task_id for r in records] == [f"m{i}" for i in range(6)]  # input order preserved
    assert all(r.status == "succeeded" for r in records)
    assert len({r.worktree for r in records}) == 6                      # each in its own worktree
    for r in records:
        assert (Path(r.worktree or "") / "out.txt").read_text() == "hi"
    assert len(fleet.state.list()) == 6                                 # all persisted, none lost


def test_run_many_runs_concurrently(repo: Path) -> None:
    fleet = Fleet(repo, {"sleeper": _Sleeper()})  # each run sleeps ~0.5s
    reqs = [RunRequest(backend_name="sleeper", task=TaskSpec(id=f"s{i}", goal="x")) for i in range(4)]
    start = time.monotonic()
    records = fleet.run_many(reqs, max_concurrency=4, stagger_s=0)
    elapsed = time.monotonic() - start
    assert all(r.status == "succeeded" for r in records)
    assert elapsed < 1.5  # 4 x 0.5s sequential = 2s; concurrent finishes in ~0.5s


def test_spawn_returns_immediately_then_completes_in_background(repo: Path) -> None:
    fleet = Fleet(repo, {"sleeper": _Sleeper()})  # each run sleeps ~0.5s
    try:
        start = time.monotonic()
        run_id = fleet.spawn(RunRequest(backend_name="sleeper", task=TaskSpec(id="sp1", goal="x")))
        assert time.monotonic() - start < 0.4  # returned without waiting for the 0.5s run

        rec = fleet.state.get(run_id)
        assert rec is not None and rec.status == "running"  # recorded RUNNING at once

        deadline = time.monotonic() + 10
        while time.monotonic() < deadline:
            rec = fleet.state.get(run_id)
            if rec and rec.status != "running":
                break
            time.sleep(0.05)
        assert rec is not None and rec.status == "succeeded"  # finished in the background
    finally:
        fleet.shutdown()


def test_spawn_terminal_stamps_a_background_failure(repo: Path) -> None:
    # A spawned run whose backend raises must end FAILED (never stranded RUNNING), _execute_bg must
    # swallow the exception (no worker-thread crash), and shutdown() must drain cleanly.
    fleet = Fleet(repo, {"boom": _Exploder()})
    try:
        run_id = fleet.spawn(RunRequest(backend_name="boom", task=TaskSpec(id="bf1", goal="x")))
        deadline = time.monotonic() + 10
        rec = fleet.state.get(run_id)
        while time.monotonic() < deadline:
            rec = fleet.state.get(run_id)
            if rec and rec.status != "running":
                break
            time.sleep(0.05)
        assert rec is not None and rec.status == "failed"  # background failure terminal-stamped
        assert rec.error and "kaboom" in rec.error
    finally:
        fleet.shutdown()  # returns cleanly despite the background failure


def test_run_id_unique_across_same_task_runs(repo: Path) -> None:
    fleet = Fleet(repo, {"writer": _Writer()})
    a = fleet.run("writer", TaskSpec(id="dup", goal="x"))
    b = fleet.run("writer", TaskSpec(id="dup", goal="x"))  # same task+backend again (a retry)
    assert a.run_id != b.run_id        # no collision on the record...
    assert a.branch != b.branch        # ...the branch...
    assert a.worktree != b.worktree    # ...or the worktree dir
    assert fleet.state.get(a.run_id) is not None
    assert fleet.state.get(b.run_id) is not None


def test_clean_run_with_no_work_is_empty(repo: Path) -> None:
    fleet = Fleet(repo, {"noop": _NoOp()})
    rec = fleet.run("noop", TaskSpec(id="e1", goal="x"))
    assert rec.status == "empty"  # exit 0 but no text and no file changes


def test_write_only_success_is_not_empty(repo: Path) -> None:
    fleet = Fleet(repo, {"silent": _SilentWriter()})
    rec = fleet.run("silent", TaskSpec(id="s1", goal="x"))
    assert rec.status == "succeeded"  # empty text but a real diff -> success, not EMPTY


def test_status_succeeds_when_changed_files_unknown(repo: Path, monkeypatch: pytest.MonkeyPatch) -> None:
    fleet = Fleet(repo, {"noop": _NoOp()})

    def _boom(wt: object) -> list[str]:
        raise WorktreeError("cannot stat worktree")

    monkeypatch.setattr(fleet.worktrees, "changed_files", _boom)
    rec = fleet.run("noop", TaskSpec(id="we1", goal="x"))
    assert rec.status == "succeeded"  # can't determine work -> don't mislabel a success as empty


def test_tokened_run_gets_estimated_cost(repo: Path) -> None:
    prices = PriceTable({"m": ModelPrice(input_per_mtok=10.0, output_per_mtok=0.0)})
    fleet = Fleet(repo, {"tok": _Tokened()}, prices=prices)
    rec = fleet.run("tok", TaskSpec(id="p1", goal="x"))
    assert rec.status == "succeeded"
    assert rec.cost_usd == 10.0          # 1M input tokens @ $10/Mtok
    assert rec.source == "estimated"
    assert rec.duration_ms >= 0


def test_native_zero_cost_is_not_repriced(repo: Path) -> None:
    prices = PriceTable({"m": ModelPrice(input_per_mtok=10.0, output_per_mtok=0.0)})
    fleet = Fleet(repo, {"nz": _NativeZero()}, prices=prices)
    rec = fleet.run("nz", TaskSpec(id="nz1", goal="x"))
    assert rec.source == "native"   # a backend-reported $0 stays native...
    assert rec.cost_usd == 0.0      # ...and is NOT fabricated into a $10 estimate


def test_tokened_run_unpriced_is_unavailable_not_zero(repo: Path) -> None:
    fleet = Fleet(repo, {"tok": _Tokened()}, prices=PriceTable({}))  # empty table
    rec = fleet.run("tok", TaskSpec(id="p2", goal="x"))
    assert rec.cost_usd == 0.0
    assert rec.source == "unavailable"   # unpriced -> cost unknown, never shown as a real $0


def test_run_many_preserves_usage_api(repo: Path) -> None:
    seen: dict[str, object] = {}

    def resolver(**kw: object) -> ExternalCost:
        seen.update(kw)
        return ExternalCost(0.42, UsageSource.ADMIN_API, 1_000_000, 0, 1)

    fleet = Fleet(
        repo, {"tok": _Tokened()}, prices=PriceTable({}), cost_resolvers={"eastrouter": resolver}
    )
    req = RunRequest(
        backend_name="tok",
        task=TaskSpec(id="rm1", goal="x"),
        model="z-ai/glm-5.1",
        usage_api="eastrouter",
    )
    records = fleet.run_many([req])
    assert len(records) == 1
    rec = records[0]
    assert rec.source == "admin-api"
    assert rec.cost_usd == 0.42
    assert seen["input_tokens"] == 1_000_000
    assert seen["model"] == "z-ai/glm-5.1"


def test_unsupported_permission_raises_before_worktree_create(repo: Path) -> None:
    from unittest.mock import MagicMock

    fleet = Fleet(repo, {"limited": _LimitedPerms()})
    create = MagicMock(side_effect=fleet.worktrees.create)
    fleet.worktrees.create = create  # type: ignore[method-assign]

    with pytest.raises(ValueError, match="does not support permission"):
        fleet.run("limited", TaskSpec(id="p1", goal="x"), permission=PermissionMode.READ_ONLY)

    create.assert_not_called()
    assert fleet.state.list() == []


def test_usage_api_overrides_cost_with_admin_api(repo: Path) -> None:
    seen: dict[str, object] = {}

    def resolver(**kw: object) -> ExternalCost:
        seen.update(kw)
        return ExternalCost(0.42, UsageSource.ADMIN_API, 1_000_000, 0, 1)

    fleet = Fleet(
        repo, {"tok": _Tokened()}, prices=PriceTable({}), cost_resolvers={"eastrouter": resolver}
    )
    rec = fleet.run("tok", TaskSpec(id="er1", goal="x"), model="z-ai/glm-5.1", usage_api="eastrouter")
    assert rec.source == "admin-api"     # real provider cost replaces the unavailable estimate
    assert rec.cost_usd == 0.42
    assert seen["input_tokens"] == 1_000_000  # the run's real tokens were handed to the resolver
    assert seen["model"] == "z-ai/glm-5.1"


def test_usage_api_no_attribution_keeps_estimate(repo: Path) -> None:
    prices = PriceTable({"z-ai/glm-5.1": ModelPrice(input_per_mtok=10.0, output_per_mtok=0.0)})
    fleet = Fleet(
        repo, {"tok": _Tokened()}, prices=prices, cost_resolvers={"eastrouter": lambda **_kw: None}
    )
    rec = fleet.run("tok", TaskSpec(id="er2", goal="x"), model="z-ai/glm-5.1", usage_api="eastrouter")
    assert rec.source == "estimated"     # resolver declined to attribute -> estimate stands
    assert rec.cost_usd == 10.0


def test_usage_api_resolver_failure_is_safe(repo: Path) -> None:
    def boom(**_kw: object) -> ExternalCost:
        raise RuntimeError("provider down")

    prices = PriceTable({"z-ai/glm-5.1": ModelPrice(input_per_mtok=10.0, output_per_mtok=0.0)})
    fleet = Fleet(repo, {"tok": _Tokened()}, prices=prices, cost_resolvers={"eastrouter": boom})
    rec = fleet.run("tok", TaskSpec(id="er3", goal="x"), model="z-ai/glm-5.1", usage_api="eastrouter")
    assert rec.status == "succeeded"     # a resolver crash never fails a finished run...
    assert rec.source == "estimated"     # ...and never corrupts the cost
    assert rec.cost_usd == 10.0


def test_collect_run_returns_diff_and_changed_files(repo: Path) -> None:
    fleet = Fleet(repo, {"writer": _Writer()})
    rec = fleet.run("writer", TaskSpec(id="c1", goal="x"), ts="2026-06-19T00:00:00Z")
    collected = fleet.collect_run(rec.run_id)
    assert collected.run_id == rec.run_id
    assert collected.branch == rec.branch
    assert collected.changed_files == ["out.txt"]
    assert "out.txt" in collected.diff  # the agent's new (untracked) file is in the diff


def test_collect_run_unknown_run_raises(repo: Path) -> None:
    fleet = Fleet(repo, {"writer": _Writer()})
    with pytest.raises(ValueError):
        fleet.collect_run("nope.writer")


def test_integrate_merges_run_into_current_branch(repo: Path) -> None:
    fleet = Fleet(repo, {"writer": _Writer()})
    run_rec = fleet.run("writer", TaskSpec(id="m1", goal="x"), ts="2026-06-19T00:00:00Z")
    result = fleet.integrate(run_rec.run_id)
    assert result.status == "merged"
    assert result.merged_into  # the repo's current branch
    assert result.changed_files == ["out.txt"]
    assert (repo / "out.txt").read_text() == "hi"  # work landed on the main checkout
    rec = fleet.state.get(run_rec.run_id)
    assert rec is not None and rec.merged_into == result.merged_into


def test_integrate_reports_conflict_and_aborts(repo: Path) -> None:
    fleet = Fleet(repo, {"patcher": _Patcher()})
    rec_a = fleet.run("patcher", TaskSpec(id="a", goal="x"))
    rec_b = fleet.run("patcher", TaskSpec(id="b", goal="x"))
    assert fleet.integrate(rec_a.run_id).status == "merged"
    conflict = fleet.integrate(rec_b.run_id)
    assert conflict.status == "conflict"
    assert "README.md" in conflict.conflicts
    assert (repo / "README.md").read_text() == "a"  # aborted -> main untouched


def _git(repo: Path, *args: str) -> str:
    return subprocess.run(
        ["git", "-C", str(repo), *args], check=True, capture_output=True, text=True
    ).stdout


def test_integrate_blocked_on_dirty_target_then_retry_merges(repo: Path) -> None:
    fleet = Fleet(repo, {"patcher": _Patcher()})
    rec = fleet.run("patcher", TaskSpec(id="d1", goal="x"))   # worktree rewrites README.md
    (repo / "README.md").write_text("local uncommitted edit\n")  # dirty the same file in main

    blocked = fleet.integrate(rec.run_id)
    assert blocked.status == "blocked"   # structured result, not a raised exception
    assert blocked.message               # explains the dirty/colliding target
    assert (repo / "README.md").read_text() == "local uncommitted edit\n"  # main untouched

    _git(repo, "checkout", "--", "README.md")  # clean the target, then retry
    merged = fleet.integrate(rec.run_id)
    assert merged.status == "merged"     # the already-committed work merges, NOT reported "empty"
    assert merged.commit                 # honest: reports the commit that landed (not None)...
    assert "README.md" in merged.changed_files  # ...and the files it changed (not [])


def test_integrate_survives_hook_rejected_merge(repo: Path) -> None:
    # A pre-merge-commit hook that fails would leave a non-FF merge half-done. merge() passes
    # --no-verify (so hooks don't run) and aborts any started-but-unfinished merge -> repo stays
    # clean and integrate reports a structured result, never a raw exception or a stuck MERGE_HEAD.
    hook = repo / ".git" / "hooks" / "pre-merge-commit"
    hook.parent.mkdir(parents=True, exist_ok=True)
    hook.write_text("#!/bin/sh\nexit 1\n")
    hook.chmod(0o755)
    # force a non-fast-forward merge: a divergent commit on the target branch
    (repo / "other.txt").write_text("on target\n")
    _git(repo, "add", "-A")
    _git(repo, "commit", "-m", "divergent target commit")

    fleet = Fleet(repo, {"writer": _Writer()})
    rec = fleet.run("writer", TaskSpec(id="h1", goal="x"))
    result = fleet.integrate(rec.run_id)
    assert result.status in ("merged", "blocked")  # --no-verify usually lets it merge cleanly
    assert not (repo / ".git" / "MERGE_HEAD").exists()  # never left mid-merge regardless


def test_integrate_reports_error_on_unrecoverable_git_failure(
    repo: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    fleet = Fleet(repo, {"writer": _Writer()})
    rec = fleet.run("writer", TaskSpec(id="er1", goal="x"))

    def _boom(branch: str, **kw: object) -> object:
        raise WorktreeError("commit failed: no space left on device")

    monkeypatch.setattr(fleet.worktrees, "merge", _boom)
    result = fleet.integrate(rec.run_id)
    assert result.status == "error"  # unrecoverable git failure -> error, NOT a retryable "blocked"
    assert "no space" in (result.message or "")


def test_integrate_retry_reports_only_branch_files_not_divergent_target(repo: Path) -> None:
    fleet = Fleet(repo, {"patcher": _Patcher()})        # branch rewrites README.md only
    rec = fleet.run("patcher", TaskSpec(id="d2", goal="x"))
    (repo / "README.md").write_text("local uncommitted\n")  # collide on README -> blocked
    assert fleet.integrate(rec.run_id).status == "blocked"

    _git(repo, "checkout", "--", "README.md")           # clear the collision
    (repo / "other.txt").write_text("target moved this\n")  # DIVERGENT target commit, separate file
    _git(repo, "add", "other.txt")
    _git(repo, "commit", "-m", "divergent target commit")

    merged = fleet.integrate(rec.run_id)
    assert merged.status == "merged"
    assert "README.md" in merged.changed_files        # the file the agent actually changed
    assert "other.txt" not in merged.changed_files    # target-only file must NOT be over-reported


def test_integrate_blocked_on_detached_head(repo: Path) -> None:
    fleet = Fleet(repo, {"writer": _Writer()})
    rec = fleet.run("writer", TaskSpec(id="dh1", goal="x"))
    _git(repo, "checkout", "--detach", "HEAD")  # detach the main checkout

    result = fleet.integrate(rec.run_id)
    assert result.status == "blocked"            # refuses before committing, no orphaned merge
    assert "detached" in result.message.lower()


# --- commit_run: freeze a run's work onto its branch so a dependent run can chain off it ---------

def test_commit_run_freezes_work_on_branch(repo: Path) -> None:
    fleet = Fleet(repo, {"writer": _Writer()})
    rec = fleet.run("writer", TaskSpec(id="cm1", goal="x"))
    result = fleet.commit_run(rec.run_id)
    assert result.status == "committed"
    assert result.commit  # the branch tip, a concrete ref to chain on
    assert result.branch == rec.branch
    # the work is now a commit on the run's branch (base..branch shows it), and the tree is clean
    assert "out.txt" in _git(repo, "diff", "--name-only", "HEAD", result.commit)
    assert fleet.collect_run(rec.run_id).changed_files == []  # nothing uncommitted left
    assert fleet.state.get(rec.run_id).commit == result.commit  # persisted for chaining/integrate


def test_commit_run_enables_dependent_chaining(repo: Path) -> None:
    # The whole point: B based on A's branch sees A's *committed* work (not just the spawn base).
    fleet = Fleet(repo, {"writer": _Writer()})
    a = fleet.run("writer", TaskSpec(id="chainA", goal="x"))
    fleet.commit_run(a.run_id)
    b = fleet.run("writer", TaskSpec(id="chainB", goal="y", base_branch=a.branch))
    assert (Path(b.worktree) / "out.txt").read_text() == "hi"  # A's work is present in B's worktree


def test_commit_run_clean_when_nothing_to_commit(repo: Path) -> None:
    fleet = Fleet(repo, {"noop": _NoOp()})
    rec = fleet.run("noop", TaskSpec(id="cm2", goal="x"))  # EMPTY run: writes nothing
    result = fleet.commit_run(rec.run_id)
    assert result.status == "clean"
    assert result.commit  # still reports the branch tip (== base) so the driver has a ref


def test_commit_run_blocked_on_running_run(repo: Path) -> None:
    fleet = Fleet(repo, {"writer": _Writer()})
    rec = fleet.run("writer", TaskSpec(id="cm3", goal="x"))
    fleet.state.update(rec.run_id, status="running")  # simulate an in-flight run
    result = fleet.commit_run(rec.run_id)
    assert result.status == "blocked"
    assert "progress" in result.message.lower()


def test_commit_run_unknown_run_raises(repo: Path) -> None:
    fleet = Fleet(repo, {"writer": _Writer()})
    with pytest.raises(ValueError):
        fleet.commit_run("nope.writer")


# --- clean: tear down finished runs' worktrees + branches, ledger + state untouched -------------

def test_clean_default_scope_protects_unintegrated_succeeded(repo: Path) -> None:
    fleet = Fleet(repo, {"writer": _Writer()})
    rec = fleet.run("writer", TaskSpec(id="cl1", goal="x"))  # succeeded, NOT integrated
    result = fleet.clean()  # scope="finished"
    assert rec.run_id not in result.removed       # un-integrated succeeded work is protected
    assert Path(rec.worktree).exists()            # worktree left intact


def test_clean_all_scope_removes_unintegrated_succeeded(repo: Path) -> None:
    fleet = Fleet(repo, {"writer": _Writer()})
    rec = fleet.run("writer", TaskSpec(id="cl2", goal="x"))
    result = fleet.clean(scope="all")
    assert rec.run_id in result.removed
    assert not Path(rec.worktree).exists()        # worktree reclaimed
    assert fleet.state.get(rec.run_id) is not None  # but the state record (history) is kept


def test_clean_merged_scope_removes_only_integrated(repo: Path) -> None:
    fleet = Fleet(repo, {"writer": _Writer()})
    kept = fleet.run("writer", TaskSpec(id="cl3keep", goal="x"))       # not integrated
    gone = fleet.run("writer", TaskSpec(id="cl3gone", goal="x"))
    fleet.integrate(gone.run_id)                                        # merged_into set
    result = fleet.clean(scope="merged")
    assert gone.run_id in result.removed and kept.run_id not in result.removed
    assert not Path(gone.worktree).exists() and Path(kept.worktree).exists()


def test_clean_removes_failed_and_empty_by_default(repo: Path) -> None:
    fleet = Fleet(repo, {"noop": _NoOp()})
    rec = fleet.run("noop", TaskSpec(id="cl4", goal="x"))  # EMPTY (terminal non-success)
    assert rec.status == "empty"
    result = fleet.clean()  # scope="finished" reclaims empty/failed/cancelled/timed_out
    assert rec.run_id in result.removed
    assert not Path(rec.worktree).exists()


def test_clean_skips_running_run(repo: Path) -> None:
    fleet = Fleet(repo, {"writer": _Writer()})
    rec = fleet.run("writer", TaskSpec(id="cl5", goal="x"))
    fleet.state.update(rec.run_id, status="running")
    result = fleet.clean(run_ids=[rec.run_id])
    assert rec.run_id not in result.removed
    assert any(s["run_id"] == rec.run_id for s in result.skipped)
    assert Path(rec.worktree).exists()  # a running run is never torn down


def test_clean_dry_run_reports_without_removing(repo: Path) -> None:
    fleet = Fleet(repo, {"writer": _Writer()})
    rec = fleet.run("writer", TaskSpec(id="cl6", goal="x"))
    result = fleet.clean(scope="all", dry_run=True)
    assert result.dry_run and rec.run_id in result.removed
    assert Path(rec.worktree).exists()  # nothing actually removed


def test_clean_older_than_filters_recent_runs(repo: Path) -> None:
    fleet = Fleet(repo, {"writer": _Writer()})
    fresh = fleet.run("writer", TaskSpec(id="cl7fresh", goal="x"))
    old = fleet.run("writer", TaskSpec(id="cl7old", goal="x"))
    fleet.state.update(old.run_id, ended_at="2000-01-01T00:00:00+00:00")  # ancient
    result = fleet.clean(scope="all", older_than_hours=24)
    assert old.run_id in result.removed and fresh.run_id not in result.removed


def test_clean_unknown_scope_raises(repo: Path) -> None:
    fleet = Fleet(repo, {"writer": _Writer()})
    fleet.run("writer", TaskSpec(id="cl8", goal="x"))
    with pytest.raises(ValueError):
        fleet.clean(scope="bogus")


# --- the orphan sweep: worktrees the ledger no longer knows about ----------------------------


def _branches(repo: Path) -> str:
    return subprocess.run(
        ["git", "-C", str(repo), "branch", "--list", "marshal/*"], capture_output=True, text=True
    ).stdout


def test_clean_reaps_orphaned_worktree(repo: Path) -> None:
    # The field bug: a run record pruned from the ledger left its worktree + branch on disk
    # forever, invisible to the ledger-driven clean.
    fleet = Fleet(repo, {"writer": _Writer()})
    rec = fleet.run("writer", TaskSpec(id="or1", goal="x"))
    (repo / ".marshal" / "runs" / f"{rec.run_id}.json").unlink()
    result = fleet.clean()
    assert result.orphans_removed == [rec.run_id]
    assert not Path(rec.worktree or "").exists()
    assert f"marshal/{rec.run_id}" not in _branches(repo)  # the branch went too


def test_clean_reaps_worktree_with_corrupt_record(repo: Path) -> None:
    # A torn/corrupt record is silently skipped by state.list(), so the run is unreachable via
    # get_run/cancel - its worktree is garbage and the sweep must reclaim it.
    fleet = Fleet(repo, {"writer": _Writer()})
    rec = fleet.run("writer", TaskSpec(id="or2", goal="x"))
    (repo / ".marshal" / "runs" / f"{rec.run_id}.json").write_text("{not json", encoding="utf-8")
    result = fleet.clean()
    assert result.orphans_removed == [rec.run_id]
    assert not Path(rec.worktree or "").exists()


def test_clean_sweep_protects_ledger_owned_running_run(repo: Path) -> None:
    fleet = Fleet(repo, {"writer": _Writer()})
    rec = fleet.run("writer", TaskSpec(id="or3", goal="x"))
    fleet.state.update(rec.run_id, status="running")  # valid record -> ledger-owned, never swept
    result = fleet.clean()
    assert result.orphans_removed == []
    assert Path(rec.worktree or "").exists()


def test_clean_dry_run_lists_orphans_without_removing(repo: Path) -> None:
    fleet = Fleet(repo, {"writer": _Writer()})
    rec = fleet.run("writer", TaskSpec(id="or4", goal="x"))
    (repo / ".marshal" / "runs" / f"{rec.run_id}.json").unlink()
    result = fleet.clean(dry_run=True)
    assert result.orphans_removed == [rec.run_id]
    assert Path(rec.worktree or "").exists()  # nothing actually removed


def test_clean_explicit_run_ids_does_not_sweep(repo: Path) -> None:
    fleet = Fleet(repo, {"writer": _Writer()})
    keep = fleet.run("writer", TaskSpec(id="or5", goal="x"))
    orphan = fleet.run("writer", TaskSpec(id="or6", goal="x"))
    (repo / ".marshal" / "runs" / f"{orphan.run_id}.json").unlink()
    result = fleet.clean(run_ids=[keep.run_id])  # an explicit clean targets exactly those runs
    assert result.orphans_removed == []
    assert Path(orphan.worktree or "").exists()


def test_clean_reaps_plain_dir_under_base(repo: Path) -> None:
    # A corrupt "worktree" that git no longer recognizes is still reclaimed (rmtree fallback).
    fleet = Fleet(repo, {"writer": _Writer()})
    junk = fleet.worktrees.base_dir / "not-a-worktree"
    junk.mkdir(parents=True)
    (junk / "leftover.txt").write_text("x")
    result = fleet.clean()
    assert result.orphans_removed == ["not-a-worktree"]
    assert not junk.exists()


# --- _executor: lazy-init + double-checked locking is safe under contention ------------------


def test_executor_lazy_init_under_concurrent_first_touch(repo: Path) -> None:
    # Fleet._executor uses double-checked locking to build its background-spawn pool on
    # first use. Eight threads racing to call it must build exactly ONE pool - a duplicate
    # build would leak a ThreadPoolExecutor (one of the two would never be shutdown(),
    # holding its workers forever). Locks the safety property Fleet.spawn relies on.
    import threading
    from marshal_engine.fleet import Fleet as _Fleet  # local alias for clarity

    fleet = _Fleet(repo, {"writer": _Writer()})
    assert fleet._bg is None  # precondition: not yet built

    seen: list[object] = []
    barrier = threading.Barrier(8)

    def touch() -> None:
        barrier.wait()  # all 8 threads release at the same instant
        seen.append(fleet._executor())

    threads = [threading.Thread(target=touch) for _ in range(8)]
    for t in threads:
        t.start()
    for t in threads:
        t.join()

    pools = set(seen)
    assert len(pools) == 1, f"expected exactly one pool, got {len(pools)}"
    assert fleet._bg is not None and fleet._bg in pools
    fleet.shutdown()  # cleanup so the suite doesn't leak the pool


def test_executor_returns_same_pool_on_repeated_calls(repo: Path) -> None:
    # Sanity counterpart to the concurrent test: serial calls reuse the same pool (no
    # re-init). Pins the contract _executor advertises via its docstring.
    fleet = Fleet(repo, {"writer": _Writer()})
    p1 = fleet._executor()
    p2 = fleet._executor()
    p3 = fleet._executor()
    assert p1 is p2 is p3
    fleet.shutdown()


# --- advisory budgets: soft warning only, never block a run ----------------------------------


class _Metered(CodingAgentBackend):
    """A fake backend that stamps a controllable native cost on every run.

    Used to drive budget spend deterministically: a recorded event with `cost_usd=N` shows up
    under `by_backend[<name>]` (and under `by_client[<name>]` when the run carried a client), so
    the budget's windowed spend hits whatever threshold the test wants.
    """

    name = "metered"
    binary = "python"
    capabilities = Capabilities()

    def __init__(self, cost_usd: float = 0.50) -> None:
        self._cost = cost_usd

    def check_available(self) -> bool:
        return True

    def build_invocation(self, task: TaskSpec, opts: RunOpts) -> list[str]:
        return [sys.executable, "-c", "open('out.txt','w').write('x')"]

    def map_permission(self, mode: PermissionMode) -> list[str]:
        return []

    def parse_output(self, raw_stdout: str, raw_stderr: str, exit_code: int) -> AgentResult:
        return AgentResult(
            status=RunStatus.SUCCEEDED if exit_code == 0 else RunStatus.FAILED,
            text=raw_stdout.strip(),
            usage=UsageRecord(
                backend="metered",
                input_tokens=10,
                output_tokens=1,
                cost_usd=self._cost,
                source=UsageSource.NATIVE,
            ),
            exit_code=exit_code,
        )


def _seed_run_event(
    fleet: Fleet,
    *,
    backend: str = "metered",
    client: str | None = "worker",
    cost: float = 0.50,
    ts: str | None = None,
) -> None:
    """Append a single UsageEvent to the ledger so the next budget check has spend to read."""
    from datetime import datetime, timezone

    from marshal_engine.usage import UsageEvent

    fleet.usage.record(
        UsageEvent(
            ts=ts or datetime.now(timezone.utc).isoformat(),
            run_id=f"seed.{backend}.x",
            backend=backend,
            client=client,
            cost_usd=cost,
            status="succeeded",
            source="native",
        )
    )


def test_check_budget_warns_when_windowed_spend_meets_cap(
    repo: Path, capsys: pytest.CaptureFixture[str]
) -> None:
    # A backend budget of $1 with $1.50 of recorded spend under that backend -> soft warning.
    # The check is wrapped in try/except (defensive) and never raises; spend >= cap -> warn.
    fleet = Fleet(
        repo,
        {"metered": _Metered(cost_usd=1.5)},
        budgets=[BudgetSpec(backend="metered", window="week", limit_usd=1.0)],
    )
    _seed_run_event(fleet, backend="metered", cost=1.5)
    fleet._check_budget(
        RunRequest(backend_name="metered", task=TaskSpec(id="t", goal="x"), client="worker")
    )
    err = capsys.readouterr().err
    assert "budget:" in err
    assert "backend:metered" in err
    assert "$1.5000 >= cap $1.0000" in err
    assert "(week)" in err


def test_check_budget_stays_silent_under_cap(
    repo: Path, capsys: pytest.CaptureFixture[str]
) -> None:
    # Below the cap -> no warning, no raise. Quietly honest: a small spend doesn't trip a $5 cap.
    fleet = Fleet(
        repo,
        {"metered": _Metered()},
        budgets=[BudgetSpec(backend="metered", window="week", limit_usd=5.0)],
    )
    _seed_run_event(fleet, backend="metered", cost=0.50)
    fleet._check_budget(
        RunRequest(backend_name="metered", task=TaskSpec(id="t", goal="x"))
    )
    assert capsys.readouterr().err == ""


def test_check_budget_does_not_match_unrelated_scope(
    repo: Path, capsys: pytest.CaptureFixture[str]
) -> None:
    # A client budget for "worker" doesn't fire on a "reviewer" run, even if a reviewer event
    # would otherwise have crossed the cap. The check matches scope, not global spend.
    fleet = Fleet(
        repo,
        {"metered": _Metered()},
        budgets=[BudgetSpec(client="worker", window="week", limit_usd=0.10)],
    )
    _seed_run_event(fleet, backend="metered", client="reviewer", cost=5.0)
    fleet._check_budget(
        RunRequest(backend_name="metered", task=TaskSpec(id="t", goal="x"), client="reviewer")
    )
    assert capsys.readouterr().err == ""  # budget is for "worker", not "reviewer"


def test_check_budget_never_raises_on_ledger_failure(
    repo: Path, monkeypatch: pytest.MonkeyPatch, capsys: pytest.CaptureFixture[str]
) -> None:
    # Defensive: a budget check failure (e.g. corrupt ledger) must NEVER block a run. We force
    # summary() to raise and verify _check_budget swallows it quietly.
    fleet = Fleet(
        repo,
        {"metered": _Metered()},
        budgets=[BudgetSpec(backend="metered", window="week", limit_usd=1.0)],
    )

    def boom(**_kw: object) -> object:
        raise RuntimeError("ledger corrupt")

    monkeypatch.setattr(fleet.usage, "summary", boom)  # type: ignore[method-assign]
    # Must not raise.
    fleet._check_budget(
        RunRequest(backend_name="metered", task=TaskSpec(id="t", goal="x"))
    )
    assert capsys.readouterr().err == ""  # failure is silent (no fake warning)


def test_check_budget_no_budgets_is_a_noop(
    repo: Path, capsys: pytest.CaptureFixture[str]
) -> None:
    # The "no behavior change at all" contract: an empty budgets list never prints anything
    # and never raises. (Backward compat for the default-constructed FleetConfig.)
    fleet = Fleet(repo, {"metered": _Metered()})
    _seed_run_event(fleet, cost=999.0)
    fleet._check_budget(
        RunRequest(backend_name="metered", task=TaskSpec(id="t", goal="x"))
    )
    assert capsys.readouterr().err == ""


def test_check_budget_runs_before_worktree(repo: Path) -> None:
    # The check is the FIRST statement of _start: it runs BEFORE the worktree is created, so a
    # loud warning doesn't cost a worktree provision. Pin the order by spying on worktree.create
    # and verifying it was NOT called when the check raises (we can't easily raise, so we verify
    # the call order on a normal warning path instead).
    from unittest.mock import MagicMock

    fleet = Fleet(
        repo,
        {"metered": _Metered()},
        budgets=[BudgetSpec(backend="metered", window="week", limit_usd=0.10)],
    )
    _seed_run_event(fleet, cost=1.0)
    create = MagicMock(side_effect=fleet.worktrees.create)
    fleet.worktrees.create = create  # type: ignore[method-assign]
    fleet.run(
        "metered", TaskSpec(id="ord", goal="x"),
        permission=PermissionMode.SAFE_EDIT, ts="2026-06-19T00:00:00Z",
    )
    assert create.call_count == 1  # the worktree was created (budget is advisory, not blocking)


def test_budget_status_reports_spent_and_remaining_with_floor(repo: Path) -> None:
    # The remaining column floors at 0 (a cap that has been blown reads $0 remaining, not a
    # misleading negative). Spent comes from the windowed rollup; limit comes from the spec.
    from datetime import datetime, timezone

    fleet = Fleet(
        repo,
        {"metered": _Metered()},
        budgets=[
            BudgetSpec(backend="metered", window="week", limit_usd=1.0),
            BudgetSpec(client="worker", window="week", limit_usd=0.10),  # blown -> remaining=0
            BudgetSpec(window="month", limit_usd=10.0),  # global
        ],
    )
    _seed_run_event(fleet, backend="metered", client="worker", cost=0.50)
    now = datetime.now(timezone.utc)
    rows = {r.scope: r for r in fleet.budget_status(now=now)}
    assert rows["backend:metered"].spent_usd == 0.50
    assert rows["backend:metered"].limit_usd == 1.0
    assert rows["backend:metered"].remaining_usd == 0.50
    assert rows["client:worker"].spent_usd == 0.50
    assert rows["client:worker"].remaining_usd == 0.0  # floored at 0 (cap is $0.10)
    assert rows["global"].spent_usd == 0.50  # totals of the windowed summary
    assert rows["global"].limit_usd == 10.0
    assert rows["global"].remaining_usd == 9.50


def test_budget_status_scope_with_no_spend_reads_zero(repo: Path) -> None:
    # A scope with no recorded events reads $0 spent (and remaining == limit). Subscription /
    # unknown-cost backends that report $0 also live here - we never fabricate a percentage.
    from datetime import datetime, timezone

    fleet = Fleet(
        repo,
        {"metered": _Metered()},
        budgets=[BudgetSpec(backend="ghost", window="week", limit_usd=2.0)],
    )
    now = datetime.now(timezone.utc)
    rows = fleet.budget_status(now=now)
    assert len(rows) == 1
    assert rows[0].scope == "backend:ghost"
    assert rows[0].spent_usd == 0.0
    assert rows[0].remaining_usd == 2.0


def test_budget_status_no_budgets_is_empty(repo: Path) -> None:
    # Backward-compat: the default-constructed FleetConfig has no budgets, so the result is [].
    from datetime import datetime, timezone

    fleet = Fleet(repo, {"metered": _Metered()})
    assert fleet.budget_status(now=datetime.now(timezone.utc)) == []
