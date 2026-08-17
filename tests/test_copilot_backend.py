"""Contract tests for CopilotBackend.

These exercise the PURE hooks (`map_permission`, `build_invocation`) and the JSONL
`parse_output` — no process spawning, no network.

The flag set and event shapes asserted here were probed against the shipped GitHub Copilot CLI
1.0.80, including live confirmation that `--mode plan` refuses a write and that `--deny-tool`
beats `--allow-all-tools`. These tests pin what the CLI *accepts and enforces*, so an upstream
change fails a test rather than a fleet run.
"""

from __future__ import annotations

import json
from pathlib import Path

import pytest

from marshal_engine import ModelSource, PermissionMode, RunOpts, RunStatus, TaskSpec, UsageSource
from marshal_engine.backends.copilot import SAFE_EDIT_DENY, CopilotBackend


@pytest.fixture
def backend() -> CopilotBackend:
    return CopilotBackend()


def _opts(**kw: object) -> RunOpts:
    kw.setdefault("cwd", Path("/tmp/wt"))
    return RunOpts(**kw)  # type: ignore[arg-type]


def _task(**kw: object) -> TaskSpec:
    kw.setdefault("id", "t1")
    kw.setdefault("goal", "do the thing")
    return TaskSpec(**kw)  # type: ignore[arg-type]


def _result_line(**kw: object) -> str:
    ev: dict[str, object] = {"type": "result", "sessionId": "sess-1", "exitCode": 0}
    ev.update(kw)
    return json.dumps(ev)


def _message_line(content: str, output_tokens: int = 0) -> str:
    return json.dumps(
        {"type": "assistant.message", "data": {"content": content, "outputTokens": output_tokens}}
    )


# --- permission mapping ----------------------------------------------------------------------


def test_read_only_uses_plan_mode(backend: CopilotBackend) -> None:
    # Verified live: plan mode denies a `create` call even alongside --allow-all-tools.
    assert backend.map_permission(PermissionMode.READ_ONLY) == [
        "--mode",
        "plan",
        "--allow-all-tools",
    ]


def test_yolo_is_unrestricted(backend: CopilotBackend) -> None:
    assert backend.map_permission(PermissionMode.YOLO) == ["--allow-all"]


def test_safe_edit_carries_every_deny_rule(backend: CopilotBackend) -> None:
    """Every curated rule must reach argv as its own --deny-tool pair — this is the enforcement."""
    flags = backend.map_permission(PermissionMode.SAFE_EDIT)
    assert flags[0] == "--allow-all-tools"
    pairs = [(flags[i], flags[i + 1]) for i in range(1, len(flags), 2)]
    assert pairs == [("--deny-tool", rule) for rule in SAFE_EDIT_DENY]


def test_safe_edit_denies_secrets_push_and_gh() -> None:
    """The overlay must cover the ways a worktree run could reach past its own boundary."""
    assert "write(.env)" in SAFE_EDIT_DENY
    assert "shell(git push)" in SAFE_EDIT_DENY
    assert "shell(gh:*)" in SAFE_EDIT_DENY


def test_yolo_has_no_deny_overlay(backend: CopilotBackend) -> None:
    # yolo intentionally drops the overlay; asserting it keeps a well-meaning edit from
    # silently turning yolo into safe-edit.
    assert "--deny-tool" not in backend.map_permission(PermissionMode.YOLO)


def test_unsupported_permission_raises(backend: CopilotBackend) -> None:
    class _Fake(str):
        pass

    with pytest.raises(ValueError):
        backend.map_permission(_Fake("nonsense"))  # type: ignore[arg-type]


# --- invocation ------------------------------------------------------------------------------


def test_build_invocation_core_flags(backend: CopilotBackend) -> None:
    argv = backend.build_invocation(_task(), _opts())
    assert argv[0] == "copilot"
    assert argv[1:3] == ["-C", "/tmp/wt"]
    assert "--output-format" in argv and argv[argv.index("--output-format") + 1] == "json"
    # The prompt is the value of -p, and comes last.
    assert argv[-2] == "-p"
    assert argv[-1] == "do the thing"


