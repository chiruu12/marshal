"""Tests for MarshalService - client resolution + run recording (dummy backend, no network)."""

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
from marshal_engine.config import ClientConfig, FleetConfig
from marshal_engine.service import MarshalService


class _Echo(CodingAgentBackend):
    name = "echo"
    binary = "python"
    capabilities = Capabilities()

    def check_available(self) -> bool:
        return True

    def build_invocation(self, task: TaskSpec, opts: RunOpts) -> list[str]:
        return [sys.executable, "-c", "print('ok')"]

    def map_permission(self, mode: PermissionMode) -> list[str]:
        return []

    def parse_output(self, raw_stdout: str, raw_stderr: str, exit_code: int) -> AgentResult:
        return AgentResult(
            status=RunStatus.SUCCEEDED if exit_code == 0 else RunStatus.FAILED,
            text=raw_stdout.strip(),
            usage=UsageRecord(backend="echo", cost_usd=0.002, source=UsageSource.NATIVE),
            exit_code=exit_code,
        )


class _Pricey(CodingAgentBackend):
    """A second strategy with a higher native cost - used to compare benchmark strategies."""

    name = "pricey"
    binary = "python"
    capabilities = Capabilities()

    def check_available(self) -> bool:
        return True

    def build_invocation(self, task: TaskSpec, opts: RunOpts) -> list[str]:
        return [sys.executable, "-c", "print('ok')"]

    def map_permission(self, mode: PermissionMode) -> list[str]:
        return []

    def parse_output(self, raw_stdout: str, raw_stderr: str, exit_code: int) -> AgentResult:
        return AgentResult(
            status=RunStatus.SUCCEEDED if exit_code == 0 else RunStatus.FAILED,
            text=raw_stdout.strip(),
            usage=UsageRecord(backend="pricey", cost_usd=0.05, source=UsageSource.NATIVE),
            exit_code=exit_code,
        )


class _Unpriced(CodingAgentBackend):
    """A strategy with no usage info - its cost is 'unavailable', not a real $0."""

    name = "noinfo"
    binary = "python"
    capabilities = Capabilities()

    def check_available(self) -> bool:
        return True

    def build_invocation(self, task: TaskSpec, opts: RunOpts) -> list[str]:
        return [sys.executable, "-c", "print('done')"]

    def map_permission(self, mode: PermissionMode) -> list[str]:
        return []

    def parse_output(self, raw_stdout: str, raw_stderr: str, exit_code: int) -> AgentResult:
        return AgentResult(status=RunStatus.SUCCEEDED, text=raw_stdout.strip(), exit_code=exit_code)


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


def _svc(repo: Path) -> MarshalService:
    cfg = FleetConfig(
        clients={"worker": ClientConfig(name="worker", backend="echo", permission=PermissionMode.SAFE_EDIT)}
    )
    return MarshalService(repo, cfg, backends={"echo": _Echo()})


def test_list_clients(repo: Path) -> None:
    svc = _svc(repo)
    clients = svc.list_clients()
    assert [c.model_dump() for c in clients] == [
        {"name": "worker", "backend": "echo", "model": None, "permission": "safe-edit"}
    ]


def test_run_agent_records(repo: Path) -> None:
    svc = _svc(repo)
    rec = svc.run_agent("worker", "do something", task_id="t1")
    assert rec.status == "succeeded"
    assert rec.run_id.startswith("t1.echo.")  # task.backend.<uuid>
    assert svc.get_run(rec.run_id) is not None
    assert svc.status()[0].run_id == rec.run_id
    assert svc.usage().totals.runs == 1
    assert abs(svc.usage().totals.cost_usd - 0.002) < 1e-9


def test_run_agent_unknown_client(repo: Path) -> None:
    svc = _svc(repo)
    with pytest.raises(ValueError):
        svc.run_agent("nope", "x")


