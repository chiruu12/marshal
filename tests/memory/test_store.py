"""Tests for the Cognee-backed Marshal Recall memory store (mocked, no network)."""

from __future__ import annotations

import asyncio
import sys
from types import ModuleType
from typing import Any
from unittest.mock import AsyncMock, MagicMock

import pytest

from marshal_engine.memory import CogneeMemory, MemoryConfig
from marshal_engine.memory.store import _DIFF_TRUNCATE, _format_recall, _format_run_document
from marshal_engine.state import RunRecord


class _FakeSearchType:
    GRAPH_COMPLETION = "graph_completion"


def _record(**kw: Any) -> RunRecord:
    base: dict[str, Any] = {
        "run_id": "run-1",
        "task_id": "task-abc",
        "backend": "cursor",
        "status": "succeeded",
        "client": "fast",
        "model": "gpt-4",
        "worktree": "/tmp/repos/my-app",
        "text": "Implemented the feature.",
        "cost_usd": 0.05,
        "duration_ms": 12000,
        "input_tokens": 100,
        "output_tokens": 50,
    }
    base.update(kw)
    return RunRecord(**base)


def _install_fake_cognee(monkeypatch: pytest.MonkeyPatch) -> ModuleType:
    fake = ModuleType("cognee")
    fake.add = AsyncMock()
    fake.cognify = AsyncMock()
    fake.search = AsyncMock(return_value=["Prior run used client fast and succeeded."])
    fake.memify = AsyncMock()
    fake.forget = AsyncMock()

    config = MagicMock()
    config.system_root_directory = MagicMock()
    config.data_root_directory = MagicMock()
    config.set_llm_config = MagicMock()
    config.set_embedding_config = MagicMock()
    fake.config = config

    monkeypatch.setitem(sys.modules, "cognee", fake)

    original_import = __import__

    def _import(
        name: str,
        globals: dict[str, Any] | None = None,
        locals: dict[str, Any] | None = None,
        fromlist: tuple[str, ...] = (),
        level: int = 0,
    ) -> Any:
        if name == "cognee" and fromlist and "SearchType" in fromlist:
            return fake
        if name == "cognee":
            return fake
        return original_import(name, globals, locals, fromlist, level)

    monkeypatch.setattr("builtins.__import__", _import)
    fake.SearchType = _FakeSearchType
    return fake


@pytest.fixture
def enabled_config(tmp_path: Any) -> MemoryConfig:
    return MemoryConfig(enabled=True, data_dir=str(tmp_path / "memory"))


def _run(coro: Any) -> Any:
    return asyncio.run(coro)


def test_remember_adds_and_cognifies(
    enabled_config: MemoryConfig, monkeypatch: pytest.MonkeyPatch
) -> None:
    fake = _install_fake_cognee(monkeypatch)
    mem = CogneeMemory(enabled_config)
    rec = _record()

    _run(mem.remember(rec, diff="diff content", repo="my-repo"))

    fake.add.assert_awaited_once()
    add_kwargs = fake.add.call_args.kwargs
    assert add_kwargs["dataset_name"] == "my-repo"
    assert add_kwargs["node_set"] == [
        "client:fast",
        "status:succeeded",
        "task:task-abc",
        "fleet-run",
    ]
    assert "task-abc" in fake.add.call_args.args[0]
    fake.cognify.assert_awaited_once_with(datasets="my-repo", run_in_background=True)


def test_remember_defaults_repo_from_worktree_basename(
    enabled_config: MemoryConfig, monkeypatch: pytest.MonkeyPatch
) -> None:
    fake = _install_fake_cognee(monkeypatch)
    mem = CogneeMemory(enabled_config)
    _run(mem.remember(_record()))

    assert fake.add.call_args.kwargs["dataset_name"] == "my-app"
    fake.cognify.assert_awaited_with(datasets="my-app", run_in_background=True)


def test_remember_disabled_is_noop(monkeypatch: pytest.MonkeyPatch) -> None:
    fake = _install_fake_cognee(monkeypatch)
    mem = CogneeMemory(MemoryConfig.disabled())
    _run(mem.remember(_record()))
    fake.add.assert_not_called()
    fake.cognify.assert_not_called()


def test_recall_returns_formatted_snippet(
    enabled_config: MemoryConfig, monkeypatch: pytest.MonkeyPatch
) -> None:
    fake = _install_fake_cognee(monkeypatch)
    mem = CogneeMemory(enabled_config)

    out = _run(mem.recall("fix the bug", "my-repo"))

    assert "Prior run" in out
    fake.search.assert_awaited_once()
    call_kwargs = fake.search.call_args.kwargs
    assert call_kwargs["query_text"] == "fix the bug"
    assert call_kwargs["datasets"] == "my-repo"
    assert call_kwargs["top_k"] == enabled_config.recall_top_k
    assert call_kwargs["query_type"] == _FakeSearchType.GRAPH_COMPLETION


