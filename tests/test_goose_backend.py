"""Contract tests for GooseBackend (pure hooks + stream-json/json parse; no spawning/network)."""

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


def test_map_permission_is_env_driven(backend: GooseBackend) -> None:
    # Goose 1.43+ has no --yes/--plan argv; permission is GOOSE_MODE via prepare().
    assert backend.map_permission(PermissionMode.READ_ONLY) == []
    assert backend.map_permission(PermissionMode.SAFE_EDIT) == []
    assert backend.map_permission(PermissionMode.YOLO) == []


def test_prepare_sets_goose_mode(backend: GooseBackend) -> None:
    opts = _opts(permission=PermissionMode.SAFE_EDIT)
    backend.prepare(opts)
    assert opts.extra_env["GOOSE_MODE"] == "auto"

    opts_ro = _opts(permission=PermissionMode.READ_ONLY)
    backend.prepare(opts_ro)
    assert opts_ro.extra_env["GOOSE_MODE"] == "chat"


def test_build_invocation_basic(backend: GooseBackend) -> None:
    argv = backend.build_invocation(
        TaskSpec(id="t1", goal="do it"), _opts(permission=PermissionMode.YOLO)
    )
    assert argv[:5] == ["goose", "run", "--output-format", "stream-json", "--no-session"]
    assert "-t" in argv
    assert argv[argv.index("-t") + 1] == "do it"
    assert "--json" not in argv
    assert "--yes" not in argv
    assert "--" not in argv


def test_build_invocation_readonly(backend: GooseBackend) -> None:
    argv = backend.build_invocation(
        TaskSpec(id="t1", goal="inspect"),
        _opts(permission=PermissionMode.READ_ONLY),
    )
    assert "--output-format" in argv and "stream-json" in argv
    assert "-t" in argv
    assert "--plan" not in argv


def test_build_invocation_with_model(backend: GooseBackend) -> None:
    argv = backend.build_invocation(
        TaskSpec(id="t1", goal="code"),
        _opts(permission=PermissionMode.SAFE_EDIT, model="gpt-4"),
    )
    assert "--model" in argv
    assert "gpt-4" in argv
    assert "--provider" not in argv


def test_build_invocation_with_provider_model(backend: GooseBackend) -> None:
    argv = backend.build_invocation(
        TaskSpec(id="t1", goal="code"),
        _opts(permission=PermissionMode.SAFE_EDIT, model="cursor-agent/auto"),
    )
    assert argv[argv.index("--provider") + 1] == "cursor-agent"
    assert argv[argv.index("--model") + 1] == "auto"


def test_compose_prompt_with_context(backend: GooseBackend) -> None:
    argv = backend.build_invocation(
        TaskSpec(id="t1", goal="fix", context_files=["a.py", "b.py"]),
        _opts(),
    )
    prompt = argv[argv.index("-t") + 1]
    assert "@a.py" in prompt
    assert "@b.py" in prompt


def test_parse_output_stream_json_success(backend: GooseBackend) -> None:
    out = "\n".join(
        [
            '{"type":"message","message":{"role":"assistant","content":[{"type":"text","text":"Result: OK"}]}}',
            '{"type":"complete","total_tokens":150,"input_tokens":100,"output_tokens":50,"cost":0.005}',
        ]
    )
    res = backend.parse_output(out, "", 0)
    assert res.status is RunStatus.SUCCEEDED
    assert "Result: OK" in res.text
    assert res.usage is not None
    assert res.usage.input_tokens == 100
    assert res.usage.output_tokens == 50
    assert abs(res.usage.cost_usd - 0.005) < 1e-6


def test_parse_output_stream_json_delta_chunks(backend: GooseBackend) -> None:
    out = "\n".join(
        [
            '{"type":"message","message":{"role":"assistant","content":[{"type":"text","text":"p"}]}}',
            '{"type":"message","message":{"role":"assistant","content":[{"type":"text","text":"ong"}]}}',
            '{"type":"complete","total_tokens":10,"input_tokens":8,"output_tokens":2}',
        ]
    )
    res = backend.parse_output(out, "", 0)
    assert res.status is RunStatus.SUCCEEDED
    assert res.text == "pong"
    assert res.usage is not None
    assert res.usage.input_tokens == 8
    assert res.usage.output_tokens == 2


def test_parse_output_bulk_json_success(backend: GooseBackend) -> None:
    out = (
        '{"messages":[{"role":"user","content":[{"type":"text","text":"hi"}]},'
        '{"role":"assistant","content":[{"type":"text","text":"pong"}]}],'
        '"metadata":{"total_tokens":12,"status":"completed"}}'
    )
    res = backend.parse_output(out, "", 0)
    assert res.status is RunStatus.SUCCEEDED
    assert res.text == "pong"
    assert res.usage is not None
    assert res.usage.output_tokens == 12


def test_parse_output_error_event(backend: GooseBackend) -> None:
    out = '{"type":"error","message":"Permission denied"}'
    res = backend.parse_output(out, "", 0)
    assert res.status is RunStatus.FAILED
    assert "Permission denied" in (res.error or "")


def test_parse_output_auth_error_in_assistant_text(backend: GooseBackend) -> None:
    out = (
        '{"type":"message","message":{"role":"assistant","content":[{"type":"text","text":'
        '"Ran into this error: Authentication error: You are not logged in to cursor-agent. '
        "Please run 'cursor-agent login' to authenticate first..\"}]}}\n"
        '{"type":"complete","total_tokens":null}'
    )
    res = backend.parse_output(out, "", 0)
    assert res.status is RunStatus.FAILED
    assert "Authentication error" in (res.error or "")


def test_parse_output_nonzero_exit(backend: GooseBackend) -> None:
    res = backend.parse_output("", "fatal error", 1)
    assert res.status is RunStatus.FAILED
    assert res.exit_code == 1


def test_parse_output_malformed_json_ignored(backend: GooseBackend) -> None:
    out = "\n".join(
        [
            '{"type":"message","message":{"role":"assistant","content":[{"type":"text","text":"start"}]}}',
            "this is not json",
            '{"type":"message","message":{"role":"assistant","content":[{"type":"text","text":"end"}]}}',
        ]
    )
    res = backend.parse_output(out, "", 0)
    assert res.status is RunStatus.SUCCEEDED
    assert "start" in res.text and "end" in res.text
