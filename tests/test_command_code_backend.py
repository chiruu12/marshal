"""Contract tests for CommandCodeBackend.

These exercise the PURE hooks (`map_permission`, `build_invocation`) and `parse_output` -
no process spawning, no network. Command Code's `-p` mode prints plain text (no JSON, no usage),
so the adapter reports usage as `unavailable` and treats exit 8 as a turn-cap failure.
"""

from __future__ import annotations

from pathlib import Path

import pytest

from marshal_engine import ModelSource, PermissionMode, RunOpts, RunStatus, TaskSpec, UsageSource
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


def test_compose_prompt_includes_read_paths_hint(backend: CommandCodeBackend) -> None:
    argv = backend.build_invocation(
        TaskSpec(id="t1", goal="fix bug", read_paths=["/tmp/notes.md"]), _opts()
    )
    assert ".marshal-context/" in argv[2]
    assert "Read-only reference material" in argv[2]


def test_parse_output_success(backend: CommandCodeBackend) -> None:
    res = backend.parse_output("pong\n", "", 0)
    assert res.status is RunStatus.EXITED_CLEAN
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


# --- --output-format json (shapes captured from a real command-code 1.7.0 run) ----------------

#: Trimmed from an actual run. The terminal `result` line is the contract this adapter reads.
_REAL_STREAM = (
    '{"type":"event","event":{"type":"model_request_start","model":"inclusionai/ling-3.0-flash-free"}}\n'
    '{"type":"event","event":{"type":"text_delta","delta":"OK"}}\n'
    '{"type":"event","event":{"type":"turn_end","turnNumber":1,"usage":'
    '{"inputTokens":29215,"outputTokens":22,"cacheReadTokens":0,"cacheWriteTokens":0}}}\n'
    '{"type":"result","subtype":"success","sessionId":"ddc03ecc-b739","stopReason":"end_turn",'
    '"usage":{"inputTokens":29215,"outputTokens":22,"cacheReadTokens":3,"cacheWriteTokens":7},'
    '"durationMs":5721,"finalText":"OK"}\n'
)


def test_build_invocation_requests_json(backend: CommandCodeBackend) -> None:
    argv = backend.build_invocation(TaskSpec(id="t1", goal="go"), _opts())
    assert argv[argv.index("--output-format") + 1] == "json"


def test_parse_output_reads_tokens_and_session_from_the_result_line(
    backend: CommandCodeBackend,
) -> None:
    res = backend.parse_output(_REAL_STREAM, "", 0)
    assert res.status is RunStatus.EXITED_CLEAN
    assert res.text == "OK", "the whole NDJSON stream leaked into text instead of finalText"
    assert res.session_id == "ddc03ecc-b739"
    assert res.usage is not None
    assert (res.usage.input_tokens, res.usage.output_tokens) == (29215, 22)
    assert (res.usage.cache_read_tokens, res.usage.cache_write_tokens) == (3, 7)
    assert res.usage.model == "inclusionai/ling-3.0-flash-free"


def test_parse_output_never_claims_a_cost_it_was_not_given(
    backend: CommandCodeBackend,
) -> None:
    """Tokens are reported; USD is not, because the CLI does not emit one.

    `source` describes the provenance of the COST. Tagging this run `native` because tokens
    arrived would put a $0.00 into every rollup and make the backend that bills the most look
    like the cheapest one in `routing` and `report`."""
    res = backend.parse_output(_REAL_STREAM, "", 0)
    assert res.usage is not None
    assert res.usage.source is UsageSource.UNAVAILABLE
    assert res.usage.cost_usd == 0.0
    assert backend.capabilities.native_usage is False, "native_usage means native COST"
    assert backend.capabilities.json_output is True


def test_parse_output_falls_back_to_text_for_a_cli_without_output_format(
    backend: CommandCodeBackend,
) -> None:
    """Older CLIs ignore the flag and print prose. Losing the answer would be worse than no tokens."""
    res = backend.parse_output("the answer is 42\n", "", 0)
    assert res.text == "the answer is 42"
    assert res.session_id is None
    assert res.usage is not None and res.usage.input_tokens == 0


def test_parse_output_takes_the_last_result_line_not_an_echoed_one(
    backend: CommandCodeBackend,
) -> None:
    """A tool that cats a transcript can echo a `result` object mid-stream; the run's own is last."""
    echoed = (
        '{"type":"result","subtype":"success","sessionId":"OLD","usage":'
        '{"inputTokens":1,"outputTokens":1},"finalText":"stale"}\n' + _REAL_STREAM
    )
    res = backend.parse_output(echoed, "", 0)
    assert res.session_id == "ddc03ecc-b739"
    assert res.text == "OK"


def test_parse_output_survives_a_torn_or_odd_result_line(backend: CommandCodeBackend) -> None:
    """Backend JSON is version-variable - a missing/odd `usage` must not raise mid-run."""
    torn = '{"type":"result","subtype":"success","usage":"nope","finalText":"done"}\n'
    res = backend.parse_output(torn, "", 0)
    assert res.text == "done"
    assert res.usage is not None and res.usage.input_tokens == 0
    assert backend.parse_output('{"type":"result"', "", 0).status is RunStatus.EXITED_CLEAN


def test_parse_output_cap_hit_still_reports_tokens(backend: CommandCodeBackend) -> None:
    """A capped run burned real tokens; dropping them would understate the fan-out's usage."""
    res = backend.parse_output(_REAL_STREAM, "", 8)
    assert res.status is RunStatus.FAILED
    assert res.usage is not None and res.usage.input_tokens == 29215


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


def test_parse_list_models_rows() -> None:
    from marshal_engine.backends.command_code import _parse_list_models

    raw = (
        "Available models  ·  2 models\n\nOpen Source\n\n"
        "zai-org/glm-5.2                      powerful coding\n"
        "claude-sonnet-4-6                    prev Sonnet\n"
    )
    assert _parse_list_models(raw) == ["zai-org/glm-5.2", "claude-sonnet-4-6"]


def test_available_models_parses_cli(
    backend: CommandCodeBackend, monkeypatch: pytest.MonkeyPatch
) -> None:
    class _Proc:
        returncode = 0
        stdout = "zai-org/glm-5.2                      powerful coding\n"
        stderr = ""

    calls: list[list[str]] = []

    def _run(argv: list[str], **_kw: object) -> _Proc:
        calls.append(list(argv))
        return _Proc()

    monkeypatch.setattr(
        "marshal_engine.backends.base.shutil.which",
        lambda _b: "/usr/bin/command-code",
    )
    monkeypatch.setattr("marshal_engine.backends.base.subprocess.run", _run)
    catalog = backend.available_models()
    assert catalog.models == ["zai-org/glm-5.2"]
    assert catalog.source is ModelSource.PROBED
    assert calls and calls[0] == ["command-code", "--list-models"]


def test_available_models_static_fallback_when_binary_missing(
    backend: CommandCodeBackend, monkeypatch: pytest.MonkeyPatch
) -> None:
    monkeypatch.setattr("marshal_engine.backends.base.shutil.which", lambda _b: None)
    catalog = backend.available_models()
    assert catalog.models == ["zai-org/glm-5.2"]
    assert catalog.source is ModelSource.STATIC
