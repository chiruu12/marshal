"""Contract tests for CodexBackend.

These exercise the PURE hooks (`map_permission`, `build_invocation`) and the JSONL
`parse_output` - no process spawning, no network. Success-path token/message shapes are
best-effort until a live successful Codex run confirms them.
"""

from __future__ import annotations

from pathlib import Path

import pytest

from marshal_engine import PermissionMode, RunOpts, RunStatus, TaskSpec
from marshal_engine.backends.codex import CodexBackend


@pytest.fixture
def backend() -> CodexBackend:
    return CodexBackend()


def _opts(**kw: object) -> RunOpts:
    kw.setdefault("cwd", Path("/tmp/wt"))
    return RunOpts(**kw)  # type: ignore[arg-type]


def test_map_permission(backend: CodexBackend) -> None:
    assert backend.map_permission(PermissionMode.READ_ONLY) == ["--sandbox", "read-only"]
    assert backend.map_permission(PermissionMode.SAFE_EDIT) == ["--sandbox", "workspace-write"]
    assert backend.map_permission(PermissionMode.YOLO) == [
        "--dangerously-bypass-approvals-and-sandbox"
    ]


def test_build_invocation_basic(backend: CodexBackend) -> None:
    argv = backend.build_invocation(
        TaskSpec(id="t1", goal="do the thing"), _opts(permission=PermissionMode.SAFE_EDIT)
    )
    assert argv[:5] == ["codex", "exec", "--json", "--color", "never"]
    assert "--skip-git-repo-check" in argv
    assert "--sandbox" in argv and "workspace-write" in argv
    assert "-C" in argv and "/tmp/wt" in argv
    assert argv[-1] == "do the thing"  # prompt is the trailing positional


def test_build_invocation_model_and_readonly(backend: CodexBackend) -> None:
    argv = backend.build_invocation(
        TaskSpec(id="t1", goal="inspect"),
        _opts(permission=PermissionMode.READ_ONLY, model="o3"),
    )
    assert "-m" in argv and "o3" in argv
    assert "read-only" in argv


def test_build_invocation_resume(backend: CodexBackend) -> None:
    argv = backend.build_invocation(TaskSpec(id="t1", goal="continue"), _opts(session_id="sess-123"))
    i = argv.index("resume")
    assert argv[i + 1] == "sess-123"


def test_compose_prompt_includes_context_files(backend: CodexBackend) -> None:
    argv = backend.build_invocation(
        TaskSpec(id="t1", goal="fix bug", context_files=["a.py", "b.py"]), _opts()
    )
    assert "Relevant files:" in argv[-1]
    assert "a.py" in argv[-1] and "b.py" in argv[-1]


def test_parse_output_failure_captures_session_and_error(backend: CodexBackend) -> None:
    stdout = "\n".join(
        [
            '{"type":"thread.started","thread_id":"abc-123"}',
            '{"type":"turn.started"}',
            '{"type":"error","message":"you hit your usage limit"}',
            '{"type":"turn.failed","error":{"message":"you hit your usage limit"}}',
        ]
    )
    res = backend.parse_output(stdout, "", 1)
    assert res.status is RunStatus.FAILED
    assert res.session_id == "abc-123"
    assert "usage limit" in (res.error or "")


def test_parse_output_success_best_effort(backend: CodexBackend) -> None:
    stdout = "\n".join(
        [
            '{"type":"thread.started","thread_id":"abc-123"}',
            '{"type":"agent_message","message":"pong"}',
            '{"type":"turn.completed","usage":{"input_tokens":10,"output_tokens":2}}',
        ]
    )
    res = backend.parse_output(stdout, "", 0)
    assert res.status is RunStatus.SUCCEEDED
    assert res.session_id == "abc-123"
    assert "pong" in res.text
    assert res.usage is not None
    assert res.usage.input_tokens == 10 and res.usage.output_tokens == 2


def test_parse_output_nonzero_exit_is_failure(backend: CodexBackend) -> None:
    res = backend.parse_output("", "boom on stderr", 2)
    assert res.status is RunStatus.FAILED
    assert res.exit_code == 2


# --- account_info / verifies_auth (codex login status) --------------------------------------


def test_verifies_auth_true(backend: CodexBackend) -> None:
    assert backend.verifies_auth() is True


def test_parse_login_status() -> None:
    from marshal_engine.backends.codex import _parse_login_status

    assert _parse_login_status("Logged in as user@example.com\n", exit_ok=True) == {
        "plan": "logged-in"
    }
    assert _parse_login_status("Not logged in\n", exit_ok=False) is None
    assert _parse_login_status("Not logged in\n", exit_ok=True) is None
    assert _parse_login_status("", exit_ok=False) is None
    # Exit 0 with empty/blank output is unknown — fail closed, not silent green.
    assert _parse_login_status("", exit_ok=True) is None
    assert _parse_login_status("  \n", exit_ok=True) is None


def test_account_info_success(
    backend: CodexBackend, monkeypatch: pytest.MonkeyPatch
) -> None:
    class _Proc:
        returncode = 0
        stdout = "Logged in using ChatGPT\n"
        stderr = ""

    calls: list[list[str]] = []

    def _run(argv: list[str], **_kw: object) -> _Proc:
        calls.append(list(argv))
        return _Proc()

    monkeypatch.setattr(
        "marshal_engine.backends.codex.shutil.which", lambda _b: "/usr/bin/codex"
    )
    monkeypatch.setattr("marshal_engine.backends.codex.subprocess.run", _run)
    assert backend.account_info() == {"plan": "logged-in"}
    assert calls and calls[0][:3] == ["codex", "login", "status"]


def test_account_info_none_when_not_logged_in(
    backend: CodexBackend, monkeypatch: pytest.MonkeyPatch
) -> None:
    class _Proc:
        returncode = 1
        stdout = "Not logged in\n"
        stderr = ""

    monkeypatch.setattr(
        "marshal_engine.backends.codex.shutil.which", lambda _b: "/usr/bin/codex"
    )
    monkeypatch.setattr(
        "marshal_engine.backends.codex.subprocess.run",
        lambda *a, **k: _Proc(),
    )
    assert backend.account_info() is None


def test_account_info_none_when_binary_missing(
    backend: CodexBackend, monkeypatch: pytest.MonkeyPatch
) -> None:
    monkeypatch.setattr("marshal_engine.backends.codex.shutil.which", lambda _b: None)
    assert backend.account_info() is None
