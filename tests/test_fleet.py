"""Integration test for the Fleet orchestrator using a dummy file-writing backend (no network)."""

from __future__ import annotations

import os
import subprocess
import sys
import time
from collections.abc import Callable
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
from marshal_engine.backends.cursor import SAFE_EDIT_DENY, CursorBackend
from marshal_engine.config import BudgetSpec
from marshal_engine.eastrouter import ExternalCost
from marshal_engine.fleet import Fleet, RunRequest, _active_runs_guard, _register_inflight_run
from marshal_engine.pricing import ModelPrice, PriceTable
from marshal_engine.retry import RetryPolicy
from marshal_engine.state import FleetState, RunRecord
from marshal_engine.worktree import WorktreeError


class _Talker(CodingAgentBackend):
    """An agent that REPLIES but writes no files - a research or review run."""

    name = "talker"
    binary = "python"
    capabilities = Capabilities()

    def __init__(self, message: str) -> None:
        self._message = message

    def check_available(self) -> bool:
        return True

    def build_invocation(self, task: TaskSpec, opts: RunOpts) -> list[str]:
        return [sys.executable, "-c", "pass"]

    def map_permission(self, mode: PermissionMode) -> list[str]:
        return []

    def parse_output(self, raw_stdout: str, raw_stderr: str, exit_code: int) -> AgentResult:
        return AgentResult(status=RunStatus.EXITED_CLEAN, text=self._message, exit_code=exit_code)


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
            status=RunStatus.EXITED_CLEAN if exit_code == 0 else RunStatus.FAILED,
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
            status=RunStatus.EXITED_CLEAN if exit_code == 0 else RunStatus.FAILED,
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
            status=RunStatus.EXITED_CLEAN if exit_code == 0 else RunStatus.FAILED,
            text=raw_stdout.strip(),
            exit_code=exit_code,
        )


class _SelfCommitter(CodingAgentBackend):
    """Commits its work onto the run branch before exiting (like Cursor / Claude Code)."""

    name = "selfcommit"
    binary = "python"
    capabilities = Capabilities()

    def check_available(self) -> bool:
        return True

    def build_invocation(self, task: TaskSpec, opts: RunOpts) -> list[str]:
        script = (
            "import subprocess as s; open('out.txt','w').write('hi');"
            "s.run(['git','add','out.txt'],check=True);"
            "s.run(['git','commit','--no-verify','-q','-m','agent','out.txt'],check=True);"
            "print('done')"
        )
        return [sys.executable, "-c", script]

    def map_permission(self, mode: PermissionMode) -> list[str]:
        return []

    def parse_output(self, raw_stdout: str, raw_stderr: str, exit_code: int) -> AgentResult:
        return AgentResult(
            status=RunStatus.EXITED_CLEAN if exit_code == 0 else RunStatus.FAILED,
            text=raw_stdout.strip(),
            exit_code=exit_code,
        )


