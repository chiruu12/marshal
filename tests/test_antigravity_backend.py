"""Contract tests for AntigravityBackend (pure hooks + text parse + trust setup; no network)."""

from __future__ import annotations

import json
import os
import sys
import threading
from collections.abc import Callable
from pathlib import Path

import pytest

from marshal_engine import ModelSource, PermissionMode, RunOpts, RunStatus, TaskSpec, UsageSource
from marshal_engine.backends import antigravity as agy_mod
from marshal_engine.backends.antigravity import (
    MIN_AGY_VERSION,
    AntigravityBackend,
    _parse_agy_models,
    _parse_agy_usage_command,
    _parse_agy_version,
    _print_timeout,
    _untrust_workspace,
)


@pytest.fixture
def backend() -> AntigravityBackend:
    return AntigravityBackend()


def _opts(**kw: object) -> RunOpts:
    kw.setdefault("cwd", Path("/tmp/wt"))
    return RunOpts(**kw)  # type: ignore[arg-type]


def test_map_permission(backend: AntigravityBackend) -> None:
    assert backend.map_permission(PermissionMode.SAFE_EDIT) == ["--dangerously-skip-permissions"]
    assert backend.map_permission(PermissionMode.YOLO) == ["--dangerously-skip-permissions"]
    # read-only is a real tier now: `--mode plan` plans and writes nothing (verified on 1.1.13).
    assert backend.map_permission(PermissionMode.READ_ONLY) == ["--mode", "plan"]


def test_read_only_never_maps_to_skip_permissions(backend: AntigravityBackend) -> None:
    # The whole value of the read-only tier is that it cannot write. If a future edit collapses
    # it into the safe-edit/yolo mapping, a review-team fan-out silently gains write access.
    assert "--dangerously-skip-permissions" not in backend.map_permission(PermissionMode.READ_ONLY)
    assert PermissionMode.READ_ONLY in backend.capabilities.permission_modes


def test_build_invocation_basic(backend: AntigravityBackend) -> None:
    argv = backend.build_invocation(
        TaskSpec(id="t1", goal="do it"), _opts(permission=PermissionMode.SAFE_EDIT)
    )
    assert argv == [
        "agy",
        "--dangerously-skip-permissions",
        "--disable-slash-commands",
        "--output-format",
        "json",
        "--print-timeout",
        "595s",
        "--add-dir",
        "/tmp/wt",
        "-p",
        "do it",
    ]


def test_build_invocation_model(backend: AntigravityBackend) -> None:
    argv = backend.build_invocation(
        TaskSpec(id="t1", goal="x"),
        _opts(permission=PermissionMode.YOLO, model="gemini-3.1-pro"),
    )
    assert argv == [
        "agy",
        "--dangerously-skip-permissions",
        "--disable-slash-commands",
        "--output-format",
        "json",
        "--print-timeout",
        "595s",
        "--add-dir",
        "/tmp/wt",
        # Long form only — `agy` dropped the `-m` alias by 1.1.13 and its parser rejects it.
        "--model",
        "gemini-3.1-pro",
        "-p",
        "x",
    ]


def test_build_invocation_conversation(backend: AntigravityBackend) -> None:
    argv = backend.build_invocation(
        TaskSpec(id="t1", goal="cont"),
        _opts(permission=PermissionMode.SAFE_EDIT, session_id="conv-1"),
    )
    assert argv == [
        "agy",
        "--dangerously-skip-permissions",
        "--disable-slash-commands",
        "--output-format",
        "json",
        "--print-timeout",
        "595s",
        "--add-dir",
        "/tmp/wt",
        "--conversation",
        "conv-1",
        "-p",
        "cont",
    ]


def test_compose_prompt_includes_context(backend: AntigravityBackend) -> None:
    argv = backend.build_invocation(
        TaskSpec(id="t1", goal="fix", context_files=["a.py"]),
        _opts(permission=PermissionMode.SAFE_EDIT),
    )
    assert argv == [
        "agy",
        "--dangerously-skip-permissions",
        "--disable-slash-commands",
        "--output-format",
        "json",
        "--print-timeout",
        "595s",
        "--add-dir",
        "/tmp/wt",
        "-p",
        "fix\n\nRelevant files:\n- a.py",
    ]


def test_capabilities_json_without_native_cost(backend: AntigravityBackend) -> None:
    """Tokens via JSON; native_usage stays False — that flag means native COST."""
    assert backend.capabilities.json_output is True
    assert backend.capabilities.native_usage is False


def test_parse_agy_version_shapes() -> None:
    assert _parse_agy_version("1.1.8") == (1, 1, 8)
    assert _parse_agy_version("agy 1.1.7\n") == (1, 1, 7)
    assert _parse_agy_version("version: 1.2") == (1, 2, 0)
    assert _parse_agy_version("not a version") is None
    assert _parse_agy_version("") is None
    # 1.1.12 is a SAFETY floor, not a feature floor: older agy silently ignores `--mode` in
    # headless -p, so a read-only run would fall back to the default mode and could write.
    assert MIN_AGY_VERSION == (1, 1, 12)


def _stub_agy_version(
    monkeypatch: pytest.MonkeyPatch, stdout: str, *, returncode: int = 0
) -> None:
    monkeypatch.setattr(agy_mod.shutil, "which", lambda _b: "/usr/bin/agy")

    class _Proc:
        def __init__(self) -> None:
            self.returncode = returncode
            self.stdout = stdout
            self.stderr = ""

    monkeypatch.setattr(agy_mod.subprocess, "run", lambda *_a, **_k: _Proc())


