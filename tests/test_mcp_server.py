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
    names = [c.name for c in svc.list_clients()]
    assert "reviewer" in names


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
    assert svc.list_clients() == []
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
        "run_agent", "run_many", "spawn", "benchmark", "report", "list_clients", "status", "usage",
        "get_run", "collect_run", "integrate", "cancel_run", "list_workflows", "run_workflow",
    }
    assert expected <= names
