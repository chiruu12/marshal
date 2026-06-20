"""Integration test for the Fleet orchestrator using a dummy file-writing backend (no network)."""

from __future__ import annotations

import subprocess
import sys
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
from marshal_engine.fleet import Fleet
from marshal_engine.pricing import ModelPrice, PriceTable


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
    assert rec.run_id == "t1.writer"

    wt = Path(rec.worktree or "")
    assert wt.exists()  # kept by default for later collect/integrate
    assert (wt / "out.txt").read_text() == "hi"

    assert fleet.state.get("t1.writer") is not None
    s = fleet.usage.summary()
    assert s["totals"]["runs"] == 1
    assert s["by_backend"]["writer"]["runs"] == 1


def test_fleet_unknown_backend(repo: Path) -> None:
    fleet = Fleet(repo, {})
    with pytest.raises(ValueError):
        fleet.run("nope", TaskSpec(id="t", goal="x"))


def test_clean_run_with_no_work_is_empty(repo: Path) -> None:
    fleet = Fleet(repo, {"noop": _NoOp()})
    rec = fleet.run("noop", TaskSpec(id="e1", goal="x"))
    assert rec.status == "empty"  # exit 0 but no text and no file changes


def test_tokened_run_gets_estimated_cost(repo: Path) -> None:
    prices = PriceTable({"m": ModelPrice(input_per_mtok=10.0, output_per_mtok=0.0)})
    fleet = Fleet(repo, {"tok": _Tokened()}, prices=prices)
    rec = fleet.run("tok", TaskSpec(id="p1", goal="x"))
    assert rec.status == "succeeded"
    assert rec.cost_usd == 10.0          # 1M input tokens @ $10/Mtok
    assert rec.source == "estimated"
    assert rec.duration_ms >= 0


def test_tokened_run_unpriced_is_unavailable_not_zero(repo: Path) -> None:
    fleet = Fleet(repo, {"tok": _Tokened()}, prices=PriceTable({}))  # empty table
    rec = fleet.run("tok", TaskSpec(id="p2", goal="x"))
    assert rec.cost_usd == 0.0
    assert rec.source == "unavailable"   # unpriced -> cost unknown, never shown as a real $0


def test_collect_run_returns_diff_and_changed_files(repo: Path) -> None:
    fleet = Fleet(repo, {"writer": _Writer()})
    fleet.run("writer", TaskSpec(id="c1", goal="x"), ts="2026-06-19T00:00:00Z")
    collected = fleet.collect_run("c1.writer")
    assert collected.run_id == "c1.writer"
    assert collected.branch == "marshal/c1.writer"
    assert collected.changed_files == ["out.txt"]
    assert "out.txt" in collected.diff  # the agent's new (untracked) file is in the diff


def test_collect_run_unknown_run_raises(repo: Path) -> None:
    fleet = Fleet(repo, {"writer": _Writer()})
    with pytest.raises(ValueError):
        fleet.collect_run("nope.writer")


def test_integrate_merges_run_into_current_branch(repo: Path) -> None:
    fleet = Fleet(repo, {"writer": _Writer()})
    fleet.run("writer", TaskSpec(id="m1", goal="x"), ts="2026-06-19T00:00:00Z")
    result = fleet.integrate("m1.writer")
    assert result.status == "merged"
    assert result.merged_into  # the repo's current branch
    assert result.changed_files == ["out.txt"]
    assert (repo / "out.txt").read_text() == "hi"  # work landed on the main checkout
    rec = fleet.state.get("m1.writer")
    assert rec is not None and rec.merged_into == result.merged_into


def test_integrate_reports_conflict_and_aborts(repo: Path) -> None:
    fleet = Fleet(repo, {"patcher": _Patcher()})
    fleet.run("patcher", TaskSpec(id="a", goal="x"))
    fleet.run("patcher", TaskSpec(id="b", goal="x"))
    assert fleet.integrate("a.patcher").status == "merged"
    conflict = fleet.integrate("b.patcher")
    assert conflict.status == "conflict"
    assert "README.md" in conflict.conflicts
    assert (repo / "README.md").read_text() == "a"  # aborted -> main untouched
