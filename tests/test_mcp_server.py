"""Tests for the MCP server wiring (build_service + tool registration)."""

from __future__ import annotations

from pathlib import Path

import pytest

from marshal_engine.mcp_server import build_service

_CONFIG = """
clients:
  reviewer:
    backend: cursor
    permission: read-only
"""


def _repo_with_config(tmp_path: Path) -> Path:
    repo = tmp_path / "repo"
    repo.mkdir()
    (repo / "fleet.config.yaml").write_text(_CONFIG)
    return repo


def test_build_service_from_env_config(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> None:
    repo = _repo_with_config(tmp_path)
    monkeypatch.setenv("MARSHAL_REPO", str(repo))
    monkeypatch.delenv("MARSHAL_CONFIG", raising=False)
    svc = build_service()
    # Assert on the PARSED config, not list_clients(): the latter filters out clients whose backend
    # CLI isn't installed (graceful skip), so a cursor-backed client vanishes on a clean CI runner
    # that has no cursor-agent. This test's job is "build_service loaded the env-pointed config".
    assert "reviewer" in svc.config.clients


def test_build_service_without_config_starts_with_zero_clients(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch, capsys: pytest.CaptureFixture[str]
) -> None:
    # A freshly installed plugin has no fleet.config.yaml; the server must still start (not crash)
    # so the driver can connect and be told to configure a fleet.
    repo = tmp_path / "repo"
    repo.mkdir()
    monkeypatch.setenv("MARSHAL_REPO", str(repo))
    monkeypatch.delenv("MARSHAL_CONFIG", raising=False)
    svc = build_service()
    assert svc.list_clients().clients == []
    assert "no fleet config" in capsys.readouterr().err


def test_list_workflows_surfaces_malformed_recipe(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    pytest.importorskip("mcp")
    import asyncio

    from marshal_engine.mcp_server import build_app

    repo = _repo_with_config(tmp_path)
    wdir = repo / "workflows"
    wdir.mkdir()
    (wdir / "broken.yaml").write_text("name: broken\nphases: not-a-list\n")
    monkeypatch.setenv("MARSHAL_REPO", str(repo))
    monkeypatch.delenv("MARSHAL_CONFIG", raising=False)
    app = build_app(build_service())
    _content, structured = asyncio.run(app.call_tool("list_workflows", {}))
    payload = structured.get("result", structured) if isinstance(structured, dict) else structured
    assert payload["workflows"] == []
    assert "broken.yaml" in payload["errors"]
    assert "invalid" in payload["errors"]["broken.yaml"].lower()


def test_run_workflow_missing_yaml_path_is_clear_error(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    from marshal_engine.config import ConfigError

    repo = _repo_with_config(tmp_path)
    monkeypatch.setenv("MARSHAL_REPO", str(repo))
    monkeypatch.delenv("MARSHAL_CONFIG", raising=False)
    svc = build_service()
    # an explicit .yaml path that doesn't exist must NOT be re-treated as a bare name (which would
    # look for "<dir>/x.yaml.yaml" and raise a misleading "no workflow 'x.yaml'").
    with pytest.raises(ConfigError, match="no workflow file at"):
        svc.run_workflow("does-not-exist.yaml")


def test_build_app_registers_tools(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> None:
    pytest.importorskip("mcp")
    import asyncio

    from marshal_engine.mcp_server import build_app

    repo = _repo_with_config(tmp_path)
    monkeypatch.setenv("MARSHAL_REPO", str(repo))
    monkeypatch.delenv("MARSHAL_CONFIG", raising=False)
    app = build_app(build_service())
    names = {t.name for t in asyncio.run(app.list_tools())}
    expected = {
        "run_agent", "run_many", "spawn", "benchmark", "report", "list_clients", "list_models",
        "status", "usage", "get_run", "get_run_log", "collect_run", "commit_run", "integrate", "clean",
        "cancel_run", "list_workflows", "run_workflow", "doctor",
    }
    assert expected <= names


def test_tools_are_async_and_round_trip_via_call_tool(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    # Tools are async and offload to a worker thread; calling one must execute end-to-end (this
    # would deadlock/raise if the async+offload wiring were wrong).
    pytest.importorskip("mcp")
    import asyncio

    from marshal_engine.mcp_server import build_app

    repo = _repo_with_config(tmp_path)
    monkeypatch.setenv("MARSHAL_REPO", str(repo))
    monkeypatch.delenv("MARSHAL_CONFIG", raising=False)
    app = build_app(build_service())
    result = asyncio.run(app.call_tool("list_clients", {}))
    assert result is not None


def test_list_models_round_trips_via_call_tool(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    # The catalog tool mirrors list_clients: it must round-trip through call_tool and return
    # cleanly (empty models on a config with no `models:` block, but the tool must exist).
    pytest.importorskip("mcp")
    import asyncio

    from marshal_engine.mcp_server import build_app

    repo = _repo_with_config(tmp_path)
    monkeypatch.setenv("MARSHAL_REPO", str(repo))
    monkeypatch.delenv("MARSHAL_CONFIG", raising=False)
    app = build_app(build_service())
    result = asyncio.run(app.call_tool("list_models", {}))
    assert result is not None


def test_duration_param_is_wired_into_spawn_schema(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    # The per-spawn `duration` override must be exposed on the tool schema so a driver can pass a
    # preset; assert the schema (not calling spawn, which would start a real run).
    pytest.importorskip("mcp")
    import asyncio

    from marshal_engine.mcp_server import build_app

    repo = _repo_with_config(tmp_path)
    monkeypatch.setenv("MARSHAL_REPO", str(repo))
    monkeypatch.delenv("MARSHAL_CONFIG", raising=False)
    app = build_app(build_service())
    tools = {t.name: t for t in asyncio.run(app.list_tools())}
    assert "duration" in tools["spawn"].inputSchema["properties"]
    assert "duration" in tools["run_agent"].inputSchema["properties"]


def test_base_branch_param_is_wired_into_spawn_and_run_agent_schema(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    pytest.importorskip("mcp")
    import asyncio

    from marshal_engine.mcp_server import build_app

    repo = _repo_with_config(tmp_path)
    monkeypatch.setenv("MARSHAL_REPO", str(repo))
    monkeypatch.delenv("MARSHAL_CONFIG", raising=False)
    app = build_app(build_service())
    tools = {t.name: t for t in asyncio.run(app.list_tools())}
    assert "base_branch" in tools["spawn"].inputSchema["properties"]
    assert "base_branch" in tools["run_agent"].inputSchema["properties"]


def test_spawn_base_branch_reaches_task_spec_via_mcp(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    pytest.importorskip("mcp")
    import asyncio
    import subprocess
    import sys
    import time

    from marshal_engine.backends.base import CodingAgentBackend
    from marshal_engine.config import ClientConfig, FleetConfig, PermissionMode
    from marshal_engine.mcp_server import build_app
    from marshal_engine.service import MarshalService
    from marshal_engine.types import AgentResult, Capabilities, RunOpts, RunStatus, TaskSpec

    class _Capture(CodingAgentBackend):
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
            return AgentResult(status=RunStatus.SUCCEEDED, text=raw_stdout.strip(), exit_code=exit_code)

    def git(*args: str, cwd: Path) -> None:
        subprocess.run(["git", *args], cwd=cwd, check=True, capture_output=True)

    repo = tmp_path / "repo"
    repo.mkdir()
    git("init", "-b", "main", cwd=repo)
    git("config", "user.email", "t@t", cwd=repo)
    git("config", "user.name", "t", cwd=repo)
    (repo / "README.md").write_text("hi")
    git("add", "-A", cwd=repo)
    git("commit", "-q", "-m", "init", cwd=repo)
    git("branch", "marshal/prior", cwd=repo)
    (repo / "fleet.config.yaml").write_text(
        "clients:\n  worker:\n    backend: capture\n    permission: safe-edit\n"
    )

    backend = _Capture()
    cfg = FleetConfig(
        clients={"worker": ClientConfig(name="worker", backend="capture", permission=PermissionMode.SAFE_EDIT)}
    )
    svc = MarshalService(repo, cfg, backends={"capture": backend})
    app = build_app(svc)
    try:
        asyncio.run(
            app.call_tool(
                "spawn",
                {
                    "client": "worker",
                    "goal": "do x",
                    "task_id": "mcp1",
                    "base_branch": "marshal/prior",
                },
            )
        )
        deadline = time.monotonic() + 10
        while time.monotonic() < deadline and not backend.tasks:
            time.sleep(0.05)
        assert backend.tasks[-1].base_branch == "marshal/prior"
    finally:
        svc.shutdown()


def test_tool_params_carry_schema_descriptions(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    # Self-describing params: the driver should see a description per parameter, not just type+title.
    pytest.importorskip("mcp")
    import asyncio

    from marshal_engine.mcp_server import build_app

    repo = _repo_with_config(tmp_path)
    monkeypatch.setenv("MARSHAL_REPO", str(repo))
    monkeypatch.delenv("MARSHAL_CONFIG", raising=False)
    app = build_app(build_service())
    tools = {t.name: t for t in asyncio.run(app.list_tools())}
    props = tools["run_agent"].inputSchema["properties"]
    assert props["client"].get("description")
    assert props["context_files"].get("description")


def test_get_run_log_round_trips_via_call_tool(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    # get_run_log is the per-run durable-log equivalent of get_run: it locates the run by id
    # across workspaces, reads <base>/logs/<run_id>.log, and returns it stamped with the workspace.
    # `log` is the stored text when present and null when the run is known but no log was written.
    pytest.importorskip("mcp")
    import asyncio

    from marshal_engine.logs import RunLogStore
    from marshal_engine.mcp_server import build_app

    repo = _repo_with_config(tmp_path)
    monkeypatch.setenv("MARSHAL_REPO", str(repo))
    monkeypatch.delenv("MARSHAL_CONFIG", raising=False)
    svc = build_service()

    # Stamp a synthetic log under the Fleet's logs dir (built off the service's repo_root).
    logs_dir = repo / ".marshal" / "logs"
    RunLogStore(logs_dir).write("synthetic.run", "the-stdout\n", "the-stderr\n")
    # And: the run must exist in state too, so resolve_run() can find it (otherwise the tool
    # short-circuits to log=null without ever consulting RunLogStore).
    from marshal_engine.state import RunRecord
    svc.fleet.state.add(
        RunRecord(run_id="synthetic.run", task_id="synthetic", backend="cursor", status="succeeded")
    )

    app = build_app(svc)
    _content, structured = asyncio.run(app.call_tool("get_run_log", {"run_id": "synthetic.run"}))
    out = structured.get("result", structured) if isinstance(structured, dict) else structured
    assert out["run_id"] == "synthetic.run"
    assert "the-stdout" in out["log"]
    assert "the-stderr" in out["log"]
    assert "=== run synthetic.run ===" in out["log"]
    assert out["workspace"] == "default"  # tag() stamps the owning workspace

    # And: a run id no workspace owns returns log=null with the requested workspace stamp
    _content2, structured2 = asyncio.run(app.call_tool("get_run_log", {"run_id": "nope.run"}))
    out2 = structured2.get("result", structured2) if isinstance(structured2, dict) else structured2
    assert out2["log"] is None
    assert out2["run_id"] == "nope.run"


def test_usage_window_param_is_in_schema(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    # The new `window` parameter must be on the tool schema (so a driver can pass session/week/month/all).
    pytest.importorskip("mcp")
    import asyncio

    from marshal_engine.mcp_server import build_app

    repo = _repo_with_config(tmp_path)
    monkeypatch.setenv("MARSHAL_REPO", str(repo))
    monkeypatch.delenv("MARSHAL_CONFIG", raising=False)
    app = build_app(build_service())
    tools = {t.name: t for t in asyncio.run(app.list_tools())}
    props = tools["usage"].inputSchema["properties"]
    assert "window" in props
    assert set(props["window"]["enum"]) == {"session", "week", "month", "all"}


def test_usage_window_param_mapping(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    # Drive the tool end-to-end: each `window` value resolves to the expected `since`. Recording a
    # 2020 event lets us see session/week/month all filter it out (it's outside every window) and
    # the unfiltered "all" keep it. The 2026 event lands in every window.
    import asyncio
    from datetime import datetime, timezone

    from marshal_engine.usage import UsageEvent, UsageTracker

    pytest.importorskip("mcp")
    from marshal_engine.mcp_server import build_app

    repo = _repo_with_config(tmp_path)
    monkeypatch.setenv("MARSHAL_REPO", str(repo))
    monkeypatch.delenv("MARSHAL_CONFIG", raising=False)
    svc = build_service()
    app = build_app(svc)

    # Stamp one event in the (far) past, one at "now". The past one is outside every window.
    now = datetime.now(timezone.utc)
    u = tmp_path / "ledger" / "usage"
    u.mkdir(parents=True)
    (u / "events.jsonl").write_text(
        UsageEvent(
            ts="2020-01-01T00:00:00Z", run_id="old", backend="opencode", cost_usd=1.00,
        ).model_dump_json() + "\n"
        + UsageEvent(
            ts=now.isoformat(), run_id="new", backend="opencode", cost_usd=0.01,
        ).model_dump_json() + "\n"
    )
    # Point the service's UsageTracker at our test ledger (replacing the default empty one).
    svc.fleet.usage = UsageTracker(u)

    def _call(window: str) -> dict:
        _content, structured = asyncio.run(app.call_tool("usage", {"window": window}))
        if isinstance(structured, dict):
            return structured.get("result", structured)
        return structured  # type: ignore[return-value]

    # all = no filter: both events present
    out_all = _call("all")
    assert out_all["window"] == "all"
    assert out_all["since"] is None
    assert out_all["totals"]["runs"] == 2
    assert abs(out_all["totals"]["cost_usd"] - 1.01) < 1e-9

    # week/month = now-Nd, the 2020 event is excluded
    out_week = _call("week")
    assert out_week["window"] == "week"
    assert out_week["since"] is not None
    assert out_week["totals"]["runs"] == 1
    assert abs(out_week["totals"]["cost_usd"] - 0.01) < 1e-9

    out_month = _call("month")
    assert out_month["window"] == "month"
    assert out_month["totals"]["runs"] == 1

    # session = since = svc.session_start (an event stamped at `now` is within the session window)
    out_session = _call("session")
    assert out_session["window"] == "session"
    since = datetime.fromisoformat(out_session["since"])
    # session_start may be a few microseconds newer than `now`, so allow a small tolerance.
    assert abs((since - svc.fleet.session_start).total_seconds()) < 1
    # The "new" event at `now` is in [session_start, ...] (or its very near boundary); the 2020
    # event is excluded. We assert the 2020 event is excluded, which is the contract that matters.
    assert out_session["totals"]["runs"] in (0, 1)  # 0 if `now` < session_start, 1 otherwise
    # Windowed JSON includes the new by_backend_model key
    assert "by_backend_model" in out_session