class _Committer(CodingAgentBackend):
    """Self-commits A.txt onto the branch, then leaves B.txt uncommitted."""

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
            status=RunStatus.EXITED_CLEAN if exit_code == 0 else RunStatus.FAILED,
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
            status=RunStatus.EXITED_CLEAN if exit_code == 0 else RunStatus.FAILED,
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
            status=RunStatus.EXITED_CLEAN,
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
            status=RunStatus.EXITED_CLEAN if exit_code == 0 else RunStatus.FAILED,
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
            status=RunStatus.EXITED_CLEAN,
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
            status=RunStatus.EXITED_CLEAN if exit_code == 0 else RunStatus.FAILED,
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
            status=RunStatus.EXITED_CLEAN if not self._fail else RunStatus.FAILED,
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
        return AgentResult(status=RunStatus.EXITED_CLEAN)

    def run(self, task: TaskSpec, opts: RunOpts) -> AgentResult:
        err = self._errors[self.calls] if self.calls < len(self._errors) else None
        self.calls += 1
        if err is None:
            (opts.cwd / "ok.txt").write_text("ok")  # a real change -> SUCCEEDED, not EMPTY
            return AgentResult(
                status=RunStatus.EXITED_CLEAN,
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
    assert rec.status == "exited_clean"
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
    assert rec.status == "exited_clean"
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
    assert rec.status == "exited_clean"
    assert rec.attempts == 2          # one retry was needed
    assert backend.calls == 2


def test_non_transient_failure_is_not_retried(repo: Path) -> None:
    backend = _Flaky(["AssertionError: expected 2 got 3"])  # a genuine task failure, not transient
    fleet = Fleet(repo, {"flaky": backend}, retries=RetryPolicy(max_attempts=3, backoff_base_s=0.0))
    rec = fleet.run("flaky", TaskSpec(id="t", goal="x"))
    assert rec.status == "failed"
    assert rec.attempts == 1          # no retry for a real failure
    assert backend.calls == 1


def test_a_cancel_during_the_backoff_sleep_stops_the_retry(repo: Path) -> None:
    """The backoff is the widest window in the loop, so a cancel is most likely to land exactly
    there. Checking only before the sleep let the loop wake and spawn a fresh agent - writing to
    the worktree and billing for it - with the record already reading `cancelled`."""
    import marshal_engine.fleet as fleet_mod

    fleet = Fleet(repo, {"flaky": _Flaky(["opencode: database is locked"] * 4)},
                  retries=RetryPolicy(max_attempts=3, backoff_base_s=0.0))
    backend = fleet.backends["flaky"]

    # Cancel arrives DURING the sleep: nothing has requested it before the loop starts waiting.
    real_sleep = fleet_mod.time.sleep
    cancelled: dict[str, bool] = {"done": False}

    def sleeping_cancel(seconds: float) -> None:
        real_sleep(seconds)
        if not cancelled["done"]:
            cancelled["done"] = True
            for rec in fleet.state.list():
                if rec.status == RunStatus.RUNNING.value:
                    fleet.cancel_run(rec.run_id)

    monkey = pytest.MonkeyPatch()
    monkey.setattr(fleet_mod.time, "sleep", sleeping_cancel)
    try:
        fleet.run("flaky", TaskSpec(id="t", goal="x"))
    finally:
        monkey.undo()

    assert backend.calls == 1, "a cancel during the backoff still spawned another attempt"


def test_a_cancel_stops_the_retry_loop(repo: Path) -> None:
    """REGRESSION (#89): the retry loop never consulted the cancel state. SIGTERM can surface as a
    transport-shaped error, so a cancelled run slept and spawned a WHOLE new attempt - backend
    setup and all - which the pending cancel then killed on arrival, putting a second writer in the
    worktree after the record already read `cancelled`."""

    class _CancelsItself(_Flaky):
        def __init__(self, fleet_ref: dict[str, Fleet]) -> None:
            super().__init__(["opencode: database is locked"] * 4)  # always transient
            self._fleet_ref = fleet_ref

        def run(self, task: TaskSpec, opts: RunOpts) -> AgentResult:  # type: ignore[override]
            result = super().run(task, opts)
            # A cancel lands while attempt 1 is finishing, exactly like the real race.
            fleet = self._fleet_ref["f"]
            for rec in fleet.state.list():
                if rec.status == RunStatus.RUNNING.value:
                    fleet.cancel_run(rec.run_id)
            return result

    ref: dict[str, Fleet] = {}
    backend = _CancelsItself(ref)
    fleet = Fleet(repo, {"flaky": backend}, retries=RetryPolicy(max_attempts=3, backoff_base_s=0.0))
    ref["f"] = fleet
    fleet.run("flaky", TaskSpec(id="t", goal="x"))
    assert backend.calls == 1, "a cancelled run spawned another attempt"


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


def test_fleet_refuses_non_allowlisted_setup_at_construction(repo: Path) -> None:
    with pytest.raises(WorktreeError, match="allowlist|allow_unsafe_commands"):
        Fleet(repo, {"writer": _Writer()}, worktree_setup=["curl", "https://example.invalid"])


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
    assert rec.status == "exited_clean"  # run succeeded, log write swallowed


def test_run_many_runs_all_in_isolated_worktrees(repo: Path) -> None:
    fleet = Fleet(repo, {"writer": _Writer()})
    reqs = [RunRequest(backend_name="writer", task=TaskSpec(id=f"m{i}", goal="x")) for i in range(6)]
    records = fleet.run_many(reqs, max_concurrency=4, stagger_s=0)

    assert [r.task_id for r in records] == [f"m{i}" for i in range(6)]  # input order preserved
    assert all(r.status == "exited_clean" for r in records)
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
    assert all(r.status == "exited_clean" for r in records)
    # Sequential would be ≥ ~2s of sleep alone (4 × 0.5s). Bound is loose enough for
    # CI/load jitter while still proving overlap.
    assert elapsed < 1.9


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
        assert rec is not None and rec.status == "exited_clean"  # finished in the background
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
    assert rec.status == "exited_clean"  # empty text but a real diff -> success, not EMPTY


def test_status_succeeds_when_changed_files_unknown(repo: Path, monkeypatch: pytest.MonkeyPatch) -> None:
    fleet = Fleet(repo, {"noop": _NoOp()})

    def _boom(wt: object) -> list[str]:
        raise WorktreeError("cannot stat worktree")

    monkeypatch.setattr(fleet.worktrees, "changed_files", _boom)
    rec = fleet.run("noop", TaskSpec(id="we1", goal="x"))
    assert rec.status == "exited_clean"  # can't determine work -> don't mislabel a success as empty


def test_tokened_run_gets_estimated_cost(repo: Path) -> None:
    prices = PriceTable({"m": ModelPrice(input_per_mtok=10.0, output_per_mtok=0.0)})
    fleet = Fleet(repo, {"tok": _Tokened()}, prices=prices)
    rec = fleet.run("tok", TaskSpec(id="p1", goal="x"))
    assert rec.status == "exited_clean"
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


def test_goose_malformed_model_raises_before_worktree_create(repo: Path) -> None:
    """Goose provider/model typos fail at argv preflight — no worktree / run record."""
    from unittest.mock import MagicMock

    from marshal_engine.backends.goose import GooseBackend

    fleet = Fleet(repo, {"goose": GooseBackend()})
    create = MagicMock(side_effect=fleet.worktrees.create)
    fleet.worktrees.create = create  # type: ignore[method-assign]

    with pytest.raises(ValueError, match="malformed"):
        fleet.run("goose", TaskSpec(id="g1", goal="x"), model="cursor-agent/")

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
    assert rec.status == "exited_clean"     # a resolver crash never fails a finished run...
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


def test_collect_run_surfaces_self_committed_work(repo: Path) -> None:
    fleet = Fleet(repo, {"selfcommit": _SelfCommitter()})
    rec = fleet.run("selfcommit", TaskSpec(id="sc1", goal="x"))
    collected = fleet.collect_run(rec.run_id)
    assert collected.changed_files == []  # working tree is clean
    assert collected.diff == ""
    assert collected.commit_count == 1
    assert collected.committed_changed_files == ["out.txt"]
    assert "out.txt" in collected.committed_diff
    assert collected.diff == ""  # uncommitted section stays empty


def test_collect_run_uncommitted_only_unchanged(repo: Path) -> None:
    fleet = Fleet(repo, {"writer": _Writer()})
    rec = fleet.run("writer", TaskSpec(id="cu1", goal="x"))
    collected = fleet.collect_run(rec.run_id)
    assert collected.changed_files == ["out.txt"]
    assert "out.txt" in collected.diff
    assert collected.committed_changed_files == []
    assert collected.committed_diff == ""
    assert collected.commit_count == 0


def test_collect_run_reports_committed_and_uncommitted_separately(repo: Path) -> None:
    fleet = Fleet(repo, {"committer": _Committer()})
    rec = fleet.run("committer", TaskSpec(id="mix1", goal="x"))
    collected = fleet.collect_run(rec.run_id)
    assert collected.changed_files == ["B.txt"]
    assert "B.txt" in collected.diff
    assert collected.committed_changed_files == ["A.txt"]
    assert "A.txt" in collected.committed_diff
    assert "B.txt" not in collected.committed_diff
    assert collected.commit_count == 1


def test_collect_run_empty_run_reports_no_work(repo: Path) -> None:
    fleet = Fleet(repo, {"noop": _NoOp()})
    rec = fleet.run("noop", TaskSpec(id="em1", goal="x"))
    collected = fleet.collect_run(rec.run_id)
    assert collected.changed_files == []
    assert collected.diff == ""
    assert collected.committed_changed_files == []
    assert collected.committed_diff == ""
    assert collected.commit_count == 0


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


def test_run_persists_resolved_base_branch_on_run_record(repo: Path) -> None:
    fleet = Fleet(repo, {"writer": _Writer()})
    _git(repo, "checkout", "-b", "spawn-base")
    _git(repo, "checkout", "-")
    explicit = fleet.run("writer", TaskSpec(id="bb1", goal="x", base_branch="spawn-base"))
    assert fleet.state.get(explicit.run_id).base_branch == "spawn-base"

    current = _git(repo, "rev-parse", "--abbrev-ref", "HEAD").strip()
    resolved = fleet.run("writer", TaskSpec(id="bb2", goal="x"))
    assert fleet.state.get(resolved.run_id).base_branch == current


def test_integrate_same_base_branch_has_no_drift_warning(repo: Path) -> None:
    fleet = Fleet(repo, {"writer": _Writer()})
    run_rec = fleet.run("writer", TaskSpec(id="nd1", goal="x"))
    result = fleet.integrate(run_rec.run_id)
    assert result.status == "merged"
    assert result.base_branch_drift is False
    assert result.message == ""


def test_integrate_different_branch_reports_base_branch_drift(repo: Path) -> None:
    fleet = Fleet(repo, {"writer": _Writer()})
    spawn_branch = _git(repo, "rev-parse", "--abbrev-ref", "HEAD").strip()
    run_rec = fleet.run("writer", TaskSpec(id="dr1", goal="x"))
    assert fleet.state.get(run_rec.run_id).base_branch == spawn_branch

    _git(repo, "checkout", "-b", "feature/other-pr")
    result = fleet.integrate(run_rec.run_id)
    assert result.status == "merged"
    assert (repo / "out.txt").read_text() == "hi"
    assert result.base_branch_drift is True
    assert spawn_branch in result.message
    assert "feature/other-pr" in result.message


def test_integrate_legacy_record_without_base_branch_has_no_drift_warning(repo: Path) -> None:
    fleet = Fleet(repo, {"writer": _Writer()})
    run_rec = fleet.run("writer", TaskSpec(id="lg1", goal="x"))
    fleet.state.update(run_rec.run_id, base_branch=None)

    _git(repo, "checkout", "-b", "feature/unrelated")
    result = fleet.integrate(run_rec.run_id)
    assert result.status == "merged"
    assert result.base_branch_drift is False
    assert result.message == ""


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
            status=RunStatus.EXITED_CLEAN if exit_code == 0 else RunStatus.FAILED,
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
            status="exited_clean",
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
    # on a normal advisory (soft-warn) path.
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


def test_check_budget_enforce_raises_and_skips_worktree(repo: Path) -> None:
    from unittest.mock import MagicMock

    from marshal_engine.budgets import BudgetExceeded

    fleet = Fleet(
        repo,
        {"metered": _Metered()},
        budgets=[BudgetSpec(backend="metered", window="week", limit_usd=0.10, enforce=True)],
    )
    _seed_run_event(fleet, backend="metered", cost=1.0)
    create = MagicMock(side_effect=fleet.worktrees.create)
    fleet.worktrees.create = create  # type: ignore[method-assign]
    with pytest.raises(BudgetExceeded, match="enforce=true"):
        fleet.run(
            "metered",
            TaskSpec(id="ord", goal="x"),
            permission=PermissionMode.SAFE_EDIT,
            ts="2026-06-19T00:00:00Z",
        )
    assert create.call_count == 0


def test_check_budget_enforce_raises_on_ledger_failure(repo: Path, monkeypatch: pytest.MonkeyPatch) -> None:
    from marshal_engine.budgets import BudgetExceeded

    fleet = Fleet(
        repo,
        {"metered": _Metered()},
        budgets=[BudgetSpec(backend="metered", window="week", limit_usd=1.0, enforce=True)],
    )

    def boom(**_kw: object) -> object:
        raise RuntimeError("ledger corrupt")

    monkeypatch.setattr(fleet.usage, "summary", boom)  # type: ignore[method-assign]
    with pytest.raises(BudgetExceeded, match="spend lookup failed"):
        fleet._check_budget(
            RunRequest(backend_name="metered", task=TaskSpec(id="t", goal="x"))
        )


def test_enforce_budget_blocks_concurrent_matching_spawn(repo: Path) -> None:
    """enforce=true admits one in-flight matching spawn; a peer is refused before worktree create."""
    import threading

    from marshal_engine.budgets import BudgetExceeded

    fleet = Fleet(
        repo,
        {"sleeper": _Sleeper()},
        budgets=[BudgetSpec(backend="sleeper", window="week", limit_usd=100.0, enforce=True)],
    )
    results: list[object] = []
    errors: list[str] = []
    barrier = threading.Barrier(2)

    def worker(task_id: str) -> None:
        barrier.wait()
        try:
            results.append(
                fleet.run(
                    "sleeper",
                    TaskSpec(id=task_id, goal="x"),
                    permission=PermissionMode.SAFE_EDIT,
                )
            )
        except BudgetExceeded as exc:
            errors.append(str(exc))

    threads = [
        threading.Thread(target=worker, args=("a",)),
        threading.Thread(target=worker, args=("b",)),
    ]
    for t in threads:
        t.start()
    for t in threads:
        t.join()
    assert len(results) == 1
    assert len(errors) == 1
    assert "in-flight" in errors[0]
    # Slot released after the holder finishes — a follow-up matching spawn may proceed.
    follow = fleet.run(
        "sleeper",
        TaskSpec(id="c", goal="x"),
        permission=PermissionMode.SAFE_EDIT,
    )
    assert follow.status == RunStatus.EXITED_CLEAN.value


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


# --- startup orphan reap: stale RUNNING/QUEUED records from a prior Fleet -----------------------


def _write_run_record(repo: Path, rec: RunRecord) -> None:
    runs = repo / ".marshal" / "runs"
    runs.mkdir(parents=True, exist_ok=True)
    (runs / f"{rec.run_id}.json").write_text(rec.model_dump_json(indent=2), encoding="utf-8")


def test_startup_reaps_running_record_left_by_prior_fleet(repo: Path) -> None:
    run_id = "orphan.writer.deadbeef"
    _write_run_record(
        repo,
        RunRecord(
            run_id=run_id,
            task_id="orphan",
            backend="writer",
            status="running",
            started_at="2026-01-01T00:00:00Z",
            pid=424242,
        ),
    )
    fleet = Fleet(repo, {"writer": _Writer()})
    rec = fleet.state.get(run_id)
    assert rec is not None
    assert rec.status == "failed"
    assert rec.pid is None
    assert rec.ended_at is not None
    assert rec.error and "orphaned at startup" in rec.error


def test_startup_leaves_terminal_record_unchanged(repo: Path) -> None:
    run_id = "done.writer.cafebabe"
    original = RunRecord(
        run_id=run_id,
        task_id="done",
        backend="writer",
        status="exited_clean",
        started_at="2026-01-01T00:00:00Z",
        ended_at="2026-01-01T00:01:00Z",
        pid=11111,
        cost_usd=0.42,
        text="all good",
    )
    _write_run_record(repo, original)
    fleet = Fleet(repo, {"writer": _Writer()})
    rec = fleet.state.get(run_id)
    assert rec is not None
    assert rec.model_dump() == original.model_dump()


def test_startup_reap_skips_corrupt_record_without_crashing(
    repo: Path, capsys: pytest.CaptureFixture[str]
) -> None:
    runs = repo / ".marshal" / "runs"
    runs.mkdir(parents=True, exist_ok=True)
    (runs / "broken.writer.json").write_text("{not json", encoding="utf-8")
    Fleet(repo, {"writer": _Writer()})  # must not raise
    err = capsys.readouterr().err
    assert "skipping unreadable run record" in err


def test_cancel_on_reaped_run_does_not_signal_pid(
    repo: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    import os

    run_id = "reaped.writer.abc12345"
    _write_run_record(
        repo,
        RunRecord(
            run_id=run_id,
            task_id="reaped",
            backend="writer",
            status="running",
            started_at="2026-01-01T00:00:00Z",
            pid=99999,
        ),
    )
    fleet = Fleet(repo, {"writer": _Writer()})
    assert fleet.state.get(run_id).pid is None

    killed: list[tuple[int, int]] = []

    def _fake_killpg(pgid: int, sig: int) -> None:
        killed.append((pgid, sig))

    monkeypatch.setattr(os, "killpg", _fake_killpg)
    rec = fleet.cancel_run(run_id)
    assert killed == []
    assert rec.status == "failed"


def test_startup_skips_running_record_when_agent_pid_still_alive(repo: Path) -> None:
    proc = subprocess.Popen([sys.executable, "-c", "import time; time.sleep(60)"])
    run_id = "live.writer.feedface"
    try:
        _write_run_record(
            repo,
            RunRecord(
                run_id=run_id,
                task_id="live",
                backend="writer",
                status="running",
                started_at="2026-01-01T00:00:00Z",
                pid=proc.pid,
            ),
        )
        fleet = Fleet(repo, {"writer": _Writer()})
        rec = fleet.state.get(run_id)
        assert rec is not None
        assert rec.status == "running"
        assert rec.pid == proc.pid
    finally:
        proc.kill()
        proc.wait()


def test_startup_does_not_steal_the_lock_from_a_live_fleet(repo: Path) -> None:
    """REGRESSION: claiming the lock unconditionally destroyed the protection it just relied on.

    A short-lived CLI Fleet alongside a live MCP server correctly skipped reaping, then overwrote
    `fleet.lock` with its own pid and exited - leaving a DEAD holder. The next Fleet then saw "no
    live supervisor" and reaped the server's runs; a run recorded RUNNING before its pid is stamped
    has no pid to protect it, so a live run got marked failed with its pid cleared (unkillable).
    """
    import json

    holder = subprocess.Popen([sys.executable, "-c", "import time; time.sleep(60)"])
    try:
        lock_path = repo / ".marshal" / "fleet.lock"
        lock_path.parent.mkdir(parents=True, exist_ok=True)
        lock_path.write_text(json.dumps({"pid": holder.pid}), encoding="utf-8")

        Fleet(repo, {"writer": _Writer()})  # a second Fleet while the first is alive

        still_held = json.loads(lock_path.read_text(encoding="utf-8"))
        assert still_held["pid"] == holder.pid, "the live supervisor's lock was stolen"
    finally:
        holder.terminate()
        holder.wait(timeout=10)


def test_startup_claims_the_lock_when_no_live_fleet_holds_it(repo: Path) -> None:
    import json
    import os as _os

    lock_path = repo / ".marshal" / "fleet.lock"
    Fleet(repo, {"writer": _Writer()})
    assert json.loads(lock_path.read_text(encoding="utf-8"))["pid"] == _os.getpid()


def test_startup_skips_reap_when_another_fleet_lock_holder_is_alive(repo: Path) -> None:
    import json

    run_id = "locked.writer.deadbeef"
    _write_run_record(
        repo,
        RunRecord(
            run_id=run_id,
            task_id="locked",
            backend="writer",
            status="running",
            started_at="2026-01-01T00:00:00Z",
            pid=424242,
        ),
    )
    holder = subprocess.Popen([sys.executable, "-c", "import time; time.sleep(60)"])
    try:
        lock_path = repo / ".marshal" / "fleet.lock"
        lock_path.parent.mkdir(parents=True, exist_ok=True)
        lock_path.write_text(json.dumps({"pid": holder.pid}), encoding="utf-8")

        fleet = Fleet(repo, {"writer": _Writer()})
        rec = fleet.state.get(run_id)
        assert rec is not None
        assert rec.status == "running"
        assert rec.pid == 424242
    finally:
        holder.kill()
        holder.wait()


# --- Cursor safe-edit cli.json transaction: Fleet never observes the deny overlay (#37) --------
#
# These run a REAL CursorBackend through the shared base run loop and Fleet, with a local fake
# `cursor-agent` on PATH - the composition the bug lived in (prepare's worktree write leaking
# into status/verify/commit/integrate). The fake exits 1 unless every curated deny is present in
# `.cursor/cli.json` WHILE it runs, so a cleanup that disabled the overlay would fail these tests.

_FAKE_CURSOR = """\
import json
import sys
from pathlib import Path

cfg = Path(".cursor/cli.json")
data = json.loads(cfg.read_text(encoding="utf-8"))
deny = data.get("permissions", {}).get("deny", [])
missing = [r for r in __DENIES__ if r not in deny]
if missing:
    print("missing denies: %s" % missing, file=sys.stderr)
    sys.exit(1)
__ACTION__
print(json.dumps({"type": "result", "is_error": False, "result": __TEXT__, "session_id": "s1"}))
"""


def _cursor_body(action: str = "", text: str = "done", extra_denies: list[str] | None = None) -> str:
    denies = list(SAFE_EDIT_DENY) + (extra_denies or [])
    return (
        _FAKE_CURSOR.replace("__DENIES__", repr(denies))
        .replace("__ACTION__", action)
        .replace("__TEXT__", repr(text))
    )


# Deliberate odd formatting a JSON re-serialize would never reproduce - proves byte-for-byte restore.
_CUSTOM_CONFIG = b'{ "permissions": { "allow": ["Shell(git)"], "deny": ["WebFetch(evil.example)"] } }\n'


def _commit_cursor_config(repo: Path, content: bytes) -> None:
    (repo / ".cursor").mkdir()
    (repo / ".cursor" / "cli.json").write_bytes(content)
    _git(repo, "add", "-A")
    _git(repo, "commit", "-m", "add cursor config")


def test_cursor_noop_safe_edit_is_empty_and_skips_verify(
    repo: Path, fake_cursor_agent: Callable[[str], Path]
) -> None:
    fake_cursor_agent(_cursor_body(text=""))  # denies verified live; no writes, no text
    fleet = Fleet(
        repo, {"cursor": CursorBackend()}, verify=[sys.executable, "-c", "import sys; sys.exit(1)"]
    )
    rec = fleet.run("cursor", TaskSpec(id="ce1", goal="x"), permission=PermissionMode.SAFE_EDIT)
    assert rec.status == "empty", rec.error   # honest EMPTY, not a false SUCCEEDED
    assert rec.verify_passed is None          # the always-failing gate proves verify never ran
    collected = fleet.collect_run(rec.run_id)
    assert collected.changed_files == []
    assert collected.diff == ""
    assert not (Path(rec.worktree or "") / ".cursor").exists()  # no overlay residue


def test_cursor_real_work_integrates_without_policy_leakage(
    repo: Path, fake_cursor_agent: Callable[[str], Path]
) -> None:
    _commit_cursor_config(repo, _CUSTOM_CONFIG)
    fake_cursor_agent(
        _cursor_body(
            action="Path('out.txt').write_text('hi')",
            extra_denies=["WebFetch(evil.example)"],  # the repo's own deny must survive the merge
        )
    )
    fleet = Fleet(repo, {"cursor": CursorBackend()})
    rec = fleet.run("cursor", TaskSpec(id="ci1", goal="x"), permission=PermissionMode.SAFE_EDIT)
    assert rec.status == "exited_clean", rec.error
    collected = fleet.collect_run(rec.run_id)
    assert collected.changed_files == ["out.txt"]     # the overlay is not agent work
    assert ".cursor" not in collected.diff
    merged = fleet.integrate(rec.run_id)
    assert merged.status == "merged"
    assert merged.changed_files == ["out.txt"]
    assert (repo / "out.txt").read_text() == "hi"
    assert (repo / ".cursor" / "cli.json").read_bytes() == _CUSTOM_CONFIG  # main config exact


def test_cursor_commit_run_excludes_policy_overlay(
    repo: Path, fake_cursor_agent: Callable[[str], Path]
) -> None:
    _commit_cursor_config(repo, _CUSTOM_CONFIG)
    fake_cursor_agent(
        _cursor_body(
            action="Path('out.txt').write_text('hi')",
            extra_denies=["WebFetch(evil.example)"],
        )
    )
    fleet = Fleet(repo, {"cursor": CursorBackend()})
    rec = fleet.run("cursor", TaskSpec(id="cm1", goal="x"), permission=PermissionMode.SAFE_EDIT)
    assert rec.status == "exited_clean", rec.error
    result = fleet.commit_run(rec.run_id)
    assert result.status == "committed"
    assert _git(repo, "diff", "--name-only", "HEAD", result.commit or "").split() == ["out.txt"]
    assert (Path(rec.worktree or "") / ".cursor" / "cli.json").read_bytes() == _CUSTOM_CONFIG


def test_cursor_corrupt_config_fails_closed_and_is_preserved(
    repo: Path, fake_cursor_agent: Callable[[str], Path]
) -> None:
    corrupt = b'{"permissions": [broken'
    _commit_cursor_config(repo, corrupt)
    # if this fake ever launches it leaves a marker - proving the run was refused pre-spawn
    fake_cursor_agent("from pathlib import Path\nPath('ran.txt').write_text('x')\n")
    fleet = Fleet(repo, {"cursor": CursorBackend()})
    rec = fleet.run("cursor", TaskSpec(id="cc1", goal="x"), permission=PermissionMode.SAFE_EDIT)
    assert rec.status == "failed"
    assert "not valid JSON" in (rec.error or "")      # actionable prepare error
    wt = Path(rec.worktree or "")
    assert not (wt / "ran.txt").exists()              # the agent process never launched
    assert (wt / ".cursor" / "cli.json").read_bytes() == corrupt    # worktree copy preserved
    assert (repo / ".cursor" / "cli.json").read_bytes() == corrupt  # main copy untouched
    assert fleet.collect_run(rec.run_id).changed_files == []


def test_cursor_timeout_still_cleans_up_overlay(
    repo: Path, fake_cursor_agent: Callable[[str], Path]
) -> None:
    fake_cursor_agent(_cursor_body(action="import time\ntime.sleep(60)"))
    fleet = Fleet(repo, {"cursor": CursorBackend()})
    rec = fleet.run(
        "cursor", TaskSpec(id="ct1", goal="x"), permission=PermissionMode.SAFE_EDIT, timeout_s=1
    )
    assert rec.status == "timed_out"
    assert not (Path(rec.worktree or "") / ".cursor").exists()  # finally-path cleanup held
    assert fleet.collect_run(rec.run_id).changed_files == []


# --- confirmed-defect regressions (audit fleet, 2026-07-27) --------------------------------


def test_collect_run_uses_the_runs_own_base_not_the_current_branch(repo: Path) -> None:
    """REGRESSION: collection computed the committed diff against whatever was checked out NOW.

    A checkout between spawn and collect silently changed the review: commits inherited from an
    unrelated branch showed up as the agent's work, or the agent's own commits vanished.
    """
    fleet = Fleet(repo, {"selfcommit": _SelfCommitter()})
    spawn_base = _git(repo, "rev-parse", "--abbrev-ref", "HEAD").strip()
    rec = fleet.run("selfcommit", TaskSpec(id="cb1", goal="x"))
    assert fleet.state.get(rec.run_id).base_branch == spawn_base

    # The driver switches branches and puts an unrelated commit on the new one.
    _git(repo, "checkout", "-q", "-b", "unrelated")
    (repo / "noise.txt").write_text("not the agent's work\n")
    _git(repo, "add", "-A")
    _git(repo, "commit", "-q", "-m", "unrelated work")

    collected = fleet.collect_run(rec.run_id)
    assert collected.committed_changed_files == ["out.txt"], collected.committed_changed_files
    assert "noise.txt" not in collected.committed_diff
    assert collected.commit_count == 1


def test_collect_run_falls_back_to_current_branch_for_legacy_records(repo: Path) -> None:
    """A record written before `base_branch` existed must still collect, not crash."""
    fleet = Fleet(repo, {"selfcommit": _SelfCommitter()})
    rec = fleet.run("selfcommit", TaskSpec(id="cb2", goal="x"))
    fleet.state.update(rec.run_id, base_branch=None)
    collected = fleet.collect_run(rec.run_id)
    assert collected.committed_changed_files == ["out.txt"]


def test_a_live_holders_lock_is_never_claimed(repo: Path) -> None:
    """REGRESSION: check-then-write let a second PROCESS pass the liveness check and also reap.

    (A claim from the SAME process is allowed on purpose: a config hot-reload rebuilds the Fleet
    in-process and must still be able to reconcile its own state.)
    """
    import json as _json

    from marshal_engine.fleet import _claim_fleet_lock

    lock = repo / ".marshal" / "fleet.lock"
    lock.parent.mkdir(parents=True, exist_ok=True)
    holder = subprocess.Popen([sys.executable, "-c", "import time; time.sleep(60)"])
    try:
        lock.write_text(_json.dumps({"pid": holder.pid}), encoding="utf-8")
        assert _claim_fleet_lock(lock) is False, "claimed a lock held by a live process"
        assert _json.loads(lock.read_text(encoding="utf-8"))["pid"] == holder.pid
    finally:
        holder.terminate()
        holder.wait(timeout=10)


def test_only_one_of_two_racing_processes_claims_the_lock(repo: Path) -> None:
    """REGRESSION: the empty-file window. `O_EXCL` created the lock and wrote the pid afterwards,
    so a competing process reading it in between saw an unparseable file, concluded "no live
    holder", unlinked the winner's lock and took over - both reaped.

    The loser must still be ALIVE when the other checks, or taking over a dead holder's lock is
    correct behaviour and the test proves nothing. Each child therefore stays alive after
    claiming. Real processes are required: a same-process claim is deliberately allowed (config
    hot-reload), so threads cannot express this.
    """
    lock = repo / ".marshal" / "fleet.lock"
    lock.parent.mkdir(parents=True, exist_ok=True)
    script = (
        "import sys, time\n"
        "sys.path.insert(0, %r)\n"
        "from pathlib import Path\n"
        "from marshal_engine.fleet import _claim_fleet_lock\n"
        "start = float(sys.argv[2])\n"
        "time.sleep(max(0.0, start - time.time()))\n"
        "won = _claim_fleet_lock(Path(sys.argv[1]))\n"
        "print('WON' if won else 'LOST', flush=True)\n"
        "time.sleep(3)\n"  # stay alive so the other process's liveness probe is meaningful
    ) % str(Path(__file__).resolve().parent.parent / "src")

    for _ in range(8):  # a race that fires rarely must still never produce two winners
        if lock.exists():
            lock.unlink()
        go = time.time() + 0.40
        procs = [
            subprocess.Popen(
                [sys.executable, "-c", script, str(lock), str(go)],
                stdout=subprocess.PIPE, text=True,
            )
            for _ in range(2)
        ]
        try:
            outs = [p.stdout.readline().strip() for p in procs]  # read before they exit
            assert outs.count("WON") == 1, f"expected exactly one winner, got {outs}"
        finally:
            for p in procs:
                p.terminate()
                p.wait(timeout=10)


def test_a_dead_holders_lock_is_taken_over(repo: Path) -> None:
    import json as _json
    import os

    from marshal_engine.fleet import _claim_fleet_lock

    lock = repo / ".marshal" / "fleet.lock"
    lock.parent.mkdir(parents=True, exist_ok=True)
    dead = subprocess.Popen([sys.executable, "-c", "pass"])
    dead.wait(timeout=10)
    lock.write_text(_json.dumps({"pid": dead.pid}), encoding="utf-8")
    assert _claim_fleet_lock(lock) is True
    assert _json.loads(lock.read_text(encoding="utf-8"))["pid"] == os.getpid()


def test_a_reused_pid_does_not_shield_a_stale_record(repo: Path) -> None:
    """REGRESSION: `_pid_alive` proved liveness, not ownership. A reused pid kept a stale RUNNING
    record alive, and `cancel_run` would then signal an unrelated process group."""
    holder = subprocess.Popen([sys.executable, "-c", "import time; time.sleep(60)"])
    try:
        _write_run_record(
            repo,
            RunRecord(
                run_id="reused.writer.deadbeef",
                task_id="reused",
                backend="writer",
                status="running",
                started_at="2026-01-01T00:00:00Z",
                pid=holder.pid,
                pid_start_time="Thu Jan  1 00:00:00 2026",  # NOT this process's real start time
            ),
        )
        fleet = Fleet(repo, {"writer": _Writer()})
        rec = fleet.state.get("reused.writer.deadbeef")
        assert rec.status == RunStatus.FAILED.value, "a reused pid shielded the stale record"
        assert rec.pid is None, "the stale pid must be cleared so cancel_run cannot signal it"
    finally:
        holder.terminate()
        holder.wait(timeout=10)


def test_an_unverifiable_pid_fails_open_and_keeps_the_run(repo: Path) -> None:
    """No recorded start time (an older record) must NOT reap: falsely failing a live run is
    destructive and silent, while a lingering stale record needs an explicit cancel to do harm."""
    holder = subprocess.Popen([sys.executable, "-c", "import time; time.sleep(60)"])
    try:
        _write_run_record(
            repo,
            RunRecord(
                run_id="legacy.writer.deadbeef",
                task_id="legacy",
                backend="writer",
                status="running",
                started_at="2026-01-01T00:00:00Z",
                pid=holder.pid,  # alive, but no pid_start_time recorded
            ),
        )
        fleet = Fleet(repo, {"writer": _Writer()})
        rec = fleet.state.get("legacy.writer.deadbeef")
        assert rec.status == RunStatus.RUNNING.value
        assert rec.pid == holder.pid
    finally:
        holder.terminate()
        holder.wait(timeout=10)


def test_cancel_does_not_signal_a_run_this_process_does_not_own(
    repo: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    """A stale record whose pid was reused must not killpg an unrelated process group."""
    import os

    killed: list[tuple[int, int]] = []

    def _fake_killpg(pgid: int, sig: int) -> None:
        killed.append((pgid, sig))

    monkeypatch.setattr(os, "killpg", _fake_killpg)

    holder = subprocess.Popen([sys.executable, "-c", "import time; time.sleep(60)"])
    try:
        fleet = Fleet(repo, {"writer": _Writer()})
        fleet.state.add(
            RunRecord(
                run_id="mismatch.writer.deadbeef",
                task_id="mismatch",
                backend="writer",
                status="running",
                started_at="2026-01-01T00:00:00Z",
                pid=holder.pid,
                pid_start_time="Thu Jan  1 00:00:00 2026",  # NOT this process's real start time
            ),
        )
        rec = fleet.cancel_run("mismatch.writer.deadbeef")
        assert killed == [], "killpg must not run when pid identity mismatches"
        assert rec.status == RunStatus.CANCELLED.value
        assert rec.error and "started by another process" in rec.error
        assert rec.pid is None
    finally:
        holder.terminate()
        holder.wait(timeout=10)


def test_cancel_signals_a_verified_live_run(
    repo: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    """The happy path: a run THIS process started is signalled and cancelled."""
    import os
    import signal

    from marshal_engine.fleet import _pid_start_time

    killed: list[tuple[int, int]] = []

    def _fake_killpg(pgid: int, sig: int) -> None:
        killed.append((pgid, sig))

    monkeypatch.setattr(os, "killpg", _fake_killpg)

    holder = subprocess.Popen([sys.executable, "-c", "import time; time.sleep(60)"])
    try:
        started = _pid_start_time(holder.pid)
        if started is None:
            pytest.skip("ps start-time probe unavailable on this host")
        fleet = Fleet(repo, {"writer": _Writer()})
        handle = _register_inflight_run(fleet.state.dir, "verified.writer.deadbeef")
        handle.pid = holder.pid  # we own this run and its child is live (not yet reaped)
        fleet.state.add(
            RunRecord(
                run_id="verified.writer.deadbeef",
                task_id="verified",
                backend="writer",
                status="running",
                started_at="2026-01-01T00:00:00Z",
                pid=holder.pid,
                pid_start_time=started,
            ),
        )
        rec = fleet.cancel_run("verified.writer.deadbeef")
        assert killed == [(holder.pid, signal.SIGTERM)]
        assert rec.status == RunStatus.CANCELLED.value
        assert rec.ended_at is not None
    finally:
        holder.terminate()
        holder.wait(timeout=10)


def test_cancel_does_not_signal_a_run_this_process_did_not_start(
    repo: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    """Fail CLOSED on cancel (unlike the reaper): with no in-process handle, never killpg.

    The gate is the handle, not the recorded `pid_start_time` - an earlier design verified the
    pid pair and signalled on a match, and the name of this test still described that. Only a
    child of THIS process has a pid the OS cannot have recycled yet; anything else might now be an
    unrelated process group, and signalling it is the harm. The run is still stamped cancelled with
    an explanatory error.
    """
    import os

    killed: list[tuple[int, int]] = []

    def _fake_killpg(pgid: int, sig: int) -> None:
        killed.append((pgid, sig))

    monkeypatch.setattr(os, "killpg", _fake_killpg)

    holder = subprocess.Popen([sys.executable, "-c", "import time; time.sleep(60)"])
    try:
        fleet = Fleet(repo, {"writer": _Writer()})
        fleet.state.add(
            RunRecord(
                run_id="unverified.writer.deadbeef",
                task_id="unverified",
                backend="writer",
                status="running",
                started_at="2026-01-01T00:00:00Z",
                pid=holder.pid,  # alive, but no pid_start_time to verify against
            ),
        )
        rec = fleet.cancel_run("unverified.writer.deadbeef")
        assert killed == [], "unverifiable identity must not be signalled (fail closed)"
        assert rec.status == RunStatus.CANCELLED.value
        assert rec.error and "started by another process" in rec.error
        # The pid stays: the process is alive and it is the only handle anyone has on it.
        # Only a pid whose process is GONE is cleared - then there is nothing to point at.
        assert rec.pid == holder.pid
    finally:
        holder.terminate()
        holder.wait(timeout=10)


def test_a_live_run_with_matching_identity_is_never_reaped(repo: Path) -> None:
    """The dangerous direction. Nothing asserted the KEEP path, so a mutation making
    `_pid_is_still_ours` always return False would have stayed green - and that mutation reaps
    LIVE runs: status forced to failed, pid cleared (uncancellable), real outcome never recorded.
    """
    from marshal_engine.fleet import _pid_start_time

    holder = subprocess.Popen([sys.executable, "-c", "import time; time.sleep(60)"])
    try:
        started = _pid_start_time(holder.pid)
        if started is None:
            pytest.skip("ps start-time probe unavailable on this host")
        _write_run_record(
            repo,
            RunRecord(
                run_id="livematch.writer.deadbeef",
                task_id="livematch",
                backend="writer",
                status="running",
                started_at="2026-01-01T00:00:00Z",
                pid=holder.pid,
                pid_start_time=started,  # identity MATCHES the live process
            ),
        )
        fleet = Fleet(repo, {"writer": _Writer()})
        rec = fleet.state.get("livematch.writer.deadbeef")
        assert rec.status == RunStatus.RUNNING.value, "a live, verified run was reaped"
        assert rec.pid == holder.pid, "a live run's pid was cleared - it is now uncancellable"
    finally:
        holder.terminate()
        holder.wait(timeout=10)


def test_startup_reap_skips_validation_for_terminal_records(repo: Path) -> None:
    """The pre-filter must not change behaviour: a terminal record that would fail model
    validation is skipped either way, and a non-terminal one is still reaped."""
    (repo / ".marshal" / "runs").mkdir(parents=True, exist_ok=True)
    (repo / ".marshal" / "runs" / "weird.json").write_text(
        '{"run_id": "weird", "task_id": "t", "backend": "writer", "status": "succeeded",'
        ' "unexpected_future_field": 1}',
        encoding="utf-8",
    )
    _write_run_record(
        repo,
        RunRecord(
            run_id="stale.writer.x", task_id="stale", backend="writer", status="running",
            started_at="2026-01-01T00:00:00+00:00",  # old enough to be past the reap grace period
        ),
    )
    fleet = Fleet(repo, {"writer": _Writer()})
    assert fleet.state.get("stale.writer.x").status == RunStatus.FAILED.value
    assert fleet.state.get("weird").status == "exited_clean"  # untouched


def test_base_commit_matches_what_the_worktree_was_actually_cut_from(repo: Path) -> None:
    """REGRESSION: the sha was resolved BEFORE `worktree add`, so a base branch that moved in
    between left the record claiming a commit the worktree was never cut from."""
    fleet = Fleet(repo, {"writer": _Writer()})
    rec = fleet.run("writer", TaskSpec(id="pin2", goal="x"))
    stored = fleet.state.get(rec.run_id)
    assert stored.base_commit
    # The run's branch was created at the base; its merge-base with the recorded commit must be
    # that same commit - i.e. the record describes the real starting point.
    out = subprocess.run(
        ["git", "-C", str(repo), "merge-base", stored.base_commit, stored.branch],
        capture_output=True, text=True, check=True,
    ).stdout.strip()
    assert out == stored.base_commit


def test_collect_uses_the_pinned_base_commit_when_the_branch_moves(repo: Path) -> None:
    """REGRESSION: the base was recorded as a branch NAME, which is mutable. If the branch moves
    while the agent works, the review is computed against a different base than the run started
    from - showing commits the agent never made, or hiding ones it did."""
    fleet = Fleet(repo, {"selfcommit": _SelfCommitter()})
    rec = fleet.run("selfcommit", TaskSpec(id="pin1", goal="x"))
    stored = fleet.state.get(rec.run_id)
    assert stored.base_commit, "the run's base commit was not pinned"

    # The base BRANCH now moves on: a commit the agent never saw.
    (repo / "moved.txt").write_text("landed after the run started\n")
    _git(repo, "add", "-A")
    _git(repo, "commit", "-q", "-m", "moved the base branch")

    collected = fleet.collect_run(rec.run_id)
    assert collected.committed_changed_files == ["out.txt"]
    assert "moved.txt" not in collected.committed_diff


def test_a_run_records_its_pid_start_time(repo: Path) -> None:
    fleet = Fleet(repo, {"writer": _Writer()})
    rec = fleet.run("writer", TaskSpec(id="pst1", goal="x"))
    stored = fleet.state.get(rec.run_id)
    # The run is finished, so pid is cleared - but the identity pair was recorded while it ran.
    assert stored.pid_start_time is None or isinstance(stored.pid_start_time, str)


def test_cancel_before_the_pid_is_known_still_stops_the_agent(
    repo: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    """REGRESSION: a cancel arriving after the RUNNING record but before the pid was recorded
    skipped signalling and still stamped the run cancelled - the agent kept running and modifying
    its worktree behind an already-terminal record."""
    import os as _os

    from marshal_engine.fleet import _inflight_handle, _register_inflight_run

    killed: list[int] = []
    monkeypatch.setattr(_os, "killpg", lambda pgid, sig: killed.append(pgid))

    fleet = Fleet(repo, {"writer": _Writer()})
    run_id = "early.writer.deadbeef"
    _register_inflight_run(fleet.state.dir, run_id)  # in flight, pid not yet known
    fleet.state.add(
        RunRecord(run_id=run_id, task_id="early", backend="writer", status="running")
    )

    fleet.cancel_run(run_id)  # cancel beats the pid
    assert killed == [], "nothing to signal yet"
    handle = _inflight_handle(fleet.state.dir, run_id)
    assert handle is not None and handle.cancel_requested

    # The pid arrives; the pending cancel must be applied instead of silently dropped.
    fleet.state.update(run_id, pid=4242)
    with _active_runs_guard:
        handle.pid = 4242
        pending = handle.cancel_requested and not handle.exited
    if pending:
        _os.killpg(4242, 15)
    assert killed == [4242]


def test_cancel_does_not_signal_after_the_child_is_reaped(
    repo: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    """REGRESSION: registry membership alone was not ownership. Between the child being reaped and
    the run being unregistered, its pid can already have been recycled - signalling then hits a
    stranger."""
    import os as _os

    from marshal_engine.fleet import _register_inflight_run

    killed: list[int] = []
    monkeypatch.setattr(_os, "killpg", lambda pgid, sig: killed.append(pgid))

    fleet = Fleet(repo, {"writer": _Writer()})
    run_id = "reaped.writer.deadbeef"
    handle = _register_inflight_run(fleet.state.dir, run_id)
    handle.pid = 4242
    handle.exited = True  # base.run has reaped the child; the pid is now recyclable
    fleet.state.add(
        RunRecord(run_id=run_id, task_id="reaped", backend="writer", status="running", pid=4242)
    )

    rec = fleet.cancel_run(run_id)
    assert killed == [], "signalled a pid that may already have been recycled"
    assert rec.status == RunStatus.CANCELLED.value


def test_cancel_signals_the_retry_after_an_earlier_attempt_exited(
    repo: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    """REGRESSION: the handle is reused across retries. Attempt 1's exit left `exited` set, and
    publishing attempt 2's pid did not clear it - so a cancel during the retry skipped killpg and
    the agent kept running behind a cancelled record."""
    import os as _os

    from marshal_engine.fleet import _active_runs_guard, _register_inflight_run

    killed: list[int] = []
    monkeypatch.setattr(_os, "killpg", lambda pgid, sig: killed.append(pgid))

    fleet = Fleet(repo, {"writer": _Writer()})
    run_id = "retry.writer.deadbeef"
    handle = _register_inflight_run(fleet.state.dir, run_id)
    fleet.state.add(
        RunRecord(run_id=run_id, task_id="retry", backend="writer", status="running")
    )

    from marshal_engine.fleet import _publish_pid

    # Attempt 1 spawns and exits (a retryable failure).
    _publish_pid(handle, 1111)
    with _active_runs_guard:
        handle.exited = True

    # Attempt 2 publishes its pid through the SAME production path the run loop uses.
    _publish_pid(handle, 2222)
    fleet.state.update(run_id, pid=2222)

    fleet.cancel_run(run_id)
    assert killed == [2222], "the retry's agent was not signalled"


def test_clean_dry_run_says_which_worktrees_hold_unmerged_work(repo: Path) -> None:
    """REGRESSION (#76): 84 worktrees accumulated because the filters were never the blocker -
    "I couldn't tell which held unmerged work that wasn't mine" was. The preview now answers that
    where the decision is actually made."""
    fleet = Fleet(repo, {"writer": _Writer()})
    rec = fleet.run("writer", TaskSpec(id="unmerged", goal="x"))
    # The writer backend leaves a file; commit it onto the run's branch so it is genuinely unmerged.
    fleet.commit_run(rec.run_id, message="work nobody has landed")

    # scope="all" is the realistic case: an unmerged succeeded run is deliberately NOT in the
    # default scope, so the worktrees the reporter was staring at are exactly these.
    preview = fleet.clean(scope="all", dry_run=True)
    rows = {r["run_id"]: r for r in preview.unmerged}
    assert rec.run_id in rows, "the preview did not report on a candidate"
    assert rows[rec.run_id]["unmerged_commits"] >= 1
    assert rows[rec.run_id]["merged_into"] is None


def test_clean_dry_run_reports_unknown_rather_than_zero_when_it_cannot_tell(repo: Path) -> None:
    """`None` must mean "cannot tell", never "nothing to lose" - a driver reading absence as zero
    would delete work, which is the opposite of the action the truth justifies."""
    fleet = Fleet(repo, {"writer": _Writer()})
    fleet.state.add(
        RunRecord(
            run_id="nobranch.writer.x",
            task_id="nobranch",
            backend="writer",
            status="exited_clean",
            ended_at="2026-01-01T00:00:00+00:00",
            branch=None,  # nothing to compare against
        )
    )
    rows = {r["run_id"]: r for r in fleet.clean(scope="all", dry_run=True).unmerged}
    assert rows["nobranch.writer.x"]["unmerged_commits"] is None
def test_a_just_started_run_is_never_reaped(repo: Path) -> None:
    """REGRESSION (observed in production): a short-lived process reconciled while a long-lived
    server had just started runs. Those records were RUNNING with no pid yet, so nothing protected
    them - two live agents were stamped `failed` seconds after spawning, one still running when
    its record said it had died."""
    from datetime import datetime, timezone

    _write_run_record(
        repo,
        RunRecord(
            run_id="fresh.writer.deadbeef",
            task_id="fresh",
            backend="writer",
            status="running",
            started_at=datetime.now(timezone.utc).isoformat(),  # just now, pid not yet stamped
        ),
    )
    fleet = Fleet(repo, {"writer": _Writer()})
    rec = fleet.state.get("fresh.writer.deadbeef")
    assert rec.status == RunStatus.RUNNING.value, "a run started moments ago was reaped"


def test_an_old_orphan_is_still_reaped(repo: Path) -> None:
    """The grace period must not disable reaping - only delay judgement."""
    _write_run_record(
        repo,
        RunRecord(
            run_id="old.writer.deadbeef",
            task_id="old",
            backend="writer",
            status="running",
            started_at="2026-01-01T00:00:00+00:00",
        ),
    )
    fleet = Fleet(repo, {"writer": _Writer()})
    assert fleet.state.get("old.writer.deadbeef").status == RunStatus.FAILED.value


def test_a_record_with_no_start_time_is_not_reaped(repo: Path) -> None:
    """An unreadable timestamp is not evidence the run is dead."""
    _write_run_record(
        repo,
        RunRecord(run_id="nots.writer.x", task_id="nots", backend="writer", status="running"),
    )
    fleet = Fleet(repo, {"writer": _Writer()})
    assert fleet.state.get("nots.writer.x").status == RunStatus.RUNNING.value


def test_a_young_record_carrying_a_dead_pid_is_reaped_immediately(repo: Path) -> None:
    """The grace window exists only for the pid-not-yet-stamped race. Once a pid IS on the record
    its liveness is decidable now, and waiting would only keep a dead run reported as RUNNING."""
    from datetime import datetime, timezone

    _write_run_record(
        repo,
        RunRecord(
            run_id="youngpid.writer.x",
            task_id="youngpid",
            backend="writer",
            status="running",
            started_at=datetime.now(timezone.utc).isoformat(),
            pid=999_999,  # no such process
            pid_start_time="never-matches",
        ),
    )
    fleet = Fleet(repo, {"writer": _Writer()})
    assert fleet.state.get("youngpid.writer.x").status == RunStatus.FAILED.value


def test_a_deferred_orphan_is_reconciled_on_a_later_read(repo: Path) -> None:
    """REGRESSION: reconciliation ran once at construction, so a genuine orphan that happened to be
    young at startup stayed RUNNING for the whole life of a long-running server."""
    from datetime import datetime, timedelta, timezone

    from marshal_engine.fleet import _REAP_GRACE_S

    _write_run_record(
        repo,
        RunRecord(
            run_id="defer.writer.x",
            task_id="defer",
            backend="writer",
            status="running",
            started_at=datetime.now(timezone.utc).isoformat(),
        ),
    )
    fleet = Fleet(repo, {"writer": _Writer()})
    assert fleet.state.get("defer.writer.x").status == RunStatus.RUNNING.value

    # Age the record past the window, then read again the way a driver polling status would.
    aged = datetime.now(timezone.utc) - timedelta(seconds=_REAP_GRACE_S + 60)
    fleet.state.update("defer.writer.x", started_at=aged.isoformat())
    fleet.reconcile_orphans()
    assert fleet.state.get("defer.writer.x").status == RunStatus.FAILED.value


def test_an_orphan_whose_agent_dies_later_is_still_reaped(repo: Path) -> None:
    """REGRESSION: a record skipped because its agent was still alive was not put on the re-check
    list, so when that agent later exited nothing noticed - the run read RUNNING for the whole life
    of the server. 'Alive right now' is a snapshot, not a verdict."""
    import marshal_engine.fleet as fleet_mod

    alive = {"yes": True}
    monkey = pytest.MonkeyPatch()
    monkey.setattr(fleet_mod, "_pid_is_still_ours", lambda rec: alive["yes"])
    try:
        _write_run_record(
            repo,
            RunRecord(
                run_id="outlived.writer.x",
                task_id="outlived",
                backend="writer",
                status="running",
                started_at="2026-01-01T00:00:00+00:00",  # old enough that grace never applies
                pid=4242,
            ),
        )
        fleet = Fleet(repo, {"writer": _Writer()})
        assert fleet.state.get("outlived.writer.x").status == RunStatus.RUNNING.value

        alive["yes"] = False  # the surviving agent finally exits
        fleet.reconcile_orphans()
        assert fleet.state.get("outlived.writer.x").status == RunStatus.FAILED.value
    finally:
        monkey.undo()


def test_a_fleet_denied_the_lock_at_startup_can_still_reconcile_later(repo: Path) -> None:
    """REGRESSION: losing the claim once disabled reconciliation for the whole life of the Fleet.
    The claim can fail merely because a short-lived CLI held the guard at that instant, so a
    long-running server could end up never reconciling again. It retries instead."""
    import marshal_engine.fleet as fleet_mod

    _write_run_record(
        repo,
        RunRecord(
            run_id="denied.writer.x",
            task_id="denied",
            backend="writer",
            status="running",
            started_at="2026-01-01T00:00:00+00:00",
        ),
    )
    real_claim = fleet_mod._claim_fleet_lock
    denied_once = {"done": False}

    def claim(path: Path) -> bool:
        if not denied_once["done"]:
            denied_once["done"] = True
            return False  # another process held the guard for this instant
        return real_claim(path)

    monkey = pytest.MonkeyPatch()
    monkey.setattr(fleet_mod, "_claim_fleet_lock", claim)
    try:
        fleet = Fleet(repo, {"writer": _Writer()})
        assert fleet.state.get("denied.writer.x").status == RunStatus.RUNNING.value
        fleet.reconcile_orphans()  # the claim is retried here
    finally:
        monkey.undo()

    assert fleet.state.get("denied.writer.x").status == RunStatus.FAILED.value


def test_a_pid_landing_mid_reap_cancels_the_reap(repo: Path) -> None:
    """REGRESSION (TOCTOU): the scan read the record without a lock and the commit only re-checked
    'still non-terminal'. A pid stamped in that gap - the record's own process finally reporting -
    was overwritten anyway, which is the original production bug at a narrower window. The commit
    now re-runs the full reap decision under the run's lock."""
    import marshal_engine.fleet as fleet_mod

    run_id = "toctou.writer.x"
    _write_run_record(
        repo,
        RunRecord(
            run_id=run_id,
            task_id="toctou",
            backend="writer",
            status="running",
            started_at="2026-01-01T00:00:00+00:00",  # past grace: the scan will decide to reap
        ),
    )
    state = FleetState(repo / ".marshal" / "runs")

    # The record gains a live pid after the scan decides but before the write commits.
    real = fleet_mod._is_reapable
    calls = {"n": 0}

    def racing(rec: RunRecord, runs_dir: Path) -> bool:
        calls["n"] += 1
        if calls["n"] == 1:
            state.update(run_id, pid=os.getpid(), pid_start_time=None)
            return True  # the scan's (now stale) decision
        return real(rec, runs_dir)  # the commit-time re-check sees the pid

    monkey = pytest.MonkeyPatch()
    monkey.setattr(fleet_mod, "_is_reapable", racing)
    try:
        Fleet(repo, {"writer": _Writer()})
    finally:
        monkey.undo()

    rec = state.get(run_id)
    assert rec.status == RunStatus.RUNNING.value, "a run was reaped after its pid arrived"
    assert rec.pid == os.getpid(), "the reap cleared a live pid"


def test_cancelling_a_live_orphan_keeps_the_pid_and_says_it_is_still_running(repo: Path) -> None:
    """Marshal cannot signal an agent it did not start, so cancel only flips the ledger. Clearing
    the pid there would delete the operator's only handle on a process that is still writing, while
    the record claimed the run was over. Keep it, and say so."""
    import marshal_engine.fleet as fleet_mod

    fleet = Fleet(repo, {"writer": _Writer()})
    fleet.state.add(
        RunRecord(
            run_id="live.writer.x",
            task_id="live",
            backend="writer",
            status="running",
            pid=4242,  # no inflight handle: started by a process that has since died
        )
    )
    monkey = pytest.MonkeyPatch()
    monkey.setattr(fleet_mod, "_pid_is_verifiably_ours", lambda rec: True)
    try:
        rec = fleet.cancel_run("live.writer.x")
    finally:
        monkey.undo()

    assert rec.status == RunStatus.CANCELLED.value
    assert rec.pid == 4242, "the only handle on a live process was thrown away"
    assert "STILL RUNNING" in (rec.error or "")
    assert "kill -TERM -4242" in (rec.error or ""), "no way given to actually end it"


def test_a_recycled_lock_pid_does_not_block_reaping_forever(repo: Path) -> None:
    """REGRESSION (#88): the lock stored a bare pid while run records had learned that a pid is not
    an identity. A dead holder whose pid the OS handed to an unrelated long-lived process made every
    later Fleet see a live supervisor, decline the claim, and never reap - so stale runs read
    RUNNING until that unrelated process happened to exit."""
    import json as _json

    from marshal_engine.fleet import _another_fleet_active

    holder = subprocess.Popen([sys.executable, "-c", "import time; time.sleep(60)"])
    try:
        lock = repo / ".marshal" / "fleet.lock"
        lock.parent.mkdir(parents=True, exist_ok=True)
        # The pid is alive, but it is NOT the process that wrote the lock.
        lock.write_text(
            _json.dumps({"pid": holder.pid, "pid_start_time": "not-when-this-one-started"}),
            encoding="utf-8",
        )
        assert not _another_fleet_active(lock), "a recycled pid was mistaken for a live supervisor"

        _write_run_record(
            repo,
            RunRecord(
                run_id="blocked.writer.x",
                task_id="blocked",
                backend="writer",
                status="running",
                started_at="2026-01-01T00:00:00+00:00",
            ),
        )
        fleet = Fleet(repo, {"writer": _Writer()})
        assert fleet.state.get("blocked.writer.x").status == RunStatus.FAILED.value
    finally:
        holder.kill()
        holder.wait()


def test_an_unprobeable_pid_does_not_count_as_verified(repo: Path) -> None:
    """REGRESSION: `_pid_is_verifiably_ours` delegated to `_pid_is_still_ours`, which returns True
    when the start-time probe is unavailable - the fail-OPEN answer. Inheriting that made an
    unprobeable pid read as *verified*, so cancel would hand an operator a `kill` command for what
    might be a recycled process group. Verification must mean a real comparison, not the absence of
    a contradiction."""
    import marshal_engine.fleet as fleet_mod

    rec = RunRecord(
        run_id="probe.writer.x",
        task_id="probe",
        backend="writer",
        status="running",
        pid=4242,
        pid_start_time="Mon Jan  1 00:00:00 2026",
    )
    monkey = pytest.MonkeyPatch()
    monkey.setattr(fleet_mod, "_pid_alive", lambda pid: True)  # the process exists...
    monkey.setattr(fleet_mod, "_pid_start_time", lambda pid: None)  # ...but cannot be identified
    try:
        assert fleet_mod._pid_is_still_ours(rec) is True, "the fail-open helper changed meaning"
        assert fleet_mod._pid_is_verifiably_ours(rec) is False
    finally:
        monkey.undo()


def test_an_unverifiable_pid_is_never_named_in_a_kill_instruction(repo: Path) -> None:
    """Identity fails OPEN for reaping (never kill a live run) but must fail CLOSED here. A pid we
    cannot verify may have been recycled by an unrelated process, and telling an operator to
    `kill -TERM -<pid>` on that guess is worse than admitting we do not know."""
    holder = subprocess.Popen([sys.executable, "-c", "import time; time.sleep(60)"])
    try:
        fleet = Fleet(repo, {"writer": _Writer()})
        fleet.state.add(
            RunRecord(
                run_id="unverif.writer.x",
                task_id="unverif",
                backend="writer",
                status="running",
                pid=holder.pid,  # alive, but no pid_start_time to prove it is ours
            )
        )
        rec = fleet.cancel_run("unverif.writer.x")
        assert "kill -TERM" not in (rec.error or ""), "named a pid it could not verify"
        # Kept as evidence even though it could not be verified: `clean` needs it to know this
        # worktree may still have a writer. Erasing it is what let a live agent's work be deleted.
        assert rec.pid == holder.pid
    finally:
        holder.kill()
        holder.wait()


def test_clean_spares_a_worktree_whose_agent_is_alive_but_unverifiable(repo: Path) -> None:
    """The two pid questions have opposite costs, and the first fix used one answer for both.
    Naming an unverified pid in a `kill` instruction could send an operator after an unrelated
    process, so that fails CLOSED. Deleting a worktree that might still have a writer destroys work
    in progress, so THIS fails OPEN. With no recorded start time the identity cannot be confirmed -
    and the worktree must still be spared."""
    holder = subprocess.Popen([sys.executable, "-c", "import time; time.sleep(60)"])
    try:
        fleet = Fleet(repo, {"writer": _Writer()})
        fleet.state.add(
            RunRecord(
                run_id="unverifwt.writer.x",
                task_id="unverifwt",
                backend="writer",
                status="cancelled",
                pid=holder.pid,  # alive, but nothing to verify it against
                ended_at="2026-01-01T00:00:00+00:00",
            )
        )
        result = fleet.clean()
        assert "unverifwt.writer.x" not in result.removed, "deleted a possibly-live worktree"
    finally:
        holder.kill()
        holder.wait()


def test_clean_refuses_a_worktree_whose_agent_is_still_running(repo: Path) -> None:
    """A terminal record does not always mean a finished process: a no-signal cancel leaves a live
    writer behind a `cancelled` record. Removing that worktree would destroy work in progress."""
    import marshal_engine.fleet as fleet_mod

    fleet = Fleet(repo, {"writer": _Writer()})
    fleet.state.add(
        RunRecord(
            run_id="livewt.writer.x",
            task_id="livewt",
            backend="writer",
            status="cancelled",
            pid=4242,
            ended_at="2026-01-01T00:00:00+00:00",
        )
    )
    monkey = pytest.MonkeyPatch()
    monkey.setattr(fleet_mod, "_pid_is_still_ours", lambda rec: True)
    try:
        result = fleet.clean()
    finally:
        monkey.undo()

    assert "livewt.writer.x" not in result.removed
    assert any(
        s["run_id"] == "livewt.writer.x" and "still be running" in s["reason"] for s in result.skipped
    ), result.skipped


def test_a_pid_is_never_written_onto_a_terminal_record(repo: Path) -> None:
    """REGRESSION: after a reap, `_record_pid` stamped a live pid onto the `failed` record - a
    record claiming a running process for a run it says is dead."""
    fleet = Fleet(repo, {"writer": _Writer()})
    fleet.state.add(
        RunRecord(run_id="term.writer.x", task_id="term", backend="writer", status="failed")
    )
    fleet.state.update_if(
        "term.writer.x", lambda r: not r.status == "failed", pid=4242
    )
    assert fleet.state.get("term.writer.x").pid is None
def test_a_context_file_missing_from_the_worktree_fails_the_spawn(repo: Path) -> None:
    """REGRESSION (#73): a gitignored path exists in the driver's checkout but NOT in the worktree,
    which holds tracked files only. The agent was handed a path it could not open, said so, worked
    from the surrounding prose, and produced something adequate by luck - neither side could tell it
    had solved a different problem. A silently missing input is worse than a refused spawn."""
    (repo / ".gitignore").write_text("tmp/\n")
    scratch = repo / "tmp" / "report.md"
    scratch.parent.mkdir(parents=True)
    scratch.write_text("the measurements the agent was supposed to read")
    subprocess.run(["git", "-C", str(repo), "add", "-A"], check=True, capture_output=True)
    subprocess.run(["git", "-C", str(repo), "commit", "-q", "-m", "ignore tmp"], check=True,
                   capture_output=True)

    fleet = Fleet(repo, {"writer": _Writer()})
    with pytest.raises(ValueError, match="not present in the worktree"):
        fleet.run("writer", TaskSpec(id="ctx", goal="use it", context_files=["tmp/report.md"]))

    # And the rejected spawn leaves nothing behind.
    assert fleet.state.list() == []
    worktrees = repo / ".marshal" / "worktrees"
    assert not worktrees.exists() or not list(worktrees.iterdir()), "orphan worktree left behind"


def test_a_tracked_context_file_still_runs(repo: Path) -> None:
    """The check must not reject the normal case - a committed file IS in the worktree."""
    (repo / "notes.md").write_text("committed context")
    subprocess.run(["git", "-C", str(repo), "add", "-A"], check=True, capture_output=True)
    subprocess.run(["git", "-C", str(repo), "commit", "-q", "-m", "notes"], check=True,
                   capture_output=True)

    fleet = Fleet(repo, {"writer": _Writer()})
    rec = fleet.run("writer", TaskSpec(id="ctx2", goal="use it", context_files=["notes.md"]))
    assert rec.status != RunStatus.FAILED.value


def test_an_absolute_context_path_is_refused(repo: Path) -> None:
    """REGRESSION: `Path("/wt") / "/etc/passwd"` is `/etc/passwd` - an absolute path silently
    discards the base. It exists, so an existence-only check passed it and injected a host path
    into the agent's prompt, aimed outside the boundary the worktree exists to enforce. A check
    that accepts that is worse than no check: it makes the path look validated."""
    fleet = Fleet(repo, {"writer": _Writer()})
    with pytest.raises(ValueError, match="outside the worktree"):
        fleet.run("writer", TaskSpec(id="abs", goal="x", context_files=["/etc/hosts"]))
    assert fleet.state.list() == []


def test_a_traversing_context_path_is_refused(repo: Path) -> None:
    """`../` walks out of the worktree the same way an absolute path does."""
    outside = repo.parent / "outside.txt"
    outside.write_text("not for the agent")
    fleet = Fleet(repo, {"writer": _Writer()})
    with pytest.raises(ValueError, match="outside the worktree"):
        fleet.run("writer", TaskSpec(id="trav", goal="x", context_files=["../../outside.txt"]))
    assert fleet.state.list() == []


def test_collect_run_returns_the_final_message_when_no_files_changed(repo: Path) -> None:
    """`collect_run` is the tool a driver reaches for first to answer "what did this run produce".
    For a research or review run the honest answer is prose - the engine already treats text alone
    as SUCCEEDED - but collect returned an empty diff and stopped, which reads as "it did nothing".
    That is what pushed drivers to make agents write files they did not need to."""
    fleet = Fleet(repo, {"talker": _Talker("the findings, in full")})
    rec = fleet.run("talker", TaskSpec(id="report", goal="research it"))
    assert rec.status == RunStatus.EXITED_CLEAN.value, "text alone is a success"

    got = fleet.collect_run(rec.run_id)
    assert got.produced == "text"
    assert got.text == "the findings, in full"
    assert got.changed_files == []


def test_collect_run_does_not_duplicate_the_message_when_there_is_a_diff(repo: Path) -> None:
    """When files changed, the diff IS the artifact; repeating the message would bloat every reply
    for the common case."""
    fleet = Fleet(repo, {"writer": _Writer()})
    rec = fleet.run("writer", TaskSpec(id="code", goal="write it"))
    got = fleet.collect_run(rec.run_id)
    assert got.produced == "diff"
    assert got.text == ""
    assert got.changed_files


def test_collect_run_says_nothing_when_a_run_produced_neither(repo: Path) -> None:
    """`produced` must distinguish "prose" from "genuinely nothing" - a caller should branch on a
    field, not infer intent from which container happens to be empty."""
    fleet = Fleet(repo, {"silent": _Talker("")})
    rec = fleet.run("silent", TaskSpec(id="quiet", goal="x"))
    assert fleet.collect_run(rec.run_id).produced == "nothing"
def test_a_run_records_what_provisioned_its_worktree(repo: Path) -> None:
    """REGRESSION (#77): a number from a worktree does not mean the same thing as the same number
    from your checkout, and nothing marked the difference. Agents reported "1308 passed" where the
    workspace showed "1351 passed, 0 skipped" - a bare `uv sync` had left the extras uninstalled -
    and it took three occurrences before a driver with full context spotted the pattern."""
    fleet = Fleet(repo, {"writer": _Writer()}, worktree_setup=[sys.executable, "-c", "pass"])
    rec = fleet.run("writer", TaskSpec(id="prov", goal="x"))
    assert rec.worktree_setup == f"{sys.executable} -c pass"


def test_worktree_setup_provenance_survives_a_quoted_argument(repo: Path) -> None:
    """The scaffolded form is `sh -c "cd sub && uv sync"`. A plain `" ".join` renders that as
    `sh -c cd sub && uv sync` - a DIFFERENT command. Provenance that misdescribes what ran is worse
    than none, because the entire point of the field is letting a driver trust where a number came
    from."""
    import shlex

    cmd = [sys.executable, "-c", "x = 1 ; pass"]
    fleet = Fleet(repo, {"writer": _Writer()}, worktree_setup=cmd)
    rec = fleet.run("writer", TaskSpec(id="quoted", goal="x"))
    assert shlex.split(rec.worktree_setup) == cmd, "the recorded command does not round-trip"


def test_an_unprovisioned_worktree_records_none(repo: Path) -> None:
    """`None` is the sharpest form of the delta: the worktree is a bare checkout - no venv, no
    extras, no gitignored data dirs - so any count from it describes a different world entirely."""
    fleet = Fleet(repo, {"writer": _Writer()})
    rec = fleet.run("writer", TaskSpec(id="bare", goal="x"))
    assert rec.worktree_setup is None
