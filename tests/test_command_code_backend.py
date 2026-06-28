"""Contract tests for CommandCodeBackend.

These exercise the PURE hooks (`map_permission`, `build_invocation`) and `parse_output` -
no process spawning, no network. Command Code's `-p` mode prints plain text (no JSON, no usage),
so the adapter reports usage as `unavailable` and treats exit 8 as a turn-cap failure.
"""

from __future__ import annotations

from pathlib import Path

import pytest

from marshal_engine import PermissionMode, RunOpts, RunStatus, TaskSpec, UsageSource
from marshal_engine.backends.command_code import CommandCodeBackend


@pytest.fixture
def backend() -> CommandCodeBackend:
    return CommandCodeBackend()


def _opts(**kw: object) -> RunOpts:
    kw.setdefault("cwd", Path("/tmp/wt"))
    return RunOpts(**kw)  # type: ignore[arg-type]


def test_map_permission(backend: CommandCodeBackend) -> None:
    assert backend.map_permission(PermissionMode.READ_ONLY) == ["--permission-mode", "plan"]
    assert backend.map_permission(PermissionMode.SAFE_EDIT) == ["--permission-mode", "auto-accept"]
    assert backend.map_permission(PermissionMode.YOLO) == ["--yolo"]


def test_build_invocation_basic(backend: CommandCodeBackend) -> None:
    argv = backend.build_invocation(
        TaskSpec(id="t1", goal="do the thing"), _opts(permission=PermissionMode.SAFE_EDIT)
    )
    assert argv[:3] == ["command-code", "-p", "do the thing"]  # prompt is the -p value
    assert "--skip-onboarding" in argv
    assert "-t" in argv
    assert "--max-turns" in argv and "50" in argv
    assert "--permission-mode" in argv and "auto-accept" in argv


def test_build_invocation_model_and_readonly(backend: CommandCodeBackend) -> None:
    argv = backend.build_invocation(
        TaskSpec(id="t1", goal="inspect"),
        _opts(permission=PermissionMode.READ_ONLY, model="zai-org/GLM-5.2"),
    )
    assert "-m" in argv and "zai-org/GLM-5.2" in argv
    assert "plan" in argv  # read-only maps to plan mode
    assert "auto-accept" not in argv


def test_build_invocation_yolo(backend: CommandCodeBackend) -> None:
    argv = backend.build_invocation(
        TaskSpec(id="t1", goal="go"), _opts(permission=PermissionMode.YOLO)
    )
    assert "--yolo" in argv
    assert "--permission-mode" not in argv


def test_compose_prompt_includes_context_files(backend: CommandCodeBackend) -> None:
    argv = backend.build_invocation(
        TaskSpec(id="t1", goal="fix bug", context_files=["a.py", "b.py"]), _opts()
    )
    assert "Relevant files:" in argv[2]
    assert "a.py" in argv[2] and "b.py" in argv[2]


def test_parse_output_success(backend: CommandCodeBackend) -> None:
    res = backend.parse_output("pong\n", "", 0)
    assert res.status is RunStatus.SUCCEEDED
    assert res.text == "pong"
    assert res.usage is not None and res.usage.source is UsageSource.UNAVAILABLE
    assert res.error is None


def test_parse_output_strips_ansi(backend: CommandCodeBackend) -> None:
    res = backend.parse_output("\x1b[32mall good\x1b[0m\n", "", 0)
    assert res.text == "all good"


def test_parse_output_nonzero_is_failure(backend: CommandCodeBackend) -> None:
    res = backend.parse_output("", "boom on stderr", 2)
    assert res.status is RunStatus.FAILED
    assert res.exit_code == 2
    assert "boom on stderr" in (res.error or "")


def test_parse_output_cap_hit_is_failure(backend: CommandCodeBackend) -> None:
    res = backend.parse_output("partial work\n", "", 8)
    assert res.status is RunStatus.FAILED
    assert "max-turns" in (res.error or "")
    assert res.text == "partial work"  # surfaced even on a cap hit
