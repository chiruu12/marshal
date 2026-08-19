"""Tests for MarshalService - client resolution + run recording (dummy backend, no network)."""

from __future__ import annotations

import shutil
import subprocess
import sys
import time
from datetime import UTC
from pathlib import Path

import pytest

from marshal_engine import (
    AgentResult,
    Capabilities,
    ModelCatalog,
    ModelSource,
    PermissionFidelity,
    PermissionMode,
    RunOpts,
    RunStatus,
    TaskSpec,
    UsageRecord,
    UsageSource,
)
from marshal_engine.backends.base import CodingAgentBackend
from marshal_engine.core.config import (
    DEFAULT_OPENCODE_MODEL,
    BudgetSpec,
    ClientConfig,
    ConfigError,
    FleetConfig,
    FleetContext,
    load_config,
)
from marshal_engine.core.layout import runs_dir
from marshal_engine.interfaces.service import MarshalService, ModelList, ModelSpec
from marshal_engine.runtime.state import FleetState, RunRecord


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

    from marshal_engine.orchestration.fleet import _pid_start_time

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
            # No notice on an ordinary client - one on every row would carry no signal.
            "billing_notice": None,
        }
    ]
    assert result.driver_context is None  # no context.driver in this config


def _fidelity_backend(
    name: str, fidelity: PermissionFidelity
) -> type[CodingAgentBackend]:
    """Minimal available backend with a fixed Capabilities.permission_fidelity."""

    class _B(CodingAgentBackend):
        # Class attrs set below after the class body (name must match registry key).
        binary = "python"
        capabilities = Capabilities(permission_fidelity=fidelity)

        def check_available(self) -> bool:
            return True

        def build_invocation(self, task: TaskSpec, opts: RunOpts) -> list[str]:
            return [sys.executable, "-c", "print('ok')"]

        def map_permission(self, mode: PermissionMode) -> list[str]:
            return []

        def parse_output(self, raw_stdout: str, raw_stderr: str, exit_code: int) -> AgentResult:
            return AgentResult(status=RunStatus.EXITED_CLEAN, text="ok", exit_code=exit_code)

    _B.name = name
    return _B


def test_list_clients_safe_edit_inherits_enforced_denies(repo: Path) -> None:
    # safe-edit on an enforcing backend still reports enforced-denies (no regression).
    cfg = FleetConfig(
        clients={
            "worker": ClientConfig(
                name="worker", backend="enforced", permission=PermissionMode.SAFE_EDIT
            )
        }
    )
    svc = MarshalService(repo, cfg, backends={"enforced": _fidelity_backend("enforced", PermissionFidelity.ENFORCED_DENIES)()})
    clients = svc.list_clients().clients
    assert len(clients) == 1
    assert clients[0].permission == "safe-edit"
    assert clients[0].permission_fidelity == "enforced-denies"


def test_list_clients_yolo_reports_unrestricted_not_enforced_denies(repo: Path) -> None:
    # #178: yolo must not inherit the backend's safe-edit enforced-denies label.
    cfg = FleetConfig(
        clients={
            "fast": ClientConfig(
                name="fast", backend="enforced", permission=PermissionMode.YOLO
            )
        }
    )
    svc = MarshalService(repo, cfg, backends={"enforced": _fidelity_backend("enforced", PermissionFidelity.ENFORCED_DENIES)()})
    clients = svc.list_clients().clients
    assert len(clients) == 1
    assert clients[0].permission == "yolo"
    assert clients[0].permission_fidelity == "unrestricted"
    assert clients[0].permission_fidelity != "enforced-denies"


def test_list_clients_safe_edit_inherits_boundary_only(repo: Path) -> None:
    # safe-edit on a boundary-only backend still reports boundary-only (no regression).
    cfg = FleetConfig(
        clients={
            "worker": ClientConfig(
                name="worker", backend="soft", permission=PermissionMode.SAFE_EDIT
            )
        }
    )
    svc = MarshalService(repo, cfg, backends={"soft": _fidelity_backend("soft", PermissionFidelity.BOUNDARY_ONLY)()})
    clients = svc.list_clients().clients
    assert len(clients) == 1
    assert clients[0].permission == "safe-edit"
    assert clients[0].permission_fidelity == "boundary-only"