def test_check_available_true_at_min_version(
    backend: AntigravityBackend, monkeypatch: pytest.MonkeyPatch
) -> None:
    _stub_agy_version(monkeypatch, "1.1.12")
    assert backend.check_available() is True


def test_check_available_false_when_too_old(
    backend: AntigravityBackend, monkeypatch: pytest.MonkeyPatch
) -> None:
    _stub_agy_version(monkeypatch, "1.1.11")
    assert backend.check_available() is False
    detail = backend.unavailable_detail()
    assert "1.1.11" in detail
    assert "1.1.12" in detail
    assert "too old" in detail


def test_check_available_false_on_unparsable_version(
    backend: AntigravityBackend, monkeypatch: pytest.MonkeyPatch
) -> None:
    _stub_agy_version(monkeypatch, "not-a-semver-build")
    assert backend.check_available() is False
    detail = backend.unavailable_detail()
    assert "unparsable" in detail
    assert "1.1.12" in detail


def test_check_available_false_when_binary_missing(
    backend: AntigravityBackend, monkeypatch: pytest.MonkeyPatch
) -> None:
    monkeypatch.setattr(agy_mod.shutil, "which", lambda _b: None)
    assert backend.check_available() is False
    assert backend.unavailable_detail() == "CLI not on PATH / not runnable"


def test_parse_output_success_text(backend: AntigravityBackend) -> None:
    # Plain-text fallback when stdout is not a JSON envelope (pre-json / envelope drift).
    res = backend.parse_output("  pong  \n", "", 0)
    assert res.status is RunStatus.EXITED_CLEAN
    assert res.text == "pong"


def test_parse_output_success_usage_unavailable(backend: AntigravityBackend) -> None:
    res = backend.parse_output("ok", "", 0)
    assert res.status is RunStatus.EXITED_CLEAN
    assert res.usage is not None
    assert res.usage.backend == "antigravity"
    assert res.usage.source is UsageSource.UNAVAILABLE


# Real envelope captured 2026-07-30 from `agy` 1.1.8:
#   agy --dangerously-skip-permissions --output-format json -p "Reply with exactly: ok"
_AGY_JSON_ENVELOPE = {
    "conversation_id": "db37ad4c-77d5-4635-b302-716c282ad6fc",
    "status": "SUCCESS",
    "response": "ok\n",
    "duration_seconds": 2.9485989999999997,
    "num_turns": 1,
    "usage": {
        "input_tokens": 11160,
        "output_tokens": 23,
        "thinking_tokens": 18,
        "cache_read_tokens": 8144,
        "total_tokens": 11183,
    },
}


def test_parse_output_json_text_and_tokens(backend: AntigravityBackend) -> None:
    res = backend.parse_output(json.dumps(_AGY_JSON_ENVELOPE), "", 0)
    assert res.status is RunStatus.EXITED_CLEAN
    assert res.text == "ok"
    assert res.session_id == "db37ad4c-77d5-4635-b302-716c282ad6fc"
    assert res.usage is not None
    assert res.usage.input_tokens == 11160
    assert res.usage.output_tokens == 23
    assert res.usage.cache_read_tokens == 8144
    assert res.usage.cost_usd == 0.0
    assert res.usage.source is UsageSource.UNAVAILABLE  # tokens yes; no USD


def test_parse_output_stream_json_terminal_result(backend: AntigravityBackend) -> None:
    # Real stream-json shape (agy 1.1.8): intermediate step_update events + terminal event:result.
    stream = "\n".join(
        [
            json.dumps(
                {
                    "event": "init",
                    "conversation_id": "f107bf29-958d-4cd7-9cc5-c9c7e8688dd3",
                    "init": {"cwd": "/tmp/wt"},
                }
            ),
            json.dumps(
                {
                    "event": "step_update",
                    "step_update": {
                        "step_type": "agent_response",
                        "text_delta": "partial\n",
                        "usage": {
                            "input_tokens": 11063,
                            "output_tokens": 24,
                            "cache_read_tokens": 8144,
                        },
                    },
                }
            ),
            json.dumps(
                {
                    "event": "result",
                    "result": {
                        "conversation_id": "f107bf29-958d-4cd7-9cc5-c9c7e8688dd3",
                        "status": "SUCCESS",
                        "response": "ok\n",
                        "usage": {
                            "input_tokens": 11160,
                            "output_tokens": 29,
                            "thinking_tokens": 23,
                            "cache_read_tokens": 8144,
                            "total_tokens": 11189,
                        },
                    },
                }
            ),
        ]
    )
    res = backend.parse_output(stream, "", 0)
    assert res.status is RunStatus.EXITED_CLEAN
    assert res.text == "ok"
    assert res.session_id == "f107bf29-958d-4cd7-9cc5-c9c7e8688dd3"
    assert res.usage is not None
    assert res.usage.input_tokens == 11160
    assert res.usage.output_tokens == 29
    assert res.usage.cache_read_tokens == 8144
    assert res.usage.source is UsageSource.UNAVAILABLE


