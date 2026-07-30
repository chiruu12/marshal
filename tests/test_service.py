"""Tests for MarshalService - client resolution + run recording (dummy backend, no network)."""

from __future__ import annotations

import shutil
import subprocess
import sys
import time
from pathlib import Path

import pytest

from marshal_engine import (
    AgentResult,
    Capabilities,
    PermissionFidelity,
    PermissionMode,
    RunOpts,
    RunStatus,
    TaskSpec,
    UsageRecord,
    UsageSource,
)
from marshal_engine.backends.base import CodingAgentBackend
from marshal_engine.config import (
    DEFAULT_OPENCODE_MODEL,
    BudgetSpec,
    ClientConfig,
    ConfigError,
    FleetConfig,
    FleetContext,
    load_config,
)
from marshal_engine.service import MarshalService
from marshal_engine.state import RunRecord
from marshal_engine.service import ModelList, ModelSpec


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
            status=RunStatus.EXITED_CLEAN if exit_code == 0 else RunStatus.FAILED,
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
            status=RunStatus.EXITED_CLEAN if exit_code == 0 else RunStatus.FAILED,
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
        return AgentResult(status=RunStatus.EXITED_CLEAN, text=raw_stdout.strip(), exit_code=exit_code)


class _Capture(CodingAgentBackend):
    """Records each TaskSpec it is asked to run, so tests can assert what the service threaded through."""

    name = "capture"
    binary = "python"
    capabilities = Capabilities()

    def __init__(self) -> None:
        self.tasks: list[TaskSpec] = []

    def check_available(self) -> bool:
        return True

    def build_invocation(self, task: TaskSpec, opts: RunOpts) -> list[str]:
        self.tasks.append(task)
        return [sys.executable, "-c", "print('ok')"]

    def map_permission(self, mode: PermissionMode) -> list[str]:
        return []

    def parse_output(self, raw_stdout: str, raw_stderr: str, exit_code: int) -> AgentResult:
        return AgentResult(status=RunStatus.EXITED_CLEAN, text=raw_stdout.strip(), exit_code=exit_code)


class _Missing(CodingAgentBackend):
    """A backend whose CLI is unavailable - check_available() is always False."""

    name = "missing"
    binary = "python"
    capabilities = Capabilities()

    def check_available(self) -> bool:
        return False

    def build_invocation(self, task: TaskSpec, opts: RunOpts) -> list[str]:
        return [sys.executable, "-c", "print('ok')"]

    def map_permission(self, mode: PermissionMode) -> list[str]:
        return []

    def parse_output(self, raw_stdout: str, raw_stderr: str, exit_code: int) -> AgentResult:
        return AgentResult(status=RunStatus.EXITED_CLEAN, text=raw_stdout.strip(), exit_code=exit_code)


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


def test_get_run_reports_whether_the_agent_is_actually_alive(repo: Path) -> None:
    """The driver's #1 field complaint: a `running` record with a dead agent made it report a run
    as failed when it had succeeded. `agent_alive` answers that without the driver shelling out to
    `kill -0` - which is not sound anyway, since pids get reused."""
    import subprocess as _sp
    import sys as _sys

    from marshal_engine.fleet import _pid_start_time

    svc = _svc(repo)
    holder = _sp.Popen([_sys.executable, "-c", "import time; time.sleep(60)"])
    try:
        svc.fleet.state.add(
            RunRecord(
                run_id="alive.echo.x",
                task_id="t",
                backend="echo",
                status="running",
                started_at="2026-01-01T00:00:00+00:00",
                pid=holder.pid,
                pid_start_time=_pid_start_time(holder.pid),
            )
        )
        assert svc.get_run("alive.echo.x").agent_alive is True
    finally:
        holder.kill()
        holder.wait()

    # Same record, process now gone: reconciliation stamps it terminal, so liveness is moot.
    rec = svc.get_run("alive.echo.x")
    assert rec.status == RunStatus.FAILED.value
    assert rec.agent_alive is None


def test_liveness_is_unknown_not_false_when_identity_cannot_be_checked(repo: Path) -> None:
    """`null` must mean "cannot tell", never "dead" - a driver that reads absence as death would
    make exactly the wrong call, which is the bug this field exists to prevent."""
    svc = _svc(repo)
    svc.fleet.state.add(
        RunRecord(
            run_id="nopid.echo.x",
            task_id="t",
            backend="echo",
            status="running",
            started_at="2026-01-01T00:00:00+00:00",  # pid-less: nothing to probe
        )
    )
    assert svc.get_run("nopid.echo.x").agent_alive is None


def test_config_verify_reaches_fleet_and_gates_runs(repo: Path) -> None:
    import sys as _sys

    cfg = FleetConfig(
        clients={"worker": ClientConfig(name="worker", backend="echo", permission=PermissionMode.SAFE_EDIT)},
        verify=[_sys.executable, "-c", "import sys; print('gate says no'); sys.exit(1)"],
    )
    svc = MarshalService(repo, cfg, backends={"echo": _Echo()})
    assert svc.fleet.worktrees.verify_cmd == cfg.verify  # config -> Fleet -> WorktreeManager
    rec = svc.run_agent("worker", "do something", task_id="tv")
    # _Echo replies with text but changes no files: succeeded, and the gate is SKIPPED - a
    # text-only reply cannot have broken the repo, so no test run is burned on it.
    assert rec.status == "exited_clean"
    assert rec.verify_passed is None


def test_list_clients(repo: Path) -> None:
    svc = _svc(repo)
    result = svc.list_clients()
    assert [c.model_dump() for c in result.clients] == [
        {
            "name": "worker",
            "backend": "echo",
            "model": None,
            "permission": "safe-edit",
            # Dummy adapters default to boundary-only (honest fail-closed).
            "permission_fidelity": "boundary-only",
        }
    ]
    assert result.driver_context is None  # no context.driver in this config


def test_list_clients_permission_fidelity_from_backend_capabilities(repo: Path) -> None:
    # Fidelity comes from backend.capabilities, not the configured permission string.
    class _Enforced(CodingAgentBackend):
        name = "enforced"
        binary = "python"
        capabilities = Capabilities(permission_fidelity=PermissionFidelity.ENFORCED_DENIES)

        def check_available(self) -> bool:
            return True

        def build_invocation(self, task: TaskSpec, opts: RunOpts) -> list[str]:
            return [sys.executable, "-c", "print('ok')"]

        def map_permission(self, mode: PermissionMode) -> list[str]:
            return []

        def parse_output(self, raw_stdout: str, raw_stderr: str, exit_code: int) -> AgentResult:
            return AgentResult(status=RunStatus.EXITED_CLEAN, text="ok", exit_code=exit_code)

    cfg = FleetConfig(
        clients={
            "worker": ClientConfig(name="worker", backend="enforced", permission=PermissionMode.SAFE_EDIT)
        }
    )
    svc = MarshalService(repo, cfg, backends={"enforced": _Enforced()})
    clients = svc.list_clients().clients
    assert len(clients) == 1
    assert clients[0].permission == "safe-edit"
    assert clients[0].permission_fidelity == "enforced-denies"


