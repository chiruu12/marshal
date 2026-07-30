"""Tests for the multi-workspace registry (env resolution, lazy cache, run-id addressing, the
process-wide concurrency gate) and its routing through the MCP layer."""

from __future__ import annotations

import subprocess
import sys
import threading
import time
from collections.abc import Callable
from datetime import datetime, timedelta, timezone
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
from marshal_engine.budgets import BudgetExceeded, EnforceBudgetGate
from marshal_engine.config import ClientConfig, FleetConfig
from marshal_engine.service import MarshalService
from marshal_engine.state import FleetState, RunRecord
from marshal_engine.usage import UsageEvent
from marshal_engine.workspaces import (
    DEFAULT_MAX_CONCURRENT,
    WorkspaceDef,
    WorkspaceRegistry,
    read_workspaces_file,
    register_workspace,
    remove_workspace,
    resolve_run_gate,
    resolve_workspaces,
    scaffold_fleet_config,
)


# --- fakes + helpers -------------------------------------------------------------------------


def _env(tmp_path: Path, **extra: str) -> dict[str, str]:
    """Hermetic env: a real MARSHAL_REPO and a registry-file path that does NOT exist, so a test
    never picks up the developer's actual ~/.marshal/workspaces.yaml."""
    return {
        "MARSHAL_REPO": str(tmp_path),
        "MARSHAL_WORKSPACES_FILE": str(tmp_path / "no-registry.yaml"),
        **extra,
    }


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
            text=raw_stdout.strip() or "ok",
            usage=UsageRecord(backend="echo", cost_usd=0.001, source=UsageSource.NATIVE),
            exit_code=exit_code,
        )


class _Slow(CodingAgentBackend):
    """Counts concurrent runs (overriding run() to skip the subprocess) so the gate is observable."""

    name = "slow"
    binary = "python"
    capabilities = Capabilities()

    def __init__(self) -> None:
        self._lock = threading.Lock()
        self.active = 0
        self.peak = 0

    def check_available(self) -> bool:
        return True

    def build_invocation(self, task: TaskSpec, opts: RunOpts) -> list[str]:
        return [sys.executable, "-c", "print('ok')"]

    def map_permission(self, mode: PermissionMode) -> list[str]:
        return []

    def parse_output(self, raw_stdout: str, raw_stderr: str, exit_code: int) -> AgentResult:
        return AgentResult(status=RunStatus.EXITED_CLEAN, text="ok", exit_code=exit_code)

    def run(self, task: TaskSpec, opts: RunOpts) -> AgentResult:  # type: ignore[override]
        with self._lock:
            self.active += 1
            self.peak = max(self.peak, self.active)
        time.sleep(0.25)
        with self._lock:
            self.active -= 1
        return AgentResult(status=RunStatus.EXITED_CLEAN, text="ok", exit_code=0)


def _explode(wdef: WorkspaceDef) -> MarshalService:
    raise AssertionError(f"builder must not run for {wdef.name!r} (a scan should not build a service)")


def _init_repo(root: Path) -> None:
    def git(*a: str) -> None:
        subprocess.run(["git", "-C", str(root), *a], check=True, capture_output=True, text=True)

    git("init", "-q")
    git("config", "user.email", "t@t")
    git("config", "user.name", "t")
    (root / "README.md").write_text("hi")
    git("add", "-A")
    git("commit", "-q", "-m", "init")


def _echo_service(repo: Path, run_gate: threading.Semaphore | None = None) -> MarshalService:
    cfg = FleetConfig(clients={"worker": ClientConfig(name="worker", backend="echo")})
    return MarshalService(repo, cfg, backends={"echo": _Echo()}, run_gate=run_gate)


def _write_run(repo: Path, run_id: str, task_id: str = "t") -> None:
    FleetState(repo / ".marshal" / "runs").add(
        RunRecord(run_id=run_id, task_id=task_id, backend="echo", status="exited_clean")
    )


# --- env resolution --------------------------------------------------------------------------


def test_resolve_workspaces_default_always_present(tmp_path: Path) -> None:
    defs = resolve_workspaces(_env(tmp_path))
    assert defs[0].name == "default"
    assert defs[0].path == tmp_path.resolve()
    assert defs[0].config_path == tmp_path.resolve() / "fleet.config.yaml"


def test_resolve_workspaces_parses_additional_entries(tmp_path: Path) -> None:
    a, b = tmp_path / "a", tmp_path / "b"
    a.mkdir()
    b.mkdir()
    # comma AND newline separators both work
    env = _env(tmp_path, MARSHAL_WORKSPACES=f"alpha={a}\nbeta={b}")
    defs = {d.name: d for d in resolve_workspaces(env)}
    assert set(defs) == {"default", "alpha", "beta"}
    assert defs["alpha"].path == a.resolve()
    assert defs["alpha"].config_path == a.resolve() / "fleet.config.yaml"


def test_resolve_workspaces_skips_bad_entries(
    tmp_path: Path, capsys: pytest.CaptureFixture[str]
) -> None:
    a = tmp_path / "a"
    a.mkdir()
    # malformed (no '='), empty name, empty path, reserved name, ok, dup-path, dup-name
    env = _env(tmp_path, MARSHAL_WORKSPACES=f"noeq, =/x, b=, default={a}, good={a}, dup={a}, good=/other")
    names = {d.name for d in resolve_workspaces(env)}
    assert names == {"default", "good"}
    assert "malformed" in capsys.readouterr().err


