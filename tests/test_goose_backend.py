"""Contract tests for GooseBackend (pure hooks + NDJSON parse; no spawning/network)."""

from __future__ import annotations

from pathlib import Path

import pytest

from marshal_engine import PermissionMode, RunOpts, RunStatus, TaskSpec
from marshal_engine.backends.goose import GooseBackend


@pytest.fixture
def backend() -> GooseBackend:
    return GooseBackend()


def _opts(**kw: object) -> RunOpts:
    kw.setdefault("cwd", Path("/tmp/wt"))
    return RunOpts(**kw)  # type: ignore[arg-type]


def test_map_permission(backend: GooseBackend) -> None:
    assert backend.map_permission(PermissionMode.READ_ONLY) == ["--plan"]
    assert backend.map_permission(PermissionMode.SAFE_EDIT) == ["--yes"]
    assert backend.map_permission(PermissionMode.YOLO) == ["--yes"]


def test_build_invocation_basic(backend: GooseBackend) -> None:
    argv = backend.build_invocation(
        TaskSpec(id="t1", goal="do it"), _opts(permission=PermissionMode.YOLO)
    )
    assert argv[:4] == ["goose", "run", "--json", "--yes"]
    assert "--" in argv
    assert argv[-1] == "do it"


def test_build_invocation_readonly(backend: GooseBackend) -> None:
    argv = backend.build_invocation(
        TaskSpec(id="t1", goal="inspect"),
        _opts(permission=PermissionMode.READ_ONLY),
    )
    assert "--plan" in argv
    assert "--json" in argv


def test_build_invocation_with_model(backend: GooseBackend) -> None:
    argv = backend.build_invocation(
        TaskSpec(id="t1", goal="code"),
        _opts(permission=PermissionMode.SAFE_EDIT, model="gpt-4"),
    )
    assert "--model" in argv
    assert "gpt-4" in argv


def test_compose_prompt_with_context(backend: GooseBackend) -> None:
    argv = backend.build_invocation(
        TaskSpec(id="t1", goal="fix", context_files=["a.py", "b.py"]),
        _opts(),
    )
    # The prompt (last arg) should mention the context files
    assert "@a.py" in argv[-1]
    assert "@b.py" in argv[-1]


def test_parse_output_success_with_usage(backend: GooseBackend) -> None:
    out = "\n".join(
        [
            '{"type":"text","text":"Starting analysis..."}',
            '{"type":"output","output":"Result: OK"}',
            '{"type":"completion","tokens":{"input":50,"output":100},"cost":0.005}',
        ]
    )
    res = backend.parse_output(out, "", 0)
    assert res.status is RunStatus.SUCCEEDED
    assert "Result: OK" in res.text
    assert res.usage is not None
    assert res.usage.input_tokens == 50
    assert res.usage.output_tokens == 100
    assert abs(res.usage.cost_usd - 0.005) < 1e-6


def test_parse_output_error_event(backend: GooseBackend) -> None:
    out = '{"type":"error","message":"Permission denied"}'
    res = backend.parse_output(out, "", 0)
    assert res.status is RunStatus.FAILED
    assert "Permission denied" in (res.error or "")


def test_parse_output_nonzero_exit(backend: GooseBackend) -> None:
    res = backend.parse_output("", "fatal error", 1)
    assert res.status is RunStatus.FAILED
    assert res.exit_code == 1


def test_parse_output_malformed_json_ignored(backend: GooseBackend) -> None:
    out = "\n".join(
        [
            '{"type":"text","text":"start"}',
            "this is not json",
            '{"type":"text","text":"end"}',
        ]
    )
    res = backend.parse_output(out, "", 0)
    assert res.status is RunStatus.SUCCEEDED
    assert "start" in res.text and "end" in res.text