def test_recall_empty_results(
    enabled_config: MemoryConfig, monkeypatch: pytest.MonkeyPatch
) -> None:
    fake = _install_fake_cognee(monkeypatch)
    fake.search = AsyncMock(return_value=[])
    mem = CogneeMemory(enabled_config)
    assert _run(mem.recall("goal", "repo")) == ""


def test_recall_disabled_returns_empty(monkeypatch: pytest.MonkeyPatch) -> None:
    fake = _install_fake_cognee(monkeypatch)
    mem = CogneeMemory(MemoryConfig.disabled())
    assert _run(mem.recall("goal", "repo")) == ""
    fake.search.assert_not_called()


def test_improve_calls_memify(
    enabled_config: MemoryConfig, monkeypatch: pytest.MonkeyPatch
) -> None:
    fake = _install_fake_cognee(monkeypatch)
    mem = CogneeMemory(enabled_config)
    _run(mem.improve("my-repo"))
    fake.memify.assert_awaited_once_with(dataset="my-repo")


def test_forget_dataset_and_everything(
    enabled_config: MemoryConfig, monkeypatch: pytest.MonkeyPatch
) -> None:
    fake = _install_fake_cognee(monkeypatch)
    mem = CogneeMemory(enabled_config)
    _run(mem.forget("my-repo"))
    fake.forget.assert_awaited_with(dataset="my-repo")
    fake.forget.reset_mock()
    _run(mem.forget(everything=True))
    fake.forget.assert_awaited_with(everything=True)


def test_improve_forget_disabled_are_noops(monkeypatch: pytest.MonkeyPatch) -> None:
    fake = _install_fake_cognee(monkeypatch)
    mem = CogneeMemory(MemoryConfig.disabled())
    _run(mem.improve("repo"))
    _run(mem.forget("repo"))
    fake.memify.assert_not_called()
    fake.forget.assert_not_called()


def test_cognee_exception_in_remember_is_swallowed(
    enabled_config: MemoryConfig, monkeypatch: pytest.MonkeyPatch, caplog: pytest.LogCaptureFixture
) -> None:
    fake = _install_fake_cognee(monkeypatch)
    fake.add = AsyncMock(side_effect=RuntimeError("boom"))
    mem = CogneeMemory(enabled_config)
    with caplog.at_level("ERROR"):
        _run(mem.remember(_record()))  # must not raise
    assert "remember failed" in caplog.text


def test_cognee_exception_in_recall_returns_empty(
    enabled_config: MemoryConfig, monkeypatch: pytest.MonkeyPatch, caplog: pytest.LogCaptureFixture
) -> None:
    fake = _install_fake_cognee(monkeypatch)
    fake.search = AsyncMock(side_effect=RuntimeError("boom"))
    mem = CogneeMemory(enabled_config)
    with caplog.at_level("ERROR"):
        out = _run(mem.recall("goal", "repo"))
    assert out == ""
    assert "recall failed" in caplog.text


def test_format_run_document_includes_fields_and_truncates_diff() -> None:
    long_diff = "x" * 5000
    doc = _format_run_document(_record(), diff=long_diff)
    assert "task-abc" in doc
    assert "fast" in doc
    assert "succeeded" in doc
    assert "Implemented the feature." in doc
    diff_section = doc.split("## Files changed", 1)[1]
    assert len(diff_section) <= _DIFF_TRUNCATE + 20
    assert diff_section.strip().endswith("...")


def test_format_recall_respects_max_chars() -> None:
    results = ["a" * 800, "b" * 800]
    out = _format_recall(results, max_chars=1200)
    assert len(out) <= 1200
    assert out.endswith("...")


def test_recall_sync_runs_without_running_loop(
    enabled_config: MemoryConfig, monkeypatch: pytest.MonkeyPatch
) -> None:
    _install_fake_cognee(monkeypatch)
    mem = CogneeMemory(enabled_config)
    assert "Prior run" in mem.recall_sync("goal", "repo")


def test_recall_sync_with_running_loop(
    enabled_config: MemoryConfig, monkeypatch: pytest.MonkeyPatch
) -> None:
    _install_fake_cognee(monkeypatch)
    mem = CogneeMemory(enabled_config)

    async def _run() -> str:
        return mem.recall_sync("goal", "repo")

    out = asyncio.run(_run())
    assert "Prior run" in out


def test_memory_config_disabled_factory() -> None:
    cfg = MemoryConfig.disabled()
    assert cfg.enabled is False
    assert cfg.recall_enabled is False
    assert cfg.remember_enabled is False