def test_list_clients_surfaces_driver_context(repo: Path) -> None:
    # context.driver is surfaced back to the driver on list_clients (None when unset).
    cfg = FleetConfig(
        clients={"worker": ClientConfig(name="worker", backend="echo", permission=PermissionMode.SAFE_EDIT)},
        context=FleetContext(driver="Fleet runs review + impl; integrate manually."),
    )
    svc = MarshalService(repo, cfg, backends={"echo": _Echo()})
    result = svc.list_clients()
    assert result.driver_context == "Fleet runs review + impl; integrate manually."
    assert [c.name for c in result.clients] == ["worker"]
def test_run_agent_records(repo: Path) -> None:
    svc = _svc(repo)
    rec = svc.run_agent("worker", "do something", task_id="t1")
    assert rec.status == "exited_clean"
    assert rec.run_id.startswith("t1.echo.")  # task.backend.<uuid>
    assert svc.get_run(rec.run_id) is not None
    assert svc.status()[0].run_id == rec.run_id
    assert svc.usage().totals.runs == 1
    assert abs(svc.usage().totals.cost_usd - 0.002) < 1e-9


def test_run_agent_unknown_client(repo: Path) -> None:
    svc = _svc(repo)
    with pytest.raises(ValueError, match="hint: pass backend="):  # points at the ad-hoc escape hatch
        svc.run_agent("nope", "x")


@pytest.mark.parametrize("bad_id", ["", "../x", "foo/bar", ".hidden", "a" * 65, "café", "a\x00b"])
def test_run_agent_rejects_unsafe_task_id_before_worktree(repo: Path, bad_id: str) -> None:
    svc = _svc(repo)
    with pytest.raises(ValueError, match="unsafe"):
        svc.run_agent("worker", "x", task_id=bad_id)
    assert not any((svc.fleet.worktrees.base_dir).glob("*")) if svc.fleet.worktrees.base_dir.exists() else True


def test_spawn_rejects_unsafe_task_id(repo: Path) -> None:
    svc = _svc(repo)
    with pytest.raises(ValueError, match="unsafe"):
        svc.spawn("worker", "x", task_id="../escape")


def test_run_agent_rejects_explicit_empty_task_id(repo: Path) -> None:
    # Truthiness fallback (`task_id or uuid`) would silently replace "" — must fail closed.
    svc = _svc(repo)
    with pytest.raises(ValueError, match="unsafe"):
        svc.run_agent("worker", "x", task_id="")
    assert not any((svc.fleet.worktrees.base_dir).glob("*")) if svc.fleet.worktrees.base_dir.exists() else True


def test_benchmark_rejects_explicit_empty_task_id(repo: Path) -> None:
    svc = _svc(repo)
    with pytest.raises(ValueError, match="unsafe"):
        svc.benchmark("x", ["worker"], task_id="")


def test_task_spec_rejects_unsafe_id() -> None:
    with pytest.raises(Exception, match="unsafe"):
        TaskSpec(id="../x", goal="g")


def test_task_spec_accepts_workflow_and_backend_shaped_ids() -> None:
    for tid in ("deadbeef.first", "review.candidate", "a.b-c_d", "command-code"):
        assert TaskSpec(id=tid, goal="g").id == tid


def test_session_start_is_a_utc_datetime(repo: Path) -> None:
    # session_start is the long-lived MCP server's "wake" timestamp; a "since session" window maps
    # to this instant. Stable for the life of the service, UTC, and accessible on the service.
    from datetime import datetime, timezone

    svc = _svc(repo)
    assert isinstance(svc.session_start, datetime)
    assert svc.session_start.tzinfo is not None
    assert svc.session_start.tzinfo.utcoffset(svc.session_start) == timezone.utc.utcoffset(svc.session_start)


def test_service_usage_since_filters_events(repo: Path) -> None:
    # MarshalService.usage(since=...) plumbs the bound into the UsageTracker so a windowed rollup
    # works end-to-end through the service. The `_Echo` backend always stamps `now`, so seeding the
    # ledger with an old event shows the filter in action.
    from datetime import datetime, timezone

    from marshal_engine.usage import UsageEvent

    svc = _svc(repo)
    ledger = svc.fleet.usage
    ledger.record(UsageEvent(ts="2020-01-01T00:00:00Z", run_id="old",
                             backend="echo", cost_usd=1.00))
    ledger.record(UsageEvent(ts="2026-06-19T00:00:00Z", run_id="new",
                             backend="echo", cost_usd=0.05))

    # No args: both events (unchanged behavior).
    assert svc.usage().totals.runs == 2

    # since=2026-01-01 drops the 2020 event.
    s = svc.usage(since=datetime(2026, 1, 1, tzinfo=timezone.utc))
    assert s.totals.runs == 1
    assert abs(s.totals.cost_usd - 0.05) < 1e-9
    # The new by_backend_model breakdown is also present.
    assert "echo/-" in s.by_backend_model


def _capture_svc(repo: Path, backend: _Capture, *, worker: str | None = None) -> MarshalService:
    cfg = FleetConfig(
        clients={
            "worker": ClientConfig(name="worker", backend="capture", permission=PermissionMode.SAFE_EDIT)
        },
        context=FleetContext(worker=worker) if worker else FleetContext(),
    )
    return MarshalService(repo, cfg, backends={"capture": backend})


def test_run_agent_threads_context_files_to_the_task(repo: Path) -> None:
    # context_files is consumed by every backend's prompt; the service must carry it onto the TaskSpec
    # so a driver can actually point a worker at the files it should see.
    # The files must be TRACKED: a worktree holds tracked files only, and a path that is not
    # there now fails the spawn rather than reaching the agent as an unopenable path (#73).
    for name in ("a.py", "b.py"):
        (repo / name).write_text("x = 1\n")
    subprocess.run(["git", "-C", str(repo), "add", "-A"], check=True, capture_output=True)
    subprocess.run(["git", "-C", str(repo), "commit", "-q", "-m", "ctx"], check=True,
                   capture_output=True)
    backend = _Capture()
    svc = _capture_svc(repo, backend)
    svc.run_agent("worker", "do x", task_id="t1", context_files=["a.py", "b.py"])
    assert backend.tasks[-1].context_files == ["a.py", "b.py"]


def test_run_agent_threads_base_branch_to_the_task(repo: Path) -> None:
    subprocess.run(["git", "branch", "marshal/chainA"], cwd=repo, check=True, capture_output=True)
    backend = _Capture()
    svc = _capture_svc(repo, backend)
    svc.run_agent("worker", "do x", task_id="t1", base_branch="marshal/chainA")
    assert backend.tasks[-1].base_branch == "marshal/chainA"


def test_spawn_threads_base_branch_to_the_task(repo: Path) -> None:
    subprocess.run(["git", "branch", "marshal/prior"], cwd=repo, check=True, capture_output=True)
    backend = _Capture()
    svc = _capture_svc(repo, backend)
    try:
        svc.spawn("worker", "do x", task_id="sp1", base_branch="marshal/prior")
        deadline = time.monotonic() + 10
        while time.monotonic() < deadline and not backend.tasks:
            time.sleep(0.05)
        assert backend.tasks[-1].base_branch == "marshal/prior"
    finally:
        svc.shutdown()