def test_parse_output_json_missing_usage_tolerated(backend: AntigravityBackend) -> None:
    envelope = {
        "conversation_id": "c-1",
        "status": "SUCCESS",
        "response": "hello\n",
    }
    res = backend.parse_output(json.dumps(envelope), "", 0)
    assert res.status is RunStatus.EXITED_CLEAN
    assert res.text == "hello"
    assert res.session_id == "c-1"
    assert res.usage is not None
    assert res.usage.input_tokens == 0
    assert res.usage.output_tokens == 0
    assert res.usage.source is UsageSource.UNAVAILABLE


def test_parse_output_malformed_envelope_falls_back_to_text(
    backend: AntigravityBackend,
) -> None:
    res = backend.parse_output("{not json\nbut still a reply", "", 0)
    assert res.status is RunStatus.EXITED_CLEAN
    assert res.text == "{not json\nbut still a reply"
    assert res.usage is not None
    assert res.usage.source is UsageSource.UNAVAILABLE


def test_parse_output_nonzero_exit_with_stderr(backend: AntigravityBackend) -> None:
    res = backend.parse_output("", "auth required", 1)
    assert res.status is RunStatus.FAILED
    assert "auth required" in (res.error or "")


def test_parse_output_nonzero_exit_empty_stderr(backend: AntigravityBackend) -> None:
    res = backend.parse_output("", "", 1)
    assert res.status is RunStatus.FAILED
    assert res.error == "agy exited 1"


def test_parse_output_nonzero_exit_keeps_json_usage(backend: AntigravityBackend) -> None:
    """A failed run may still have emitted a JSON envelope — keep tokens for the ledger."""
    envelope = {
        "conversation_id": "fail-conv",
        "status": "FAILED",
        "response": "partial\n",
        "usage": {
            "input_tokens": 500,
            "output_tokens": 12,
            "cache_read_tokens": 100,
        },
    }
    res = backend.parse_output(json.dumps(envelope), "boom", 1)
    assert res.status is RunStatus.FAILED
    assert "boom" in (res.error or "")
    assert res.text == "partial"
    assert res.session_id == "fail-conv"
    assert res.usage is not None
    assert res.usage.input_tokens == 500
    assert res.usage.output_tokens == 12
    assert res.usage.cache_read_tokens == 100
    assert res.usage.source is UsageSource.UNAVAILABLE


def test_prepare_trusts_the_worktree(backend: AntigravityBackend, tmp_path: Path) -> None:
    # prepare() must register cwd in agy's trustedWorkspaces so headless edits land in the worktree.
    backend.settings_path = tmp_path / "settings.json"
    wt = tmp_path / "wt"
    wt.mkdir()
    backend.prepare(_opts(cwd=wt))
    data = json.loads(backend.settings_path.read_text())
    assert data["trustedWorkspaces"] == [str(wt.resolve())]


def test_prepare_preserves_other_settings_and_prunes_dead(
    backend: AntigravityBackend, tmp_path: Path
) -> None:
    backend.settings_path = tmp_path / "settings.json"
    live = tmp_path / "live"
    live.mkdir()
    # A dead entry is swept ONLY when it is one of Marshal's own worktrees. A dead path that is
    # not ours belongs to the user (an unmounted volume looks exactly like this) and must survive.
    dead = tmp_path / "repo" / ".marshal" / "worktrees" / "gone"
    user_dead = tmp_path / "users-own-project"  # never created, not ours -> must be kept
    backend.settings_path.write_text(
        json.dumps(
            {
                "allowNonWorkspaceAccess": True,  # an unrelated key must survive
                "trustedWorkspaces": [
                    str(live.resolve()), str(dead), str(user_dead)
                ],
            }
        )
    )
    wt = tmp_path / "wt"
    wt.mkdir()
    backend.prepare(_opts(cwd=wt))
    backend.prepare(_opts(cwd=wt))  # idempotent: a second call must not duplicate the entry
    data = json.loads(backend.settings_path.read_text())
    tw = data["trustedWorkspaces"]
    assert data["allowNonWorkspaceAccess"] is True          # preserved
    assert tw.count(str(wt.resolve())) == 1                 # added exactly once
    assert str(live.resolve()) in tw                        # still-existing trust kept
    assert str(dead) not in tw                              # our own dead worktree swept
    assert str(user_dead) in tw                             # a user path we did not create is kept


# --- _trust_workspace internals: unique temp filename, no torn writes under failure ---------


def test_trust_workspace_uses_unique_temp_filename(
    backend: AntigravityBackend, tmp_path: Path
) -> None:
    # Regression for the H1 finding: the previous implementation used a fixed
    # `settings.json.tmp` filename, which (a) left a stale partial file after a crash and
    # (b) raced if any future code path released the lock between write and replace. The
    # fix uses tempfile.mkstemp to mint a unique temp; this test asserts that the only file
    # left in the settings dir is the final `settings.json` - no temp leftovers.
    backend.settings_path = tmp_path / "settings.json"
    wt = tmp_path / "wt"
    wt.mkdir()
    backend.prepare(_opts(cwd=wt))
    leftover = [p.name for p in (tmp_path).iterdir() if p.name.startswith("settings.json")]
    assert leftover == ["settings.json"], f"unexpected files: {leftover}"


