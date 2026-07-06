"""Contract tests for AntigravityBackend (pure hooks + text parse + trust setup; no network)."""

from __future__ import annotations

import json
from pathlib import Path

import pytest

from marshal_engine import PermissionMode, RunOpts, RunStatus, TaskSpec, UsageSource
from marshal_engine.backends.antigravity import AntigravityBackend


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
    dead = tmp_path / "gone"  # never created -> a dead trust entry that should be pruned
    backend.settings_path.write_text(
        json.dumps(
            {
                "allowNonWorkspaceAccess": True,  # an unrelated key must survive
                "trustedWorkspaces": [str(live.resolve()), str(dead.resolve())],
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
    assert str(dead.resolve()) not in tw                    # dead path pruned


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
