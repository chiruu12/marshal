"""Contract tests for GeminiBackend.

These exercise the PURE hooks (`map_permission`, `build_invocation`) and JSON `parse_output` -
no process spawning, no network. Runtime behaviour is unverified here (`gemini` was absent on
PATH in the adapter worktree).
"""

from __future__ import annotations

import json
from pathlib import Path

import pytest

from marshal_engine import PermissionMode, RunOpts, RunStatus, TaskSpec, UsageSource
from marshal_engine.backends.gemini import GeminiBackend, _extract_session_id, _extract_usage


@pytest.fixture
def backend() -> GeminiBackend:
    return GeminiBackend()


def _opts(**kw: object) -> RunOpts:
    kw.setdefault("cwd", Path("/tmp/wt"))
    return RunOpts(**kw)  # type: ignore[arg-type]


def test_map_permission(backend: GeminiBackend) -> None:
    assert backend.map_permission(PermissionMode.READ_ONLY) == ["--approval-mode", "plan"]
    assert backend.map_permission(PermissionMode.SAFE_EDIT) == ["--approval-mode", "yolo"]
    assert backend.map_permission(PermissionMode.YOLO) == ["--approval-mode", "yolo"]


def test_no_mode_maps_to_a_prompting_approval_mode(backend: GeminiBackend) -> None:
    """`default` and `auto_edit` both wait for a human: `auto_edit` auto-approves edits but still
    asks before shell and other non-edit tools. Headless has closed stdin and would hang."""
    for mode in PermissionMode:
        assert set(backend.map_permission(mode)) & {"default", "auto_edit"} == set()


def test_build_invocation_basic(backend: GeminiBackend) -> None:
    argv = backend.build_invocation(
        TaskSpec(id="t1", goal="do the thing"), _opts(permission=PermissionMode.SAFE_EDIT)
    )
    assert argv[0] == "gemini"
    assert argv[1:3] == ["-p", "do the thing"]
    assert "--output-format" in argv and "json" in argv
    assert "--approval-mode" in argv and "yolo" in argv
    assert "--skip-trust" in argv


def test_build_invocation_model_and_readonly(backend: GeminiBackend) -> None:
    argv = backend.build_invocation(
        TaskSpec(id="t1", goal="inspect"),
        _opts(permission=PermissionMode.READ_ONLY, model="flash"),
    )
    assert "--model" in argv and "flash" in argv
    assert "plan" in argv


def test_build_invocation_resume(backend: GeminiBackend) -> None:
    argv = backend.build_invocation(TaskSpec(id="t1", goal="continue"), _opts(session_id="sess-123"))
    i = argv.index("--resume")
    assert argv[i + 1] == "sess-123"


def test_compose_prompt_includes_context_files(backend: GeminiBackend) -> None:
    argv = backend.build_invocation(
        TaskSpec(id="t1", goal="fix bug", context_files=["a.py", "b.py"]), _opts()
    )
    prompt = argv[2]  # immediately after `-p`
    assert "Relevant files:" in prompt
    assert "a.py" in prompt and "b.py" in prompt


def test_unsupported_permission_raises(backend: GeminiBackend) -> None:
    with pytest.raises(ValueError):
        backend.map_permission("nonsense")  # type: ignore[arg-type]


def test_verifies_auth_default_false(backend: GeminiBackend) -> None:
    assert backend.verifies_auth() is False
    assert backend.account_info() is None


def test_parse_output_success(backend: GeminiBackend) -> None:
    stdout = json.dumps(
        {
            "response": "done the thing",
            "stats": {
                "models": {
                    "gemini-2.5-pro": {
                        "tokens": {"prompt": 100, "candidates": 200, "cached": 50}
                    }
                },
                "sessionId": "abc-123",
            },
            "error": None,
        }
    )
    res = backend.parse_output(stdout, "", 0)
    assert res.status is RunStatus.SUCCEEDED
    assert res.session_id == "abc-123"
    assert res.text == "done the thing"
    assert res.usage is not None
    assert res.usage.source is UsageSource.UNAVAILABLE
    assert res.usage.input_tokens == 100
    assert res.usage.output_tokens == 200
    assert res.usage.cache_read_tokens == 50


def test_parse_output_tokens_without_cost_is_unavailable(backend: GeminiBackend) -> None:
    stdout = json.dumps(
        {
            "response": "ok",
            "stats": {"models": {"m": {"tokens": {"prompt": 5, "candidates": 1}}}},
            "error": None,
        }
    )
    res = backend.parse_output(stdout, "", 0)
    assert res.usage is not None
    assert res.usage.source is UsageSource.UNAVAILABLE
    assert res.usage.cost_usd == 0.0


def test_parse_output_error_object_is_failure(backend: GeminiBackend) -> None:
    stdout = json.dumps(
        {
            "response": None,
            "stats": {},
            "error": {"type": "AuthError", "message": "not logged in"},
        }
    )
    res = backend.parse_output(stdout, "", 1)
    assert res.status is RunStatus.FAILED
    assert "not logged in" in (res.error or "")


def test_an_error_object_fails_the_run_even_on_exit_zero(backend: GeminiBackend) -> None:
    """The error object is authoritative, not the exit code: a CLI that reports a failure in its
    JSON body and still exits 0 must not be recorded as a success."""
    stdout = json.dumps(
        {
            "response": "partial text",
            "stats": {},
            "error": {"type": "QuotaError", "message": "quota exhausted"},
        }
    )
    res = backend.parse_output(stdout, "", 0)
    assert res.status is RunStatus.FAILED
    assert "quota exhausted" in (res.error or "")


def test_parse_output_unparseable_is_failure(backend: GeminiBackend) -> None:
    res = backend.parse_output("not json at all", "stderr", 1)
    assert res.status is RunStatus.FAILED
    assert res.exit_code == 1


def test_extract_session_id_nested_session() -> None:
    assert _extract_session_id({"session": {"id": "nested-1"}}) == "nested-1"


def test_extract_usage_sums_models() -> None:
    usage = _extract_usage(
        {
            "models": {
                "a": {"tokens": {"prompt": 1, "candidates": 2, "cached": 0}},
                "b": {"tokens": {"prompt": 3, "candidates": 4, "cached": 1}},
            }
        },
        "gemini",
    )
    assert usage is not None
    assert usage.input_tokens == 4
    assert usage.output_tokens == 6
    assert usage.cache_read_tokens == 1