def test_trust_workspace_no_temp_leftover_after_concurrent_prepares(
    backend: AntigravityBackend, tmp_path: Path
) -> None:
    # N parallel prepare() calls (under the class lock) must end with ONE settings.json and
    # no orphaned .tmp files. Catches a regression where a future refactor reintroduces a
    # fixed temp name and a racing crash leaves a stale .tmp behind.
    import threading

    from marshal_engine.backends.antigravity import AntigravityBackend as _AB

    backend.settings_path = tmp_path / "settings.json"
    _AB._settings_lock = threading.Lock()  # fresh lock so this test doesn't share state

    def make_and_prepare(i: int) -> None:
        wt = tmp_path / f"wt{i}"
        wt.mkdir()
        backend.prepare(_opts(cwd=wt))

    threads = [threading.Thread(target=make_and_prepare, args=(i,)) for i in range(8)]
    for t in threads:
        t.start()
    for t in threads:
        t.join()

    settings_files = sorted(p.name for p in tmp_path.iterdir() if p.name.startswith("settings.json"))
    assert settings_files == ["settings.json"], f"orphaned temps remain: {settings_files}"
    # The final settings.json has all 8 worktrees trusted.
    data = json.loads((tmp_path / "settings.json").read_text())
    assert len(data["trustedWorkspaces"]) == 8


def test_verifies_auth_true_via_print_mode_usage(backend: AntigravityBackend) -> None:
    # Explicit overrides (not base defaults), so the probe stays documented in the adapter.
    assert "account_info" in AntigravityBackend.__dict__
    assert "verifies_auth" in AntigravityBackend.__dict__
    assert backend.verifies_auth() is True


def test_available_models_parses_cli_rows(
    backend: AntigravityBackend, monkeypatch: pytest.MonkeyPatch
) -> None:
    class _Proc:
        returncode = 0
        # Real 1.1.13 shape: `id<TAB>Human Label`. The old fixture omitted the label, which is
        # why keeping the whole line looked correct in tests and failed against the CLI.
        stdout = (
            "gemini-3.1-pro-high\tGemini 3.1 Pro (High)\n"
            "claude-sonnet-4-6\tClaude Sonnet 4.6 (Thinking)\n"
        )
        stderr = ""

    calls: list[list[str]] = []

    def _run(argv: list[str], **_kw: object) -> _Proc:
        calls.append(list(argv))
        return _Proc()

    monkeypatch.setattr(
        "marshal_engine.backends.base.shutil.which", lambda _b: "/usr/bin/agy"
    )
    monkeypatch.setattr("marshal_engine.backends.base.subprocess.run", _run)
    catalog = backend.available_models()
    assert catalog.models == ["gemini-3.1-pro-high", "claude-sonnet-4-6"]
    assert catalog.source is ModelSource.PROBED
    assert calls and calls[0] == ["agy", "models"]


def test_available_models_static_fallback_when_binary_missing(
    backend: AntigravityBackend, monkeypatch: pytest.MonkeyPatch
) -> None:
    monkeypatch.setattr("marshal_engine.backends.base.shutil.which", lambda _b: None)
    catalog = backend.available_models()
    assert "gemini-3.1-pro-high" in catalog.models
    assert "claude-sonnet-4-6" in catalog.models
    assert catalog.source is ModelSource.STATIC
    # Every fallback id must be one the CLI accepts. A bare family name is rejected outright
    # ("requires --effort"), so shipping one guarantees a failed run on the fallback path.
    assert not any(m in {"gemini-3.5-flash", "claude-sonnet-4.6", "gpt-oss-120b"}
                   for m in catalog.models)


# --- trustedWorkspaces transaction (run() adds on prepare, removes on completion) -------------


@pytest.fixture
def fake_agy(
    tmp_path_factory: pytest.TempPathFactory, monkeypatch: pytest.MonkeyPatch
) -> Callable[[], None]:
    """Install a fake ``agy`` on PATH so ``AntigravityBackend.run()`` exercises the real loop."""

    def _install() -> None:
        bindir = tmp_path_factory.mktemp("fake-agy-bin")
        impl = bindir / "impl.py"
        impl.write_text('print("ok")\n', encoding="utf-8")
        shim = bindir / "agy"
        shim.write_text(f'#!/bin/sh\nexec "{sys.executable}" "{impl}" "$@"\n', encoding="utf-8")
        shim.chmod(0o755)
        monkeypatch.setenv("PATH", f"{bindir}{os.pathsep}{os.environ.get('PATH', '')}")

    return _install


def test_run_removes_trust_entry_after_completion(
    backend: AntigravityBackend, tmp_path: Path, fake_agy: Callable[[], None]
) -> None:
    fake_agy()
    backend.settings_path = tmp_path / "settings.json"
    wt = tmp_path / "wt"
    wt.mkdir()
    res = backend.run(TaskSpec(id="t", goal="x"), _opts(cwd=wt))
    assert res.status is RunStatus.EXITED_CLEAN, res.error
    assert not backend.settings_path.exists() or json.loads(backend.settings_path.read_text()).get(
        "trustedWorkspaces", []
    ) == []


def test_run_preserves_user_trusted_paths_after_completion(
    backend: AntigravityBackend, tmp_path: Path, fake_agy: Callable[[], None]
) -> None:
    fake_agy()
    backend.settings_path = tmp_path / "settings.json"
    user_wt = tmp_path / "user-trusted"
    user_wt.mkdir()
    wt = tmp_path / "wt"
    wt.mkdir()
    backend.settings_path.write_text(
        json.dumps({"trustedWorkspaces": [str(user_wt.resolve())]}), encoding="utf-8"
    )
    res = backend.run(TaskSpec(id="t", goal="x"), _opts(cwd=wt))
    assert res.status is RunStatus.EXITED_CLEAN, res.error
    data = json.loads(backend.settings_path.read_text())
    assert data["trustedWorkspaces"] == [str(user_wt.resolve())]
    assert str(wt.resolve()) not in data["trustedWorkspaces"]