def test_list_clients_read_only_inherits_backend_fidelity(repo: Path) -> None:
    # read-only keeps the backend's restriction honesty (plan/sandbox on enforcing backends).
    enforced = FleetConfig(
        clients={
            "reviewer": ClientConfig(
                name="reviewer", backend="enforced", permission=PermissionMode.READ_ONLY
            )
        }
    )
    soft = FleetConfig(
        clients={
            "reviewer": ClientConfig(
                name="reviewer", backend="soft", permission=PermissionMode.READ_ONLY
            )
        }
    )
    e = MarshalService(
        repo, enforced, backends={"enforced": _fidelity_backend("enforced", PermissionFidelity.ENFORCED_DENIES)()}
    ).list_clients().clients[0]
    s = MarshalService(
        repo, soft, backends={"soft": _fidelity_backend("soft", PermissionFidelity.BOUNDARY_ONLY)()}
    ).list_clients().clients[0]
    assert e.permission == "read-only" and e.permission_fidelity == "enforced-denies"
    assert s.permission == "read-only" and s.permission_fidelity == "boundary-only"


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


@pytest.mark.parametrize(
    "bad_kind",
    ["", "../x", "foo/bar", ".hidden", "a" * 65, "café", "a\x00b", "multi\nline", "has space"],
)
def test_task_kind_rejects_unsafe_or_multiline_values(bad_kind: str) -> None:
    with pytest.raises(Exception, match="unsafe"):
        TaskSpec(id="ok", goal="g", task_kind=bad_kind)


def test_task_kind_accepts_safe_tokens() -> None:
    for kind in ("refactor", "bugfix", "docs", "test-writing", "review"):
        assert TaskSpec(id="ok", goal="g", task_kind=kind).task_kind == kind


def test_run_agent_rejects_unsafe_task_kind(repo: Path) -> None:
    svc = _svc(repo)
    with pytest.raises(ValueError, match="unsafe"):
        svc.run_agent("worker", "x", task_kind="bad\nkind")
    assert not any((svc.fleet.worktrees.base_dir).glob("*")) if svc.fleet.worktrees.base_dir.exists() else True


def test_run_agent_threads_task_kind_to_usage_event(repo: Path) -> None:
    svc = _svc(repo)
    rec = svc.run_agent("worker", "do the thing", task_kind="refactor")
    events = svc.fleet.usage.events()
    assert len(events) == 1
    assert events[0].task_kind == "refactor"
    assert events[0].run_id == rec.run_id
    assert events[0].goal_digest is not None
    assert "do the thing" not in svc.fleet.usage.events_path.read_text(encoding="utf-8")


def test_session_start_is_a_utc_datetime(repo: Path) -> None:
    # session_start is the long-lived MCP server's "wake" timestamp; a "since session" window maps
    # to this instant. Stable for the life of the service, UTC, and accessible on the service.
    from datetime import datetime

    svc = _svc(repo)
    assert isinstance(svc.session_start, datetime)
    assert svc.session_start.tzinfo is not None
    assert svc.session_start.tzinfo.utcoffset(svc.session_start) == UTC.utcoffset(svc.session_start)