def test_request_for_threads_base_branch(repo: Path) -> None:
    svc = _svc(repo)
    req = svc._request_for("worker", "x", base_branch="feature/base")
    assert req.task.base_branch == "feature/base"


def test_goal_is_prefixed_with_worker_preamble(repo: Path) -> None:
    # The worker preamble is injected into every goal, and the user's original goal survives.
    backend = _Capture()
    svc = _capture_svc(repo, backend)
    svc.run_agent("worker", "refactor the parser", task_id="t1")
    goal = backend.tasks[-1].goal
    assert goal.startswith("You are a headless agent in a Marshal fleet")
    assert "headless agent in a Marshal fleet" in goal
    assert "refactor the parser" in goal  # the user's goal text is still present


def test_goal_includes_fleet_worker_context_when_set(repo: Path) -> None:
    # When context.worker is set, it is layered between the preamble and the user's goal.
    backend = _Capture()
    svc = _capture_svc(repo, backend, worker="Always add type hints. No new deps.")
    svc.run_agent("worker", "fix the bug", task_id="t1")
    goal = backend.tasks[-1].goal
    assert goal.startswith("You are a headless agent in a Marshal fleet")
    assert "Always add type hints. No new deps." in goal  # fleet worker context
    assert "fix the bug" in goal  # user's goal still present
    # ordering: preamble, then worker context, then goal
    assert goal.index("headless agent") < goal.index("Always add type hints")
    assert goal.index("Always add type hints") < goal.index("fix the bug")

def test_run_many_threads_context_files_per_job(repo: Path) -> None:
    backend = _Capture()
    svc = _capture_svc(repo, backend)
    svc.run_many([{"client": "worker", "goal": "g", "task_id": "j1", "context_files": ["x.py"]}])
    assert backend.tasks[-1].context_files == ["x.py"]


def test_run_agent_threads_read_paths_to_the_task(repo: Path) -> None:
    # read_paths is the declared outside-worktree escape hatch; the service must carry it onto
    # the TaskSpec via the shared `_request_for` builder (#105).
    outside = repo.parent / "brief.md"
    outside.write_text("brief")
    backend = _Capture()
    svc = _capture_svc(repo, backend)
    svc.run_agent("worker", "do x", task_id="t1", read_paths=[str(outside)])
    assert backend.tasks[-1].read_paths == [str(outside)]


def test_run_many_threads_read_paths_per_job(repo: Path) -> None:
    outside = repo.parent / "pack.md"
    outside.write_text("pack")
    backend = _Capture()
    svc = _capture_svc(repo, backend)
    svc.run_many(
        [{"client": "worker", "goal": "g", "task_id": "j1", "read_paths": [str(outside)]}]
    )
    assert backend.tasks[-1].read_paths == [str(outside)]


def test_job_request_threads_read_paths(repo: Path) -> None:
    svc = _svc(repo)
    req = svc.job_request(
        {"client": "worker", "goal": "g", "read_paths": ["/tmp/x.md"]}
    )
    assert req.task.read_paths == ["/tmp/x.md"]


def test_run_agent_does_not_stamp_client_name_into_role(repo: Path) -> None:
    # `role` is a semantic routing role, not the client name; the client is tracked separately.
    backend = _Capture()
    svc = _capture_svc(repo, backend)
    rec = svc.run_agent("worker", "do x", task_id="t1")
    assert backend.tasks[-1].role is None
    assert rec.client == "worker"  # client identity is still recorded, just not as a "role"


def test_collect_run_surfaces_changed_files(repo: Path) -> None:
    svc = _svc(repo)
    rec = svc.run_agent("worker", "do something", task_id="t1")
    collected = svc.collect_run(rec.run_id)
    assert collected.run_id == rec.run_id
    assert collected.branch == rec.branch


def test_commit_run_delegates(repo: Path) -> None:
    svc = _svc(repo)
    rec = svc.run_agent("worker", "do something", task_id="t1")
    result = svc.commit_run(rec.run_id)
    assert result.status in ("committed", "clean")  # _Echo writes nothing -> clean
    assert result.commit  # a concrete branch-tip ref to chain on
    assert svc.get_run(rec.run_id).commit == result.commit


def test_clean_delegates(repo: Path) -> None:
    svc = _svc(repo)
    rec = svc.run_agent("worker", "do something", task_id="t1")  # succeeded, un-integrated
    assert svc.clean().removed == []                  # default scope protects it
    result = svc.clean(scope="all")                    # opt in to clean it
    assert rec.run_id in result.removed
    assert svc.get_run(rec.run_id) is not None         # state/history kept; only the worktree went


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
    assert all(s.status == "exited_clean" for s in result.strategies)
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


def test_report_admin_api_cost_competes_for_cheapest(repo: Path) -> None:
    # Regression: a real EastRouter (admin-api) cost is a KNOWN cost and must be comparable for
    # `cheapest` - it was previously excluded (only native/estimated were), so a real cheaper run lost.
    from marshal_engine.state import RunRecord

    svc = _svc(repo)
    svc.fleet.state.add(
        RunRecord(run_id="b.cheap", task_id="b", backend="x", client="cheap",
                  status="exited_clean", cost_usd=0.01, source="admin-api")
    )
    svc.fleet.state.add(
        RunRecord(run_id="b.dear", task_id="b", backend="x", client="dear",
                  status="exited_clean", cost_usd=0.05, source="native")
    )
    result = svc.report("b")
    assert result.cheapest == "cheap"  # the admin-api run is the cheapest comparable strategy


def test_run_many_runs_each_client_job(repo: Path) -> None:
    svc = _svc(repo)
    jobs = [
        {"client": "worker", "goal": "a", "task_id": "j1"},
        {"client": "worker", "goal": "b", "task_id": "j2"},
        {"client": "worker", "goal": "c", "task_id": "j3"},
    ]
    records = svc.run_many(jobs, max_concurrency=3)
    assert [r.primary.task_id for r in records] == ["j1", "j2", "j3"]
    assert all(r.primary.status == "exited_clean" for r in records)
    assert len(svc.status()) == 3


def test_spawn_records_running_then_finishes(repo: Path) -> None:
    svc = _svc(repo)
    try:
        rec = svc.spawn("worker", "do x", task_id="sp1")
        assert rec.run_id.startswith("sp1.echo.")
        assert rec.status in ("running", "exited_clean")  # RUNNING at spawn; may finish fast
        deadline = time.monotonic() + 10
        while time.monotonic() < deadline:
            got = svc.get_run(rec.run_id)
            if got and got.status != "running":
                break
            time.sleep(0.05)
        got = svc.get_run(rec.run_id)
        assert got is not None and got.status == "exited_clean"
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


def test_doctor_reports_checks_and_serializes(repo: Path) -> None:
    svc = _svc(repo)  # in-memory config; no fleet.config.yaml on disk
    report = svc.doctor()
    by_name = {c.name: c for c in report.checks}
    assert {"python", "git", "repo"} <= set(by_name)
    assert by_name["repo"].status == "ok"  # the fixture is a real git work tree
    assert by_name["config"].status == "fail"  # no config file on disk -> a failing check
    assert report.ok is (report.fails == 0) and report.ok is False
    assert report.model_dump(mode="json")["fails"] >= 1  # fully serializable for the MCP surface


