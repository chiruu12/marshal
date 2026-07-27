"""Contract tests for AntigravityBackend (pure hooks + text parse + trust setup; no network)."""

from __future__ import annotations

import json
import os
import sys
import threading
from collections.abc import Callable
from pathlib import Path

import pytest

from marshal_engine import PermissionMode, RunOpts, RunStatus, TaskSpec, UsageSource
from marshal_engine.backends.antigravity import AntigravityBackend, _untrust_workspace


@pytest.fixture
def backend() -> AntigravityBackend:
    return AntigravityBackend()


def _opts(**kw: object) -> RunOpts:
    kw.setdefault("cwd", Path("/tmp/wt"))
    return RunOpts(**kw)  # type: ignore[arg-type]


def test_map_permission(backend: AntigravityBackend) -> None:
    assert backend.map_permission(PermissionMode.SAFE_EDIT) == ["--dangerously-skip-permissions"]
    assert backend.map_permission(PermissionMode.YOLO) == ["--dangerously-skip-permissions"]
    with pytest.raises(ValueError, match="not supported"):
        backend.map_permission(PermissionMode.READ_ONLY)


def test_build_invocation_basic(backend: AntigravityBackend) -> None:
    argv = backend.build_invocation(
        TaskSpec(id="t1", goal="do it"), _opts(permission=PermissionMode.SAFE_EDIT)
    )
    assert argv == [
        "agy",
        "--dangerously-skip-permissions",
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
        "--add-dir",
        "/tmp/wt",
        "-m",
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
        "--add-dir",
        "/tmp/wt",
        "-p",
        "fix\n\nRelevant files:\n- a.py",
    ]


def test_parse_output_success_text(backend: AntigravityBackend) -> None:
    res = backend.parse_output("  pong  \n", "", 0)
    assert res.status is RunStatus.SUCCEEDED
    assert res.text == "pong"


def test_parse_output_success_usage_unavailable(backend: AntigravityBackend) -> None:
    res = backend.parse_output("ok", "", 0)
    assert res.status is RunStatus.SUCCEEDED
    assert res.usage is not None
    assert res.usage.backend == "antigravity"
    assert res.usage.source is UsageSource.UNAVAILABLE


def test_parse_output_nonzero_exit_with_stderr(backend: AntigravityBackend) -> None:
    res = backend.parse_output("", "auth required", 1)
    assert res.status is RunStatus.FAILED
    assert "auth required" in (res.error or "")


def test_parse_output_nonzero_exit_empty_stderr(backend: AntigravityBackend) -> None:
    res = backend.parse_output("", "", 1)
    assert res.status is RunStatus.FAILED
    assert res.error == "agy exited 1"


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


def test_verifies_auth_false_documented_gap(backend: AntigravityBackend) -> None:
    # No cheap dedicated auth/status/whoami probe on `agy`; doctor stays path-only.
    assert backend.verifies_auth() is False
    assert backend.account_info() is None


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
    assert res.status is RunStatus.SUCCEEDED, res.error
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
    assert res.status is RunStatus.SUCCEEDED, res.error
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