def test_prepare_fails_closed_on_malformed_settings(
    backend: AntigravityBackend, tmp_path: Path
) -> None:
    backend.settings_path = tmp_path / "settings.json"
    original = b"{not-json\n"
    backend.settings_path.write_bytes(original)
    wt = tmp_path / "wt"
    wt.mkdir()
    res = backend.run(TaskSpec(id="t", goal="x"), _opts(cwd=wt))
    assert res.status is RunStatus.FAILED
    assert "not valid JSON" in (res.error or "")
    assert backend.settings_path.read_bytes() == original


def test_run_teardown_swallows_missing_settings(
    backend: AntigravityBackend, tmp_path: Path, capsys: pytest.CaptureFixture[str]
) -> None:
    wt = tmp_path / "wt"
    wt.mkdir()
    missing = tmp_path / "settings.json"
    _untrust_workspace(missing, wt, threading.Lock())
    assert "missing during trust cleanup" in capsys.readouterr().err


@pytest.mark.skipif(os.geteuid() == 0, reason="root ignores file permission bits")
def test_run_teardown_swallows_unreadable_settings(
    backend: AntigravityBackend, tmp_path: Path, capsys: pytest.CaptureFixture[str]
) -> None:
    wt = tmp_path / "wt"
    wt.mkdir()
    settings = tmp_path / "settings.json"
    settings.write_text(json.dumps({"trustedWorkspaces": [str(wt.resolve())]}), encoding="utf-8")
    os.chmod(settings, 0o000)
    try:
        _untrust_workspace(settings, wt, threading.Lock())
    finally:
        os.chmod(settings, 0o644)
    assert "cannot read" in capsys.readouterr().err
    assert str(wt.resolve()) in json.loads(settings.read_text())["trustedWorkspaces"]


def test_run_keeps_a_workspace_the_user_already_trusted(
    backend: AntigravityBackend, tmp_path: Path
) -> None:
    """REGRESSION: teardown revoked trust the USER granted, redirecting their later agy edits
    to the scratch dir. Only revoke what this run introduced."""
    wt = tmp_path / "wt"
    wt.mkdir()
    settings = tmp_path / "settings.json"
    settings.write_text(
        json.dumps({"trustedWorkspaces": [str(wt.resolve())]}), encoding="utf-8"
    )
    backend.settings_path = settings
    backend.run(TaskSpec(id="t", goal="x"), _opts(cwd=wt))
    data = json.loads(settings.read_text(encoding="utf-8"))
    assert str(wt.resolve()) in data["trustedWorkspaces"]


def test_trust_does_not_sweep_a_user_path_that_is_merely_unavailable(
    backend: AntigravityBackend, tmp_path: Path
) -> None:
    """A user's trusted path on an unmounted volume must survive; only Marshal's own dead
    worktrees are swept."""
    wt = tmp_path / "wt"
    wt.mkdir()
    settings = tmp_path / "settings.json"
    user_path = "/Volumes/external/project"
    stale_marshal = str(tmp_path / "repo" / ".marshal" / "worktrees" / "gone")
    settings.write_text(
        json.dumps({"trustedWorkspaces": [user_path, stale_marshal]}), encoding="utf-8"
    )
    backend.settings_path = settings
    backend.prepare(_opts(cwd=wt))
    trusted = json.loads(settings.read_text(encoding="utf-8"))["trustedWorkspaces"]
    assert user_path in trusted, "a user path that is merely unavailable was destroyed"
    assert stale_marshal not in trusted, "Marshal's own dead worktree should be swept"


def test_untrust_leaves_other_unavailable_paths_alone(
    backend: AntigravityBackend, tmp_path: Path
) -> None:
    wt = tmp_path / "wt"
    wt.mkdir()
    settings = tmp_path / "settings.json"
    user_path = "/Volumes/external/project"
    settings.write_text(json.dumps({"trustedWorkspaces": [user_path]}), encoding="utf-8")
    backend.settings_path = settings
    backend.run(TaskSpec(id="t", goal="x"), _opts(cwd=wt))
    trusted = json.loads(settings.read_text(encoding="utf-8"))["trustedWorkspaces"]
    assert trusted == [user_path]


def test_concurrent_runs_on_one_cwd_do_not_revoke_user_trust(
    backend: AntigravityBackend, tmp_path: Path
) -> None:
    """REGRESSION: bookkeeping keyed on cwd with a bool let the SECOND run's teardown fall back to
    "assume we added it" after the first consumed the key - silently deleting the user's entry."""
    wt = tmp_path / "wt"
    wt.mkdir()
    settings = tmp_path / "settings.json"
    settings.write_text(
        json.dumps({"trustedWorkspaces": [str(wt.resolve())]}), encoding="utf-8"
    )
    backend.settings_path = settings
    # Two runs share one worktree path; neither added the entry (the user already trusted it).
    backend.run(TaskSpec(id="a", goal="x"), _opts(cwd=wt))
    backend.run(TaskSpec(id="b", goal="x"), _opts(cwd=wt))
    trusted = json.loads(settings.read_text(encoding="utf-8"))["trustedWorkspaces"]
    assert str(wt.resolve()) in trusted, "the user's own trust entry was revoked"