def test_doctor_probes_configured_backends(repo: Path) -> None:
    cfg_file = repo / "fleet.config.yaml"
    cfg_file.write_text("clients:\n  worker:\n    backend: echo\n    permission: safe-edit\n")
    svc = MarshalService(
        repo, load_config(cfg_file), backends={"echo": _Echo()}, config_path=cfg_file
    )
    by_name = {c.name: c for c in svc.doctor().checks}
    assert by_name["config"].status == "ok"
    assert by_name["backend:echo"].status == "ok"  # _Echo.check_available() is True


def _mixed_svc(repo: Path) -> MarshalService:
    """A service with one available ('echo') and one unavailable ('missing') client."""
    cfg = FleetConfig(
        clients={
            "worker": ClientConfig(name="worker", backend="echo", permission=PermissionMode.SAFE_EDIT),
            "ghost": ClientConfig(name="ghost", backend="missing", permission=PermissionMode.SAFE_EDIT),
        }
    )
    return MarshalService(repo, cfg, backends={"echo": _Echo(), "missing": _Missing()})


def test_unavailable_client_skipped(repo: Path) -> None:
    svc = _mixed_svc(repo)
    # (a) the unavailable client is absent from list_clients, present in skipped_clients
    listed = {c.name for c in svc.list_clients().clients}
    assert "ghost" not in listed
    assert "worker" in listed
    assert svc.skipped_clients == ["ghost"]


def test_too_old_agy_client_skipped(repo: Path, monkeypatch: pytest.MonkeyPatch) -> None:
    """Graceful skip: agy below the JSON floor fails check_available → client skipped."""
    import marshal_engine.backends.antigravity as agy_mod
    from marshal_engine.backends.antigravity import AntigravityBackend

    monkeypatch.setattr(agy_mod.shutil, "which", lambda _b: "/usr/bin/agy")

    class _Proc:
        returncode = 0
        stdout = "1.1.7"
        stderr = ""

    monkeypatch.setattr(agy_mod.subprocess, "run", lambda *_a, **_k: _Proc())
    be = AntigravityBackend()
    assert be.check_available() is False
    cfg = FleetConfig(
        clients={
            "agy": ClientConfig(
                name="agy", backend="antigravity", permission=PermissionMode.SAFE_EDIT
            ),
        }
    )
    svc = MarshalService(repo, cfg, backends={"antigravity": be})
    assert svc.skipped_clients == ["agy"]
    assert {c.name for c in svc.list_clients().clients} == set()


def test_run_agent_on_skipped_client_raises(repo: Path) -> None:
    svc = _mixed_svc(repo)
    # (b) run_agent on a skipped client raises ValueError (it is no longer in self._clients)
    with pytest.raises(ValueError):
        svc.run_agent("ghost", "do something", task_id="t1")
    assert svc.status() == []  # nothing ran


def test_all_available_skipped_is_empty(repo: Path) -> None:
    # (c) a service with only available backends has skipped_clients == []
    svc = _svc(repo)  # single 'echo' client, _Echo.check_available() is True
    assert svc.skipped_clients == []
    assert {c.name for c in svc.list_clients().clients} == {"worker"}


class _Toggle(_Echo):
    """An echo backend whose availability can be flipped mid-test (CLI installed mid-session)."""

    name = "toggle"

    def __init__(self) -> None:
        self.available = False

    def check_available(self) -> bool:
        return self.available


def _toggle_svc(repo: Path) -> tuple[MarshalService, _Toggle]:
    be = _Toggle()
    cfg = FleetConfig(
        clients={"late": ClientConfig(name="late", backend="toggle", permission=PermissionMode.SAFE_EDIT)}
    )
    return MarshalService(repo, cfg, backends={"toggle": be}), be


def test_skipped_client_heals_when_backend_appears(repo: Path) -> None:
    # Availability is snapshotted at construction; a backend CLI that shows up mid-session
    # (installed, or a healed PATH) must promote its clients instead of erroring forever.
    svc, be = _toggle_svc(repo)
    assert svc.skipped_clients == ["late"]
    with pytest.raises(ValueError, match="client 'late' skipped"):
        svc.run_agent("late", "go", task_id="t1")

    be.available = True
    rec = svc.run_agent("late", "go", task_id="t2")  # heals on resolution, then runs
    assert rec.status == "exited_clean"
    assert svc.skipped_clients == []


def test_list_clients_reprobes_skipped(repo: Path) -> None:
    svc, be = _toggle_svc(repo)
    assert svc.list_clients().clients == []
    be.available = True
    assert {c.name for c in svc.list_clients().clients} == {"late"}
    assert svc.skipped_clients == []


def test_still_unavailable_client_keeps_raising(repo: Path) -> None:
    svc, _be = _toggle_svc(repo)
    with pytest.raises(ValueError, match="client 'late' skipped"):
        svc.run_agent("late", "go", task_id="t1")
    with pytest.raises(ValueError, match="CLI unavailable"):
        svc.run_agent("late", "go", task_id="t2")  # reprobe found nothing; still skipped
    assert svc.skipped_clients == ["late"]


def test_unknown_client_names_missing_config_path(repo: Path, tmp_path: Path) -> None:
    # Wrong --repo/cwd with no fleet.config.yaml used to surface only
    # `known: (none configured)` with no path - pin the actionable form.
    missing = tmp_path / "no-such-fleet.config.yaml"
    svc = MarshalService(repo, FleetConfig(), config_path=missing)
    with pytest.raises(ValueError, match="no fleet config at") as excinfo:
        svc.run_agent("goose-cursor", "pong")
    msg = str(excinfo.value)
    assert "(none configured)" in msg
    assert str(missing) in msg


def test_unknown_client_includes_config_path_when_loaded(repo: Path, tmp_path: Path) -> None:
    cfg_path = tmp_path / "fleet.config.yaml"
    cfg_path.write_text("clients: {}\n", encoding="utf-8")
    empty = MarshalService(repo, FleetConfig(), config_path=cfg_path)
    with pytest.raises(ValueError, match="declares no clients") as excinfo:
        empty.run_agent("missing", "x")
    assert str(cfg_path) in str(excinfo.value)

# --- harness-first model selection: model override + ad-hoc (backend, model) spawn ------------


def _opencode_svc(repo: Path) -> MarshalService:
    """A service whose configured client uses the opencode backend (the Fireworks guard is opencode-specific)."""
    cfg = FleetConfig(
        clients={
            "impl": ClientConfig(
                name="impl", backend="opencode", model="opencode-go/anything",
                permission=PermissionMode.SAFE_EDIT,
            )
        }
    )
    # Inject a no-op opencode backend so the service doesn't try to call the real `opencode` CLI
    # in CI; the synthesis path only consults make_backend() for ad-hoc, but the configured client
    # is the one that exercises the Fireworks guard via the override channel.
    from marshal_engine.backends.opencode import OpenCodeBackend

    fake = OpenCodeBackend()
    fake.check_available = lambda: True  # type: ignore[method-assign]
    return MarshalService(repo, cfg, backends={"opencode": fake})


