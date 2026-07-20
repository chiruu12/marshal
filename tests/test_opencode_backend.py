"""Contract tests for OpenCodeBackend (pure hooks + NDJSON parse; no spawning/network)."""

from __future__ import annotations

import json
from pathlib import Path

import pytest

from marshal_engine import PermissionMode, RunOpts, RunStatus, TaskSpec
from marshal_engine.backends.opencode import OpenCodeBackend, permission_config_for


@pytest.fixture
def backend() -> OpenCodeBackend:
    return OpenCodeBackend()


def _opts(**kw: object) -> RunOpts:
    kw.setdefault("cwd", Path("/tmp/wt"))
    return RunOpts(**kw)  # type: ignore[arg-type]


def test_map_permission(backend: OpenCodeBackend) -> None:
    assert backend.map_permission(PermissionMode.READ_ONLY) == ["--agent", "plan"]
    assert backend.map_permission(PermissionMode.SAFE_EDIT) == ["--dangerously-skip-permissions"]
    assert backend.map_permission(PermissionMode.YOLO) == ["--dangerously-skip-permissions"]


def test_permission_config_safe_edit_denies_question_and_curated() -> None:
    cfg = permission_config_for(PermissionMode.SAFE_EDIT)
    assert cfg is not None
    perms = cfg["permission"]
    assert perms["question"] == "deny"
    assert perms["*"] == "allow"
    assert perms["external_directory"] == "deny"
    assert perms["bash"]["rm"] == "deny"
    assert perms["bash"]["rm *"] == "deny"
    assert perms["edit"]["*.env"] == "deny"
    assert perms["read"]["*.env"] == "deny"
    # never ask - headless stdin is closed
    assert '"ask"' not in json.dumps(cfg)


def test_permission_config_yolo_only_denies_question() -> None:
    cfg = permission_config_for(PermissionMode.YOLO)
    assert cfg is not None
    assert cfg["permission"] == {"*": "allow", "question": "deny"}
    assert permission_config_for(PermissionMode.READ_ONLY) is None


def test_prepare_stamps_opencode_config_content(
    backend: OpenCodeBackend, tmp_path: Path
) -> None:
    opts = _opts(cwd=tmp_path, permission=PermissionMode.SAFE_EDIT, extra_env={"KEEP": "1"})
    backend.prepare(opts)
    raw = opts.extra_env["OPENCODE_CONFIG_CONTENT"]
    assert opts.extra_env["KEEP"] == "1"
    stamped = json.loads(raw)
    assert stamped == permission_config_for(PermissionMode.SAFE_EDIT)
    # argv stays skip-permissions; managed config is what differentiates safe-edit
    argv = backend.build_invocation(TaskSpec(id="t", goal="x"), opts)
    assert "--dangerously-skip-permissions" in argv


def test_prepare_yolo_stamps_question_deny_only(
    backend: OpenCodeBackend, tmp_path: Path
) -> None:
    opts = _opts(cwd=tmp_path, permission=PermissionMode.YOLO)
    backend.prepare(opts)
    stamped = json.loads(opts.extra_env["OPENCODE_CONFIG_CONTENT"])
    assert stamped == permission_config_for(PermissionMode.YOLO)


def test_prepare_readonly_skips_config_content(
    backend: OpenCodeBackend, tmp_path: Path
) -> None:
    opts = _opts(cwd=tmp_path, permission=PermissionMode.READ_ONLY)
    backend.prepare(opts)
    assert "OPENCODE_CONFIG_CONTENT" not in opts.extra_env


def test_build_invocation_basic(backend: OpenCodeBackend) -> None:
    argv = backend.build_invocation(
        TaskSpec(id="t1", goal="do it"), _opts(permission=PermissionMode.SAFE_EDIT)
    )
    assert argv[:4] == ["opencode", "run", "--format", "json"]
    assert "--dangerously-skip-permissions" in argv
    assert "--dir" in argv and "/tmp/wt" in argv
    assert argv[-1] == "do it"


def test_build_invocation_model_and_readonly(backend: OpenCodeBackend) -> None:
    argv = backend.build_invocation(
        TaskSpec(id="t1", goal="inspect"),
        _opts(permission=PermissionMode.READ_ONLY, model="anthropic/claude-sonnet-4"),
    )
    assert "-m" in argv and "anthropic/claude-sonnet-4" in argv
    assert "--agent" in argv and "plan" in argv


def test_build_invocation_resume(backend: OpenCodeBackend) -> None:
    argv = backend.build_invocation(TaskSpec(id="t1", goal="cont"), _opts(session_id="sess-9"))
    i = argv.index("-s")
    assert argv[i + 1] == "sess-9"


def test_compose_prompt_includes_context(backend: OpenCodeBackend) -> None:
    argv = backend.build_invocation(
        TaskSpec(id="t1", goal="fix", context_files=["a.py"]), _opts()
    )
    assert "Relevant files:" in argv[-1] and "a.py" in argv[-1]


def test_parse_output_success_with_usage(backend: OpenCodeBackend) -> None:
    out = "\n".join(
        [
            '{"part":{"type":"text","text":"Hello "}}',
            '{"part":{"type":"text","text":"world"}}',
            '{"part":{"type":"step-finish","reason":"stop","cost":0.012,'
            '"tokens":{"input":100,"output":20,"cache":{"read":5,"write":0}}}}',
        ]
    )
    res = backend.parse_output(out, "", 0)
    assert res.status is RunStatus.SUCCEEDED
    assert res.text == "Hello world"
    assert res.usage is not None
    assert res.usage.input_tokens == 100
    assert res.usage.output_tokens == 20
    assert res.usage.cache_read_tokens == 5
    assert abs(res.usage.cost_usd - 0.012) < 1e-9


def test_parse_output_error_event_is_failure(backend: OpenCodeBackend) -> None:
    out = '{"error":{"name":"RateLimit","data":{"message":"too many requests"}}}'
    res = backend.parse_output(out, "", 0)
    assert res.status is RunStatus.FAILED
    assert "too many requests" in (res.error or "")


def test_parse_output_nonzero_exit(backend: OpenCodeBackend) -> None:
    res = backend.parse_output("", "boom", 1)
    assert res.status is RunStatus.FAILED
    assert res.exit_code == 1