def test_teardown_without_bookkeeping_leaves_the_entry_alone(
    backend: AntigravityBackend, tmp_path: Path
) -> None:
    """If we cannot prove we added it, leave it: a stray entry is recoverable, a deleted user
    grant is not."""
    wt = tmp_path / "wt"
    wt.mkdir()
    settings = tmp_path / "settings.json"
    settings.write_text(
        json.dumps({"trustedWorkspaces": [str(wt.resolve())]}), encoding="utf-8"
    )
    backend.settings_path = settings
    AntigravityBackend._trust_added.clear()
    _untrust_if_ours = getattr(backend, "run")
    _untrust_if_ours(TaskSpec(id="c", goal="x"), _opts(cwd=wt))
    trusted = json.loads(settings.read_text(encoding="utf-8"))["trustedWorkspaces"]
    assert str(wt.resolve()) in trusted


def test_overlapping_runs_keep_trust_until_the_last_one_finishes(
    backend: AntigravityBackend, tmp_path: Path
) -> None:
    """REGRESSION: run A introduced the entry, run B joined; A's teardown revoked it while B's
    agy process was still live, redirecting B's edits to the scratch dir."""
    wt = tmp_path / "wt"
    wt.mkdir()
    settings = tmp_path / "settings.json"
    backend.settings_path = settings
    AntigravityBackend._trust_added.clear()
    key = str(wt.resolve())

    backend.prepare(_opts(cwd=wt))  # run A introduces the entry
    backend.prepare(_opts(cwd=wt))  # run B joins the same path
    assert key in json.loads(settings.read_text(encoding="utf-8"))["trustedWorkspaces"]

    backend.release_trust(_opts(cwd=wt))  # A finishes
    assert key in json.loads(settings.read_text(encoding="utf-8"))["trustedWorkspaces"], (
        "trust was revoked while a sibling run was still using it"
    )

    backend.release_trust(_opts(cwd=wt))  # B finishes
    assert key not in json.loads(settings.read_text(encoding="utf-8"))["trustedWorkspaces"]


def test_a_failed_prepare_does_not_release_a_siblings_claim(
    backend: AntigravityBackend, tmp_path: Path
) -> None:
    """REGRESSION: teardown ran unconditionally, so a run whose `prepare()` raised before
    registering decremented a SIBLING's live claim - revoking trust while that agy process was
    still running and silently redirecting its edits to the scratch dir."""
    wt = tmp_path / "wt"
    wt.mkdir()
    settings = tmp_path / "settings.json"
    backend.settings_path = settings
    AntigravityBackend._trust_added.clear()
    key = str(wt.resolve())

    backend.prepare(_opts(cwd=wt))  # sibling run A holds a real claim
    assert key in json.loads(settings.read_text(encoding="utf-8"))["trustedWorkspaces"]

    # Run B never registered (its prepare failed). Its teardown must be a no-op.
    settings.write_text("{ not json", encoding="utf-8")  # what B's prepare would have choked on
    settings.write_text(
        json.dumps({"trustedWorkspaces": [key]}), encoding="utf-8"
    )
    import threading

    def b_teardown_only() -> None:  # a different thread = a different run, holding no claim
        backend.release_trust(_opts(cwd=wt))

    t = threading.Thread(target=b_teardown_only)
    t.start()
    t.join(timeout=10)

    trusted = json.loads(settings.read_text(encoding="utf-8"))["trustedWorkspaces"]
    assert key in trusted, "a run that never claimed released the sibling's live grant"

    backend.release_trust(_opts(cwd=wt))  # A finishes for real
    assert key not in json.loads(settings.read_text(encoding="utf-8"))["trustedWorkspaces"]


def test_teardown_and_a_new_prepare_cannot_interleave(
    backend: AntigravityBackend, tmp_path: Path
) -> None:
    """REGRESSION: teardown dropped the bookkeeping, unlocked, THEN removed the settings entry.

    A new run's prepare() could slot into that gap, see the entry still present, record it as
    user-owned - and then the old teardown deleted it, leaving the new run with no trust at all
    and its agy edits silently redirected to the scratch dir.
    """
    wt = tmp_path / "wt"
    wt.mkdir()
    settings = tmp_path / "settings.json"
    backend.settings_path = settings
    AntigravityBackend._trust_added.clear()
    key = str(wt.resolve())

    backend.prepare(_opts(cwd=wt))          # run A introduces the entry
    backend.release_trust(_opts(cwd=wt))    # A finishes: entry must be gone, atomically
    assert key not in json.loads(settings.read_text(encoding="utf-8"))["trustedWorkspaces"]

    # A fresh run now re-introduces it and owns it outright - no stale provenance survived.
    backend.prepare(_opts(cwd=wt))
    assert key in json.loads(settings.read_text(encoding="utf-8"))["trustedWorkspaces"]
    assert AntigravityBackend._trust_added[key][0] is True, "provenance did not reset for the new run"


# --- model id parsing ---------------------------------------------------------------------


def test_parse_agy_models_drops_the_label_half() -> None:
    # `agy models` prints `id<TAB>Human Label`. Returning the joined line makes the CLI reject
    # the run with "invalid model selection", so a driver copying an id out of `list_models`
    # got a guaranteed failure.
    rows = "gemini-3.7-flash-high\tGemini 3.7 Flash (High)\ngpt-oss-120b-medium\tGPT-OSS 120B\n"
    assert _parse_agy_models(rows) == ["gemini-3.7-flash-high", "gpt-oss-120b-medium"]