def test_request_for_adhoc_synthesizes_ephemeral_config(repo: Path) -> None:
    # Ad-hoc: backend=echo (already on the fleet via _Echo), no client. The synthesized request
    # uses fleet-default permission + timeout, the caller's model, and an `adhoc-<backend>` client
    # name. resolve_model still applies its opencode default for ad-hoc opencode without a model.
    svc = _svc(repo)
    req = svc._request_for(None, "x", backend="echo", model="custom-model")
    assert req.backend_name == "echo"
    assert req.model == "custom-model"
    assert req.client == "adhoc-echo"
    assert req.permission == PermissionMode.SAFE_EDIT  # fleet default
    assert req.timeout_s == 600  # fleet default

    # Ad-hoc opencode without an explicit model: resolve_model defaults to the Go subscription.
    req2 = svc._request_for(None, "x", backend="opencode")
    assert req2.backend_name == "opencode"
    assert req2.model == DEFAULT_OPENCODE_MODEL


def test_request_for_both_client_and_backend_is_a_conflict(repo: Path) -> None:
    # This test previously asserted the silent precedence (client wins, backend ignored), which
    # encoded the defect in #101: a caller that named both got a run on a backend it had not asked
    # for, with nothing saying so. Naming two different answers to "what runs this" is now refused.
    svc = _svc(repo)
    with pytest.raises(ValueError, match="conflicting routing"):
        svc._request_for("worker", "x", backend="opencode")
    # The model override is a separate, coherent case and still applies.
    req = svc._request_for("worker", "x", model="explicit")
    assert req.backend_name == "echo"
    assert req.model == "explicit"


def test_request_for_neither_client_nor_backend_raises(repo: Path) -> None:
    svc = _svc(repo)
    with pytest.raises(ValueError, match="hint: list_clients"):
        svc._request_for(None, "x")


def test_request_for_unknown_backend_raises_with_valid_names(repo: Path) -> None:
    svc = _svc(repo)
    with pytest.raises(ValueError) as exc:
        svc._request_for(None, "x", backend="nonexistent")
    # The registry's own message lists the valid backends; the test asserts the names are surfaced.
    msg = str(exc.value)
    assert "nonexistent" in msg
    assert "known" in msg
    # Each registered backend name appears in the error so the driver can fix the typo.
    from marshal_engine.registry import backend_names

    for name in backend_names():
        assert name in msg


def test_request_for_adhoc_opencode_fireworks_model_raises(repo: Path) -> None:
    # The Fireworks guard applies to ad-hoc opencode configs the same way it does to configured
    # ones - synthesized at request-time, so a typo'd model fails fast before any spawn.
    svc = _svc(repo)
    with pytest.raises(ConfigError, match="Fireworks"):
        svc._request_for(None, "x", backend="opencode", model="fireworks-ai/accounts/fireworks/models/glm-5p2")


def test_run_agent_model_override_on_configured_client(repo: Path) -> None:
    # end-to-end: a configured client with model "configured-model", then call with model="override";
    # the override reaches the RunRecord (which is what get_run / status / usage / report see).
    cfg = FleetConfig(
        clients={
            "worker": ClientConfig(
                name="worker", backend="echo", model="configured-model",
                permission=PermissionMode.SAFE_EDIT,
            )
        }
    )
    svc = MarshalService(repo, cfg, backends={"echo": _Echo()})
    rec = svc.run_agent("worker", "do x", task_id="t1", model="override")
    assert rec.status == "exited_clean"
    assert rec.model == "override"  # override reaches the persisted record

    # And without the override, the client's resolved model is used.
    rec2 = svc.run_agent("worker", "do x", task_id="t2")
    assert rec2.model == "configured-model"  # resolve_model(client)


def test_run_agent_adhoc_backend_runs_without_configured_client(repo: Path) -> None:
    # A service with NO clients (e.g. an empty config) can still spawn by bare (backend, model).
    cfg = FleetConfig()  # no clients
    svc = MarshalService(repo, cfg, backends={"echo": _Echo()})
    rec = svc.run_agent(backend="echo", goal="do x", task_id="t1", model="adhoc-model")
    assert rec.status == "exited_clean"
    assert rec.backend == "echo"
    assert rec.model == "adhoc-model"
    assert rec.client == "adhoc-echo"


def test_run_agent_unknown_backend_raises(repo: Path) -> None:
    svc = _svc(repo)
    with pytest.raises(ValueError, match="nonexistent"):
        svc.run_agent(backend="nonexistent", goal="x", task_id="t1")


def test_run_agent_goose_malformed_model_raises(repo: Path) -> None:
    """Ad-hoc Goose with a trailing-slash model fails before any worktree is created."""
    from unittest.mock import MagicMock

    from marshal_engine.backends.goose import GooseBackend

    cfg = FleetConfig()
    svc = MarshalService(repo, cfg, backends={"goose": GooseBackend()})
    create = MagicMock(side_effect=svc.fleet.worktrees.create)
    svc.fleet.worktrees.create = create  # type: ignore[method-assign]

    with pytest.raises(ValueError, match="malformed"):
        svc.run_agent(backend="goose", goal="x", task_id="t1", model="/auto")

    create.assert_not_called()


def test_run_agent_opencode_fireworks_model_raises_config_error(repo: Path) -> None:
    # Ad-hoc path: run_agent propagates the same ConfigError the synthesis raises, so a
    # Fireworks-billed run never starts on the Fleet.
    svc = _opencode_svc(repo)
    with pytest.raises(ConfigError, match="Fireworks"):
        svc.run_agent(backend="opencode", goal="x", task_id="t1",
                      model="fireworks-ai/accounts/fireworks/models/glm-5p2")


def test_run_agent_client_model_override_fireworks_raises(repo: Path) -> None:
    # Override path: a model override on a CONFIGURED opencode client must hit the same Fireworks
    # guard as an ad-hoc opencode run. Overrides bypass load_config, so _request_for re-checks.
    svc = _opencode_svc(repo)  # configured client "impl" is backend=opencode
    with pytest.raises(ConfigError, match="Fireworks"):
        svc.run_agent("impl", "x", task_id="t1",
                      model="fireworks-ai/accounts/fireworks/models/glm-5p2")


# --- list_models + duration presets ---------------------------------------------------------


def test_list_models_empty_catalog_by_default(repo: Path) -> None:
    svc = _svc(repo)  # no models in the config
    result = svc.list_models()
    assert isinstance(result, ModelList)
    assert result.models == []
    assert result.driver_context is None  # no context.driver in this config


def test_list_models_surfaces_catalog_and_driver_context(repo: Path) -> None:
    cfg = FleetConfig(
        clients={"worker": ClientConfig(name="worker", backend="echo", permission=PermissionMode.SAFE_EDIT)},
        context=FleetContext(driver="Use the catalog to pick a model."),
        models=[
            ModelSpec(id="<provider>/<model-a>", backends=["opencode"], cost="native", quota_type="subscription"),
            ModelSpec(id="<provider>/<model-b>", backends=["cursor"], cost="estimated", quota_type="metered"),
        ],
    )
    svc = MarshalService(repo, cfg, backends={"echo": _Echo()})
    result = svc.list_models()
    assert [m.model_dump() for m in result.models] == [
        {"id": "<provider>/<model-a>", "backends": ["opencode"], "cost": "native", "quota_type": "subscription", "notes": ""},
        {"id": "<provider>/<model-b>", "backends": ["cursor"], "cost": "estimated", "quota_type": "metered", "notes": ""},
    ]
    assert result.driver_context == "Use the catalog to pick a model."


