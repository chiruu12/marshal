"""Tests for the multi-workspace registry (env resolution, lazy cache, run-id addressing, the
process-wide concurrency gate) and its routing through the MCP layer."""

from __future__ import annotations

import subprocess
import sys
import threading
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
from marshal_engine.config import ClientConfig, FleetConfig
from marshal_engine.service import MarshalService
from marshal_engine.state import FleetState, RunRecord
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
            status=RunStatus.SUCCEEDED if exit_code == 0 else RunStatus.FAILED,
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
        return AgentResult(status=RunStatus.SUCCEEDED, text="ok", exit_code=exit_code)

    def run(self, task: TaskSpec, opts: RunOpts) -> AgentResult:  # type: ignore[override]
        with self._lock:
            self.active += 1
            self.peak = max(self.peak, self.active)
        time.sleep(0.25)
        with self._lock:
            self.active -= 1
        return AgentResult(status=RunStatus.SUCCEEDED, text="ok", exit_code=0)


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
        RunRecord(run_id=run_id, task_id=task_id, backend="echo", status="succeeded")
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
    assert all(r.status == "succeeded" for r in recs)
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
    assert rows["beta"]["configured"] is False
    assert rows["beta"]["client_count"] == 0
    assert rows["beta"]["default"] is False


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
    assert tools["run_agent"].inputSchema["properties"]["workspace"].get("description")
    assert tools["get_run"].inputSchema["properties"]["workspace"].get("description")


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
    assert unknown is not None  # FastMCP wraps even a None tool-return in a (content, ...) envelope


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
    assert got is not None and got.status == "succeeded"


def test_mcp_add_workspace_tool(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> None:
    pytest.importorskip("mcp")
    import asyncio

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
    app = build_app(reg)

    asyncio.run(app.call_tool("add_workspace", {"name": "beta", "path": str(repo_b)}))
    assert "beta" in reg.names()  # registered + hot-reloaded into the live registry
    assert read_workspaces_file(reg_file)[0].get("beta") == str(repo_b.resolve())


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
    defs = [
        WorkspaceDef("default", repo_a.resolve(), repo_a / "fleet.config.yaml"),
        WorkspaceDef("beta", repo_b.resolve(), repo_b / "fleet.config.yaml"),
    ]
    reg = WorkspaceRegistry(defs, prebuilt={"default": _echo_service(repo_a), "beta": _echo_service(repo_b)})
    return build_app(reg), reg, repo_a, repo_b


def _call(app: object, name: str, args: dict[str, object] | None = None) -> object:
    """Call an MCP tool and return its structured payload (unwrapping FastMCP's {'result': …})."""
    import asyncio

    _content, structured = asyncio.run(app.call_tool(name, args or {}))  # type: ignore[attr-defined]
    if isinstance(structured, dict):
        return structured.get("result", structured)
    return structured


def test_mcp_round_trip_run_query_cancel(tmp_path: Path) -> None:
    pytest.importorskip("mcp")
    app, _reg, _a, repo_b = _two_ws_app(tmp_path)

    lc = _call(app, "list_clients", {"workspace": "beta"})
    assert lc["workspace"] == "beta" and [c["name"] for c in lc["clients"]] == ["worker"]

    doc = _call(app, "doctor", {"workspace": "beta"})
    assert doc["workspace"] == "beta" and isinstance(doc["checks"], list)

    rec = _call(app, "run_agent", {"client": "worker", "goal": "x", "task_id": "t1", "workspace": "beta"})
    assert rec["workspace"] == "beta" and rec["status"] == "succeeded"
    assert Path(rec["worktree"]).resolve().is_relative_to(repo_b.resolve())
    rid = rec["run_id"]

    got = _call(app, "get_run", {"run_id": rid})  # resolves cross-workspace, no hint
    assert got["workspace"] == "beta" and got["run_id"] == rid

    col = _call(app, "collect_run", {"run_id": rid})
    assert col["workspace"] == "beta" and col["run_id"] == rid

    allruns = _call(app, "status", {})  # aggregates across workspaces
    assert any(r["run_id"] == rid and r["workspace"] == "beta" for r in allruns)
    assert _call(app, "status", {"workspace": "default"}) == []  # nothing ran in default

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
    assert rm[0]["workspace"] == "beta" and rm[0]["status"] == "succeeded"

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
    assert paired[0][0] == "default" and paired[0][1].status == "succeeded"
    assert paired[1][0] == "beta" and paired[1][1].status == "succeeded"
    assert Path(paired[0][1].worktree).resolve().is_relative_to(repo_a.resolve())
    assert Path(paired[1][1].worktree).resolve().is_relative_to(repo_b.resolve())
    # Ledgers stay per-workspace (no shared run state).
    assert {r.run_id for r in FleetState(repo_a / ".marshal" / "runs").list()} == {paired[0][1].run_id}
    assert {r.run_id for r in FleetState(repo_b / ".marshal" / "runs").list()} == {paired[1][1].run_id}


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
    assert rm[0]["workspace"] == "default" and rm[0]["status"] == "succeeded"
    assert rm[1]["workspace"] == "beta" and rm[1]["status"] == "succeeded"
    assert Path(rm[0]["worktree"]).resolve().is_relative_to(repo_a.resolve())
    assert Path(rm[1]["worktree"]).resolve().is_relative_to(repo_b.resolve())


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
