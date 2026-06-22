"""Contract tests for AntigravityBackend (pure hooks + text parse; no spawning/network)."""

from __future__ import annotations

from pathlib import Path

import pytest

from marshal_engine import PermissionMode, RunOpts, RunStatus, TaskSpec, UsageSource
from marshal_engine.backends.antigravity import AntigravityBackend


@pytest.fixture
def backend() -> AntigravityBackend:
    return AntigravityBackend()


def _opts(**kw: object) -> RunOpts:
    kw.setdefault("cwd", Path("/tmp/wt"))
    return RunOpts(**kw)  # type: ignore[arg-type]


def test_map_permission(backend: AntigravityBackend) -> None:
    assert backend.map_permission(PermissionMode.SAFE_EDIT) == ["--dangerously-skip-permissions"]
    assert backend.map_permission(PermissionMode.YOLO) == ["--dangerously-skip-permissions"]
    with pytest.raises(ValueError, match="not supported"):
        backend.map_permission(PermissionMode.READ_ONLY)


def test_build_invocation_basic(backend: AntigravityBackend) -> None:
    argv = backend.build_invocation(
        TaskSpec(id="t1", goal="do it"), _opts(permission=PermissionMode.SAFE_EDIT)
    )
    assert argv == [
        "agy",
        "--dangerously-skip-permissions",
        "-p",
        "do it",
    ]


def test_build_invocation_model(backend: AntigravityBackend) -> None:
    argv = backend.build_invocation(
        TaskSpec(id="t1", goal="x"),
        _opts(permission=PermissionMode.YOLO, model="gemini-3.1-pro"),
    )
    assert argv == [
        "agy",
        "--dangerously-skip-permissions",
        "-m",
        "gemini-3.1-pro",
        "-p",
        "x",
    ]


def test_build_invocation_conversation(backend: AntigravityBackend) -> None:
    argv = backend.build_invocation(
        TaskSpec(id="t1", goal="cont"),
        _opts(permission=PermissionMode.SAFE_EDIT, session_id="conv-1"),
    )
    assert argv == [
        "agy",
        "--dangerously-skip-permissions",
        "--conversation",
        "conv-1",
        "-p",
        "cont",
    ]


def test_compose_prompt_includes_context(backend: AntigravityBackend) -> None:
    argv = backend.build_invocation(
        TaskSpec(id="t1", goal="fix", context_files=["a.py"]),
        _opts(permission=PermissionMode.SAFE_EDIT),
    )
    assert argv == [
        "agy",
        "--dangerously-skip-permissions",
        "-p",
        "fix\n\nRelevant files:\n- a.py",
    ]


def test_parse_output_success_text(backend: AntigravityBackend) -> None:
    res = backend.parse_output("  pong  \n", "", 0)
    assert res.status is RunStatus.SUCCEEDED
    assert res.text == "pong"


def test_parse_output_success_usage_unavailable(backend: AntigravityBackend) -> None:
    res = backend.parse_output("ok", "", 0)
    assert res.status is RunStatus.SUCCEEDED
    assert res.usage is not None
    assert res.usage.backend == "antigravity"
    assert res.usage.source is UsageSource.UNAVAILABLE


def test_parse_output_nonzero_exit_with_stderr(backend: AntigravityBackend) -> None:
    res = backend.parse_output("", "auth required", 1)
    assert res.status is RunStatus.FAILED
    assert "auth required" in (res.error or "")


def test_parse_output_nonzero_exit_empty_stderr(backend: AntigravityBackend) -> None:
    res = backend.parse_output("", "", 1)
    assert res.status is RunStatus.FAILED
    assert res.error == "agy exited 1"