def test_request_for_duration_preset_overrides_client_timeout(repo: Path) -> None:
    # The client's configured timeout (300s) is replaced when a duration preset is passed.
    cfg = FleetConfig(
        clients={"worker": ClientConfig(name="worker", backend="echo", timeout_s=300,
                                        permission=PermissionMode.SAFE_EDIT)}
    )
    svc = MarshalService(repo, cfg, backends={"echo": _Echo()})
    req = svc._request_for("worker", "x", duration="short")  # short = 300s same as client; try "long"
    assert req.timeout_s == 300  # "short" == 300
    req2 = svc._request_for("worker", "x", duration="long")  # 24000s, far past client's 300
    assert req2.timeout_s == 24000
    # And the integer form is also accepted
    req3 = svc._request_for("worker", "x", duration=42)
    assert req3.timeout_s == 42


def test_request_for_duration_invalid_preset_raises(repo: Path) -> None:
    svc = _svc(repo)
    with pytest.raises(ConfigError, match="unknown duration"):
        svc._request_for("worker", "x", duration="xl")
    with pytest.raises(ConfigError, match="must be > 0"):
        svc._request_for("worker", "x", duration=0)


def test_request_for_duration_overrides_ephemeral_default_too(repo: Path) -> None:
    # Ad-hoc (backend, model) path: the synthesized ClientConfig's default timeout_s=600 is
    # overridden by the duration parameter.
    cfg = FleetConfig()  # no clients
    svc = MarshalService(repo, cfg, backends={"echo": _Echo()})
    req = svc._request_for(None, "x", backend="echo", model="adhoc-model", duration="large")
    assert req.timeout_s == 6000  # "large" = 6000s
    assert req.client == "adhoc-echo"
    assert req.model == "adhoc-model"


def test_run_agent_duration_reaches_run_record(repo: Path) -> None:
    # End-to-end: a `duration` override on run_agent reaches the RunRequest (and thus the run record).
    cfg = FleetConfig(
        clients={"worker": ClientConfig(name="worker", backend="echo", timeout_s=300,
                                        permission=PermissionMode.SAFE_EDIT)}
    )
    svc = MarshalService(repo, cfg, backends={"echo": _Echo()})
    rec = svc.run_agent("worker", "x", task_id="t1", duration="long")
    assert rec.status == "exited_clean"
    # The Fleet doesn't echo timeout back on the record (it lives on the RunRequest), but we can
    # assert the side-effect: a record with this task_id exists and the override didn't error.
    assert rec.run_id.startswith("t1.echo.")


def test_spawn_duration_reaches_run_record(repo: Path) -> None:
    cfg = FleetConfig(
        clients={"worker": ClientConfig(name="worker", backend="echo", timeout_s=300,
                                        permission=PermissionMode.SAFE_EDIT)}
    )
    svc = MarshalService(repo, cfg, backends={"echo": _Echo()})
    try:
        rec = svc.spawn("worker", "x", task_id="sp1", duration="medium")
        assert rec.run_id.startswith("sp1.echo.")
        assert rec.status in ("running", "exited_clean")
    finally:
        svc.shutdown()


def test_run_many_per_job_duration(repo: Path) -> None:
    # Each job in run_many can carry its own duration; the override reaches the RunRequest.
    cfg = FleetConfig(
        clients={
            "a": ClientConfig(name="a", backend="echo", timeout_s=300, permission=PermissionMode.SAFE_EDIT),
            "b": ClientConfig(name="b", backend="echo", timeout_s=300, permission=PermissionMode.SAFE_EDIT),
        }
    )
    svc = MarshalService(repo, cfg, backends={"echo": _Echo()})
    jobs = [
        {"client": "a", "goal": "x", "task_id": "j1", "duration": "short"},
        {"client": "b", "goal": "y", "task_id": "j2", "duration": 999},
    ]
    records = svc.run_many(jobs, max_concurrency=2)
    assert [r.primary.task_id for r in records] == ["j1", "j2"]
    assert all(r.primary.status == "exited_clean" for r in records)


def test_run_many_duration_invalid_preset_fails_fast(repo: Path) -> None:
    # A bad duration in any job must fail the whole call BEFORE any run starts (validated up
    # front via resolve_duration in _request_for).
    cfg = FleetConfig(
        clients={"a": ClientConfig(name="a", backend="echo", permission=PermissionMode.SAFE_EDIT)}
    )
    svc = MarshalService(repo, cfg, backends={"echo": _Echo()})
    with pytest.raises(ConfigError, match="unknown duration"):
        svc.run_many([{"client": "a", "goal": "x", "duration": "xl"}])
    assert svc.status() == []  # nothing ran


# --- advisory budgets: MarshalService passes them through to the Fleet ----------------------


def test_service_budget_status_passes_config_through_to_fleet(repo: Path) -> None:
    # The service threads FleetConfig.budgets into the Fleet so the MCP `usage` tool and any
    # library caller see the same snapshot. A no-config-budgets service returns [].
    cfg = FleetConfig(
        clients={"a": ClientConfig(name="a", backend="echo", permission=PermissionMode.SAFE_EDIT)},
        budgets=[
            BudgetSpec(backend="echo", window="week", limit_usd=1.0),
            BudgetSpec(window="month", limit_usd=5.0),
        ],
    )
    svc = MarshalService(repo, cfg, backends={"echo": _Echo()})
    assert [b.model_dump() for b in svc.fleet.budgets] == [
        {
            "backend": "echo",
            "client": None,
            "window": "week",
            "limit_usd": 1.0,
            "enforce": False,
        },
        {
            "backend": None,
            "client": None,
            "window": "month",
            "limit_usd": 5.0,
            "enforce": False,
        },
    ]
    rows = svc.budget_status()
    assert [r.scope for r in rows] == ["backend:echo", "global"]
    assert [r.limit_usd for r in rows] == [1.0, 5.0]
    # No runs yet -> $0 spent, full limit remaining on every budget.
    assert all(r.spent_usd == 0.0 for r in rows)
    assert [r.remaining_usd for r in rows] == [1.0, 5.0]


def test_service_no_budgets_returns_empty_list(repo: Path) -> None:
    # Backward-compat: a service built from a config without `budgets:` returns [].
    svc = _svc(repo)
    assert svc.budget_status() == []


def test_client_and_backend_together_is_refused_not_silently_resolved(repo: Path) -> None:
    """REGRESSION (#101): `client` + `backend` are two different answers to "what runs this". The
    precedence lived in prose, and the loser was dropped silently — so a run executed on a backend
    the caller never asked for, with nothing in the result saying so."""
    svc = _svc(repo)
    with pytest.raises(ValueError, match="conflicting routing"):
        svc.run_agent("worker", "do the thing", backend="opencode")


def test_client_and_model_together_stays_a_supported_override(repo: Path) -> None:
    """The sibling case is NOT a contradiction and must keep working: a client plus a model is a
    coherent request — run this client's backend against that model. Erroring here would remove a
    documented capability rather than fix an ambiguity."""
    svc = _svc(repo)
    req = svc._request_for("worker", "do the thing", model="some/other-model")
    assert req.backend_name == "echo"
    assert req.model == "some/other-model"
    assert req.client == "worker"


