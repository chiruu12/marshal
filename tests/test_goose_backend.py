"""Contract tests for GooseBackend (pure hooks + stream-json/json parse; no spawning/network)."""

from __future__ import annotations

from pathlib import Path

import pytest

from marshal_engine import PermissionMode, RunOpts, RunStatus, TaskSpec
from marshal_engine.backends.goose import GooseBackend, _parse_info_check, _split_provider_model


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


@pytest.mark.parametrize(
    ("raw", "provider", "model"),
    [
        (None, None, None),
        ("", None, None),
        ("   ", None, None),
        ("gpt-4", None, "gpt-4"),
        ("cursor-agent/auto", "cursor-agent", "auto"),
        ("prov/a/b", "prov", "a/b"),  # nested id after first slash is the model
    ],
)
def test_split_provider_model_valid(
    raw: str | None, provider: str | None, model: str | None
) -> None:
    assert _split_provider_model(raw) == (provider, model)


@pytest.mark.parametrize(
    "raw",
    ["cursor-agent/", "/auto", "/", "  /auto", "cursor-agent/  ", "provider/"],
)
def test_split_provider_model_rejects_empty_side(raw: str) -> None:
    with pytest.raises(ValueError, match="malformed"):
        _split_provider_model(raw)


def test_build_invocation_rejects_trailing_slash(backend: GooseBackend) -> None:
    with pytest.raises(ValueError, match="empty model"):
        backend.build_invocation(
            TaskSpec(id="t1", goal="code"),
            _opts(permission=PermissionMode.SAFE_EDIT, model="cursor-agent/"),
        )


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
    assert "fatal error" in (res.error or "")


def test_parse_output_unknown_provider_stdout(backend: GooseBackend) -> None:
    # Live-captured Goose failure: plain text on stdout, empty stderr, exit 1. Drivers must see
    # "Unknown provider" on record.error — not a bare "exited with code 1".
    out = (
        "error: Error Unknown provider: nonexistent-provider.\n"
        "Please check your system keychain and run 'goose configure' again.\n"
    )
    res = backend.parse_output(out, "", 1)
    assert res.status is RunStatus.FAILED
    assert res.error is not None
    assert "Unknown provider" in res.error
    assert res.text == ""


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


# --- account_info / verifies_auth (goose info -v --check) --------------------------------------


def test_verifies_auth_true(backend: GooseBackend) -> None:
    assert backend.verifies_auth() is True


def test_parse_info_check_success_verbose() -> None:
    raw = """
goose Version:
  Version:                  1.43.0

goose Configuration:
  GOOSE_MODEL: auto
  GOOSE_PROVIDER: cursor-agent
  active_provider: cursor-agent

Provider Check:
  Auth:                     OK
  Models:                   3 available
"""
    assert _parse_info_check(raw, exit_ok=True) == {
        "plan": "cursor-agent",
        "model": "auto",
    }


def test_parse_info_check_success_without_verbose_keys() -> None:
    raw = """
goose Version: 1.43.0

Provider Check:
  Auth:                     OK
"""
    assert _parse_info_check(raw, exit_ok=True) == {"plan": "configured"}


def test_parse_info_check_auth_failed_returns_none() -> None:
    raw = """
goose Configuration:
  GOOSE_PROVIDER: cursor-agent
  GOOSE_MODEL: auto

Provider Check:
  Auth:                     FAILED Authentication error: You are not logged in to cursor-agent.
  Hint:                     Check your API key or run 'goose configure'
Error: provider check failed
"""
    # Even with provider keys present, Auth FAILED + non-zero exit must not green-light doctor.
    assert _parse_info_check(raw, exit_ok=False) is None
    assert _parse_info_check(raw, exit_ok=True) is None


def test_parse_info_check_empty_or_nonzero() -> None:
    assert _parse_info_check("", exit_ok=True) is None
    assert _parse_info_check("GOOSE_PROVIDER: x", exit_ok=False) is None


def test_account_info_uses_info_check(
    backend: GooseBackend, monkeypatch: pytest.MonkeyPatch
) -> None:
    class _Proc:
        returncode = 0
        stdout = (
            "GOOSE_PROVIDER: anthropic\nGOOSE_MODEL: claude-sonnet\n"
            "Provider Check:\n  Auth:                     OK\n"
        )
        stderr = ""

    calls: list[list[str]] = []

    def _run(argv: list[str], **_kw: object) -> _Proc:
        calls.append(list(argv))
        return _Proc()

    monkeypatch.setattr(
        "marshal_engine.backends.goose.shutil.which", lambda _b: "/usr/bin/goose"
    )
    monkeypatch.setattr("marshal_engine.backends.goose.subprocess.run", _run)
    assert backend.account_info() == {"plan": "anthropic", "model": "claude-sonnet"}
    assert calls and calls[0][:4] == ["goose", "info", "-v", "--check"]


def test_account_info_none_when_check_fails(
    backend: GooseBackend, monkeypatch: pytest.MonkeyPatch
) -> None:
    class _Proc:
        returncode = 1
        stdout = "Provider Check:\n  Auth:                     FAILED not logged in\n"
        stderr = "Error: provider check failed\n"

    monkeypatch.setattr(
        "marshal_engine.backends.goose.shutil.which", lambda _b: "/usr/bin/goose"
    )
    monkeypatch.setattr(
        "marshal_engine.backends.goose.subprocess.run",
        lambda *a, **k: _Proc(),
    )
    assert backend.account_info() is None


def test_account_info_none_when_binary_missing(
    backend: GooseBackend, monkeypatch: pytest.MonkeyPatch
) -> None:
    monkeypatch.setattr("marshal_engine.backends.goose.shutil.which", lambda _b: None)
    assert backend.account_info() is None
