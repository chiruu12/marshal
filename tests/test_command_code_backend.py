"""Contract tests for CommandCodeBackend.

These exercise the PURE hooks (`map_permission`, `build_invocation`) and `parse_output` -
no process spawning, no network. Command Code's `-p` mode prints plain text (no JSON, no usage),
so the adapter reports usage as `unavailable` and treats exit 8 as a turn-cap failure.
"""

from __future__ import annotations

from pathlib import Path

import pytest

from marshal_engine import PermissionMode, RunOpts, RunStatus, TaskSpec, UsageSource
from marshal_engine.backends import base as backend_base
from marshal_engine.backends.command_code import CommandCodeBackend


@pytest.fixture
def backend() -> CommandCodeBackend:
    return CommandCodeBackend()


def _opts(**kw: object) -> RunOpts:
    kw.setdefault("cwd", Path("/tmp/wt"))
    return RunOpts(**kw)  # type: ignore[arg-type]


def test_map_permission(backend: CommandCodeBackend) -> None:
    assert backend.map_permission(PermissionMode.READ_ONLY) == ["--permission-mode", "plan"]
    assert backend.map_permission(PermissionMode.SAFE_EDIT) == ["--yolo"]
    assert backend.map_permission(PermissionMode.YOLO) == ["--yolo"]


def test_build_invocation_basic(backend: CommandCodeBackend) -> None:
    argv = backend.build_invocation(
        TaskSpec(id="t1", goal="do the thing"), _opts(permission=PermissionMode.SAFE_EDIT)
    )
    assert argv[:3] == ["command-code", "-p", "do the thing"]  # prompt is the -p value
    assert "--skip-onboarding" in argv
    assert "-t" in argv
    assert "--max-turns" in argv and "50" in argv
    assert "--yolo" in argv  # headless auto-accept blocks writes, so safe-edit uses --yolo


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


# --- check_available (no real CLI; the spawn is mocked) --------------------------------------


def test_check_available_false_when_binary_missing(
    backend: CommandCodeBackend, monkeypatch: pytest.MonkeyPatch
) -> None:
    monkeypatch.setattr(backend_base.shutil, "which", lambda _b: None)
    assert backend.check_available() is False


def test_check_available_false_on_subprocess_error(
    backend: CommandCodeBackend, monkeypatch: pytest.MonkeyPatch
) -> None:
    monkeypatch.setattr(backend_base.shutil, "which", lambda _b: "/usr/bin/command-code")

    def _boom(*_a: object, **_k: object) -> object:
        raise OSError("cannot exec")

    monkeypatch.setattr(backend_base.subprocess, "run", _boom)
    assert backend.check_available() is False


def test_check_available_true_when_version_succeeds(
    backend: CommandCodeBackend, monkeypatch: pytest.MonkeyPatch
) -> None:
    monkeypatch.setattr(backend_base.shutil, "which", lambda _b: "/usr/bin/command-code")

    class _Proc:
        returncode = 0

    monkeypatch.setattr(backend_base.subprocess, "run", lambda *_a, **_k: _Proc())
    assert backend.check_available() is True


# --- account_info / verifies_auth (command-code status --json) -----------------------------


def test_verifies_auth_true(backend: CommandCodeBackend) -> None:
    assert backend.verifies_auth() is True


def test_parse_status_json_success() -> None:
    from marshal_engine.backends.command_code import _parse_status_json

    raw = (
        '{"authenticated":true,"version":"0.52.5","user":"chiruu12",'
        '"provider":"command-code","model":"xiaomi/mimo-v2.5-pro"}'
    )
    assert _parse_status_json(raw) == {
        "plan": "command-code",
        "model": "xiaomi/mimo-v2.5-pro",
    }


def test_parse_status_json_unauthenticated() -> None:
    from marshal_engine.backends.command_code import _parse_status_json

    assert _parse_status_json('{"authenticated":false}') is None
    assert _parse_status_json('{"authenticated":"true"}') is None
    assert _parse_status_json("") is None
    assert _parse_status_json("not json") is None


def test_account_info_success(
    backend: CommandCodeBackend, monkeypatch: pytest.MonkeyPatch
) -> None:
    class _Proc:
        returncode = 0
        stdout = (
            '{"authenticated":true,"provider":"command-code",'
            '"model":"zai-org/GLM-5.2","user":"u"}'
        )
        stderr = ""

    calls: list[list[str]] = []

    def _run(argv: list[str], **_kw: object) -> _Proc:
        calls.append(list(argv))
        return _Proc()

    monkeypatch.setattr(
        "marshal_engine.backends.command_code.shutil.which",
        lambda _b: "/usr/bin/command-code",
    )
    monkeypatch.setattr("marshal_engine.backends.command_code.subprocess.run", _run)
    assert backend.account_info() == {"plan": "command-code", "model": "zai-org/GLM-5.2"}
    assert calls and calls[0][:3] == ["command-code", "status", "--json"]


def test_account_info_none_when_unauthenticated(
    backend: CommandCodeBackend, monkeypatch: pytest.MonkeyPatch
) -> None:
    class _Proc:
        returncode = 0
        stdout = '{"authenticated":false}'
        stderr = ""

    monkeypatch.setattr(
        "marshal_engine.backends.command_code.shutil.which",
        lambda _b: "/usr/bin/command-code",
    )
    monkeypatch.setattr(
        "marshal_engine.backends.command_code.subprocess.run",
        lambda *a, **k: _Proc(),
    )
    assert backend.account_info() is None


def test_account_info_none_when_binary_missing(
    backend: CommandCodeBackend, monkeypatch: pytest.MonkeyPatch
) -> None:
    monkeypatch.setattr("marshal_engine.backends.command_code.shutil.which", lambda _b: None)
    assert backend.account_info() is None


def test_account_info_ignores_config_json_alone(
    backend: CommandCodeBackend, tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    # config.json presence is NOT auth — without a status probe success, return None.
    monkeypatch.setenv("HOME", str(tmp_path))
    cfg_dir = tmp_path / ".commandcode"
    cfg_dir.mkdir()
    (cfg_dir / "config.json").write_text('{"provider": "zai", "model": "zai-org/GLM-5.2"}')
    monkeypatch.setattr("marshal_engine.backends.command_code.shutil.which", lambda _b: None)
    assert backend.account_info() is None
