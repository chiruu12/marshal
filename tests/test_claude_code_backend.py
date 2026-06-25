"""Contract tests for ClaudeCodeBackend.

These exercise the PURE hooks (`map_permission`, `build_invocation`) and the JSON
`parse_output` - no process spawning, no network. Runtime behaviour (acceptEdits never
blocking, edits landing in the worktree) is confirmed by a separate guarded live probe.
"""

from __future__ import annotations

import json
from pathlib import Path

import pytest

from marshal_engine import PermissionMode, RunOpts, RunStatus, TaskSpec, UsageSource
from marshal_engine.backends.claude_code import ClaudeCodeBackend


@pytest.fixture
def backend() -> ClaudeCodeBackend:
    return ClaudeCodeBackend()


def _opts(**kw: object) -> RunOpts:
    kw.setdefault("cwd", Path("/tmp/wt"))
    return RunOpts(**kw)  # type: ignore[arg-type]


def test_map_permission(backend: ClaudeCodeBackend) -> None:
    assert backend.map_permission(PermissionMode.READ_ONLY) == ["--permission-mode", "plan"]
    assert backend.map_permission(PermissionMode.SAFE_EDIT) == ["--permission-mode", "acceptEdits"]
    assert backend.map_permission(PermissionMode.YOLO) == ["--permission-mode", "bypassPermissions"]


def test_build_invocation_basic(backend: ClaudeCodeBackend) -> None:
    argv = backend.build_invocation(
        TaskSpec(id="t1", goal="do the thing"), _opts(permission=PermissionMode.SAFE_EDIT)
    )
    assert argv[:4] == ["claude", "-p", "--output-format", "json"]
    assert "--permission-mode" in argv and "acceptEdits" in argv
    assert argv[-1] == "do the thing"  # prompt is the trailing positional


def test_build_invocation_model_and_readonly(backend: ClaudeCodeBackend) -> None:
    argv = backend.build_invocation(
        TaskSpec(id="t1", goal="inspect"),
        _opts(permission=PermissionMode.READ_ONLY, model="claude-opus-4-8"),
    )
    assert "--model" in argv and "claude-opus-4-8" in argv
    assert "plan" in argv


def test_build_invocation_resume(backend: ClaudeCodeBackend) -> None:
    argv = backend.build_invocation(TaskSpec(id="t1", goal="continue"), _opts(session_id="sess-123"))
    i = argv.index("--resume")
    assert argv[i + 1] == "sess-123"


def test_compose_prompt_includes_context_files(backend: ClaudeCodeBackend) -> None:
    argv = backend.build_invocation(
        TaskSpec(id="t1", goal="fix bug", context_files=["a.py", "b.py"]), _opts()
    )
    assert "Relevant files:" in argv[-1]
    assert "a.py" in argv[-1] and "b.py" in argv[-1]


def test_unsupported_permission_raises(backend: ClaudeCodeBackend) -> None:
    # The capabilities<->map_permission invariant relies on unsupported modes raising ValueError
    # (not KeyError); all three tiers are supported, so this guards the raise path directly.
    with pytest.raises(ValueError):
        backend.map_permission("nonsense")  # type: ignore[arg-type]


def test_parse_output_success_keeps_native_cost(backend: ClaudeCodeBackend) -> None:
    stdout = json.dumps(
        {
            "type": "result",
            "subtype": "success",
            "is_error": False,
            "result": "done the thing",
            "session_id": "abc-123",
            "total_cost_usd": 0.0123,
            "usage": {
                "input_tokens": 100,
                "output_tokens": 200,
                "cache_read_input_tokens": 1000,
            },
        }
    )
    res = backend.parse_output(stdout, "", 0)
    assert res.status is RunStatus.SUCCEEDED
    assert res.session_id == "abc-123"
    assert res.text == "done the thing"
    assert res.usage is not None
    assert res.usage.source is UsageSource.NATIVE  # the win: real cost, no estimation
    assert abs(res.usage.cost_usd - 0.0123) < 1e-9
    assert res.usage.input_tokens == 100 and res.usage.output_tokens == 200
    assert res.usage.cache_read_tokens == 1000


def test_parse_output_tokens_without_cost_is_unavailable(backend: ClaudeCodeBackend) -> None:
    # Honesty: tokens but no reported cost must NOT be claimed as a native $0.
    stdout = json.dumps(
        {"type": "result", "is_error": False, "result": "ok", "usage": {"input_tokens": 5}}
    )
    res = backend.parse_output(stdout, "", 0)
    assert res.usage is not None
    assert res.usage.source is UsageSource.UNAVAILABLE
    assert res.usage.cost_usd == 0.0


def test_parse_output_is_error_is_failure(backend: ClaudeCodeBackend) -> None:
    stdout = json.dumps(
        {"type": "result", "subtype": "error_during_execution", "is_error": True, "result": "boom"}
    )
    res = backend.parse_output(stdout, "", 0)  # is_error wins even on a 0 exit
    assert res.status is RunStatus.FAILED
    assert "boom" in (res.error or "")


def test_parse_output_unparseable_is_failure(backend: ClaudeCodeBackend) -> None:
    res = backend.parse_output("not json at all", "auth error on stderr", 1)
    assert res.status is RunStatus.FAILED
    assert res.exit_code == 1
