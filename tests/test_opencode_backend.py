"""Contract tests for OpenCodeBackend (pure hooks + NDJSON parse; no spawning/network)."""

from __future__ import annotations

import json
import shutil
import subprocess
import sys
from pathlib import Path
from typing import Any
from unittest.mock import MagicMock

import pytest

from marshal_engine import PermissionMode, RunOpts, RunStatus, TaskSpec, UsageSource
from marshal_engine.backends.opencode import OpenCodeBackend, permission_config_for


@pytest.fixture
def backend() -> OpenCodeBackend:
    return OpenCodeBackend()


def _opts(**kw: object) -> RunOpts:
    kw.setdefault("cwd", Path("/tmp/wt"))
    return RunOpts(**kw)  # type: ignore[arg-type]


def _finalize(
    backend: OpenCodeBackend, out: str, err: str = "", exit_code: int = 0
) -> object:
    """Exercise parse_output (pure) then finalize (export reconciliation hook)."""
    return backend.finalize(backend.parse_output(out, err, exit_code))


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


# --- export-reconciliation step (finalize hook; parse_output stays pure) ------------------


def _stub_export(payload: dict[str, Any], returncode: int = 0) -> MagicMock:
    """A mock CompletedProcess that `opencode export <sid>` would return."""
    proc = MagicMock()
    proc.returncode = returncode
    proc.stdout = json.dumps(payload)
    proc.stderr = ""
    return proc


def _patch_export(
    monkeypatch: pytest.MonkeyPatch,
    *,
    proc: MagicMock | None = None,
    side_effect: BaseException | None = None,
    binary_path: str | None = "/usr/bin/opencode",
) -> None:
    """Bind the opencode backend's reconciler to a fake subprocess + a fake `which` result."""
    if proc is not None:
        monkeypatch.setattr(
            "marshal_engine.backends.opencode.subprocess.run",
            lambda *a, **kw: proc if side_effect is None else (_ for _ in ()).throw(side_effect),
        )
    monkeypatch.setattr(shutil, "which", lambda cmd: binary_path if cmd == "opencode" else None)


def test_reconcile_recovers_dropped_text(
    backend: OpenCodeBackend, monkeypatch: pytest.MonkeyPatch
) -> None:
    # Live event stream carries only the FIRST text part - opencode dropped the rest on
    # stdout. The export has the full report; reconciliation must restore the missing parts.
    out = '{"sessionID":"ses_x","part":{"type":"text","text":"partial "}}\n'
    export = {
        "info": {"id": "ses_x", "tokens": {"input": 1, "output": 1}, "cost": 0.0},
        "messages": [
            {"info": {}, "parts": [
                {"type": "text", "text": "full "},
                {"type": "text", "text": "report "},
                {"type": "text", "text": "restored."},
            ]},
        ],
    }
    _patch_export(monkeypatch, proc=_stub_export(export))
    res = _finalize(backend, out)
    assert res.status is RunStatus.SUCCEEDED
    assert res.text == "full report restored."  # export overrode the live stream's "partial "


def test_reconcile_uses_authoritative_tokens(
    backend: OpenCodeBackend, monkeypatch: pytest.MonkeyPatch
) -> None:
    # The live stream never emitted a step-finish (no tokens, no cost). The export has the
    # authoritative ledger; result.usage must reflect that, not the live stream's zero.
    out = '{"sessionID":"ses_x","part":{"type":"text","text":"done"}}\n'
    export = {
        "info": {
            "id": "ses_x",
            "tokens": {"input": 250, "output": 80, "cache": {"read": 12, "write": 0}},
            "cost": 0.0042,
            "model": {"id": "opencode-go/glm-5.2"},
        },
        "messages": [{"info": {}, "parts": [{"type": "text", "text": "done"}]}],
    }
    _patch_export(monkeypatch, proc=_stub_export(export))
    res = _finalize(backend, out)
    assert res.usage is not None
    assert res.usage.input_tokens == 250
    assert res.usage.output_tokens == 80
    assert res.usage.cache_read_tokens == 12
    assert abs(res.usage.cost_usd - 0.0042) < 1e-9
    assert res.usage.model == "opencode-go/glm-5.2"
    assert res.usage.source is UsageSource.NATIVE  # positive cost -> NATIVE


