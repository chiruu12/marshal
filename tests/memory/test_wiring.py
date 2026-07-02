"""Hermetic tests for memory wiring into MarshalService and Fleet (no real Cognee)."""

from __future__ import annotations

import subprocess
import sys
from pathlib import Path
from unittest.mock import MagicMock

import pytest

from marshal_engine.backends.base import CodingAgentBackend
from marshal_engine.config import ClientConfig, FleetConfig, FleetContext
from marshal_engine.memory import MemoryConfig
from marshal_engine.service import MarshalService, _WORKER_PREAMBLE
from marshal_engine.state import RunRecord
from marshal_engine.types import Capabilities, PermissionMode, RunOpts, RunStatus, TaskSpec
from marshal_engine.types import AgentResult


class _Stub(CodingAgentBackend):
    name = "stub"
    binary = "python"
    capabilities = Capabilities()

    def check_available(self) -> bool:
        return True

    def build_invocation(self, task: TaskSpec, opts: RunOpts) -> list[str]:
        return [sys.executable, "-c", "print('ok')"]

    def map_permission(self, mode: PermissionMode) -> list[str]:
        return []

    def parse_output(self, raw_stdout: str, raw_stderr: str, exit_code: int) -> AgentResult:
        return AgentResult(status=RunStatus.SUCCEEDED, text=raw_stdout.strip(), exit_code=exit_code)


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
    r = tmp_path / "my-project"
    r.mkdir()
    _init_repo(r)
    return r


def _base_cfg(*, memory: MemoryConfig | None = None, worker: str | None = None) -> FleetConfig:
    return FleetConfig(
        clients={"worker": ClientConfig(name="worker", backend="stub", permission=PermissionMode.SAFE_EDIT)},
        context=FleetContext(worker=worker) if worker else FleetContext(),
        memory=memory or MemoryConfig(),
    )


def test_compose_goal_unchanged_when_memory_disabled(repo: Path) -> None:
    svc = MarshalService(repo, _base_cfg(), backends={"stub": _Stub()})
    composed = svc._compose_goal("fix the bug")

    assert "## Memory from past runs" not in composed
    assert composed == f"{_WORKER_PREAMBLE}\n\nfix the bug"


def test_compose_goal_injects_recall_between_worker_context_and_goal(
    repo: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    snippet = "Prior run fixed auth in src/auth.py."
    recall = MagicMock(return_value=snippet)
    monkeypatch.setattr("marshal_engine.service.CogneeMemory.recall_sync", recall)

    cfg = _base_cfg(memory=MemoryConfig(enabled=True), worker="Always add tests.")
    svc = MarshalService(repo, cfg, backends={"stub": _Stub()})
    composed = svc._compose_goal("refactor parser")

    assert "## Memory from past runs" in composed
    assert snippet in composed
    assert composed.index("Always add tests.") < composed.index("## Memory from past runs")
    assert composed.index("## Memory from past runs") < composed.index("refactor parser")
    recall.assert_called_once_with("refactor parser", "my-project")


def test_compose_goal_empty_recall_omits_memory_segment(repo: Path, monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setattr(
        "marshal_engine.service.CogneeMemory.recall_sync",
        MagicMock(return_value=""),
    )

    cfg = _base_cfg(memory=MemoryConfig(enabled=True))
    svc = MarshalService(repo, cfg, backends={"stub": _Stub()})
    composed = svc._compose_goal("do work")

    assert "## Memory from past runs" not in composed
    assert composed == f"{_WORKER_PREAMBLE}\n\ndo work"


def test_service_passes_on_run_complete_to_fleet(repo: Path, monkeypatch: pytest.MonkeyPatch) -> None:
    captured: list[object] = []
    from marshal_engine.fleet import Fleet

    original_init = Fleet.__init__

    def capture_init(self: Fleet, *args: object, **kwargs: object) -> None:
        captured.append(kwargs.get("on_run_complete"))
        original_init(self, *args, **kwargs)

    monkeypatch.setattr("marshal_engine.service.Fleet.__init__", capture_init)
    MarshalService(repo, _base_cfg(), backends={"stub": _Stub()})

    assert len(captured) == 1
    assert captured[0] is not None
    assert callable(captured[0])


def test_on_run_complete_callback_calls_remember_sync(repo: Path, monkeypatch: pytest.MonkeyPatch) -> None:
    remember = MagicMock()
    monkeypatch.setattr("marshal_engine.service.CogneeMemory.remember_sync", remember)

    svc = MarshalService(repo, _base_cfg(), backends={"stub": _Stub()})
    record = RunRecord(run_id="r1", task_id="t1", backend="stub", status="succeeded")
    diff = "diff --git a/foo.py"

    assert svc.fleet._on_run_complete is not None
    svc.fleet._on_run_complete(record, diff)

    remember.assert_called_once_with(record, diff, repo="my-project")


def test_run_agent_triggers_on_run_complete(repo: Path, monkeypatch: pytest.MonkeyPatch) -> None:
    remember = MagicMock()
    monkeypatch.setattr("marshal_engine.service.CogneeMemory.remember_sync", remember)

    svc = MarshalService(repo, _base_cfg(), backends={"stub": _Stub()})
    rec = svc.run_agent("worker", "touch nothing", task_id="t1")

    remember.assert_called_once()
    args, kwargs = remember.call_args
    assert args[0].run_id == rec.run_id
    assert kwargs["repo"] == "my-project"


def test_memory_remember_wires_through_to_remember_note_sync(
    repo: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    remember_note = MagicMock()
    monkeypatch.setattr("marshal_engine.service.CogneeMemory.remember_note_sync", remember_note)

    cfg = _base_cfg(memory=MemoryConfig(enabled=True))
    svc = MarshalService(repo, cfg, backends={"stub": _Stub()})
    result = svc.memory_remember("worth remembering", ["idea"])

    remember_note.assert_called_once_with("worth remembering", repo="my-project", tags=["idea"])
    assert result == "stored note in memory for my-project"


def test_memory_remember_disabled_returns_message_without_calling_cognee(
    repo: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    remember_note = MagicMock()
    monkeypatch.setattr("marshal_engine.service.CogneeMemory.remember_note_sync", remember_note)

    svc = MarshalService(repo, _base_cfg(), backends={"stub": _Stub()})
    result = svc.memory_remember("should not be stored")

    remember_note.assert_not_called()
    assert "memory is disabled" in result


def test_disabled_memory_does_not_import_cognee(repo: Path, monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.delitem(sys.modules, "cognee", raising=False)
    svc = MarshalService(repo, _base_cfg(), backends={"stub": _Stub()})
    svc._compose_goal("x")
    svc._memory.remember_sync(
        RunRecord(run_id="r", task_id="t", backend="b"),
        None,
        repo="repo",
    )
    assert "cognee" not in sys.modules
