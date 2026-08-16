"""Contract tests for ZCodeBackend.

These exercise the PURE hooks (`map_permission`, `build_invocation`, `resolve_launcher`) and the
JSON `parse_output` - no process spawning, no network.

The flag set asserted here was probed against the shipped zcode 0.16.3 binary, because its
`--help` advertises options the parser rejects (`--max-turns`, `--settings`, `--allowed-tools`).
These tests pin what the CLI *accepts*, not what it documents.
"""

from __future__ import annotations

import json
from pathlib import Path

import pytest

from marshal_engine import ModelSource, PermissionMode, RunOpts, RunStatus, TaskSpec, UsageSource
from marshal_engine.backends.zcode import ZCodeBackend


@pytest.fixture
def backend() -> ZCodeBackend:
    return ZCodeBackend()


def _opts(**kw: object) -> RunOpts:
    kw.setdefault("cwd", Path("/tmp/wt"))
    return RunOpts(**kw)  # type: ignore[arg-type]


@pytest.fixture
def shimmed(monkeypatch: pytest.MonkeyPatch) -> ZCodeBackend:
    """A backend whose launcher resolves to a plain PATH shim, so argv is stable to assert on."""
    monkeypatch.delenv("MARSHAL_ZCODE_BIN", raising=False)
    monkeypatch.setattr("marshal_engine.backends.zcode.shutil.which", lambda _b: "/usr/bin/zcode")
    return ZCodeBackend()


# --- launcher resolution -------------------------------------------------------------------


def test_resolve_launcher_prefers_client_env(
    backend: ZCodeBackend, monkeypatch: pytest.MonkeyPatch, tmp_path: Path
) -> None:
    monkeypatch.setenv("MARSHAL_ZCODE_BIN", "/should/not/win")
    target = tmp_path / "zcode"
    target.write_text("")
    assert backend.resolve_launcher({"ZCODE_BIN": str(target)}) == [str(target)]


def test_resolve_launcher_env_override(
    backend: ZCodeBackend, monkeypatch: pytest.MonkeyPatch, tmp_path: Path
) -> None:
    target = tmp_path / "zcode"
    monkeypatch.setenv("MARSHAL_ZCODE_BIN", str(target))
    assert backend.resolve_launcher() == [str(target)]


def test_resolve_launcher_node_prefixes_script(
    backend: ZCodeBackend, monkeypatch: pytest.MonkeyPatch, tmp_path: Path
) -> None:
    """A `.cjs` entry point must be run through node - it is not itself executable."""
    monkeypatch.setattr("marshal_engine.backends.zcode.shutil.which", lambda _b: "/usr/bin/node")
    bundle = tmp_path / "zcode.cjs"
    argv = backend.resolve_launcher({"ZCODE_BIN": str(bundle)})
    assert argv == ["/usr/bin/node", str(bundle)]


def test_resolve_launcher_falls_back_to_binary_name(
    backend: ZCodeBackend, monkeypatch: pytest.MonkeyPatch
) -> None:
    """Nothing resolvable -> `[binary]`, so base.run() emits its actionable not-found error
    instead of an exception escaping build_invocation()."""
    monkeypatch.delenv("MARSHAL_ZCODE_BIN", raising=False)
    monkeypatch.setattr("marshal_engine.backends.zcode.shutil.which", lambda _b: None)
    monkeypatch.setattr("marshal_engine.backends.zcode._BUNDLE_CANDIDATES", ())
    assert backend.resolve_launcher() == ["zcode"]


def test_check_available_false_when_nothing_resolves(
    backend: ZCodeBackend, monkeypatch: pytest.MonkeyPatch
) -> None:
    monkeypatch.delenv("MARSHAL_ZCODE_BIN", raising=False)
    monkeypatch.setattr("marshal_engine.backends.zcode.shutil.which", lambda _b: None)
    monkeypatch.setattr("marshal_engine.backends.zcode._BUNDLE_CANDIDATES", ())

    def _boom(*_a: object, **_k: object) -> None:
        raise AssertionError("must not spawn when nothing resolved")

    monkeypatch.setattr("marshal_engine.backends.zcode.subprocess.run", _boom)
    assert backend.check_available() is False


# --- permission mapping --------------------------------------------------------------------


def test_map_permission(backend: ZCodeBackend) -> None:
    assert backend.map_permission(PermissionMode.READ_ONLY) == ["--mode", "plan"]
    assert backend.map_permission(PermissionMode.SAFE_EDIT) == ["--mode", "edit"]
    assert backend.map_permission(PermissionMode.YOLO) == ["--mode", "yolo"]


def test_prompting_modes_are_never_mapped(backend: ZCodeBackend) -> None:
    """`build` ("Ask before each file change") and `auto` prompt for approval. The shared runner
    closes stdin, so mapping any tier onto them would hang or EOF-fail every headless run."""
    mapped = {flag for mode in backend.capabilities.permission_modes
              for flag in backend.map_permission(mode)}
    assert "build" not in mapped
    assert "auto" not in mapped


def test_unsupported_permission_raises(backend: ZCodeBackend) -> None:
    with pytest.raises(ValueError):
        backend.map_permission("nonsense")  # type: ignore[arg-type]


# --- invocation ----------------------------------------------------------------------------


def test_build_invocation_basic(shimmed: ZCodeBackend) -> None:
    argv = shimmed.build_invocation(
        TaskSpec(id="t1", goal="do the thing"), _opts(permission=PermissionMode.SAFE_EDIT)
    )
    assert argv[0] == "/usr/bin/zcode"
    assert argv[argv.index("--cwd") + 1] == "/tmp/wt"
    assert argv[argv.index("--mode") + 1] == "edit"
    assert "--json" in argv and "--no-color" in argv
    assert argv[-2] == "--prompt"
    assert argv[-1] == "do the thing"


