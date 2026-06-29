"""Hermetic tests for `marshal memory` CLI commands (no real Cognee)."""

from __future__ import annotations

import json
from pathlib import Path
from unittest.mock import MagicMock

import pytest

from marshal_engine import cli


def _repo(tmp_path: Path, yaml: str) -> Path:
    repo = tmp_path / "my-project"
    repo.mkdir()
    (repo / "fleet.config.yaml").write_text(yaml)
    return repo


def _mem_args(repo: Path, *extra: str) -> list[str]:
    return ["memory", *extra, "--repo", str(repo), "--config", str(repo / "fleet.config.yaml")]


_ENABLED = "memory:\n  enabled: true\n  recall_enabled: true\n"
_DISABLED = "memory:\n  enabled: false\n"


def test_memory_query_prints_snippet(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch, capsys: pytest.CaptureFixture[str]
) -> None:
    repo = _repo(tmp_path, _ENABLED)
    monkeypatch.setattr(
        "marshal_engine.service.CogneeMemory.recall_sync",
        MagicMock(return_value="Prior run fixed auth."),
    )
    assert cli.main(_mem_args(repo, "query", "fix auth")) == 0
    assert "Prior run fixed auth." in capsys.readouterr()[0]


def test_memory_query_disabled_hint(
    tmp_path: Path, capsys: pytest.CaptureFixture[str]
) -> None:
    repo = _repo(tmp_path, _DISABLED)
    assert cli.main(_mem_args(repo, "query", "fix auth")) == 0
    out = capsys.readouterr()[0]
    assert "memory is disabled" in out
    assert "fleet.config.yaml" in out


def test_memory_query_empty_prints_placeholder(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch, capsys: pytest.CaptureFixture[str]
) -> None:
    repo = _repo(tmp_path, _ENABLED)
    monkeypatch.setattr(
        "marshal_engine.service.CogneeMemory.recall_sync",
        MagicMock(return_value=""),
    )
    assert cli.main(_mem_args(repo, "query", "nothing here")) == 0
    assert "(no relevant memory)" in capsys.readouterr()[0]


def test_memory_stats_prints_config(
    tmp_path: Path, capsys: pytest.CaptureFixture[str]
) -> None:
    repo = _repo(tmp_path, _ENABLED + "  data_dir: /tmp/custom-memory\n")
    assert cli.main(_mem_args(repo, "stats")) == 0
    out = capsys.readouterr()[0]
    assert "enabled=True" in out
    assert "repo_key=my-project" in out
    assert "data_dir=/tmp/custom-memory" in out
    assert "recall_top_k=5" in out


def test_memory_stats_json(
    tmp_path: Path, capsys: pytest.CaptureFixture[str]
) -> None:
    repo = _repo(tmp_path, _ENABLED)
    assert cli.main(_mem_args(repo, "stats", "--json")) == 0
    data = json.loads(capsys.readouterr()[0])
    assert data["enabled"] is True
    assert data["repo_key"] == "my-project"


def test_memory_improve_calls_service(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch, capsys: pytest.CaptureFixture[str]
) -> None:
    repo = _repo(tmp_path, _ENABLED)
    improve = MagicMock()
    monkeypatch.setattr("marshal_engine.service.MarshalService.memory_improve", improve)
    assert cli.main(_mem_args(repo, "improve")) == 0
    improve.assert_called_once()
    assert "improved memory dataset 'my-project'" in capsys.readouterr()[0]


def test_memory_forget_all_calls_service(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch, capsys: pytest.CaptureFixture[str]
) -> None:
    repo = _repo(tmp_path, _ENABLED)
    forget = MagicMock()
    monkeypatch.setattr("marshal_engine.service.MarshalService.memory_forget", forget)
    assert cli.main(_mem_args(repo, "forget", "--all")) == 0
    forget.assert_called_once_with(all=True)
    assert "forgot all memory datasets" in capsys.readouterr()[0]


def test_memory_forget_repo_calls_service(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch, capsys: pytest.CaptureFixture[str]
) -> None:
    repo = _repo(tmp_path, _ENABLED)
    forget = MagicMock()
    monkeypatch.setattr("marshal_engine.service.MarshalService.memory_forget", forget)
    assert cli.main(_mem_args(repo, "forget")) == 0
    forget.assert_called_once_with()
    assert "forgot memory dataset 'my-project'" in capsys.readouterr()[0]