def test_parse_agy_models_tolerates_bare_rows_and_progress_noise() -> None:
    # The progress line is on stderr as of 1.1.13; drop it defensively if it ever moves.
    assert _parse_agy_models("Fetching available models...\ngemini-3.1-pro-low\n\n") == [
        "gemini-3.1-pro-low"
    ]
    assert _parse_agy_models("") == []


# --- the /usage auth probe ----------------------------------------------------------------


_USAGE_RESPONSE = (
    "Gemini Models\tWeekly Limit Remaining\t93%\t2026-08-18T21:11:07Z\n"
    "Gemini Models\tFive Hour Limit Remaining\t100%\t2026-08-19T01:11:04Z\n"
    "Claude and GPT models\tWeekly Limit Remaining\t100%\t2026-08-25T20:11:04Z\n"
)


def test_parse_agy_usage_command_summarises_weekly_quota_only() -> None:
    payload = json.dumps({"status": "SUCCESS", "response": _USAGE_RESPONSE})
    info = _parse_agy_usage_command(payload)
    assert info is not None
    assert "Gemini Models 93%" in info["plan"]
    assert "Claude and GPT models 100%" in info["plan"]
    # The five-hour window refills on its own; it is noise on a doctor line.
    assert "Five Hour" not in info["plan"]


def test_parse_agy_usage_command_reports_logged_in_when_rows_drift() -> None:
    # The CLI answered, so we are authenticated - but an unparsed row is not evidence of
    # headroom, so no quota figure is invented.
    payload = json.dumps({"status": "SUCCESS", "response": "something else entirely\n"})
    assert _parse_agy_usage_command(payload) == {"plan": "logged-in"}


@pytest.mark.parametrize(
    "payload",
    [
        json.dumps({"status": "ERROR", "response": "", "error": "not signed in"}),
        json.dumps({"status": "SUCCESS", "response": "   "}),
        json.dumps({"status": "SUCCESS"}),
        "not json at all",
        "",
    ],
)
def test_parse_agy_usage_command_fails_closed(payload: str) -> None:
    # Doctor reads None as "not authenticated (or the probe failed)" and FAILs. A false negative
    # costs a re-run; a false OK costs a whole fan-out that dies on its first real call.
    assert _parse_agy_usage_command(payload) is None


def test_account_info_probes_usage_without_spending_quota(
    backend: AntigravityBackend, monkeypatch: pytest.MonkeyPatch
) -> None:
    calls: list[list[str]] = []

    class _Proc:
        returncode = 0
        stdout = json.dumps(
            {
                "status": "SUCCESS",
                "response": _USAGE_RESPONSE,
                "usage": {"total_tokens": 0},
            }
        )
        stderr = ""

    def _run(argv: list[str], **_kw: object) -> _Proc:
        calls.append(list(argv))
        return _Proc()

    monkeypatch.setattr(agy_mod.shutil, "which", lambda _b: "/usr/bin/agy")
    monkeypatch.setattr(agy_mod.subprocess, "run", _run)
    info = backend.account_info()
    assert info is not None and info["plan"].startswith("logged-in")
    # The probe must stay a print-mode slash command: it is answered by the CLI itself, with no
    # agent turn and no quota spent. Anything else here would bill the user to run `doctor`.
    assert calls == [["agy", "-p", "/usage", "--output-format", "json"]]


def test_account_info_none_when_binary_missing(
    backend: AntigravityBackend, monkeypatch: pytest.MonkeyPatch
) -> None:
    monkeypatch.setattr(agy_mod.shutil, "which", lambda _b: None)
    assert backend.account_info() is None


def test_account_info_never_raises_on_probe_failure(
    backend: AntigravityBackend, monkeypatch: pytest.MonkeyPatch
) -> None:
    monkeypatch.setattr(agy_mod.shutil, "which", lambda _b: "/usr/bin/agy")

    def _boom(*_a: object, **_k: object) -> None:
        raise OSError("no exec")

    monkeypatch.setattr(agy_mod.subprocess, "run", _boom)
    assert backend.account_info() is None


def test_account_info_none_on_nonzero_exit(
    backend: AntigravityBackend, monkeypatch: pytest.MonkeyPatch
) -> None:
    class _Proc:
        returncode = 1
        stdout = ""
        stderr = "not signed in"

    monkeypatch.setattr(agy_mod.shutil, "which", lambda _b: "/usr/bin/agy")
    monkeypatch.setattr(agy_mod.subprocess, "run", lambda *_a, **_k: _Proc())
    assert backend.account_info() is None


# --- read-only leaves the host-global settings alone ---------------------------------------


def test_read_only_run_writes_no_trust_entry(
    backend: AntigravityBackend, tmp_path: Path
) -> None:
    settings = tmp_path / "settings.json"
    backend.settings_path = settings
    wt = tmp_path / "wt"
    wt.mkdir()
    backend.prepare(_opts(cwd=wt, permission=PermissionMode.READ_ONLY))
    # A plan-mode run cannot write, so it has no reason to mutate a HOST-GLOBAL settings file -
    # least of all in a read-only fan-out across many worktrees.
    assert not settings.exists()
    # Teardown must stay safe for a run that never claimed anything.
    backend.release_trust(_opts(cwd=wt, permission=PermissionMode.READ_ONLY))
    assert not settings.exists()