def test_build_invocation_omits_flags_the_cli_rejects(shimmed: ZCodeBackend) -> None:
    """zcode 0.16.3 documents these in --help but its parser rejects them; passing one turns
    every run into a usage error."""
    argv = shimmed.build_invocation(TaskSpec(id="t1", goal="go"), _opts(model="glm-5.3"))
    for phantom in ("--max-turns", "--settings", "--allowed-tools", "--model"):
        assert phantom not in argv


def test_build_invocation_resume(shimmed: ZCodeBackend) -> None:
    argv = shimmed.build_invocation(TaskSpec(id="t1", goal="continue"), _opts(session_id="sess_abc"))
    assert argv[argv.index("--resume") + 1] == "sess_abc"


def test_build_invocation_is_deterministic(shimmed: ZCodeBackend) -> None:
    task, opts = TaskSpec(id="t1", goal="go"), _opts()
    assert shimmed.build_invocation(task, opts) == shimmed.build_invocation(task, opts)


def test_compose_prompt_includes_context_files(shimmed: ZCodeBackend) -> None:
    argv = shimmed.build_invocation(
        TaskSpec(id="t1", goal="fix bug", context_files=["a.py", "b.py"]), _opts()
    )
    assert "Relevant files:" in argv[-1]
    assert "a.py" in argv[-1] and "b.py" in argv[-1]


# --- model routing (env, not a flag) --------------------------------------------------------


def test_prepare_stamps_model_env(backend: ZCodeBackend) -> None:
    """ZCode has no --model flag; routing rides ZCODE_MODEL into the child env."""
    opts = _opts(model="builtin:zai-start-plan/glm-5.3")
    backend.prepare(opts)
    assert opts.extra_env["ZCODE_MODEL"] == "builtin:zai-start-plan/glm-5.3"


def test_prepare_without_model_stamps_nothing(backend: ZCodeBackend) -> None:
    opts = _opts()
    backend.prepare(opts)
    assert "ZCODE_MODEL" not in opts.extra_env


def test_available_models_is_static(backend: ZCodeBackend) -> None:
    catalog = backend.available_models()
    assert catalog.models == ["glm-5.3", "glm-5.2", "glm-5-turbo"]
    assert catalog.source is ModelSource.STATIC


# --- parse_output --------------------------------------------------------------------------


def _result(**kw: object) -> str:
    payload: dict[str, object] = {
        "sessionId": "sess_abc",
        "response": "done the thing",
        "projection": {"status": "completed", "turnCount": 3, "totalTokenCount": 300},
    }
    payload.update(kw)
    return json.dumps(payload)


def test_parse_output_success(backend: ZCodeBackend) -> None:
    res = backend.parse_output(
        _result(usage={"inputTokens": 100, "outputTokens": 200, "cacheReadTokens": 50}), "", 0
    )
    assert res.status is RunStatus.EXITED_CLEAN
    assert res.session_id == "sess_abc"
    assert res.text == "done the thing"
    assert res.usage is not None
    assert res.usage.input_tokens == 100
    assert res.usage.output_tokens == 200
    assert res.usage.cache_read_tokens == 50


def test_usage_is_never_native_cost(backend: ZCodeBackend) -> None:
    """The honesty invariant: ZCode reports tokens but no price, so cost stays unavailable.
    A NATIVE $0 here would assert a free run nobody reported."""
    res = backend.parse_output(_result(usage={"inputTokens": 10, "outputTokens": 5}), "", 0)
    assert res.usage is not None
    assert res.usage.source is UsageSource.UNAVAILABLE
    assert res.usage.cost_usd == 0.0


def test_usage_falls_back_to_projection_total(backend: ZCodeBackend) -> None:
    """No `usage` block, but the projection knows the token count - keep it rather than drop it."""
    res = backend.parse_output(_result(), "", 0)
    assert res.usage is not None
    assert res.usage.input_tokens == 300


def test_usage_none_when_no_tokens_reported(backend: ZCodeBackend) -> None:
    """No tokens anywhere -> no record, rather than a fabricated all-zero one."""
    res = backend.parse_output(
        json.dumps({"sessionId": "s", "response": "hi", "projection": {"status": "completed"}}),
        "",
        0,
    )
    assert res.usage is None


def test_bad_projection_status_is_failure_even_on_exit_zero(backend: ZCodeBackend) -> None:
    """A turn that errored mid-flight must not be integrated as a success."""
    res = backend.parse_output(
        _result(projection={"status": "error", "turnCount": 1, "totalTokenCount": 10}), "", 0
    )
    assert res.status is RunStatus.FAILED
    assert "error" in (res.error or "")


def test_nonzero_exit_is_failure(backend: ZCodeBackend) -> None:
    res = backend.parse_output(_result(), "", 1)
    assert res.status is RunStatus.FAILED


def test_parse_output_unparseable_is_failure(backend: ZCodeBackend) -> None:
    res = backend.parse_output("not json at all", "ProviderBusinessError: captcha", 1)
    assert res.status is RunStatus.FAILED
    assert res.exit_code == 1
    assert res.usage is None


def test_parse_output_salvages_object_after_preamble(backend: ZCodeBackend) -> None:
    res = backend.parse_output("warning: something\n" + _result(), "", 0)
    assert res.status is RunStatus.EXITED_CLEAN
    assert res.text == "done the thing"