def test_reconcile_zero_cost_keeps_unavailable(
    backend: OpenCodeBackend, monkeypatch: pytest.MonkeyPatch
) -> None:
    # Tokens without a positive cost mean the model is unpriced (custom opencode provider, no
    # price table). Must NOT be reported as NATIVE $0; that would claim a free run.
    out = '{"sessionID":"ses_x","part":{"type":"text","text":"ok"}}\n'
    export = {
        "info": {
            "id": "ses_x",
            "tokens": {"input": 100, "output": 10, "cache": {"read": 0, "write": 0}},
            "cost": 0.0,
        },
        "messages": [{"info": {}, "parts": [{"type": "text", "text": "ok"}]}],
    }
    _patch_export(monkeypatch, proc=_stub_export(export))
    res = _finalize(backend, out)
    assert res.usage is not None
    assert res.usage.input_tokens == 100
    assert res.usage.source is UsageSource.UNAVAILABLE  # $0 cost -> still unknown, not "free"


def test_reconcile_missing_binary_is_noop(
    backend: OpenCodeBackend, monkeypatch: pytest.MonkeyPatch
) -> None:
    # No opencode on PATH -> reconciler must short-circuit, NOT crash, and the live event
    # stream's result stands.
    out = '{"sessionID":"ses_x","part":{"type":"text","text":"from-stream"}}'
    _patch_export(monkeypatch, binary_path=None)  # shutil.which("opencode") -> None
    res = _finalize(backend, out)
    assert res.text == "from-stream"  # live stream result preserved


def test_reconcile_nonzero_exit_is_noop(
    backend: OpenCodeBackend, monkeypatch: pytest.MonkeyPatch
) -> None:
    out = '{"sessionID":"ses_x","part":{"type":"text","text":"from-stream"}}'
    _patch_export(monkeypatch, proc=_stub_export({}, returncode=1))
    res = _finalize(backend, out)
    assert res.text == "from-stream"  # failed export -> live stream stands


def test_reconcile_unparseable_json_is_noop(
    backend: OpenCodeBackend, monkeypatch: pytest.MonkeyPatch
) -> None:
    out = '{"sessionID":"ses_x","part":{"type":"text","text":"from-stream"}}'
    proc = MagicMock()
    proc.returncode = 0
    proc.stdout = "this is not json"
    proc.stderr = ""
    _patch_export(monkeypatch, proc=proc)
    res = _finalize(backend, out)
    assert res.text == "from-stream"


def test_reconcile_subprocess_exception_is_noop(
    backend: OpenCodeBackend, monkeypatch: pytest.MonkeyPatch
) -> None:
    out = '{"sessionID":"ses_x","part":{"type":"text","text":"from-stream"}}'
    _patch_export(monkeypatch, side_effect=subprocess.SubprocessError("boom"))
    res = _finalize(backend, out)
    assert res.text == "from-stream"


def test_reconcile_no_text_in_export_keeps_stream_text(
    backend: OpenCodeBackend, monkeypatch: pytest.MonkeyPatch
) -> None:
    # Export has tokens but no text parts (e.g. the run was cancelled mid-write). Don't blank
    # the live stream's text - the stream stands, the export's tokens are still useful.
    out = '{"sessionID":"ses_x","part":{"type":"text","text":"stream-kept"}}'
    export = {
        "info": {"id": "ses_x", "tokens": {"input": 1, "output": 1}, "cost": 0.0},
        "messages": [{"info": {}, "parts": [{"type": "step-finish"}]}],  # no text parts
    }
    _patch_export(monkeypatch, proc=_stub_export(export))
    res = _finalize(backend, out)
    assert res.text == "stream-kept"
    assert res.usage is not None  # tokens still applied


def test_reconcile_no_tokens_in_export_keeps_stream_usage(
    backend: OpenCodeBackend, monkeypatch: pytest.MonkeyPatch
) -> None:
    # Defensive: an export with no info.tokens (older opencode? partial session?) must not
    # wipe the live stream's recorded tokens.
    out = (
        '{"sessionID":"ses_x","part":{"type":"text","text":"hello"}}'
        '\n{"part":{"type":"step-finish","reason":"stop","cost":0.001,'
        '"tokens":{"input":3,"output":2,"cache":{"read":0,"write":0}}}}'
    )
    export = {
        "info": {},  # no tokens
        "messages": [{"info": {}, "parts": [{"type": "text", "text": "hello"}]}],
    }
    _patch_export(monkeypatch, proc=_stub_export(export))
    res = _finalize(backend, out)
    assert res.text == "hello"
    assert res.usage is not None
    assert res.usage.input_tokens == 3
    assert res.usage.output_tokens == 2
    assert abs(res.usage.cost_usd - 0.001) < 1e-9