def test_read_only_teardown_does_not_release_a_sibling_write_runs_claim(
    backend: AntigravityBackend, tmp_path: Path
) -> None:
    settings = tmp_path / "settings.json"
    backend.settings_path = settings
    wt = tmp_path / "wt"
    wt.mkdir()
    backend.prepare(_opts(cwd=wt, permission=PermissionMode.SAFE_EDIT))
    backend.release_trust(_opts(cwd=wt, permission=PermissionMode.READ_ONLY))
    # The read-only run never claimed the entry, so its teardown must not pull trust out from
    # under the safe-edit run still relying on it.
    trusted = json.loads(settings.read_text())["trustedWorkspaces"]
    assert str(wt.resolve()) in trusted
    backend.release_trust(_opts(cwd=wt, permission=PermissionMode.SAFE_EDIT))
    assert json.loads(settings.read_text())["trustedWorkspaces"] == []


def test_build_invocation_read_only_uses_plan_mode(backend: AntigravityBackend) -> None:
    argv = backend.build_invocation(
        TaskSpec(id="t1", goal="review this"), _opts(permission=PermissionMode.READ_ONLY)
    )
    assert argv[:3] == ["agy", "--mode", "plan"]
    assert argv[-2:] == ["-p", "review this"]


# --- agy's own print deadline --------------------------------------------------------------


def test_print_timeout_lands_just_inside_the_run_timeout() -> None:
    # agy's default is 5 minutes, so leaving it alone silently truncated every longer run at 5m
    # with "timeout waiting for response" regardless of what the engine was told.
    assert _print_timeout(600) == "595s"
    assert _print_timeout(30) == "25s"


def test_print_timeout_never_emits_zero_or_negative() -> None:
    # agy reads `0s` as "no deadline", which would invert the whole point; a negative duration
    # would not parse at all. A very short run must still produce a valid, bounded deadline.
    assert _print_timeout(5) == "1s"
    assert _print_timeout(1) == "1s"
    assert _print_timeout(0) == "1s"


def test_build_invocation_derives_print_timeout_from_opts(
    backend: AntigravityBackend,
) -> None:
    argv = backend.build_invocation(
        TaskSpec(id="t1", goal="x"), _opts(permission=PermissionMode.SAFE_EDIT, timeout_s=1800)
    )
    assert "--print-timeout" in argv
    assert argv[argv.index("--print-timeout") + 1] == "1795s"


# --- slash-command hijack -------------------------------------------------------------------


def test_write_modes_disable_slash_expansion(backend: AntigravityBackend) -> None:
    # Without this, a goal beginning with "/" is parsed as a CLI slash command and the agent
    # never runs at all (status=ERROR, num_turns=0) — a task that silently never happened.
    for mode in (PermissionMode.SAFE_EDIT, PermissionMode.YOLO):
        argv = backend.build_invocation(
            TaskSpec(id="t1", goal="/usr/local needs fixing"), _opts(permission=mode)
        )
        assert "--disable-slash-commands" in argv
        assert argv[-1] == "/usr/local needs fixing"


def test_read_only_must_not_disable_slash_expansion(backend: AntigravityBackend) -> None:
    # agy warns "--mode plan has no effect while slash command expansion is disabled" and means
    # it: with both flags the write was stopped only by the DEFAULT mode's unattended denial, not
    # by the plan tier. Passing this flag here would leave read-only unenforced.
    argv = backend.build_invocation(
        TaskSpec(id="t1", goal="review it"), _opts(permission=PermissionMode.READ_ONLY)
    )
    assert "--disable-slash-commands" not in argv
    assert argv[:3] == ["agy", "--mode", "plan"]


@pytest.mark.parametrize("goal", ["/usage now", "  /help me", "/model"])
def test_read_only_refuses_a_slash_leading_prompt(
    backend: AntigravityBackend, goal: str
) -> None:
    # Refused in build_invocation — a pure function, before any worktree is created. Leading
    # whitespace does not escape agy's parse, so it must not escape ours either.
    with pytest.raises(ValueError, match="must not begin with"):
        backend.build_invocation(
            TaskSpec(id="t1", goal=goal), _opts(permission=PermissionMode.READ_ONLY)
        )


def test_read_only_refusal_names_the_way_out(backend: AntigravityBackend) -> None:
    with pytest.raises(ValueError) as excinfo:
        backend.build_invocation(
            TaskSpec(id="t1", goal="/usage"), _opts(permission=PermissionMode.READ_ONLY)
        )
    msg = str(excinfo.value)
    # A driver reading this must learn why the obvious fix is unavailable and what to do instead.
    assert "--disable-slash-commands" in msg
    assert "safe-edit" in msg


def test_write_modes_accept_a_slash_leading_prompt(backend: AntigravityBackend) -> None:
    # The refusal is specific to read-only; write modes carry the flag that makes it safe.
    argv = backend.build_invocation(
        TaskSpec(id="t1", goal="/usage"), _opts(permission=PermissionMode.SAFE_EDIT)
    )
    assert argv[-1] == "/usage"


def test_read_only_slash_check_sees_the_composed_prompt(backend: AntigravityBackend) -> None:
    # The check must run on the COMPOSED prompt, not the raw goal: context files are appended
    # after the goal, so a slash-leading goal stays slash-leading and must still be refused.
    with pytest.raises(ValueError, match="must not begin with"):
        backend.build_invocation(
            TaskSpec(id="t1", goal="/usage", context_files=["a.py"]),
            _opts(permission=PermissionMode.READ_ONLY),
        )
