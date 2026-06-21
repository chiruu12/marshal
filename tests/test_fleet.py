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
from marshal_engine.fleet import Fleet, RunRequest
from marshal_engine.pricing import ModelPrice, PriceTable
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
    """Rewrites a tracked file with task-specific content — used to force merge conflicts."""

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
    """Sleeps then prints — used to prove run_many actually runs concurrently."""

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
    """Exits 0 but writes nothing and prints nothing — should be recorded as EMPTY, not success."""

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
    """Reports tokens but no cost (like Codex) — the engine must price it via the table."""

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
                source=UsageSource.UNAVAILABLE,  # tokens known, cost not — engine prices it
            ),
            exit_code=exit_code,
        )


class _SilentWriter(CodingAgentBackend):
    """Writes a file but returns empty text — a write-only success that must NOT be marked EMPTY."""

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
            text=raw_stdout.strip(),  # empty — the only success signal is the file it wrote
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


class _Exploder(CodingAgentBackend):
    """parse_output raises (propagates out of base.run) — the run loop must terminal-stamp it."""

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
    assert rec.run_id.startswith("t1.writer.")  # task.backend.<uuid> — globally unique

    wt = Path(rec.worktree or "")
    assert wt.exists()  # kept by default for later collect/integrate
    assert (wt / "out.txt").read_text() == "hi"

    assert fleet.state.get(rec.run_id) is not None
    s = fleet.usage.summary()
    assert s.totals.runs == 1
    assert s.by_backend["writer"].runs == 1


def test_fleet_unknown_backend(repo: Path) -> None:
    fleet = Fleet(repo, {})
    with pytest.raises(ValueError):
        fleet.run("nope", TaskSpec(id="t", goal="x"))


def test_run_loop_stamps_failed_on_exception(repo: Path) -> None:
    fleet = Fleet(repo, {"boom": _Exploder()})
    with pytest.raises(RuntimeError):
        fleet.run("boom", TaskSpec(id="x1", goal="x"))
    runs = fleet.state.list()
    assert len(runs) == 1
    assert runs[0].status == "failed"  # not left stranded as RUNNING
    assert runs[0].error and "kaboom" in runs[0].error


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