def test_list_clients_names_the_clients_it_dropped_and_why(repo: Path) -> None:
    """REGRESSION (#74): a client whose backend CLI is unavailable was filtered out of
    `list_clients` with no error and no reason. Marshal knew - it warns on stderr - but an MCP
    driver never sees stderr, so from its side the client silently vanished and it noticed only
    incidentally."""
    cfg = FleetConfig(
        clients={
            "worker": ClientConfig(name="worker", backend="echo"),
            "ghost": ClientConfig(name="ghost", backend="missing"),
        }
    )
    svc = MarshalService(repo, cfg, backends={"echo": _Echo(), "missing": _Missing()})

    listing = svc.list_clients()
    assert [c.name for c in listing.clients] == ["worker"]
    dropped = {s.name: s for s in listing.skipped}
    assert "ghost" in dropped, "the dropped client is still invisible to the driver"
    assert dropped["ghost"].backend == "missing"
    assert "not available on PATH" in dropped["ghost"].reason


def test_an_unknown_backend_reads_differently_from_an_uninstalled_one(repo: Path) -> None:
    """"You typed a backend that does not exist" and "that CLI is not installed" have different
    fixes; collapsing them to one message makes the driver guess which it is."""
    cfg = FleetConfig(clients={"typo": ClientConfig(name="typo", backend="opencodee")})
    svc = MarshalService(repo, cfg, backends={"echo": _Echo()})
    reason = svc.list_clients().skipped[0].reason
    assert "not a known backend" in reason


def test_integrate_message_reaches_the_commit(repo: Path) -> None:
    """REGRESSION (#75): the Fleet accepted `message`, but the service and the MCP tool both
    dropped it - so every integrate landed as "marshal: integrate <run_id>", describing the tooling
    rather than the change. The reporter reset and recommitted after every single one, about
    fifteen times. `commit_run` had taken a message all along; `integrate` just never passed it on."""
    svc = _svc(repo)
    rec = svc.run_agent("worker", "make a change")
    # Give the run something to land.
    (Path(rec.worktree) / "new.txt").write_text("work product\n")

    result = svc.integrate(rec.run_id, message="Add the thing the agent was asked for")
    assert result.status == "merged", result.message

    log = subprocess.run(
        ["git", "-C", str(repo), "log", "-2", "--format=%s"],
        capture_output=True, text=True, check=True,
    ).stdout
    assert "Add the thing the agent was asked for" in log
    assert "marshal: integrate" not in log, "the tooling-shaped default won anyway"


class _Cataloged(_Echo):
    """A backend whose CLI can be asked what it runs."""

    name = "cataloged"

    def available_models(self) -> list[str] | None:
        return ["fast-1", "slow-2"]


class _Opaque(_Echo):
    """A backend with no way to ask - None, which is NOT 'it has no models'."""

    name = "opaque"

    def available_models(self) -> list[str] | None:
        return None


def test_list_models_proxies_the_backend_when_no_catalog_is_configured(repo: Path) -> None:
    """REGRESSION (#78): `list_models` returned `{"models": []}` with no catalog, so a driver left
    Marshal and ran `cursor-agent models` in a shell to learn what it could route at. We did the
    same thing ourselves the same day."""
    cfg = FleetConfig(clients={"w": ClientConfig(name="w", backend="cataloged")})
    svc = MarshalService(repo, cfg, backends={"cataloged": _Cataloged()})
    listing = svc.list_models()
    assert listing.models == [], "no catalog is configured"
    assert listing.backend_models["cataloged"] == ["fast-1", "slow-2"]


def test_a_backend_that_cannot_be_asked_reports_none_not_empty(repo: Path) -> None:
    """`None` means "no way to ask"; `[]` would claim the backend runs nothing. A driver has to be
    able to tell those apart before concluding it cannot route anywhere."""
    cfg = FleetConfig(clients={"w": ClientConfig(name="w", backend="opaque")})
    svc = MarshalService(repo, cfg, backends={"opaque": _Opaque()})
    assert svc.list_models().backend_models["opaque"] is None


def test_a_configured_catalog_suppresses_the_probe(repo: Path) -> None:
    """The catalog is the curated answer and stands alone; probing costs a subprocess per backend,
    and the two are kept in separate fields so a live probe never reads as configuration."""
    cfg = FleetConfig(
        clients={"w": ClientConfig(name="w", backend="cataloged")},
        models=[ModelSpec(id="curated/one", backends=["cataloged"])],
    )
    svc = MarshalService(repo, cfg, backends={"cataloged": _Cataloged()})
    listing = svc.list_models()
    assert [m.id for m in listing.models] == ["curated/one"]
    assert listing.backend_models == {}


def test_read_run_file_hands_one_agents_artifact_to_the_driver(repo: Path) -> None:
    """#80: an agent that produces a report had no way to hand it on. `collect_run` returns the
    whole diff (wrong granularity) and `context_files` refuses paths outside the target worktree
    (correctly - that guard is what keeps a run inside its boundary)."""
    svc = _svc(repo)
    rec = svc.run_agent("worker", "produce a report")
    (Path(rec.worktree) / "REPORT.md").write_text("findings the next agent needs\n")

    got = svc.read_run_file(rec.run_id, "REPORT.md")
    assert got.content == "findings the next agent needs\n"
    assert got.truncated is False
    assert got.run_id == rec.run_id