def test_service_usage_since_filters_events(repo: Path) -> None:
    # MarshalService.usage(since=...) plumbs the bound into the UsageTracker so a windowed rollup
    # works end-to-end through the service. The `_Echo` backend always stamps `now`, so seeding the
    # ledger with an old event shows the filter in action.
    from datetime import datetime

    from marshal_engine.accounting.usage import UsageEvent

    svc = _svc(repo)
    ledger = svc.fleet.usage
    ledger.record(UsageEvent(ts="2020-01-01T00:00:00Z", run_id="old",
                             backend="echo", cost_usd=1.00))
    ledger.record(UsageEvent(ts="2026-06-19T00:00:00Z", run_id="new",
                             backend="echo", cost_usd=0.05))

    # No args: both events (unchanged behavior).
    assert svc.usage().totals.runs == 2

    # since=2026-01-01 drops the 2020 event.
    s = svc.usage(since=datetime(2026, 1, 1, tzinfo=UTC))
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
    # the TaskSpec via the shared `_request_for` builder (#105). Declared INSIDE the repo: this
    # asserts the threading, and an out-of-repo path is refused by default now (#176).
    outside = repo / "brief.md"
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
    from marshal_engine.runtime.state import RunRecord

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
    from marshal_engine.orchestration.registry import backend_names

    for name in backend_names():
        assert name in msg


def test_request_for_adhoc_opencode_accepts_a_metered_model(repo: Path) -> None:
    # An ad-hoc opencode run may name a Fireworks model: it is a provider choice, and those runs
    # are the ones that come back with real per-run USD attached.
    svc = _svc(repo)
    req = svc._request_for(
        None, "x", backend="opencode", model="fireworks-ai/accounts/fireworks/models/glm-5p2"
    )
    assert req.model == "fireworks-ai/accounts/fireworks/models/glm-5p2"


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


def test_list_clients_carries_the_billing_notice(repo: Path) -> None:
    """A driver over MCP never sees stderr, so the advisory must ride the listing.

    This is the same reason `SkippedClient` exists: a fact that only reaches stderr is invisible
    to exactly the caller deciding what to spend money on.
    """
    from marshal_engine.core.config import ClientConfig, FleetConfig

    cfg = FleetConfig(
        clients={
            "paid": ClientConfig(
                name="paid",
                backend="opencode",
                model="fireworks-ai/accounts/fireworks/models/glm-5p2",
            ),
            "sub": ClientConfig(name="sub", backend="opencode", model="opencode-go/glm-5.2"),
        }
    )
    svc = MarshalService(repo, cfg, backends={"opencode": _Echo()})
    by_name = {c.name: c for c in svc.list_clients().clients}
    assert "Fireworks credits" in (by_name["paid"].billing_notice or "")
    # The subscription client must carry no notice at all - a notice on every row is no signal.
    assert by_name["sub"].billing_notice is None


def test_a_metered_model_warns_on_the_paths_validate_never_sees(
    repo: Path, capsys: pytest.CaptureFixture[str]
) -> None:
    """The override and ad-hoc paths start a billed run without passing through validate().

    Replacing the hard rejection with a `validate()`-only warning would have made those two
    paths - the ones that actually launch the Fireworks-billed run - completely silent.
    """
    svc = _opencode_svc(repo)
    fw = "fireworks-ai/accounts/fireworks/models/glm-5p2"

    svc._request_for("impl", "x", model=fw)  # override on a configured client
    err = capsys.readouterr()[1]
    assert "Fireworks credits" in err

    svc._request_for(None, "x", backend="opencode", model=fw)  # ad-hoc
    assert "Fireworks credits" in capsys.readouterr()[1]


def test_the_metered_warning_is_not_repeated_per_run(
    repo: Path, capsys: pytest.CaptureFixture[str]
) -> None:
    """A fan-out would otherwise print the same line once per job and train the reader to skip it."""
    svc = _opencode_svc(repo)
    fw = "fireworks-ai/accounts/fireworks/models/glm-5p2"
    for _ in range(5):
        svc._request_for("impl", "x", model=fw)
    assert capsys.readouterr()[1].count("Fireworks credits") == 1


