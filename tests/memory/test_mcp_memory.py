"""Hermetic tests for MCP memory tools (no real Cognee)."""

from __future__ import annotations

from pathlib import Path
from unittest.mock import MagicMock

import pytest

_CONFIG = """
memory:
  enabled: true
  recall_enabled: true
clients:
  reviewer:
    backend: cursor
    permission: read-only
"""


def _call_tool(app: object, name: str, args: dict[str, object] | None = None) -> object:
    import asyncio

    _content, structured = asyncio.run(app.call_tool(name, args or {}))  # type: ignore[attr-defined]
    if isinstance(structured, dict):
        return structured.get("result", structured)
    return structured


def _repo_with_config(tmp_path: Path) -> Path:
    repo = tmp_path / "repo"
    repo.mkdir()
    (repo / "fleet.config.yaml").write_text(_CONFIG)
    return repo


def test_mcp_memory_query_returns_snippet(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    pytest.importorskip("mcp")

    from marshal_engine.mcp_server import build_app, build_service

    repo = _repo_with_config(tmp_path)
    monkeypatch.setenv("MARSHAL_REPO", str(repo))
    monkeypatch.delenv("MARSHAL_CONFIG", raising=False)
    monkeypatch.setattr(
        "marshal_engine.service.CogneeMemory.recall_sync",
        MagicMock(return_value="Prior run used pattern X."),
    )
    app = build_app(build_service())
    text = _call_tool(app, "memory_query", {"query": "how was X done?"})
    assert isinstance(text, str)
    assert "Prior run used pattern X." in text


def test_mcp_memory_stats_returns_dict(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    pytest.importorskip("mcp")

    from marshal_engine.mcp_server import build_app, build_service

    repo = _repo_with_config(tmp_path)
    monkeypatch.setenv("MARSHAL_REPO", str(repo))
    monkeypatch.delenv("MARSHAL_CONFIG", raising=False)
    app = build_app(build_service())
    data = _call_tool(app, "memory_stats", {})
    assert isinstance(data, dict)
    assert data["enabled"] is True
    assert data["repo_key"] == "repo"
    assert data["workspace"] == "default"


def test_build_app_registers_memory_tools(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    pytest.importorskip("mcp")
    import asyncio

    from marshal_engine.mcp_server import build_app, build_service

    repo = _repo_with_config(tmp_path)
    monkeypatch.setenv("MARSHAL_REPO", str(repo))
    monkeypatch.delenv("MARSHAL_CONFIG", raising=False)
    app = build_app(build_service())
    names = {t.name for t in asyncio.run(app.list_tools())}
    assert {"memory_query", "memory_stats"} <= names