def test_collect_run_surfaces_changed_files(repo: Path) -> None:
    svc = _svc(repo)
    rec = svc.run_agent("worker", "do something", task_id="t1")
    collected = svc.collect_run(rec.run_id)
    assert collected.run_id == rec.run_id
    assert collected.branch == rec.branch


def _bench_svc(repo: Path, backends: dict[str, object], **clients: str) -> MarshalService:
    cfg = FleetConfig(
        clients={
            name: ClientConfig(name=name, backend=backend, permission=PermissionMode.SAFE_EDIT)
            for name, backend in clients.items()
        }
    )
    return MarshalService(repo, cfg, backends=backends)  # type: ignore[arg-type]


def test_benchmark_compares_strategies(repo: Path) -> None:
    svc = _bench_svc(repo, {"echo": _Echo(), "pricey": _Pricey()}, cheap="echo", dear="pricey")
    result = svc.benchmark("do x", ["cheap", "dear"], task_id="b1")

    assert result.task_id == "b1"
    assert {s.client for s in result.strategies} == {"cheap", "dear"}
    assert all(s.status == "succeeded" for s in result.strategies)
    assert result.cheapest == "cheap"          # 0.002 < 0.05, both costs native (known)
    assert result.fastest in {"cheap", "dear"}
    assert len({s.run_id for s in result.strategies}) == 2  # distinct runs, shared task_id


def test_report_requeries_a_past_benchmark(repo: Path) -> None:
    svc = _bench_svc(repo, {"echo": _Echo(), "pricey": _Pricey()}, cheap="echo", dear="pricey")
    svc.benchmark("do x", ["cheap", "dear"], task_id="b2")
    again = svc.report("b2")  # pure re-query from the ledger
    assert again.cheapest == "cheap"
    assert len(again.strategies) == 2


def test_benchmark_cheapest_excludes_unknown_cost(repo: Path) -> None:
    # a strategy whose cost is "unavailable" must NOT win cheapest just because it reports $0
    svc = _bench_svc(repo, {"echo": _Echo(), "noinfo": _Unpriced()}, known="echo", mystery="noinfo")
    result = svc.benchmark("x", ["known", "mystery"], task_id="b3")
    assert result.cheapest == "known"  # not "mystery", despite its $0 unavailable cost


def test_run_many_runs_each_client_job(repo: Path) -> None:
    svc = _svc(repo)
    jobs = [
        {"client": "worker", "goal": "a", "task_id": "j1"},
        {"client": "worker", "goal": "b", "task_id": "j2"},
        {"client": "worker", "goal": "c", "task_id": "j3"},
    ]
    records = svc.run_many(jobs, max_concurrency=3)
    assert [r.task_id for r in records] == ["j1", "j2", "j3"]
    assert all(r.status == "succeeded" for r in records)
    assert len(svc.status()) == 3


def test_spawn_records_running_then_finishes(repo: Path) -> None:
    svc = _svc(repo)
    try:
        rec = svc.spawn("worker", "do x", task_id="sp1")
        assert rec.run_id.startswith("sp1.echo.")
        assert rec.status in ("running", "succeeded")  # RUNNING at spawn; may finish fast
        deadline = time.monotonic() + 10
        while time.monotonic() < deadline:
            got = svc.get_run(rec.run_id)
            if got and got.status != "running":
                break
            time.sleep(0.05)
        got = svc.get_run(rec.run_id)
        assert got is not None and got.status == "succeeded"
    finally:
        svc.shutdown()


def test_run_many_unknown_client_fails_fast(repo: Path) -> None:
    svc = _svc(repo)
    with pytest.raises(ValueError):
        svc.run_many([{"client": "nope", "goal": "x"}])
    assert svc.status() == []  # nothing ran - validated before launching


def test_integrate_empty_run_is_noop(repo: Path) -> None:
    svc = _svc(repo)  # _Echo prints but writes no files
    rec = svc.run_agent("worker", "do nothing", task_id="e1")
    result = svc.integrate(rec.run_id)
    assert result.status == "empty"