def test_resolve_workspaces_resolves_relative_paths(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    (tmp_path / "sub").mkdir()
    monkeypatch.chdir(tmp_path)
    defs = {d.name: d for d in resolve_workspaces(_env(tmp_path, MARSHAL_WORKSPACES="rel=sub"))}
    assert defs["rel"].path == (tmp_path / "sub").resolve()


def test_marshal_config_scoped_to_default_only(tmp_path: Path) -> None:
    cfg = tmp_path / "custom.yaml"
    cfg.write_text("clients: {}")
    a = tmp_path / "a"
    a.mkdir()
    env = _env(tmp_path, MARSHAL_CONFIG=str(cfg), MARSHAL_WORKSPACES=f"alpha={a}")
    defs = {d.name: d for d in resolve_workspaces(env)}
    assert defs["default"].config_path == cfg.resolve()
    assert defs["alpha"].config_path == a.resolve() / "fleet.config.yaml"  # NOT MARSHAL_CONFIG


# --- the process-wide concurrency gate -------------------------------------------------------


def test_run_gate_uses_max_concurrent_env(tmp_path: Path) -> None:
    defs = resolve_workspaces(_env(tmp_path))
    gate = resolve_run_gate(defs, {"MARSHAL_MAX_CONCURRENT": "3"})
    assert gate is not None
    assert all(gate.acquire(blocking=False) for _ in range(3))
    assert not gate.acquire(blocking=False)  # capped at 3


def test_run_gate_default_only_when_multi_workspace(tmp_path: Path) -> None:
    a = tmp_path / "a"
    a.mkdir()
    single = resolve_workspaces(_env(tmp_path))
    assert resolve_run_gate(single, {}) is None  # single repo, no file -> uncapped (today's behavior)
    multi = resolve_workspaces(_env(tmp_path, MARSHAL_WORKSPACES=f"a={a}"))
    gate = resolve_run_gate(multi, {})
    assert gate is not None
    assert all(gate.acquire(blocking=False) for _ in range(DEFAULT_MAX_CONCURRENT))
    assert not gate.acquire(blocking=False)


def test_run_gate_uses_file_max_and_file_exists(tmp_path: Path) -> None:
    single = resolve_workspaces(_env(tmp_path))
    # the file's max_concurrent applies when the env var is unset...
    gate = resolve_run_gate(single, {}, file_max=2, file_exists=True)
    assert gate is not None
    assert all(gate.acquire(blocking=False) for _ in range(2))
    assert not gate.acquire(blocking=False)
    # ...and merely having a registry file (even single-workspace) turns the default cap on.
    assert resolve_run_gate(single, {}, file_max=None, file_exists=True) is not None
    # env still wins over the file value.
    env_gate = resolve_run_gate(single, {"MARSHAL_MAX_CONCURRENT": "1"}, file_max=9, file_exists=True)
    assert env_gate is not None and env_gate.acquire(blocking=False)
    assert not env_gate.acquire(blocking=False)


def test_run_gate_invalid_env_falls_back(tmp_path: Path) -> None:
    single = resolve_workspaces(_env(tmp_path))
    assert resolve_run_gate(single, {"MARSHAL_MAX_CONCURRENT": "abc"}) is None


def test_run_gate_caps_concurrency_across_runs(tmp_path: Path) -> None:
    repo = tmp_path / "r"
    repo.mkdir()
    _init_repo(repo)
    backend = _Slow()
    cfg = FleetConfig(clients={f"w{i}": ClientConfig(name=f"w{i}", backend="slow") for i in range(3)})
    svc = MarshalService(repo, cfg, backends={"slow": backend}, run_gate=threading.BoundedSemaphore(1))
    recs = svc.run_many([{"client": f"w{i}", "goal": "g", "task_id": f"j{i}"} for i in range(3)], max_concurrency=3)
    assert all(r.primary.status == "exited_clean" for r in recs)
    assert backend.peak == 1  # the shared gate serialized them despite max_concurrency=3


def test_without_gate_runs_overlap(tmp_path: Path) -> None:
    repo = tmp_path / "r"
    repo.mkdir()
    _init_repo(repo)
    backend = _Slow()
    cfg = FleetConfig(clients={f"w{i}": ClientConfig(name=f"w{i}", backend="slow") for i in range(3)})
    svc = MarshalService(repo, cfg, backends={"slow": backend})  # no gate
    svc.run_many([{"client": f"w{i}", "goal": "g", "task_id": f"j{i}"} for i in range(3)], max_concurrency=3)
    assert backend.peak >= 2  # uncapped: runs overlap


# --- registry: lazy build + cache ------------------------------------------------------------


def test_get_unknown_workspace_lists_known(tmp_path: Path) -> None:
    reg = WorkspaceRegistry(
        [WorkspaceDef("default", tmp_path, tmp_path / "x")], prebuilt={"default": object()}  # type: ignore[dict-item]
    )
    with pytest.raises(ValueError, match="unknown workspace.*hint: register it"):
        reg.get("nope")


def test_get_does_not_cache_failed_builds(tmp_path: Path) -> None:
    calls = {"n": 0}

    def flaky(wdef: WorkspaceDef) -> MarshalService:
        calls["n"] += 1
        if calls["n"] == 1:
            raise RuntimeError("boom")
        return object()  # type: ignore[return-value]

    reg = WorkspaceRegistry([WorkspaceDef("default", tmp_path, tmp_path / "x")], builder=flaky)
    with pytest.raises(RuntimeError):
        reg.get("default")
    svc = reg.get("default")  # a transient failure must be retryable, not poison the workspace
    assert reg.get("default") is svc  # cached only after the successful build
    assert calls["n"] == 2


def test_get_builds_once_under_concurrent_access(tmp_path: Path) -> None:
    calls: list[int] = []

    def slow_build(wdef: WorkspaceDef) -> MarshalService:
        calls.append(1)
        time.sleep(0.05)
        return object()  # type: ignore[return-value]

    reg = WorkspaceRegistry([WorkspaceDef("default", tmp_path, tmp_path / "x")], builder=slow_build)
    results: list[object] = []
    threads = [threading.Thread(target=lambda: results.append(reg.get("default"))) for _ in range(6)]
    for t in threads:
        t.start()
    for t in threads:
        t.join()
    assert len(calls) == 1  # built once despite concurrent first-touches
    assert all(r is results[0] for r in results)


def test_for_service_wraps_single_workspace(tmp_path: Path) -> None:
    repo = tmp_path / "r"
    repo.mkdir()
    _init_repo(repo)
    svc = _echo_service(repo)
    reg = WorkspaceRegistry.for_service(svc)
    assert reg.names() == ["default"]
    assert reg.get() is svc and reg.get("default") is svc


# --- registry: run-id addressing (service-free scan) -----------------------------------------


def test_owner_of_scans_ledgers_without_building(tmp_path: Path) -> None:
    a, b = tmp_path / "a", tmp_path / "b"
    a.mkdir()
    b.mkdir()
    _write_run(a, "r-a")
    _write_run(b, "r-b")
    defs = [WorkspaceDef("default", a, a / "c.yaml"), WorkspaceDef("beta", b, b / "c.yaml")]
    reg = WorkspaceRegistry(defs, builder=_explode)  # builder explodes if a scan ever builds
    assert reg.owner_of("r-a") == "default"
    assert reg.owner_of("r-b") == "beta"
    assert reg.owner_of("missing") is None
    assert reg.owner_of("r-b", hint="default") == "beta"  # wrong hint falls back to the scan


@pytest.mark.parametrize(
    "bad_id", ["", ".", "..", "../x", "a/b", "a\\b", ".hidden", "café", "a" * 129]
)
def test_owner_of_and_require_run_refuse_unsafe_run_id(tmp_path: Path, bad_id: str) -> None:
    # The scan stats `<ws>/runs/<run_id>.json` per workspace: an unvalidated id is a path
    # traversal (host `*.json` existence oracle) AND a cross-tenant read. Refused before any
    # path is composed - the refusal must name the id, not masquerade as "no such run".
    a = tmp_path / "a"
    a.mkdir()
    reg = WorkspaceRegistry([WorkspaceDef("default", a, a / "c.yaml")], builder=_explode)
    with pytest.raises(ValueError, match="unsafe run_id"):
        reg.owner_of(bad_id)
    with pytest.raises(ValueError, match="unsafe run_id"):
        reg.require_run(bad_id)


def test_owner_of_cannot_be_traversed_into_another_workspace(tmp_path: Path) -> None:
    # REGRESSION: run_id `../../../b/.marshal/runs/r-b` stat'ed through workspace A's ledger dir
    # straight onto workspace B's run record, so a run-handle call tagged B's run as A's -
    # the "never share run state across workspaces" invariant, broken by one relative path.
    a, b = tmp_path / "a", tmp_path / "b"
    a.mkdir()
    b.mkdir()
    _write_run(a, "r-a")  # A's ledger dir must exist for the `..` to resolve through it
    _write_run(b, "r-b")
    defs = [WorkspaceDef("default", a, a / "c.yaml"), WorkspaceDef("beta", b, b / "c.yaml")]
    reg = WorkspaceRegistry(defs, builder=_explode)
    # three `..`: runs -> .marshal -> repo root -> the sibling level where B lives
    traversal = "../../../b/.marshal/runs/r-b"
    # sanity: the pre-fix path really does resolve from A's runs dir onto B's record
    assert (a / ".marshal" / "runs" / f"{traversal}.json").exists()
    with pytest.raises(ValueError, match="unsafe run_id"):
        reg.owner_of(traversal)
    with pytest.raises(ValueError, match="unsafe run_id"):
        reg.require_run(traversal)


def test_ledger_runs_aggregates_and_scopes(tmp_path: Path) -> None:
    a, b = tmp_path / "a", tmp_path / "b"
    a.mkdir()
    b.mkdir()
    _write_run(a, "r-a")
    _write_run(b, "r-b1")
    _write_run(b, "r-b2")
    defs = [WorkspaceDef("default", a, a / "c.yaml"), WorkspaceDef("beta", b, b / "c.yaml")]
    reg = WorkspaceRegistry(defs, builder=_explode)
    assert {(ws, r.run_id) for ws, r in reg.ledger_runs()} == {
        ("default", "r-a"), ("beta", "r-b1"), ("beta", "r-b2")
    }
    assert {r.run_id for _, r in reg.ledger_runs("beta")} == {"r-b1", "r-b2"}
    with pytest.raises(ValueError):
        reg.ledger_runs("ghost")


def test_ledger_runs_finishes_a_deferred_reconciliation(tmp_path: Path) -> None:
    """REGRESSION: MCP `status` reads ledgers here rather than through MarshalService, so a
    reconciliation the Fleet deferred at startup was never finished on the one surface a driver
    actually polls - the orphan kept reading RUNNING however long the server lived."""

    class _StubFleet:
        def __init__(self) -> None:
            self.calls = 0
            self.session_start = None  # seeded into the runtime cache at registry construction
            self.budget_gate = None

        def reconcile_orphans(self) -> None:
            self.calls += 1

        def with_liveness(self, rec: RunRecord) -> RunRecord:
            return rec  # this test is about the reconcile hook, not the liveness fill-in

    class _StubService:
        def __init__(self) -> None:
            self.fleet = _StubFleet()

    a = tmp_path / "a"
    a.mkdir()
    _write_run(a, "r-a")
    defs = [WorkspaceDef("default", a, a / "c.yaml")]
    svc = _StubService()
    reg = WorkspaceRegistry(defs, builder=_explode, prebuilt={"default": svc})  # type: ignore[dict-item]

    assert {r.run_id for _, r in reg.ledger_runs()} == {"r-a"}
    assert svc.fleet.calls == 1, "the ledger read did not finish the deferred reconciliation"


def test_a_deferred_orphan_reaches_failed_through_a_real_ledger_read(tmp_path: Path) -> None:
    """End-to-end companion to the stubbed hook test: a REAL Fleet defers a young orphan, and a
    later real `ledger_runs()` (what MCP status calls) is what finally stamps it `failed`. The stub
    test proves the hook fires; this proves the hook does something."""
    from marshal_engine.fleet import _REAP_GRACE_S

    repo = tmp_path / "repo"
    repo.mkdir()
    _init_repo(repo)
    runs = FleetState(repo / ".marshal" / "runs")
    runs.add(
        RunRecord(
            run_id="orph.echo.x",
            task_id="orph",
            backend="echo",
            status="running",  # left behind by a supervisor that is gone
            started_at=datetime.now(timezone.utc).isoformat(),
        )
    )

    svc = _echo_service(repo)  # real MarshalService -> real Fleet -> startup reap defers it
    assert runs.get("orph.echo.x").status == "running"

    defs = [WorkspaceDef("default", repo, repo / "fleet.config.yaml")]
    reg = WorkspaceRegistry(defs, builder=_explode, prebuilt={"default": svc})

    aged = datetime.now(timezone.utc) - timedelta(seconds=_REAP_GRACE_S + 60)
    runs.update("orph.echo.x", started_at=aged.isoformat())

    rows = {r.run_id: r.status for _, r in reg.ledger_runs()}
    assert rows["orph.echo.x"] == "failed", "a real ledger read did not finish the reconciliation"


def test_ledger_runs_does_not_build_a_service_to_reconcile(tmp_path: Path) -> None:
    """A workspace with no Fleet in this process never built one, so nothing reaped and there is
    nothing to finish - reading its ledger must stay build-free."""
    a = tmp_path / "a"
    a.mkdir()
    _write_run(a, "r-a")
    reg = WorkspaceRegistry([WorkspaceDef("default", a, a / "c.yaml")], builder=_explode)
    assert {r.run_id for _, r in reg.ledger_runs()} == {"r-a"}  # _explode would raise on a build


def test_describe_reports_configured_and_counts(tmp_path: Path) -> None:
    a, b = tmp_path / "a", tmp_path / "b"
    a.mkdir()
    b.mkdir()
    (a / "fleet.config.yaml").write_text("clients:\n  w:\n    backend: cursor\n")
    defs = [WorkspaceDef("default", a, a / "fleet.config.yaml"), WorkspaceDef("beta", b, b / "fleet.config.yaml")]
    rows = {r["name"]: r for r in WorkspaceRegistry(defs, builder=_explode).describe()}
    assert rows["default"]["configured"] is True
    assert rows["default"]["client_count"] == 1
    assert rows["default"]["default"] is True
    assert rows["default"]["ready"] is True
    assert rows["default"]["ready_reason"] is None
    assert rows["beta"]["configured"] is False
    assert rows["beta"]["client_count"] == 0
    assert rows["beta"]["default"] is False
    assert rows["beta"]["ready"] is False
    assert "no config file" in rows["beta"]["ready_reason"]


def test_a_config_with_no_clients_is_not_ready(tmp_path: Path) -> None:
    """REGRESSION: `configured: true` meant only "a file exists there". A driver read it as "usable",
    ran against the workspace, got nothing, and had to fall back to an ad-hoc spawn. `ready` answers
    the question that was actually being asked, and says why when the answer is no."""
    a = tmp_path / "a"
    a.mkdir()
    (a / "fleet.config.yaml").write_text("clients: {}\n")  # a real, valid, useless config
    defs = [WorkspaceDef("default", a, a / "fleet.config.yaml")]
    row = WorkspaceRegistry(defs, builder=_explode).describe()[0]
    assert row["configured"] is True, "the file does exist - that field keeps its old meaning"
    assert row["ready"] is False
    assert "no clients" in row["ready_reason"]


def test_an_unparseable_config_says_so_rather_than_reporting_zero_clients(tmp_path: Path) -> None:
    """"0 clients" and "this file is broken" need different fixes; collapsing them to one number
    just moves the guessing onto the reader."""
    a = tmp_path / "a"
    a.mkdir()
    (a / "fleet.config.yaml").write_text("clients: [broken: yaml: here")
    defs = [WorkspaceDef("default", a, a / "fleet.config.yaml")]
    row = WorkspaceRegistry(defs, builder=_explode).describe()[0]
    assert row["ready"] is False
    assert "does not load" in row["ready_reason"]


def test_describe_survives_malformed_config(tmp_path: Path) -> None:
    # Regression: a broken per-repo fleet.config.yaml must degrade to 0 clients, not crash
    # list_workspaces / `marshal workspace list`.
    a = tmp_path / "a"
    a.mkdir()
    (a / "fleet.config.yaml").write_text("clients: [broken: yaml: here")  # malformed YAML
    defs = [WorkspaceDef("default", a, a / "fleet.config.yaml")]
    rows = WorkspaceRegistry(defs, builder=_explode).describe()
    assert rows[0]["configured"] is True and rows[0]["client_count"] == 0  # degraded, not raised


def test_run_routes_to_correct_repo_and_resolves_cold(tmp_path: Path) -> None:
    repo_a, repo_b = tmp_path / "a", tmp_path / "b"
    for r in (repo_a, repo_b):
        r.mkdir()
        _init_repo(r)
    defs = [
        WorkspaceDef("default", repo_a.resolve(), repo_a / "fleet.config.yaml"),
        WorkspaceDef("beta", repo_b.resolve(), repo_b / "fleet.config.yaml"),
    ]
    reg = WorkspaceRegistry(defs, prebuilt={"default": _echo_service(repo_a), "beta": _echo_service(repo_b)})
    rec = reg.get("beta").run_agent("worker", "do x", task_id="t1")
    assert Path(rec.worktree or "").resolve().is_relative_to(repo_b.resolve())
    assert not Path(rec.worktree or "").resolve().is_relative_to(repo_a.resolve())

    # A COLD registry (no prebuilt, no in-memory index) resolves the run purely by scanning ledgers,
    # building only the owning workspace (which has no config -> zero clients, still fine for get_run).
    cold = WorkspaceRegistry(defs)
    owner, svc = cold.require_run(rec.run_id)
    assert owner == "beta"
    assert svc.get_run(rec.run_id) is not None
    assert cold.owner_of(rec.run_id, hint="default") == "beta"

    with pytest.raises(ValueError, match="no run"):
        cold.require_run("nonexistent")


# --- routing through the MCP layer -----------------------------------------------------------


def test_build_app_registers_workspace_tools_and_params(tmp_path: Path) -> None:
    pytest.importorskip("mcp")
    import asyncio

    from marshal_engine.mcp_server import build_app

    repo = tmp_path / "r"
    repo.mkdir()
    _init_repo(repo)
    app = build_app(WorkspaceRegistry.for_service(_echo_service(repo)))
    tools = {t.name: t for t in asyncio.run(app.list_tools())}
    assert "list_workspaces" in tools
    assert tools["run_agent"].input_schema["properties"]["workspace"].get("description")
    assert tools["get_run"].input_schema["properties"]["workspace"].get("description")


def test_mcp_workspace_param_routes_via_call_tool(tmp_path: Path) -> None:
    pytest.importorskip("mcp")
    import asyncio

    from marshal_engine.mcp_server import build_app

    repo_a, repo_b = tmp_path / "a", tmp_path / "b"
    for r in (repo_a, repo_b):
        r.mkdir()
        _init_repo(r)
    defs = [
        WorkspaceDef("default", repo_a.resolve(), repo_a / "fleet.config.yaml"),
        WorkspaceDef("beta", repo_b.resolve(), repo_b / "fleet.config.yaml"),
    ]
    reg = WorkspaceRegistry(defs, prebuilt={"default": _echo_service(repo_a), "beta": _echo_service(repo_b)})
    app = build_app(reg)

    asyncio.run(app.call_tool("run_agent", {"client": "worker", "goal": "x", "task_id": "t1", "workspace": "beta"}))
    assert len(reg.get("beta").status()) == 1  # routed to beta...
    assert reg.get("default").status() == []  # ...not the default repo

    run_id = reg.get("beta").status()[0].run_id
    # get_run resolves cross-workspace without a hint; status aggregates; list_workspaces serves.
    assert asyncio.run(app.call_tool("get_run", {"run_id": run_id})) is not None
    assert asyncio.run(app.call_tool("status", {})) is not None
    assert asyncio.run(app.call_tool("list_workspaces", {})) is not None
    # an unknown id must not raise through the MCP transport (get_run's None contract holds);
    # reaching the assert means call_tool returned cleanly rather than erroring.
    unknown = asyncio.run(app.call_tool("get_run", {"run_id": "no-such-run"}))
    assert unknown is not None  # the server wraps even a None tool-return in a result envelope


# --- the central registry file (~/.marshal/workspaces.yaml) -----------------------------------


def test_read_workspaces_file_parses(tmp_path: Path) -> None:
    f = tmp_path / "w.yaml"
    f.write_text("max_concurrent: 5\nworkspaces:\n  a: /x\n  b: /y\n")
    ws, mc = read_workspaces_file(f)
    assert ws == {"a": "/x", "b": "/y"}
    assert mc == 5


def test_read_workspaces_file_missing_is_empty(tmp_path: Path) -> None:
    assert read_workspaces_file(tmp_path / "nope.yaml") == ({}, None)


def test_read_workspaces_file_malformed_warns(tmp_path: Path, capsys: pytest.CaptureFixture[str]) -> None:
    f = tmp_path / "w.yaml"
    f.write_text("just a string, not a mapping")
    assert read_workspaces_file(f) == ({}, None)
    assert "expected a mapping" in capsys.readouterr().err


def test_read_workspaces_file_directory_or_binary_no_crash(
    tmp_path: Path, capsys: pytest.CaptureFixture[str]
) -> None:
    # A directory or a binary/non-UTF8 file at the registry path must degrade, not crash on connect.
    a_dir = tmp_path / "is_a_dir.yaml"
    a_dir.mkdir()
    assert read_workspaces_file(a_dir) == ({}, None)
    binary = tmp_path / "binary.yaml"
    binary.write_bytes(b"\xff\xfe\x00\x01not utf8")
    assert read_workspaces_file(binary) == ({}, None)
    assert "unreadable" in capsys.readouterr().err


def test_read_workspaces_file_ignores_bad_max(tmp_path: Path, capsys: pytest.CaptureFixture[str]) -> None:
    f = tmp_path / "w.yaml"
    f.write_text("max_concurrent: -1\nworkspaces:\n  a: /x\n")
    ws, mc = read_workspaces_file(f)
    assert mc is None and ws == {"a": "/x"}
    assert "max_concurrent" in capsys.readouterr().err


def test_resolve_workspaces_merges_file_and_env(tmp_path: Path) -> None:
    a, b = tmp_path / "a", tmp_path / "b"
    a.mkdir()
    b.mkdir()
    f = tmp_path / "w.yaml"
    f.write_text(f"workspaces:\n  fromfile: {a}\n")
    env = _env(tmp_path, MARSHAL_WORKSPACES_FILE=str(f), MARSHAL_WORKSPACES=f"fromenv={b}")
    defs = {d.name: d for d in resolve_workspaces(env)}
    assert set(defs) == {"default", "fromfile", "fromenv"}
    assert defs["fromfile"].path == a.resolve()
    assert defs["fromenv"].path == b.resolve()


def test_file_wins_over_env_on_duplicate_name(tmp_path: Path, capsys: pytest.CaptureFixture[str]) -> None:
    a, b = tmp_path / "a", tmp_path / "b"
    a.mkdir()
    b.mkdir()
    f = tmp_path / "w.yaml"
    f.write_text(f"workspaces:\n  dup: {a}\n")
    env = _env(tmp_path, MARSHAL_WORKSPACES_FILE=str(f), MARSHAL_WORKSPACES=f"dup={b}")
    defs = {d.name: d for d in resolve_workspaces(env)}
    assert defs["dup"].path == a.resolve()  # file is processed first, so it wins
    assert "duplicate workspace name 'dup'" in capsys.readouterr().err


def test_register_and_remove_workspace(tmp_path: Path) -> None:
    f = tmp_path / "w.yaml"
    repo = tmp_path / "r"
    repo.mkdir()
    wdef = register_workspace("alpha", repo, file_path=f)
    assert wdef.path == repo.resolve()
    assert read_workspaces_file(f)[0] == {"alpha": str(repo.resolve())}
    assert remove_workspace("alpha", file_path=f) is True
    assert read_workspaces_file(f)[0] == {}
    assert remove_workspace("alpha", file_path=f) is False  # already gone


def test_register_preserves_max_concurrent(tmp_path: Path) -> None:
    f = tmp_path / "w.yaml"
    f.write_text("max_concurrent: 4\nworkspaces: {}\n")
    repo = tmp_path / "r"
    repo.mkdir()
    register_workspace("alpha", repo, file_path=f)
    ws, mc = read_workspaces_file(f)
    assert mc == 4 and "alpha" in ws


def test_register_rejects_bad_name_and_path(tmp_path: Path) -> None:
    f = tmp_path / "w.yaml"
    repo = tmp_path / "r"
    repo.mkdir()
    with pytest.raises(ValueError, match="reserved"):
        register_workspace("default", repo, file_path=f)
    with pytest.raises(ValueError, match="invalid workspace name"):
        register_workspace("bad name", repo, file_path=f)
    with pytest.raises(ValueError, match="not an existing directory"):
        register_workspace("alpha", tmp_path / "missing", file_path=f)


def test_scaffold_fleet_config(tmp_path: Path) -> None:
    from marshal_engine.config import load_config

    repo = tmp_path / "r"
    repo.mkdir()
    assert scaffold_fleet_config(repo) is True
    assert (repo / "fleet.config.yaml").exists()
    assert scaffold_fleet_config(repo) is False  # idempotent - never overwrites
    assert load_config(repo / "fleet.config.yaml").clients == {}  # a loadable zero-client stub


def test_scaffold_suggests_read_only_reviewers(tmp_path: Path) -> None:
    """Without these, the first `run_team` a new user tries fails: a team needs read-only clients.

    They stay commented (the stub must load as a valid zero-client config), but uncommenting one is
    the whole setup step - the alternative is discovering the requirement from a validation error.
    """
    from marshal_engine.config import load_config

    repo = tmp_path / "r"
    repo.mkdir()
    scaffold_fleet_config(repo)
    stub = (repo / "fleet.config.yaml").read_text(encoding="utf-8")
    assert "permission: read-only" in stub
    assert "claude-readonly" in stub and "cursor-readonly" in stub
    assert "examples/teams/" in stub  # points at the starter panels
    # Still commented, so the stub stays a zero-client config that loads.
    assert load_config(repo / "fleet.config.yaml").clients == {}


def test_scaffold_includes_stock_openai_codex_example(tmp_path: Path) -> None:
    """New users with a stock Codex CLI should see a copy-paste OpenAI client, not a router-only path."""
    from marshal_engine.config import load_config

    repo = tmp_path / "r"
    repo.mkdir()
    scaffold_fleet_config(repo)
    stub = (repo / "fleet.config.yaml").read_text(encoding="utf-8")
    assert "# codex:" in stub
    assert "backend: codex" in stub
    assert "model: gpt-5.5" in stub
    assert "codex login" in stub and "OPENAI_API_KEY" in stub
    assert "codex-readonly" in stub
    assert load_config(repo / "fleet.config.yaml").clients == {}


def test_detect_project_markers_root_wins(tmp_path: Path) -> None:
    from marshal_engine.workspaces import detect_project_markers

    (tmp_path / "pyproject.toml").write_text("[project]\n")
    (tmp_path / "sdk").mkdir()
    (tmp_path / "sdk" / "package.json").write_text("{}")
    assert detect_project_markers(tmp_path) == [("pyproject.toml", "")]  # nested is not scanned


def test_detect_project_markers_nested_depth_1_and_2(tmp_path: Path) -> None:
    from marshal_engine.workspaces import detect_project_markers

    (tmp_path / "sdk").mkdir()
    (tmp_path / "sdk" / "pyproject.toml").write_text("[project]\n")
    assert detect_project_markers(tmp_path) == [("pyproject.toml", "sdk")]

    deep = tmp_path / "deep"
    (deep / "packages" / "core").mkdir(parents=True)
    (deep / "packages" / "core" / "go.mod").write_text("module x\n")
    assert detect_project_markers(deep) == [("go.mod", "packages/core")]


def test_detect_project_markers_skips_vendored_and_dot_dirs(tmp_path: Path) -> None:
    from marshal_engine.workspaces import detect_project_markers

    for skip in (".venv", "node_modules", ".git", ".hidden"):
        (tmp_path / skip).mkdir()
        (tmp_path / skip / "pyproject.toml").write_text("[project]\n")
    assert detect_project_markers(tmp_path) == []


def test_detect_project_markers_caps_results(tmp_path: Path) -> None:
    from marshal_engine.workspaces import detect_project_markers

    for name in ("a", "b", "c", "d", "e"):
        (tmp_path / name).mkdir()
        (tmp_path / name / "pyproject.toml").write_text("[project]\n")
    assert len(detect_project_markers(tmp_path)) == 3  # a short stub, even in a monorepo


def test_scaffold_templates_nested_project_hint(tmp_path: Path) -> None:
    from marshal_engine.config import load_config

    repo = tmp_path / "r"
    (repo / "sdk").mkdir(parents=True)
    (repo / "sdk" / "pyproject.toml").write_text("[project]\n")
    assert scaffold_fleet_config(repo) is True
    body = (repo / "fleet.config.yaml").read_text()
    # nested projects need the shell form: worktree_setup executes as argv, no shell, at the root
    assert '# worktree_setup: sh -c "cd sdk && uv sync"' in body
    assert "# allow_unsafe_commands: true" in body
    assert "sdk/" in body  # the worker-context hint names the package dir
    assert load_config(repo / "fleet.config.yaml").clients == {}  # still a valid zero-client stub


def test_scaffold_templates_root_project_hint(tmp_path: Path) -> None:
    from marshal_engine.config import load_config

    repo = tmp_path / "r"
    repo.mkdir()
    (repo / "package.json").write_text("{}")
    assert scaffold_fleet_config(repo) is True
    body = (repo / "fleet.config.yaml").read_text()
    assert "# worktree_setup: npm install" in body
    assert "sh -c" not in body  # root projects need no cd
    assert load_config(repo / "fleet.config.yaml").clients == {}


def test_registry_hot_reloads_new_workspace(tmp_path: Path) -> None:
    repo_a, repo_b = tmp_path / "a", tmp_path / "b"
    for r in (repo_a, repo_b):
        r.mkdir()
        _init_repo(r)
    reg_file = tmp_path / "workspaces.yaml"
    env = {"MARSHAL_REPO": str(repo_a), "MARSHAL_WORKSPACES_FILE": str(reg_file)}
    reg = WorkspaceRegistry.from_env(env)
    assert reg.names() == ["default"]  # file doesn't exist yet
    default_svc = reg.get("default")

    register_workspace("beta", repo_b, file_path=reg_file)  # add to the file the registry watches
    assert "beta" in reg.names()  # hot-reloaded without reconnecting
    # Identity is preserved while the config file is unchanged (absent before and after here).
    assert reg.get("default") is default_svc
    assert Path(reg.get("beta").repo_root).resolve() == repo_b.resolve()


def _config_aware_builder() -> Callable[[WorkspaceDef], MarshalService]:
    """A hermetic stand-in for build_service_for: reads the workspace's config file live but
    injects the fake echo backend so no real agent CLI is ever probed."""
    from marshal_engine.config import load_config

    def build(wdef: WorkspaceDef) -> MarshalService:
        cfg = load_config(wdef.config_path) if wdef.config_path.exists() else FleetConfig()
        return MarshalService(
            wdef.path, cfg, backends={"echo": _Echo()}, config_path=wdef.config_path
        )

    return build


_ECHO_CLIENT_YAML = "clients:\n  worker:\n    backend: echo\n"


def test_registry_rebuilds_when_config_appears(tmp_path: Path) -> None:
    """The field bug: add_workspace before fleet.config.yaml exists must not freeze the client
    list at zero - the config appearing on disk is picked up on the next call, no reconnect."""
    repo = tmp_path / "r"
    repo.mkdir()
    _init_repo(repo)
    wdef = WorkspaceDef("default", repo.resolve(), repo / "fleet.config.yaml")
    reg = WorkspaceRegistry([wdef], builder=_config_aware_builder())
    assert reg.get().list_clients().clients == []  # registered before any config exists

    (repo / "fleet.config.yaml").write_text(_ECHO_CLIENT_YAML)
    assert [c.name for c in reg.get().list_clients().clients] == ["worker"]


def test_registry_rebuilds_on_config_edit_and_delete(tmp_path: Path) -> None:
    import os as _os

    repo = tmp_path / "r"
    repo.mkdir()
    _init_repo(repo)
    cfg = repo / "fleet.config.yaml"
    cfg.write_text(_ECHO_CLIENT_YAML)
    wdef = WorkspaceDef("default", repo.resolve(), cfg)
    reg = WorkspaceRegistry([wdef], builder=_config_aware_builder())
    first = reg.get()
    assert [c.name for c in first.list_clients().clients] == ["worker"]

    cfg.write_text("clients:\n  worker:\n    backend: echo\n  second:\n    backend: echo\n")
    _os.utime(cfg, ns=(1, 1))  # force a distinct mtime_ns on coarse-timestamp filesystems
    second = reg.get()
    assert second is not first
    assert {c.name for c in second.list_clients().clients} == {"worker", "second"}

    cfg.unlink()
    assert reg.get().list_clients().clients == []  # deleted config degrades to zero clients


def test_registry_unchanged_config_builds_once(tmp_path: Path) -> None:
    repo = tmp_path / "r"
    repo.mkdir()
    _init_repo(repo)
    (repo / "fleet.config.yaml").write_text(_ECHO_CLIENT_YAML)
    wdef = WorkspaceDef("default", repo.resolve(), repo / "fleet.config.yaml")
    builds = 0
    inner = _config_aware_builder()

    def counting(d: WorkspaceDef) -> MarshalService:
        nonlocal builds
        builds += 1
        return inner(d)

    reg = WorkspaceRegistry([wdef], builder=counting)
    services = {id(reg.get()) for _ in range(5)}
    assert len(services) == 1
    assert builds == 1


def test_registry_add_evicts_cached_service(tmp_path: Path) -> None:
    repo_a, repo_b = tmp_path / "a", tmp_path / "b"
    for r in (repo_a, repo_b):
        r.mkdir()
        _init_repo(r)
    reg_file = tmp_path / "workspaces.yaml"
    env = {"MARSHAL_REPO": str(repo_a), "MARSHAL_WORKSPACES_FILE": str(reg_file)}
    reg = WorkspaceRegistry.from_env(env)
    reg.add("beta", repo_b)
    first = reg.get("beta")
    reg.add("beta", repo_b)  # re-registering must be picked up, not served from the stale cache
    assert reg.get("beta") is not first


def test_registry_rebuild_failure_keeps_retrying(tmp_path: Path) -> None:
    """A malformed config raises loudly on get() (same as the single-repo path) but never
    poisons the cache: fixing the file heals the workspace on the next call."""
    repo = tmp_path / "r"
    repo.mkdir()
    _init_repo(repo)
    cfg = repo / "fleet.config.yaml"
    cfg.write_text(_ECHO_CLIENT_YAML)
    wdef = WorkspaceDef("default", repo.resolve(), cfg)
    inner = _config_aware_builder()

    def strict(d: WorkspaceDef) -> MarshalService:
        return inner(d)

    reg = WorkspaceRegistry([wdef], builder=strict)
    first = reg.get()

    from marshal_engine.config import ConfigError

    cfg.write_text("clients:\n  worker: {}\n")  # malformed: a client needs a backend
    with pytest.raises(ConfigError, match="missing required 'backend'"):
        reg.get()
    cfg.write_text(_ECHO_CLIENT_YAML + "  second:\n    backend: echo\n")
    healed = reg.get()
    assert healed is not first
    assert {c.name for c in healed.list_clients().clients} == {"worker", "second"}


def test_registry_rebuild_preserves_inflight_run(tmp_path: Path) -> None:
    """Replacing a workspace's service must not lose a background spawn: the old Fleet finishes
    on its own thread and the terminal record is visible through the NEW service (shared ledger)."""
    repo = tmp_path / "r"
    repo.mkdir()
    _init_repo(repo)
    cfg = repo / "fleet.config.yaml"
    cfg.write_text("clients:\n  worker:\n    backend: slow\n")
    wdef = WorkspaceDef("default", repo.resolve(), cfg)

    def build(d: WorkspaceDef) -> MarshalService:
        from marshal_engine.config import load_config

        return MarshalService(
            d.path, load_config(d.config_path), backends={"slow": _Slow()}, config_path=d.config_path
        )

    reg = WorkspaceRegistry([wdef], builder=build)
    old = reg.get()
    rec = old.spawn("worker", "go")
    cfg.write_text("clients:\n  worker:\n    backend: slow\n  extra:\n    backend: slow\n")
    import os as _os

    _os.utime(cfg, ns=(1, 1))
    new = reg.get()
    assert new is not old
    deadline = time.time() + 10
    while time.time() < deadline:
        got = new.get_run(rec.run_id)
        if got is not None and got.status not in ("queued", "running"):
            break
        time.sleep(0.05)
    got = new.get_run(rec.run_id)
    assert got is not None and got.status == "exited_clean"


# --- registry: durable runtime (enforce gate + session clock) across hot-reload ----------------


_ENFORCE_SESSION_BUDGET_YAML = (
    "budgets:\n  - window: session\n    limit_usd: 5.0\n    enforce: true\n"
)


def _durable_registry(
    wdef: WorkspaceDef, backends_factory: Callable[[], dict[str, CodingAgentBackend]]
) -> WorkspaceRegistry:
    """A registry whose builder injects fake backends but opts into the registry's durable
    runtime via ``runtime_for`` - the same wiring the default build_service_for builder uses."""
    from marshal_engine.config import load_config

    holder: list[WorkspaceRegistry] = []

    def build(d: WorkspaceDef) -> MarshalService:
        cfg = load_config(d.config_path) if d.config_path.exists() else FleetConfig()
        rt = holder[0].runtime_for(d)
        return MarshalService(
            d.path, cfg, backends=backends_factory(), config_path=d.config_path,
            session_start=rt.session_start, budget_gate=rt.budget_gate,
        )

    reg = WorkspaceRegistry([wdef], builder=build)
    holder.append(reg)
    return reg


def test_enforce_budget_survives_midflight_config_reload(tmp_path: Path) -> None:
    """#36 AC: with an ``enforce: true`` budget and one run in flight, an unrelated config edit
    (which rebuilds the service) must not admit a second matching spawn - and the old run's
    terminal release must free the slot for the NEW service."""
    import os as _os

    repo = tmp_path / "r"
    repo.mkdir()
    _init_repo(repo)
    cfg = repo / "fleet.config.yaml"
    cfg.write_text("clients:\n  worker:\n    backend: slow\n" + _ENFORCE_SESSION_BUDGET_YAML)
    wdef = WorkspaceDef("default", repo.resolve(), cfg)
    reg = _durable_registry(wdef, lambda: {"slow": _Slow()})

    old = reg.get()
    rec = old.spawn("worker", "first")  # holds the enforce slot until it finishes

    cfg.write_text(
        "clients:\n  worker:\n    backend: slow\n  extra:\n    backend: slow\n"
        + _ENFORCE_SESSION_BUDGET_YAML
    )
    _os.utime(cfg, ns=(1, 1))  # force a distinct signature on coarse-timestamp filesystems
    new = reg.get()
    assert new is not old
    assert new.fleet.budget_gate is old.fleet.budget_gate  # one gate per repo, never a fork

    with pytest.raises(BudgetExceeded, match="in-flight"):
        new.spawn("worker", "second")  # still refused: the old Fleet's run holds the shared slot

    # Drain the OLD Fleet's background pool: this blocks past the run's finally (which releases
    # the shared gate slot), so the check below is deterministic rather than status-poll racy.
    old.shutdown()
    finished = new.get_run(rec.run_id)
    assert finished is not None and finished.status == "exited_clean"
    third = new.run_agent("worker", "third", task_id="t3")  # release freed the shared slot
    assert third.status == "exited_clean"


def test_session_clock_and_spend_survive_config_reload(tmp_path: Path) -> None:
    """#36 AC: a config hot-reload must not reset ``window: session`` accounting - the
    replacement service keeps the same session_start and still counts pre-reload spend."""
    import os as _os

    repo = tmp_path / "r"
    repo.mkdir()
    _init_repo(repo)
    cfg = repo / "fleet.config.yaml"
    cfg.write_text(
        "budgets:\n  - window: session\n    limit_usd: 0.5\n    enforce: true\n"
        "clients:\n  worker:\n    backend: echo\n"
    )
    wdef = WorkspaceDef("default", repo.resolve(), cfg)
    reg = _durable_registry(wdef, lambda: {"echo": _Echo()})

    old = reg.get()
    old.fleet.usage.record(
        UsageEvent(
            ts=datetime.now(timezone.utc).isoformat(), run_id="seed", backend="echo",
            cost_usd=1.0, source="estimated",
        )
    )
    time.sleep(0.05)  # a (buggy) reset session_start would now exclude the seeded event

    cfg.write_text(cfg.read_text() + "  # unrelated tweak\n")
    _os.utime(cfg, ns=(1, 1))
    new = reg.get()
    assert new is not old
    assert new.session_start == old.session_start  # the session clock did not restart
    status = new.fleet.budget_status()[0]
    assert status.spent_usd == pytest.approx(1.0)  # pre-reload spend still in the window
    with pytest.raises(BudgetExceeded, match="cap"):
        new.run_agent("worker", "over-cap")  # the un-reset window keeps the cap enforced


def test_add_eviction_preserves_runtime(tmp_path: Path) -> None:
    """add()'s cache pop (re-registration) rebuilds the service but keeps the same durable
    runtime - it is keyed by resolved repo path, not by the cache entry."""
    repo_a, repo_b = tmp_path / "a", tmp_path / "b"
    for r in (repo_a, repo_b):
        r.mkdir()
        _init_repo(r)
    reg_file = tmp_path / "workspaces.yaml"
    env = {"MARSHAL_REPO": str(repo_a), "MARSHAL_WORKSPACES_FILE": str(reg_file)}
    reg = WorkspaceRegistry.from_env(env)
    reg.add("beta", repo_b)
    first = reg.get("beta")
    reg.add("beta", repo_b)  # evicts the cached service
    second = reg.get("beta")
    assert second is not first
    assert second.fleet.budget_gate is first.fleet.budget_gate
    assert second.session_start == first.session_start


def test_workspaces_do_not_share_budget_gate(tmp_path: Path) -> None:
    """Two workspaces are two repos with two ledgers - their enforce gates must stay separate."""
    repo_a, repo_b = tmp_path / "a", tmp_path / "b"
    for r in (repo_a, repo_b):
        r.mkdir()
        _init_repo(r)
    defs = [
        WorkspaceDef("default", repo_a.resolve(), repo_a / "fleet.config.yaml"),
        WorkspaceDef("beta", repo_b.resolve(), repo_b / "fleet.config.yaml"),
    ]
    reg = WorkspaceRegistry(defs)  # the default build_service_for builder (no config = 0 clients)
    a, b = reg.get("default"), reg.get("beta")
    assert a.fleet.budget_gate is not b.fleet.budget_gate
    # The default builder binds the registry's own capsule - the durable identity get() reuses.
    assert a.fleet.budget_gate is reg.runtime_for(defs[0]).budget_gate
    assert a.session_start == reg.runtime_for(defs[0]).session_start


def test_prebuilt_service_seeds_durable_runtime(tmp_path: Path) -> None:
    """for_service (the single-repo MCP path): a rebuild after a config edit inherits the
    prebuilt service's gate + session clock instead of forking them."""
    repo = tmp_path / "r"
    repo.mkdir()
    _init_repo(repo)
    svc = _echo_service(repo)
    reg = WorkspaceRegistry.for_service(svc)
    rt = reg.runtime_for(WorkspaceDef("default", Path(svc.repo_root).resolve(), Path(svc.config_path)))
    assert rt.budget_gate is svc.fleet.budget_gate
    assert rt.session_start == svc.fleet.session_start


def test_direct_construction_keeps_private_gate_default(tmp_path: Path) -> None:
    """Library path unchanged: bare Fleets/services get their OWN gate + clock unless injected."""
    repo = tmp_path / "r"
    repo.mkdir()
    _init_repo(repo)
    svc1, svc2 = _echo_service(repo), _echo_service(repo)
    assert svc1.fleet.budget_gate is not svc2.fleet.budget_gate  # default: private per Fleet

    gate = EnforceBudgetGate()
    ts = datetime(2024, 1, 1, tzinfo=timezone.utc)
    cfg = FleetConfig(clients={"worker": ClientConfig(name="worker", backend="echo")})
    svc3 = MarshalService(repo, cfg, backends={"echo": _Echo()}, budget_gate=gate, session_start=ts)
    assert svc3.fleet.budget_gate is gate  # injection is honored end-to-end
    assert svc3.session_start == ts


_MCP_REG_OPT_IN = "MARSHAL_ALLOW_MCP_WORKSPACE_REGISTRATION"


def _registry_app(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> tuple[object, WorkspaceRegistry, Path, Path]:
    """An env-driven registry + MCP app over repo `a` (default), with repo `b` unregistered.

    Returns (app, registry, reg_file, repo_b). The opt-in env var must be set/unset by the caller
    BEFORE this runs - build_app captures it at construction time.
    """
    from marshal_engine.mcp_server import build_app

    repo_a, repo_b = tmp_path / "a", tmp_path / "b"
    for r in (repo_a, repo_b):
        r.mkdir()
        _init_repo(r)
    reg_file = tmp_path / "w.yaml"
    monkeypatch.setenv("MARSHAL_REPO", str(repo_a))
    monkeypatch.setenv("MARSHAL_WORKSPACES_FILE", str(reg_file))
    monkeypatch.delenv("MARSHAL_WORKSPACES", raising=False)
    monkeypatch.delenv("MARSHAL_CONFIG", raising=False)
    reg = WorkspaceRegistry.from_env()
    return build_app(reg), reg, reg_file, repo_b


def test_mcp_add_workspace_refused_by_default(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    pytest.importorskip("mcp")
    import asyncio

    monkeypatch.delenv(_MCP_REG_OPT_IN, raising=False)
    app, reg, reg_file, repo_b = _registry_app(tmp_path, monkeypatch)

    with pytest.raises(Exception, match="marshal workspace add") as exc_info:
        asyncio.run(app.call_tool("add_workspace", {"name": "beta", "path": str(repo_b), "scaffold": True}))
    assert _MCP_REG_OPT_IN in str(exc_info.value)  # the refusal names the server-side opt-in
    assert "beta" not in reg.names()  # nothing registered
    assert not reg_file.exists()  # registry file never written
    assert not (repo_b / "fleet.config.yaml").exists()  # scaffold=true wrote nothing


def test_mcp_add_workspace_opt_in_allows_registration(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    pytest.importorskip("mcp")
    import asyncio

    monkeypatch.setenv(_MCP_REG_OPT_IN, "1")
    app, reg, reg_file, repo_b = _registry_app(tmp_path, monkeypatch)

    structured = asyncio.run(
        app.call_tool("add_workspace", {"name": "beta", "path": str(repo_b), "scaffold": True})
    ).structured_content
    assert "beta" in reg.names()  # registered + hot-reloaded into the live registry
    assert read_workspaces_file(reg_file)[0].get("beta") == str(repo_b.resolve())
    payload = structured.get("result", structured)
    assert payload["scaffolded"] is True  # the full mutating path ran under the opt-in
    assert (repo_b / "fleet.config.yaml").exists()


@pytest.mark.parametrize("value", ["", "0", "false", "true", "yes", " 1"])
def test_mcp_add_workspace_opt_in_fails_closed_for_other_values(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch, value: str
) -> None:
    """Only the exact value "1" enables the tool - never generic string truthiness."""
    pytest.importorskip("mcp")
    import asyncio

    monkeypatch.setenv(_MCP_REG_OPT_IN, value)
    app, reg, reg_file, repo_b = _registry_app(tmp_path, monkeypatch)

    with pytest.raises(Exception, match="disabled by default"):
        asyncio.run(app.call_tool("add_workspace", {"name": "beta", "path": str(repo_b), "scaffold": True}))
    assert "beta" not in reg.names()
    assert not reg_file.exists()
    assert not (repo_b / "fleet.config.yaml").exists()


def test_mcp_add_workspace_denial_precedes_path_validation(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    """A disabled tool must not act as a path-existence oracle: the refusal is the policy
    message, not "path is not an existing directory"."""
    pytest.importorskip("mcp")
    import asyncio

    monkeypatch.delenv(_MCP_REG_OPT_IN, raising=False)
    app, reg, reg_file, _repo_b = _registry_app(tmp_path, monkeypatch)

    with pytest.raises(Exception, match="disabled by default") as exc_info:
        asyncio.run(app.call_tool("add_workspace", {"name": "x", "path": str(tmp_path / "nope")}))
    assert "existing directory" not in str(exc_info.value)
    assert not reg_file.exists()


def test_mcp_add_workspace_concurrent_denials_have_no_side_effects(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    pytest.importorskip("mcp")
    import asyncio

    monkeypatch.delenv(_MCP_REG_OPT_IN, raising=False)
    app, reg, reg_file, repo_b = _registry_app(tmp_path, monkeypatch)

    async def _fan_out() -> list[BaseException | object]:
        calls = [
            app.call_tool("add_workspace", {"name": f"ws{i}", "path": str(repo_b), "scaffold": True})
            for i in range(4)
        ]
        return await asyncio.gather(*calls, return_exceptions=True)

    results = asyncio.run(_fan_out())
    assert len(results) == 4
    for r in results:
        assert isinstance(r, Exception) and "disabled by default" in str(r)  # every call refused
    assert reg.names() == ["default"]  # no registration leaked through the race
    assert not reg_file.exists()
    assert not (repo_b / "fleet.config.yaml").exists()


def test_mcp_list_clients_reflects_config_edit(tmp_path: Path) -> None:
    """The end-to-end shape of the field bug: list_clients over MCP must see a config that was
    added after the workspace was registered, without reconnecting the server."""
    pytest.importorskip("mcp")
    from marshal_engine.mcp_server import build_app

    repo = tmp_path / "r"
    repo.mkdir()
    _init_repo(repo)
    wdef = WorkspaceDef("default", repo.resolve(), repo / "fleet.config.yaml")
    reg = WorkspaceRegistry([wdef], builder=_config_aware_builder())
    app = build_app(reg)

    out = _call(app, "list_clients")
    assert out["clients"] == []
    (repo / "fleet.config.yaml").write_text(_ECHO_CLIENT_YAML)
    out = _call(app, "list_clients")
    assert [c["name"] for c in out["clients"]] == ["worker"]
    assert out["clients"][0]["permission_fidelity"] == "boundary-only"


def test_cli_workspace_add_and_list(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch, capsys: pytest.CaptureFixture[str]
) -> None:
    import json as _json

    from marshal_engine.cli import main

    repo = tmp_path / "r"
    repo.mkdir()
    reg_file = tmp_path / "w.yaml"
    monkeypatch.setenv("MARSHAL_REPO", str(tmp_path))
    monkeypatch.setenv("MARSHAL_WORKSPACES_FILE", str(reg_file))
    monkeypatch.delenv("MARSHAL_WORKSPACES", raising=False)

    assert main(["workspace", "add", "alpha", str(repo)]) == 0
    assert (repo / "fleet.config.yaml").exists()  # scaffolded by default
    assert "registered workspace 'alpha'" in capsys.readouterr().out

    assert main(["workspace", "list", "--json"]) == 0
    rows = _json.loads(capsys.readouterr().out)
    assert {"default", "alpha"} <= {r["name"] for r in rows}


def _two_ws_app(tmp_path: Path) -> tuple[object, WorkspaceRegistry, Path, Path]:
    """Build the MCP app over a real, echo-backed 2-workspace registry (default=a, beta=b)."""
    from marshal_engine.mcp_server import build_app

    repo_a, repo_b = tmp_path / "a", tmp_path / "b"
    for r in (repo_a, repo_b):
        r.mkdir()
        _init_repo(r)
        # On-disk config so doctor (which reloads from config_path) sees the same clients
        # as the prebuilt in-memory FleetConfig.
        (r / "fleet.config.yaml").write_text(_ECHO_CLIENT_YAML)
    defs = [
        WorkspaceDef("default", repo_a.resolve(), repo_a / "fleet.config.yaml"),
        WorkspaceDef("beta", repo_b.resolve(), repo_b / "fleet.config.yaml"),
    ]
    reg = WorkspaceRegistry(defs, prebuilt={"default": _echo_service(repo_a), "beta": _echo_service(repo_b)})
    return build_app(reg), reg, repo_a, repo_b


def _call(app: object, name: str, args: dict[str, object] | None = None) -> object:
    """Call an MCP tool and return its structured payload (unwrapping the {'result': …} envelope)."""
    import asyncio

    result = asyncio.run(app.call_tool(name, args or {}))  # type: ignore[attr-defined]
    structured = result.structured_content
    if isinstance(structured, dict):
        return structured.get("result", structured)
    return structured


def test_mcp_round_trip_run_query_cancel(tmp_path: Path) -> None:
    pytest.importorskip("mcp")
    app, _reg, _a, repo_b = _two_ws_app(tmp_path)

    lc = _call(app, "list_clients", {"workspace": "beta"})
    assert lc["workspace"] == "beta" and [c["name"] for c in lc["clients"]] == ["worker"]
    assert lc["clients"][0]["permission_fidelity"] == "boundary-only"

    doc = _call(app, "doctor", {"workspace": "beta"})
    assert doc["workspace"] == "beta" and isinstance(doc["checks"], list)
    perm_checks = [c for c in doc["checks"] if c["name"] == "permission:echo"]
    assert len(perm_checks) == 1
    assert perm_checks[0]["status"] == "warn"
    assert "boundary-only" in perm_checks[0]["detail"]

    rec = _call(app, "run_agent", {"client": "worker", "goal": "x", "task_id": "t1", "workspace": "beta"})
    assert rec["workspace"] == "beta" and rec["status"] == "exited_clean"
    assert Path(rec["worktree"]).resolve().is_relative_to(repo_b.resolve())
    rid = rec["run_id"]

    got = _call(app, "get_run", {"run_id": rid})  # resolves cross-workspace, no hint
    assert got["workspace"] == "beta" and got["run_id"] == rid

    col = _call(app, "collect_run", {"run_id": rid})
    assert col["workspace"] == "beta" and col["run_id"] == rid

    allruns = _call(app, "status", {})["runs"]  # aggregates across workspaces
    assert any(r["run_id"] == rid and r["workspace"] == "beta" for r in allruns)
    assert _call(app, "status", {"workspace": "default"})["runs"] == []  # nothing ran in default

    us = _call(app, "usage", {"workspace": "beta"})
    assert us["workspace"] == "beta" and us["totals"]["runs"] == 1

    cancelled = _call(app, "cancel_run", {"run_id": rid})  # no-op on a finished run
    assert cancelled["workspace"] == "beta" and cancelled["run_id"] == rid

    cleaned = _call(app, "clean", {"workspace": "beta", "dry_run": True})
    assert cleaned["workspace"] == "beta"
    assert cleaned["orphans_removed"] == []  # the sweep result is part of the MCP shape


def test_mcp_round_trip_many_benchmark_integrate(tmp_path: Path) -> None:
    pytest.importorskip("mcp")
    app, _reg, _a, _b = _two_ws_app(tmp_path)

    rm = _call(app, "run_many", {"jobs": [{"client": "worker", "goal": "g", "task_id": "j1"}], "workspace": "beta"})
    assert rm[0]["workspace"] == "beta" and rm[0]["primary"]["status"] == "exited_clean"

    bench = _call(app, "benchmark", {"goal": "b", "clients": ["worker"], "task_id": "bench1", "workspace": "beta"})
    assert bench["workspace"] == "beta" and bench["task_id"] == "bench1"
    rep = _call(app, "report", {"task_id": "bench1", "workspace": "beta"})
    assert rep["workspace"] == "beta" and len(rep["strategies"]) == 1

    rec = _call(app, "run_agent", {"client": "worker", "goal": "x", "task_id": "t2", "workspace": "beta"})
    integ = _call(app, "integrate", {"run_id": rec["run_id"]})
    # _Echo writes no files, so this run is "empty"; the point is the tool routes + tags correctly.
    assert integ["workspace"] == "beta" and integ["status"] in {"merged", "empty", "conflict", "blocked", "error"}

    lw = _call(app, "list_workflows", {"workspace": "beta"})
    assert lw == {"workflows": [], "errors": {}, "workspace": "beta"}


def test_registry_run_many_mixed_workspaces(tmp_path: Path) -> None:
    """One registry.run_many call fans out to ≥2 workspaces; ledgers stay isolated."""
    repo_a = tmp_path / "a"
    repo_b = tmp_path / "b"
    repo_a.mkdir()
    repo_b.mkdir()
    _init_repo(repo_a)
    _init_repo(repo_b)
    reg = WorkspaceRegistry(
        [
            WorkspaceDef("default", repo_a.resolve(), repo_a / "fleet.config.yaml"),
            WorkspaceDef("beta", repo_b.resolve(), repo_b / "fleet.config.yaml"),
        ],
        prebuilt={"default": _echo_service(repo_a), "beta": _echo_service(repo_b)},
    )
    paired = reg.run_many(
        [
            {"client": "worker", "goal": "in-a", "task_id": "ja", "workspace": "default"},
            {"client": "worker", "goal": "in-b", "task_id": "jb", "workspace": "beta"},
        ],
        max_concurrency=2,
        stagger_s=0,
    )
    assert len(paired) == 2
    assert paired[0][0] == "default" and paired[0][1].primary.status == "exited_clean"
    assert paired[1][0] == "beta" and paired[1][1].primary.status == "exited_clean"
    assert Path(paired[0][1].primary.worktree).resolve().is_relative_to(repo_a.resolve())
    assert Path(paired[1][1].primary.worktree).resolve().is_relative_to(repo_b.resolve())
    # Ledgers stay per-workspace (no shared run state).
    assert {r.run_id for r in FleetState(repo_a / ".marshal" / "runs").list()} == {paired[0][1].primary.run_id}
    assert {r.run_id for r in FleetState(repo_b / ".marshal" / "runs").list()} == {paired[1][1].primary.run_id}


def test_registry_run_many_unknown_workspace_fails_fast(tmp_path: Path) -> None:
    repo = tmp_path / "r"
    repo.mkdir()
    _init_repo(repo)
    reg = WorkspaceRegistry(
        [WorkspaceDef("default", repo.resolve(), repo / "fleet.config.yaml")],
        prebuilt={"default": _echo_service(repo)},
    )
    with pytest.raises(ValueError, match="unknown workspace"):
        reg.run_many(
            [
                {"client": "worker", "goal": "ok", "task_id": "j1"},
                {"client": "worker", "goal": "bad", "task_id": "j2", "workspace": "nope"},
            ],
            stagger_s=0,
        )
    assert FleetState(repo / ".marshal" / "runs").list() == []  # nothing started


def test_registry_run_many_call_level_default(tmp_path: Path) -> None:
    """Jobs without per-job workspace use default_workspace; per-job overrides it."""
    repo_a = tmp_path / "a"
    repo_b = tmp_path / "b"
    repo_a.mkdir()
    repo_b.mkdir()
    _init_repo(repo_a)
    _init_repo(repo_b)
    reg = WorkspaceRegistry(
        [
            WorkspaceDef("default", repo_a.resolve(), repo_a / "fleet.config.yaml"),
            WorkspaceDef("beta", repo_b.resolve(), repo_b / "fleet.config.yaml"),
        ],
        prebuilt={"default": _echo_service(repo_a), "beta": _echo_service(repo_b)},
    )
    paired = reg.run_many(
        [
            {"client": "worker", "goal": "uses-default", "task_id": "j1"},
            {"client": "worker", "goal": "overrides", "task_id": "j2", "workspace": "default"},
        ],
        default_workspace="beta",
        stagger_s=0,
    )
    assert paired[0][0] == "beta"
    assert paired[1][0] == "default"


def test_mcp_run_many_mixed_workspaces(tmp_path: Path) -> None:
    pytest.importorskip("mcp")
    app, _reg, repo_a, repo_b = _two_ws_app(tmp_path)
    rm = _call(
        app,
        "run_many",
        {
            "jobs": [
                {"client": "worker", "goal": "a", "task_id": "ja", "workspace": "default"},
                {"client": "worker", "goal": "b", "task_id": "jb", "workspace": "beta"},
            ],
            "max_concurrency": 2,
        },
    )
    assert len(rm) == 2
    assert rm[0]["workspace"] == "default" and rm[0]["primary"]["status"] == "exited_clean"
    assert rm[1]["workspace"] == "beta" and rm[1]["primary"]["status"] == "exited_clean"
    assert Path(rm[0]["primary"]["worktree"]).resolve().is_relative_to(repo_a.resolve())
    assert Path(rm[1]["primary"]["worktree"]).resolve().is_relative_to(repo_b.resolve())


def test_mcp_run_many_unknown_workspace_errors(tmp_path: Path) -> None:
    pytest.importorskip("mcp")
    app, _reg, _a, _b = _two_ws_app(tmp_path)
    with pytest.raises(Exception, match="unknown workspace"):
        _call(
            app,
            "run_many",
            {"jobs": [{"client": "worker", "goal": "x", "task_id": "j", "workspace": "missing"}]},
        )


def test_cli_workspace_add_bad_path_errors_cleanly(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch, capsys: pytest.CaptureFixture[str]
) -> None:
    from marshal_engine.cli import main

    reg_file = tmp_path / "w.yaml"
    monkeypatch.setenv("MARSHAL_REPO", str(tmp_path))
    monkeypatch.setenv("MARSHAL_WORKSPACES_FILE", str(reg_file))
    missing = tmp_path / "nope"
    # a nonexistent path is a clean error (rc 1), not a traceback - and scaffolds nothing.
    assert main(["workspace", "add", "x", str(missing)]) == 1
    assert "error" in capsys.readouterr().err
    assert not (missing / "fleet.config.yaml").exists()
    assert not reg_file.exists()  # nothing registered


def test_describe_reports_when_a_workspace_last_saw_activity(tmp_path: Path) -> None:
    """Fifteen registered repos are unnavigable by name alone - recency is how anyone finds the one
    they were just working in."""
    a, b = tmp_path / "a", tmp_path / "b"
    a.mkdir()
    b.mkdir()
    for repo in (a, b):
        (repo / "fleet.config.yaml").write_text("clients:\n  w:\n    backend: cursor\n")
    _write_run(a, "r-a")  # only `a` has ever run anything
    defs = [WorkspaceDef("default", a, a / "fleet.config.yaml"),
            WorkspaceDef("beta", b, b / "fleet.config.yaml")]
    rows = {r["name"]: r for r in WorkspaceRegistry(defs, builder=_explode).describe()}
    assert rows["default"]["last_activity_at"] is not None
    assert rows["beta"]["last_activity_at"] is None, "no runs must read as absent, not as a date"
    # Parseable, timezone-aware ISO - a driver should not have to guess the format or the zone.
    parsed = datetime.fromisoformat(rows["default"]["last_activity_at"])
    assert parsed.tzinfo is not None


def test_recency_does_not_parse_run_records(tmp_path: Path) -> None:
    """It is a stat, not a read: `describe()` builds no services and parses no records, and with a
    dozen workspaces a full ledger parse per row would turn a listing into real work. A corrupt
    record must therefore not affect it."""
    a = tmp_path / "a"
    a.mkdir()
    (a / "fleet.config.yaml").write_text("clients:\n  w:\n    backend: cursor\n")
    runs = a / ".marshal" / "runs"
    runs.mkdir(parents=True)
    (runs / "torn.json").write_text("{not json")  # unparseable, but it is still activity
    defs = [WorkspaceDef("default", a, a / "fleet.config.yaml")]
    row = WorkspaceRegistry(defs, builder=_explode).describe()[0]
    assert row["last_activity_at"] is not None


def test_ledger_runs_reports_liveness_without_a_built_service(tmp_path: Path) -> None:
    """REGRESSION: liveness was gated on a cached Fleet, so a fresh server - or any workspace not
    touched this session - reported `null` for a verifiably LIVE agent. Probing a pid needs only
    the record, so it must not depend on whether a service happens to be built. (Reconciliation is
    different: it mutates the ledger and is rightly gated on owning the fleet lock.)"""
    from marshal_engine.fleet import _pid_start_time

    holder = subprocess.Popen([sys.executable, "-c", "import time; time.sleep(60)"])
    try:
        a = tmp_path / "a"
        a.mkdir()
        FleetState(a / ".marshal" / "runs").add(
            RunRecord(
                run_id="live.echo.x", task_id="t", backend="echo", status="running",
                pid=holder.pid, pid_start_time=_pid_start_time(holder.pid),
            )
        )
        # builder=_explode: no service is ever built, which is the whole point.
        reg = WorkspaceRegistry([WorkspaceDef("default", a, a / "c.yaml")], builder=_explode)
        assert [r.agent_alive for _, r in reg.ledger_runs()] == [True]
    finally:
        holder.kill()
        holder.wait()