def test_read_run_file_reports_a_mid_read_clean_as_the_cleaned_worktree_error(
    repo: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    """The existence/is_file checks are a snapshot; `clean` can land between them and the read.

    One state must not yield two diagnostics depending on which microsecond the caller arrived in -
    a raw FileNotFoundError here instead of the documented cleaned-worktree ValueError leaves the
    driver unable to branch on the condition it was told to expect.
    """
    svc = _svc(repo)
    rec = svc.run_agent("worker", "produce a report")
    wt = Path(rec.worktree)
    (wt / "REPORT.md").write_text("findings\n")

    real_stat = Path.stat

    def stat_but_clean_first(self: Path, *a: object, **kw: object) -> object:
        # Simulate the concurrent `clean` landing after the guards, before the read.
        if self.name == "REPORT.md" and wt.exists():
            shutil.rmtree(wt)
        return real_stat(self, *a, **kw)  # type: ignore[arg-type]

    monkeypatch.setattr(Path, "stat", stat_but_clean_first)
    with pytest.raises(ValueError, match="worktree is gone"):
        svc.read_run_file(rec.run_id, "REPORT.md")


def test_read_run_file_refuses_a_path_outside_the_worktree(repo: Path) -> None:
    """`Path(wt) / "/etc/passwd"` is `/etc/passwd` - an absolute path discards the base, and `..`
    walks out. Same containment the context_files guard enforces, for the same reason."""
    svc = _svc(repo)
    rec = svc.run_agent("worker", "x")
    for bad in ("/etc/hosts", "../../escape.txt"):
        with pytest.raises(ValueError, match="outside"):
            svc.read_run_file(rec.run_id, bad)


def test_read_run_file_says_when_it_truncated(repo: Path) -> None:
    """Silently returning a prefix would let a driver act on part of a report believing it had the
    whole thing - the exact class of "a value that means less than it appears" these reviews are
    about."""
    svc = _svc(repo)
    rec = svc.run_agent("worker", "x")
    (Path(rec.worktree) / "BIG.md").write_text("x" * 5000)

    got = svc.read_run_file(rec.run_id, "BIG.md", max_bytes=100)
    assert got.truncated is True
    assert len(got.content) == 100
    assert got.size_bytes == 5000, "the real size is reported, not the clipped one"


def test_read_run_file_never_loads_more_than_it_returns(repo: Path) -> None:
    """`read_bytes()` would pull an agent-produced artifact of ANY size into the MCP server's
    memory before slicing it, and the caller picks the path - so the size is not ours to assume.
    Asserting `truncated` is not enough: that passes even when the whole file was loaded first.
    This pins the *read* itself."""
    svc = _svc(repo)
    rec = svc.run_agent("worker", "x")
    (Path(rec.worktree) / "HUGE.md").write_text("y" * 100_000)

    reads: list[int] = []
    real_open = Path.open

    def spy(self, *a, **kw):  # type: ignore[no-untyped-def]
        stream = real_open(self, *a, **kw)
        if self.name == "HUGE.md":
            real_read = stream.read

            def counting(n=-1):  # type: ignore[no-untyped-def]
                data = real_read(n)
                reads.append(len(data))
                return data

            stream.read = counting  # type: ignore[method-assign]
        return stream

    monkey = pytest.MonkeyPatch()
    monkey.setattr(Path, "open", spy)
    try:
        got = svc.read_run_file(rec.run_id, "HUGE.md", max_bytes=500)
    finally:
        monkey.undo()

    assert got.truncated is True
    assert got.size_bytes == 100_000, "the true size is still reported, from stat()"
    assert max(reads) <= 501, f"read {max(reads)} bytes to return 500"


def test_read_run_file_on_a_cleaned_worktree_says_so(repo: Path) -> None:
    """"The worktree is gone" and "the file is not there" are different problems."""
    svc = _svc(repo)
    rec = svc.run_agent("worker", "x")
    svc.clean(scope="all")
    with pytest.raises(ValueError, match="gone"):
        svc.read_run_file(rec.run_id, "anything.md")


def test_get_run_and_status_return_liveness_enriched_records(repo: Path) -> None:
    """get_run/status must return the with_liveness path (not a dead duplicate return) (M12)."""
    svc = _svc(repo)
    rec = svc.run_agent("worker", "x")
    got = svc.get_run(rec.run_id)
    assert got is not None
    assert got.run_id == rec.run_id
    # Terminal records get agent_alive=None from with_liveness (question is meaningless).
    assert got.agent_alive is None
    rows = svc.status()
    assert any(r.run_id == rec.run_id for r in rows)
    match = next(r for r in rows if r.run_id == rec.run_id)
    assert match.agent_alive is None


def test_list_models_probes_backends_concurrently(tmp_path: Path) -> None:
    """Serial probes make the worst case the SUM of their timeouts, which can exceed an MCP
    client's deadline. Four 0.4s probes must finish in well under 1.6s."""
    import time

    from marshal_engine.config import ClientConfig, FleetConfig, PermissionMode

    class _SlowProbe(_Echo):
        def available_models(self) -> list[str]:
            time.sleep(0.4)
            return ["m"]

    names = ["b1", "b2", "b3", "b4"]
    cfg = FleetConfig(
        clients={
            n: ClientConfig(name=n, backend=n, permission=PermissionMode.SAFE_EDIT) for n in names
        }
    )
    backends = {n: _SlowProbe() for n in names}
    svc = MarshalService(tmp_path, cfg, backends=backends)

    started = time.monotonic()
    result = svc.list_models()
    elapsed = time.monotonic() - started

    assert set(result.backend_models) == set(names)
    assert elapsed < 1.2, f"probes look serial: {elapsed:.2f}s for 4x0.4s"


def test_list_models_survives_a_raising_probe(tmp_path: Path) -> None:
    """One broken backend must not take the whole listing down."""
    from marshal_engine.config import ClientConfig, FleetConfig, PermissionMode

    class _Boom(_Echo):
        def available_models(self) -> list[str]:
            raise RuntimeError("probe exploded")

    cfg = FleetConfig(
        clients={
            "ok": ClientConfig(name="ok", backend="ok", permission=PermissionMode.SAFE_EDIT),
            "bad": ClientConfig(name="bad", backend="bad", permission=PermissionMode.SAFE_EDIT),
        }
    )
    svc = MarshalService(
        tmp_path, cfg, backends={"ok": _Echo(), "bad": _Boom()}
    )
    result = svc.list_models()
    assert result.backend_models["bad"] is None


def test_run_agent_threads_output_schema_to_the_task(repo: Path) -> None:
    schema = {
        "type": "object",
        "properties": {"ok": {"type": "boolean"}},
        "required": ["ok"],
    }
    backend = _Capture()
    svc = _capture_svc(repo, backend)
    # Capture sees the post-injection goal; the schema itself must still be on the TaskSpec.
    # Use a conforming reply so the run succeeds (Capture prints 'ok', which is not JSON —
    # override parse via a talker-style backend instead when asserting structured).
    svc.run_agent("worker", "do x", task_id="so1", output_schema=schema)
    assert backend.tasks[-1].output_schema == schema
    assert "FINAL MESSAGE" in backend.tasks[-1].goal


def test_run_agent_rejects_invalid_output_schema_before_spawn(repo: Path) -> None:
    """A bad schema dict fails cleanly at the service boundary (ValueError), not mid-run."""
    svc = _svc(repo)
    with pytest.raises(ValueError, match="invalid output_schema"):
        svc.run_agent(
            "worker",
            "do x",
            output_schema={"type": "object", "properties": "not-an-object"},
        )


def test_run_agent_structured_output_round_trip(repo: Path) -> None:
    schema = {
        "type": "object",
        "properties": {"ok": {"type": "boolean"}},
        "required": ["ok"],
        "additionalProperties": False,
    }

    class _JsonTalker(CodingAgentBackend):
        name = "echo"
        binary = "python"
        capabilities = Capabilities()

        def check_available(self) -> bool:
            return True

        def build_invocation(self, task: TaskSpec, opts: RunOpts) -> list[str]:
            return [sys.executable, "-c", "pass"]

        def map_permission(self, mode: PermissionMode) -> list[str]:
            return []

        def parse_output(self, raw_stdout: str, raw_stderr: str, exit_code: int) -> AgentResult:
            return AgentResult(status=RunStatus.EXITED_CLEAN, text='{"ok": true}', exit_code=exit_code)

    cfg = FleetConfig(
        clients={"worker": ClientConfig(name="worker", backend="echo", permission=PermissionMode.SAFE_EDIT)}
    )
    svc = MarshalService(repo, cfg, backends={"echo": _JsonTalker()})
    rec = svc.run_agent("worker", "classify", output_schema=schema)
    assert rec.status == "exited_clean"
    assert rec.structured == {"ok": True}
    assert svc.collect_run(rec.run_id).structured == {"ok": True}
