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
        "status", "usage", "get_run", "collect_run", "commit_run", "integrate", "clean",
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