def test_run_agent_passes_a_metered_model_through(repo: Path) -> None:
    # Both the ad-hoc and the client-override paths reach the backend with the model as written.
    svc = _opencode_svc(repo)
    for kwargs in (
        {"backend": "opencode", "goal": "x", "task_id": "t1"},
        {"client_name": "impl", "goal": "x", "task_id": "t2"},
    ):
        req = svc._request_for(
            kwargs.get("client_name"),
            "x",
            backend=kwargs.get("backend"),
            model="fireworks-ai/accounts/fireworks/models/glm-5p2",
        )
        assert req.model == "fireworks-ai/accounts/fireworks/models/glm-5p2"


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
            "limit_runs": None,
            "enforce": False,
        },
        {
            "backend": None,
            "client": None,
            "window": "month",
            "limit_usd": 5.0,
            "limit_runs": None,
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


def _git(root: Path, *a: str) -> str:
    proc = subprocess.run(
        ["git", "-C", str(root), *a], check=True, capture_output=True, text=True
    )
    return proc.stdout.strip()


def test_a_conflict_from_a_rewritten_base_says_so_instead_of_blaming_the_files(repo: Path) -> None:
    """The driver's #5 field complaint, reproduced: rewording commits while agents are running
    orphans their base, every file then conflicts, and the conflict list points at files nobody
    touched. The real cause - "your base is no longer in history" - was invisible, because the
    conflict result carried no message at all."""
    svc = _svc(repo)
    (repo / "shared.txt").write_text("original\n")
    _git(repo, "add", "-A")
    _git(repo, "commit", "-q", "-m", "add shared")

    rec = svc.run_agent("worker", "edit the shared file")
    (Path(rec.worktree) / "shared.txt").write_text("the agent's version\n")

    # Reword the base commit out of existence while the run is "in flight", exactly as the author
    # did (soft reset + recommit), then diverge so the merge actually conflicts.
    _git(repo, "reset", "-q", "--soft", "HEAD~1")
    (repo / "shared.txt").write_text("the human's version\n")
    _git(repo, "commit", "-q", "-a", "-m", "add shared, reworded")

    result = svc.integrate(rec.run_id)
    assert result.status == "conflict"
    assert result.message and "reachable from no branch or tag" in result.message, (
        "conflict reported with no explanation - the file list is the misleading part"
    )


def test_an_ordinary_conflict_does_not_claim_the_base_was_rewritten(repo: Path) -> None:
    """The diagnosis must only speak when it is true, or it becomes the new misleading message."""
    svc = _svc(repo)
    (repo / "shared.txt").write_text("original\n")
    _git(repo, "add", "-A")
    _git(repo, "commit", "-q", "-m", "add shared")

    rec = svc.run_agent("worker", "edit the shared file")
    (Path(rec.worktree) / "shared.txt").write_text("the agent's version\n")

    # A genuine overlap: the base commit is untouched and still reachable.
    (repo / "shared.txt").write_text("the human's version\n")
    _git(repo, "commit", "-q", "-a", "-m", "human edit on top")

    result = svc.integrate(rec.run_id)
    assert result.status == "conflict"
    assert not (result.message and "reachable from no branch or tag" in result.message)


def test_a_divergent_base_branch_is_not_reported_as_rewritten_history(repo: Path) -> None:
    """`base_branch` chaining is a supported flow, not a broken repo.

    A run spawned from another branch and integrated into this one has a base that is legitimately
    not an ancestor of the target. Non-ancestry alone therefore cannot mean "rewritten": claiming
    it would send the driver hunting a history rewrite that never happened - the same misdirection
    this diagnosis exists to remove.
    """
    svc = _svc(repo)
    (repo / "shared.txt").write_text("original\n")
    _git(repo, "add", "-A")
    _git(repo, "commit", "-q", "-m", "add shared")

    # A side branch that stays alive, with its own commit; the run is based on it.
    _git(repo, "checkout", "-q", "-b", "side")
    (repo / "shared.txt").write_text("the side branch's version\n")
    _git(repo, "commit", "-q", "-a", "-m", "side edit")
    rec = svc.run_agent("worker", "edit the shared file", base_branch="side")
    (Path(rec.worktree) / "shared.txt").write_text("the agent's version\n")

    # Back on the original branch, conflict for an ordinary reason: both sides touched the file.
    _git(repo, "checkout", "-q", "-")
    (repo / "shared.txt").write_text("the main line's version\n")
    _git(repo, "commit", "-q", "-a", "-m", "main-line edit")

    result = svc.integrate(rec.run_id)
    assert result.status == "conflict"
    assert not (result.message and "reachable from no branch or tag" in result.message), (
        "a live base branch was reported as rewritten history"
    )


def test_an_orphaned_base_offers_causes_rather_than_asserting_a_rewrite(repo: Path) -> None:
    """`base_branch` takes any commit-ish, so "no surviving ref" does not prove a rewrite.

    A run based on a branch that is later deleted reaches the same state with no rewrite involved.
    The observation (nothing reaches this base) is what was measured and is the actionable part;
    the cause is inferred, so the message must offer it rather than assert it. Stating a confident
    wrong cause is the exact failure this diagnosis exists to remove."""
    svc = _svc(repo)
    (repo / "shared.txt").write_text("original\n")
    _git(repo, "add", "-A")
    _git(repo, "commit", "-q", "-m", "add shared")

    _git(repo, "checkout", "-q", "-b", "doomed")
    (repo / "shared.txt").write_text("the doomed branch's version\n")
    _git(repo, "commit", "-q", "-a", "-m", "doomed edit")
    rec = svc.run_agent("worker", "edit the shared file", base_branch="doomed")
    (Path(rec.worktree) / "shared.txt").write_text("the agent's version\n")

    _git(repo, "checkout", "-q", "-")
    (repo / "shared.txt").write_text("the main line's version\n")
    _git(repo, "commit", "-q", "-a", "-m", "main-line edit")
    _git(repo, "branch", "-qD", "doomed")  # no rewrite happened; the ref simply went away

    result = svc.integrate(rec.run_id)
    assert result.status == "conflict"
    assert "reachable from no branch or tag" in result.message, "the true observation went unsaid"
    assert "Usually this means" in result.message, "asserted one cause as fact"


def test_a_base_that_was_never_on_a_branch_still_gets_a_true_message(repo: Path) -> None:
    """`base_branch` accepts a raw sha, which is reachable from no ref by construction.

    The diagnosis fires, and should: the measured claim (nothing reaches this base) and the remedy
    are both correct here. What must not happen is the message insisting history was rewritten, so
    the cause list names this case too."""
    svc = _svc(repo)
    (repo / "shared.txt").write_text("original\n")
    _git(repo, "add", "-A")
    _git(repo, "commit", "-q", "-m", "add shared")

    # A commit reachable from no branch: made on a detached HEAD, addressed by sha alone.
    _git(repo, "checkout", "-q", "--detach")
    (repo / "shared.txt").write_text("the detached version\n")
    _git(repo, "commit", "-q", "-a", "-m", "detached edit")
    loose_sha = _git(repo, "rev-parse", "HEAD")
    _git(repo, "checkout", "-q", "-")

    rec = svc.run_agent("worker", "edit the shared file", base_branch=loose_sha)
    (Path(rec.worktree) / "shared.txt").write_text("the agent's version\n")
    (repo / "shared.txt").write_text("the main line's version\n")
    _git(repo, "commit", "-q", "-a", "-m", "main-line edit")

    result = svc.integrate(rec.run_id)
    assert result.status == "conflict"
    assert "reachable from no branch or tag" in result.message
    assert "never on one" in result.message, "the cause list omits the case that produced it"


def test_a_sibling_run_sharing_the_base_does_not_silence_the_diagnosis(repo: Path) -> None:
    """A fan-out shares one base, so siblings must not count as refs keeping it alive.

    Run branches are cut FROM the base and contain it by construction. Skipping only the asking
    run's own branch left every sibling able to vouch for a base that is really gone - so the
    larger the fan-out, the more certainly the diagnosis went silent. That is backwards: a rewrite
    under a 1-run fleet is a nuisance, under an 8-run fleet it is eight confusing conflicts."""
    svc = _svc(repo)
    (repo / "shared.txt").write_text("original\n")
    _git(repo, "add", "-A")
    _git(repo, "commit", "-q", "-m", "add shared")

    rec = svc.run_agent("worker", "edit the shared file")
    sibling = svc.run_agent("worker", "a concurrent run off the same base")  # noqa: F841
    (Path(rec.worktree) / "shared.txt").write_text("the agent's version\n")

    _git(repo, "reset", "-q", "--soft", "HEAD~1")
    (repo / "shared.txt").write_text("the human's version\n")
    _git(repo, "commit", "-q", "-a", "-m", "add shared, reworded")

    result = svc.integrate(rec.run_id)
    assert result.status == "conflict"
    assert "reachable from no branch or tag" in result.message, (
        "a sibling run branch vouched for a base that is actually orphaned"
    )


def test_integrate_clears_the_note_of_the_verdict_it_supersedes(repo: Path) -> None:
    """A run rejected WITH A REASON and later integrated must not keep the reason.

    `outcome_note` explains the verdict it was written with. Partial state updates preserve
    unmentioned fields, so replacing `rejected` with `integrated` alone would leave a record that
    reads "this was merged" annotated with "this was refused because ..." - and `status --full`
    prints both."""
    svc = _svc(repo)
    rec = svc.run_agent("worker", "make a change")
    (Path(rec.worktree) / "new.txt").write_text("work product\n")

    rejected = svc.set_outcome(rec.run_id, "rejected", note="wrong approach")
    assert rejected.status == "recorded"

    assert svc.integrate(rec.run_id).status == "merged"
    after = FleetState(runs_dir(repo)).get(rec.run_id)
    assert after is not None
    assert after.outcome == "integrated"
    assert after.outcome_note is None, "kept the note explaining why it was rejected"


class _Cataloged(_Echo):
    """A backend whose CLI can be asked what it runs."""

    name = "cataloged"

    def available_models(self) -> ModelCatalog:
        return ModelCatalog(models=["fast-1", "slow-2"], source=ModelSource.PROBED)


class _Opaque(_Echo):
    """A backend with no way to ask - UNAVAILABLE, which is NOT 'it has no models'."""

    name = "opaque"

    def available_models(self) -> ModelCatalog:
        return ModelCatalog()


def test_list_models_proxies_the_backend_when_no_catalog_is_configured(repo: Path) -> None:
    """REGRESSION (#78): `list_models` returned `{"models": []}` with no catalog, so a driver left
    Marshal and ran `cursor-agent models` in a shell to learn what it could route at. We did the
    same thing ourselves the same day."""
    cfg = FleetConfig(clients={"w": ClientConfig(name="w", backend="cataloged")})
    svc = MarshalService(repo, cfg, backends={"cataloged": _Cataloged()})
    listing = svc.list_models()
    assert listing.models == [], "no catalog is configured"
    assert listing.backend_models["cataloged"].models == ["fast-1", "slow-2"]
    assert listing.backend_models["cataloged"].source is ModelSource.PROBED


def test_a_backend_that_cannot_be_asked_reports_unavailable_not_empty(repo: Path) -> None:
    """`UNAVAILABLE` means "no way to ask"; an untagged `[]` would claim the backend runs nothing.

    A driver has to be able to tell those apart before concluding it cannot route anywhere -
    and, separately, to tell both apart from a curated `STATIC` list that may be stale.
    """
    cfg = FleetConfig(clients={"w": ClientConfig(name="w", backend="opaque")})
    svc = MarshalService(repo, cfg, backends={"opaque": _Opaque()})
    catalog = svc.list_models().backend_models["opaque"]
    assert catalog.models == []
    assert catalog.source is ModelSource.UNAVAILABLE


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
    got = svc.read_run_file(rec.run_id, "REPORT.md")
    assert got.status == "gone"
    assert "worktree is gone" in (got.error or "")
    assert got.content == ""


def test_read_run_file_refuses_a_path_outside_the_worktree(repo: Path) -> None:
    """`Path(wt) / "/etc/passwd"` is `/etc/passwd` - an absolute path discards the base, and `..`
    walks out. Same containment the context_files guard enforces, for the same reason."""
    svc = _svc(repo)
    rec = svc.run_agent("worker", "x")
    for bad in ("/etc/hosts", "../../escape.txt"):
        got = svc.read_run_file(rec.run_id, bad)
        assert got.status == "refused", bad
        assert "outside" in (got.error or "")
        assert got.content == "", "a refused path must never return content"


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
    """"The worktree is gone" and "the file is not there" are different problems, and they call for
    opposite reactions: a cleaned worktree means the run FINISHED and re-running it is wasted,
    while a missing path means the agent never wrote it. They used to arrive as the same bare
    ValueError, so the likely response to both was re-spawning finished work."""
    svc = _svc(repo)
    rec = svc.run_agent("worker", "x")

    present = svc.read_run_file(rec.run_id, "never-written.md")
    assert present.status == "not_found", "the worktree is right there; the file simply is not"

    svc.clean(scope="all")
    gone = svc.read_run_file(rec.run_id, "anything.md")
    assert gone.status == "gone"
    assert "gone" in (gone.error or "")
    assert gone.status != present.status, "the two states must not be one shape to a driver"


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

    from marshal_engine.core.config import ClientConfig, FleetConfig, PermissionMode

    class _SlowProbe(_Echo):
        def available_models(self) -> ModelCatalog:
            time.sleep(0.4)
            return ModelCatalog(models=["m"], source=ModelSource.PROBED)

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
    from marshal_engine.core.config import ClientConfig, FleetConfig, PermissionMode

    class _Ok(_Echo):
        def available_models(self) -> ModelCatalog:
            return ModelCatalog(models=["good-model"], source=ModelSource.PROBED)

    class _Boom(_Echo):
        def available_models(self) -> ModelCatalog:
            raise RuntimeError("probe exploded")

    cfg = FleetConfig(
        clients={
            "ok": ClientConfig(name="ok", backend="ok", permission=PermissionMode.SAFE_EDIT),
            "bad": ClientConfig(name="bad", backend="bad", permission=PermissionMode.SAFE_EDIT),
        }
    )
    svc = MarshalService(
        tmp_path, cfg, backends={"ok": _Ok(), "bad": _Boom()}
    )
    result = svc.list_models()
    assert result.backend_models["bad"].source is ModelSource.UNAVAILABLE
    # "Survives" means the good probe's answer is still present and correct — not that the
    # whole map collapsed alongside the failure.
    assert result.backend_models["ok"].models == ["good-model"]
    assert result.backend_models["ok"].source is ModelSource.PROBED


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


# --- routing (the join, through the real service) ---------------------------------------------


def test_routing_joins_the_ledger_to_run_outcomes(repo: Path) -> None:
    """The two stores meet only here: task_kind is on the event, outcome on the record."""
    from marshal_engine.accounting.usage import UsageEvent
    from marshal_engine.core.layout import runs_dir, usage_dir
    from marshal_engine.runtime.state import FleetState, RunRecord

    u = usage_dir(repo)
    u.mkdir(parents=True, exist_ok=True)
    (u / "events.jsonl").write_text(
        UsageEvent(
            ts="2026-07-30T00:00:00+00:00", run_id="r1", backend="echo", client="worker",
            task_kind="refactor", cost_usd=0.5, source="native", duration_ms=1000,
            status="exited_clean",
        ).model_dump_json() + "\n"
    )
    FleetState(runs_dir(repo)).add(
        RunRecord(run_id="r1", task_id="t", backend="echo", client="worker",
                  status="exited_clean", outcome="integrated")
    )
    svc = _svc(repo)
    ledger = svc.routing()
    assert [(c.task_kind, c.client) for c in ledger.cells] == [("refactor", "worker")]
    assert ledger.cells[0].integration_rate == 1.0
    assert ledger.cells[0].mean_cost_per_integrated == pytest.approx(0.5)
    assert ledger.recommended == "worker"


def test_routing_on_an_empty_repo_recommends_nothing(repo: Path) -> None:
    """Never a guessed client: no evidence must read as no recommendation."""
    ledger = _svc(repo).routing()
    assert ledger.cells == []
    assert ledger.recommended is None


def test_report_serializes_unmeasured_cost_as_null(repo: Path) -> None:
    """The MCP/JSON surface a driver reads must not present unmeasured spend as $0.

    `cheapest` was already honest; the per-strategy rows were not, and the rows are what an agent
    sums when it decides which lane is expensive.
    """
    svc = _svc(repo)
    state = FleetState(runs_dir(repo))
    state.add(RunRecord(
        run_id="r1", task_id="bench", backend="cursor", client="composer",
        status="exited_clean", cost_usd=0.0, source="unavailable",
    ))
    state.add(RunRecord(
        run_id="r2", task_id="bench", backend="opencode", client="fw-kimi",
        status="exited_clean", cost_usd=0.0012, source="native",
    ))

    result = svc.report("bench")
    by_client = {s.client: s for s in result.strategies}
    assert by_client["composer"].cost_usd is None
    assert by_client["fw-kimi"].cost_usd == 0.0012
    assert result.cheapest == "fw-kimi"   # the unmeasured row never wins by defaulting to zero

    payload = result.model_dump(mode="json")
    row = next(s for s in payload["strategies"] if s["client"] == "composer")
    assert row["cost_usd"] is None
    assert row["source"] == "unavailable"


def test_report_cheapest_ignores_unmeasured_rows_entirely(repo: Path) -> None:
    """With nothing measured there is no cheapest - not a tie at zero."""
    svc = _svc(repo)
    state = FleetState(runs_dir(repo))
    for i in (1, 2):
        state.add(RunRecord(
            run_id=f"r{i}", task_id="bench", backend="cursor", client=f"c{i}",
            status="exited_clean", cost_usd=0.0, source="unavailable",
        ))
    result = svc.report("bench")
    assert result.cheapest is None
    assert all(s.cost_usd is None for s in result.strategies)


def test_wait_for_runs_returns_at_once_when_everything_is_already_terminal(repo: Path) -> None:
    """The real-clock path: a wait on finished runs must not idle out a poll interval."""
    import time

    svc = _svc(repo)
    rec = svc.run_agent("worker", "do a thing")

    started = time.monotonic()
    result = svc.wait_for_runs([rec.run_id], timeout_s=30)
    elapsed = time.monotonic() - started

    assert result.all_settled
    assert [r.run_id for r in result.settled] == [rec.run_id]
    assert result.timed_out is False
    assert elapsed < 1.0, "it slept despite the run already being finished"


def test_wait_for_runs_reports_an_unknown_id_without_waiting_for_it(repo: Path) -> None:
    """A nonexistent run can never settle, so waiting on it would only burn the timeout."""
    import time

    svc = _svc(repo)
    started = time.monotonic()
    result = svc.wait_for_runs(["no-such-run"], timeout_s=30)

    assert result.unknown == ["no-such-run"]
    assert result.settled == [] and result.pending == []
    assert result.timed_out is False
    assert time.monotonic() - started < 1.0


def test_wait_for_runs_hands_back_a_partial_result_on_expiry(repo: Path) -> None:
    """A run stuck `running` must return as `pending`, not raise and not hang past the timeout."""
    svc = _svc(repo)
    done = svc.run_agent("worker", "finished work")
    svc.fleet.state.add(
        RunRecord(run_id="stuck", task_id="t", backend="echo", status="running")
    )

    result = svc.wait_for_runs([done.run_id, "stuck"], timeout_s=0.2, poll_interval_s=0.05)

    assert result.timed_out is True
    assert [r.run_id for r in result.settled] == [done.run_id]
    assert [r.run_id for r in result.pending] == ["stuck"]