def test_build_invocation_is_headless_and_bounded(backend: CopilotBackend) -> None:
    """Each of these keeps an unattended run non-prompting and inside the worktree boundary."""
    argv = backend.build_invocation(_task(), _opts())
    # no stdin -> the ask_user tool would hit EOF
    assert "--no-ask-user" in argv
    # the built-in GitHub MCP acts on the real repo with the user's token, outside the worktree
    assert "--disable-builtin-mcps" in argv
    # an unattended session must not be remote-controlled mid-run
    assert "--no-remote" in argv
    # a background upgrade mid-fleet is nondeterminism
    assert "--no-auto-update" in argv


def test_build_invocation_passes_model(backend: CopilotBackend) -> None:
    argv = backend.build_invocation(_task(), _opts(model="claude-sonnet-5"))
    assert argv[argv.index("--model") + 1] == "claude-sonnet-5"


def test_build_invocation_omits_model_when_unset(backend: CopilotBackend) -> None:
    assert "--model" not in backend.build_invocation(_task(), _opts())


def test_resume_uses_session_id_not_resume(backend: CopilotBackend) -> None:
    """`--resume` takes an OPTIONAL value, so `--resume <id>` would parse the id as the prompt."""
    argv = backend.build_invocation(_task(), _opts(session_id="sess-9"))
    assert argv[argv.index("--session-id") + 1] == "sess-9"
    assert "--resume" not in argv


def test_build_invocation_is_deterministic(backend: CopilotBackend) -> None:
    task, opts = _task(), _opts(model="gpt-5-mini")
    assert backend.build_invocation(task, opts) == backend.build_invocation(task, opts)


def test_context_files_reach_the_prompt(backend: CopilotBackend) -> None:
    argv = backend.build_invocation(_task(context_files=["src/a.py"]), _opts())
    assert "src/a.py" in argv[-1]


def test_safe_edit_denies_are_in_the_invocation(backend: CopilotBackend) -> None:
    argv = backend.build_invocation(_task(), _opts(permission=PermissionMode.SAFE_EDIT))
    assert argv.count("--deny-tool") == len(SAFE_EDIT_DENY)


# --- parse_output ----------------------------------------------------------------------------


def test_parse_success(backend: CopilotBackend) -> None:
    stdout = "\n".join([_message_line("all done", 17), _result_line()])
    res = backend.parse_output(stdout, "", 0)
    assert res.status is RunStatus.EXITED_CLEAN
    assert res.text == "all done"
    assert res.session_id == "sess-1"
    assert res.error is None


def test_missing_result_event_is_failure(backend: CopilotBackend) -> None:
    """A bad --model or missing auth emits `Error: ...` and NO result event."""
    res = backend.parse_output('Error: Model "__nope__" is not available.\n', "", 1)
    assert res.status is RunStatus.FAILED


def test_empty_stdout_is_failure(backend: CopilotBackend) -> None:
    assert backend.parse_output("", "", 0).status is RunStatus.FAILED


def test_nonzero_reported_exit_fails_even_when_process_exited_zero(backend: CopilotBackend) -> None:
    """Trust the stricter of the two exit codes — a reported failure is not a success."""
    res = backend.parse_output(_result_line(exitCode=1), "", 0)
    assert res.status is RunStatus.FAILED
    assert res.error is not None


def test_nonzero_process_exit_fails(backend: CopilotBackend) -> None:
    assert backend.parse_output(_result_line(), "", 1).status is RunStatus.FAILED


def test_error_event_fails_the_run(backend: CopilotBackend) -> None:
    stdout = "\n".join(
        [json.dumps({"type": "error", "data": {"message": "rate limited"}}), _result_line()]
    )
    res = backend.parse_output(stdout, "", 0)
    assert res.status is RunStatus.FAILED
    assert res.error == "rate limited"


def test_denied_tool_is_not_a_run_failure(backend: CopilotBackend) -> None:
    """A deny rule refusing a call is the overlay working — the agent routes around it."""
    denied = json.dumps(
        {
            "type": "tool.execution_complete",
            "data": {"success": False, "error": {"message": "denied", "code": "denied"}},
        }
    )
    res = backend.parse_output("\n".join([denied, _message_line("used another way"), _result_line()]), "", 0)
    assert res.status is RunStatus.EXITED_CLEAN
    assert res.error is None