def test_finalize_skipped_on_failed_run(
    backend: OpenCodeBackend, monkeypatch: pytest.MonkeyPatch
) -> None:
    # A FAILED run must NOT trigger the export call - the live event stream's error is the
    # final answer; spending another subprocess on it is noise.
    out = '{"error":{"name":"Boom","data":{"message":"x"}}}'
    called: list[list[str]] = []

    def must_not_run(*a, **kw):  # noqa: ANN001, ARG001
        called.append(list(a[0]) if a else [])
        raise AssertionError("export must not be called on a FAILED run")

    monkeypatch.setattr("marshal_engine.backends.opencode.subprocess.run", must_not_run)
    monkeypatch.setattr(shutil, "which", lambda cmd: "/usr/bin/opencode" if cmd == "opencode" else None)
    res = _finalize(backend, out)
    assert res.status is RunStatus.FAILED
    assert called == []


def test_finalize_skipped_without_session_id(
    backend: OpenCodeBackend, monkeypatch: pytest.MonkeyPatch
) -> None:
    # A live stream that never emitted a sessionID (rare; some configurations) can't be
    # reconciled. No subprocess call should be made.
    out = '{"part":{"type":"text","text":"alone"}}'

    def must_not_run(*a, **kw):  # noqa: ANN001, ARG001
        raise AssertionError("export must not be called without a session_id")

    monkeypatch.setattr("marshal_engine.backends.opencode.subprocess.run", must_not_run)
    monkeypatch.setattr(shutil, "which", lambda cmd: "/usr/bin/opencode" if cmd == "opencode" else None)
    res = _finalize(backend, out)
    assert res.text == "alone"
    assert res.session_id is None


def test_parse_output_does_not_invoke_export(
    backend: OpenCodeBackend, monkeypatch: pytest.MonkeyPatch
) -> None:
    # parse_output is pure - export reconciliation lives in finalize() only.
    out = '{"part":{"type":"text","text":"only-stream"}}'

    def must_not_run(*a, **kw):  # noqa: ANN001, ARG001
        raise AssertionError("export must not be called from parse_output")

    monkeypatch.setattr("marshal_engine.backends.opencode.subprocess.run", must_not_run)
    monkeypatch.setattr(shutil, "which", lambda cmd: "/usr/bin/opencode" if cmd == "opencode" else None)
    res = backend.parse_output(out, "", 0)
    assert res.text == "only-stream"


def test_timeout_never_invokes_opencode_export(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    # A timed-out run must not spend +15s in a hidden `opencode export` subprocess.
    export_called: list[list[str]] = []

    def track_subprocess_run(argv: list[str], **kw: object) -> MagicMock:  # noqa: ARG001
        if len(argv) > 1 and argv[1] == "export":
            export_called.append(argv)
            raise AssertionError("export must not be called on timeout")
        proc = MagicMock()
        proc.returncode = 0
        proc.stdout = "0.0.0"
        proc.stderr = ""
        return proc

    monkeypatch.setattr("marshal_engine.backends.opencode.subprocess.run", track_subprocess_run)

    class _SlowOpenCode(OpenCodeBackend):
        def build_invocation(self, task: TaskSpec, opts: RunOpts) -> list[str]:
            return [sys.executable, "-c", "import time; time.sleep(30)"]

    res = _SlowOpenCode().run(TaskSpec(id="t", goal="g"), RunOpts(cwd=tmp_path, timeout_s=1))
    assert res.status is RunStatus.TIMED_OUT
    assert export_called == []


def test_parse_export_payload_handles_leading_non_json(
    backend: OpenCodeBackend,
) -> None:
    # Defensive: a future opencode that prepends a log line to stdout (e.g. "Exporting
    # session: ses_x") must still parse. We skip to the first '{'.
    raw = "Exporting session: ses_x\n" + json.dumps({"info": {"tokens": {"input": 1}}})
    data = OpenCodeBackend._parse_export_payload(raw)
    assert isinstance(data, dict)
    assert data["info"]["tokens"]["input"] == 1


def test_parse_export_payload_returns_none_on_garbage(
    backend: OpenCodeBackend,
) -> None:
    assert OpenCodeBackend._parse_export_payload("") is None
    assert OpenCodeBackend._parse_export_payload("not even close") is None
    assert OpenCodeBackend._parse_export_payload("{not json") is None