def test_message_deltas_do_not_duplicate_text(backend: CopilotBackend) -> None:
    """Copilot streams `assistant.message_delta` per token; only the consolidated one counts."""
    stdout = "\n".join(
        [
            json.dumps({"type": "assistant.message_delta", "data": {"deltaContent": "all "}}),
            json.dumps({"type": "assistant.message_delta", "data": {"deltaContent": "done"}}),
            _message_line("all done"),
            _result_line(),
        ]
    )
    assert backend.parse_output(stdout, "", 0).text == "all done"


def test_malformed_lines_are_skipped(backend: CopilotBackend) -> None:
    stdout = "\n".join(["not json", "{broken", _message_line("ok"), _result_line()])
    assert backend.parse_output(stdout, "", 0).status is RunStatus.EXITED_CLEAN


# --- usage honesty ---------------------------------------------------------------------------


def test_usage_is_never_priced(backend: CopilotBackend) -> None:
    """Copilot reports premiumRequests (a quota unit), never money — cost must stay unavailable."""
    stdout = "\n".join(
        [_message_line("done", 42), _result_line(usage={"premiumRequests": 3})]
    )
    usage = backend.parse_output(stdout, "", 0).usage
    assert usage is not None
    assert usage.source is UsageSource.UNAVAILABLE
    assert usage.cost_usd == 0.0
    assert usage.output_tokens == 42


def test_usage_input_tokens_stay_zero(backend: CopilotBackend) -> None:
    """Copilot never reports input tokens; inventing one would be a fabrication."""
    stdout = "\n".join([_message_line("done", 10), _result_line()])
    usage = backend.parse_output(stdout, "", 0).usage
    assert usage is not None and usage.input_tokens == 0


def test_usage_sums_across_messages(backend: CopilotBackend) -> None:
    stdout = "\n".join([_message_line("a", 5), _message_line("b", 7), _result_line()])
    usage = backend.parse_output(stdout, "", 0).usage
    assert usage is not None and usage.output_tokens == 12


def test_no_usage_when_nothing_reported(backend: CopilotBackend) -> None:
    """A zero-token, zero-change run records no usage rather than a fabricated empty one."""
    assert backend.parse_output(_result_line(), "", 0).usage is None


def test_usage_recorded_when_files_changed_without_tokens(backend: CopilotBackend) -> None:
    stdout = _result_line(usage={"codeChanges": {"filesModified": ["a.py"]}})
    assert backend.parse_output(stdout, "", 0).usage is not None


def test_capabilities_do_not_claim_native_usage(backend: CopilotBackend) -> None:
    assert backend.capabilities.native_usage is False


# --- model catalog ---------------------------------------------------------------------------


_HELP_CONFIG = """\
  `logLevel`: log level for CLI; defaults to "default".
    - "none"
    - "error"

  `model`: AI model to use for Copilot CLI; can be changed with /model command or --model flag.
    - "claude-sonnet-5"
    - "gpt-5.4-mini"
    - "gpt-5-mini"

  `contextTier`: context window tier for tiered-pricing models.
    - "default"
    - "long_context"
"""


def test_model_catalog_parses_only_the_model_section(monkeypatch: pytest.MonkeyPatch) -> None:
    """The help text quotes enum values for several keys; only the model bullets are models."""
    from marshal_engine.backends.copilot import _parse_model_catalog

    assert _parse_model_catalog(_HELP_CONFIG) == [
        "auto",
        "claude-sonnet-5",
        "gpt-5.4-mini",
        "gpt-5-mini",
    ]


def test_model_catalog_leads_with_auto() -> None:
    """`auto` is never listed in help config, yet it is the only id a free plan accepts."""
    from marshal_engine.backends.copilot import _parse_model_catalog

    assert _parse_model_catalog(_HELP_CONFIG)[0] == "auto"


def test_model_catalog_empty_without_model_section() -> None:
    from marshal_engine.backends.copilot import _parse_model_catalog

    assert _parse_model_catalog("nothing to see here") == []


def test_available_models_falls_back_to_static(
    backend: CopilotBackend, monkeypatch: pytest.MonkeyPatch
) -> None:
    """A CLI that is absent must yield the curated list tagged STATIC, never an empty PROBED."""
    monkeypatch.setattr("marshal_engine.backends.base.shutil.which", lambda _b: None)
    catalog = backend.available_models()
    assert catalog.source is ModelSource.STATIC
    assert catalog.models
