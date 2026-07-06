"""Tests for child-process environment hygiene (VIRTUAL_ENV scrub + user-PATH recovery)."""

from __future__ import annotations

import os
import subprocess
import sys
from pathlib import Path

import pytest

import marshal_engine.env as env_mod
from marshal_engine.env import child_env, merge_user_path, user_path


@pytest.fixture(autouse=True)
def _reset_user_path_cache() -> None:
    """Each test sees a fresh user_path() cache - the module-level memo must not leak state.

    merge_user_path() depends on user_path() returning the same thing for the whole process, so
    a leaking cache would couple test order. Reset before AND after so a mid-test assert can't
    pin a stale value into a sibling test.
    """
    env_mod._USER_PATH_CACHE = None
    yield
    env_mod._USER_PATH_CACHE = None


# --- child_env: existing behavior is unchanged by the new helpers -------------------------


def test_child_env_strips_driver_venv_pins(monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setenv("VIRTUAL_ENV", "/driver/.venv")
    monkeypatch.setenv("PYTHONHOME", "/driver/python")
    env = child_env()
    assert "VIRTUAL_ENV" not in env   # driver's venv pin removed so the worktree's own wins
    assert "PYTHONHOME" not in env


def test_child_env_preserves_path(monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setenv("PATH", "/usr/bin:/bin")
    monkeypatch.setenv("VIRTUAL_ENV", "/driver/.venv")
    env = child_env()
    assert env["PATH"] == "/usr/bin:/bin"  # PATH must survive - uv/git/the CLIs need it


def test_child_env_extra_overrides(monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setenv("VIRTUAL_ENV", "/driver/.venv")
    env = child_env({"VIRTUAL_ENV": "/wanted/.venv", "MARSHAL_X": "1"})
    assert env["VIRTUAL_ENV"] == "/wanted/.venv"  # caller can deliberately set it back
    assert env["MARSHAL_X"] == "1"


# --- user_path: derive the login-shell PATH ----------------------------------------------


def test_user_path_uses_first_responding_shell(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    # Mock subprocess.run: the "first" shell prints PATH; later candidates must not be probed.
    calls: list[list[str]] = []

    def fake_run(argv, *args, **kwargs):  # noqa: ANN001, ARG001 - mirrors subprocess.run signature
        calls.append(argv)
        if argv[0] == "/bin/zsh":
            from unittest.mock import MagicMock

            m = MagicMock()
            m.returncode = 0
            m.stdout = "/from/zsh/bin:/from/zsh/sbin\n"
            m.stderr = ""
            return m
        # Subsequent candidates should not be reached; signal that loudly if they are.
        raise AssertionError(f"unexpected shell probe: {argv!r}")

    monkeypatch.setattr(env_mod.subprocess, "run", fake_run)
    monkeypatch.setattr(env_mod, "_SHELL_CANDIDATES", ("/bin/zsh", "/bin/bash"))

    path = user_path()
    assert path == "/from/zsh/bin:/from/zsh/sbin"
    assert calls == [["/bin/zsh", "-ilc", "echo $PATH"]]


def test_user_path_falls_back_to_next_shell_on_nonzero(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    from unittest.mock import MagicMock

    def fake_run(argv, *args, **kwargs):  # noqa: ANN001, ARG001
        m = MagicMock()
        if argv[0] == "/bin/zsh":
            m.returncode = 1  # first candidate refuses (e.g. permission denied)
            m.stdout = ""
            m.stderr = "zsh: permission denied"
            return m
        m.returncode = 0
        m.stdout = "/from/bash/bin\n"
        m.stderr = ""
        return m

    monkeypatch.setattr(env_mod.subprocess, "run", fake_run)
    monkeypatch.setattr(env_mod, "_SHELL_CANDIDATES", ("/bin/zsh", "/bin/bash"))

    assert user_path() == "/from/bash/bin"


def test_user_path_returns_none_when_all_shells_fail(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    from unittest.mock import MagicMock

    def fake_run(argv, *args, **kwargs):  # noqa: ANN001, ARG001
        m = MagicMock()
        m.returncode = 1
        m.stdout = ""
        m.stderr = "no"
        return m

    monkeypatch.setattr(env_mod.subprocess, "run", fake_run)
    monkeypatch.setattr(env_mod, "_SHELL_CANDIDATES", ("/bin/zsh",))

    assert user_path() is None


def test_user_path_returns_none_on_subprocess_exception(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    import subprocess

    def fake_run(*args, **kwargs):  # noqa: ANN001, ARG001
        raise subprocess.SubprocessError("boom")

    monkeypatch.setattr(env_mod.subprocess, "run", fake_run)
    monkeypatch.setattr(env_mod, "_SHELL_CANDIDATES", ("/bin/zsh",))

    assert user_path() is None


def test_user_path_skips_unavailable_shells(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    import shutil
    from unittest.mock import MagicMock

    monkeypatch.setattr(env_mod, "_SHELL_CANDIDATES", ("/no/such/shell", "/bin/zsh"))
    monkeypatch.setattr(shutil, "which", lambda cmd: "/bin/zsh" if cmd == "/bin/zsh" else None)

    def fake_run(argv, *args, **kwargs):  # noqa: ANN001, ARG001
        m = MagicMock()
        m.returncode = 0
        m.stdout = "/x\n"
        m.stderr = ""
        return m

    monkeypatch.setattr(env_mod.subprocess, "run", fake_run)

    assert user_path() == "/x"  # the missing shell was skipped, not probed


def test_user_path_is_cached(monkeypatch: pytest.MonkeyPatch) -> None:
    from unittest.mock import MagicMock

    call_count = 0

    def fake_run(argv, *args, **kwargs):  # noqa: ANN001, ARG001
        nonlocal call_count
        call_count += 1
        m = MagicMock()
        m.returncode = 0
        m.stdout = "/cached\n"
        m.stderr = ""
        return m

    monkeypatch.setattr(env_mod.subprocess, "run", fake_run)
    monkeypatch.setattr(env_mod, "_SHELL_CANDIDATES", ("/bin/zsh",))

    assert user_path() == "/cached"
    assert user_path() == "/cached"   # second call must hit the cache
    assert user_path() == "/cached"
    assert call_count == 1


def test_user_path_caches_miss(monkeypatch: pytest.MonkeyPatch) -> None:
    # A miss must also be remembered: otherwise a broken shell config would re-spawn the shell on
    # every doctor run / every backend availability check.
    from unittest.mock import MagicMock

    call_count = 0

    def fake_run(argv, *args, **kwargs):  # noqa: ANN001, ARG001
        nonlocal call_count
        call_count += 1
        m = MagicMock()
        m.returncode = 1
        m.stdout = ""
        m.stderr = "nope"
        return m

    monkeypatch.setattr(env_mod.subprocess, "run", fake_run)
    monkeypatch.setattr(env_mod, "_SHELL_CANDIDATES", ("/bin/zsh",))

    assert user_path() is None
    assert user_path() is None
    assert call_count == 1


# --- merge_user_path: union into os.environ['PATH'] --------------------------------------


def test_merge_user_path_appends_new_dirs(
    monkeypatch: pytest.MonkeyPatch, capsys: pytest.CaptureFixture[str]
) -> None:
    monkeypatch.setenv("PATH", "/usr/bin:/bin")
    monkeypatch.delenv("MARSHAL_NO_PATH_FIX", raising=False)
    monkeypatch.setattr(env_mod, "user_path", lambda **_: "/opt/homebrew/bin:/usr/local/bin")

    changed = merge_user_path()
    assert changed is True
    # Original dirs first (so the system wins ties), appended dirs in user-path order.
    assert os.environ["PATH"] == "/usr/bin:/bin:/opt/homebrew/bin:/usr/local/bin"


def test_merge_user_path_is_noop_when_all_dirs_present(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    monkeypatch.setenv("PATH", "/opt/homebrew/bin:/usr/local/bin:/usr/bin")
    monkeypatch.delenv("MARSHAL_NO_PATH_FIX", raising=False)
    monkeypatch.setattr(env_mod, "user_path", lambda **_: "/usr/local/bin:/opt/homebrew/bin")

    assert merge_user_path() is False
    assert os.environ["PATH"] == "/opt/homebrew/bin:/usr/local/bin:/usr/bin"  # order untouched


def test_merge_user_path_is_idempotent(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    monkeypatch.setenv("PATH", "/usr/bin")
    monkeypatch.delenv("MARSHAL_NO_PATH_FIX", raising=False)
    monkeypatch.setattr(env_mod, "user_path", lambda **_: "/opt/homebrew/bin")

    assert merge_user_path() is True
    assert merge_user_path() is False   # second call: nothing left to add
    assert os.environ["PATH"] == "/usr/bin:/opt/homebrew/bin"


def test_merge_user_path_respects_opt_out(monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setenv("PATH", "/usr/bin")
    monkeypatch.setenv("MARSHAL_NO_PATH_FIX", "1")
    # If opt-out is honored, user_path() must not be called at all.
    def must_not_run(**_):
        raise AssertionError("user_path() called despite MARSHAL_NO_PATH_FIX=1")

    monkeypatch.setattr(env_mod, "user_path", must_not_run)

    assert merge_user_path() is False
    assert os.environ["PATH"] == "/usr/bin"


def test_merge_user_path_handles_no_user_path(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    monkeypatch.setenv("PATH", "/usr/bin")
    monkeypatch.delenv("MARSHAL_NO_PATH_FIX", raising=False)
    monkeypatch.setattr(env_mod, "user_path", lambda **_: None)  # all shells failed

    assert merge_user_path() is False
    assert os.environ["PATH"] == "/usr/bin"


def test_merge_user_path_dedups_overlap(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    # User PATH and current PATH both contain /usr/bin - it must appear once, in the current
    # PATH's position (the system PATH wins for ties, not the user PATH).
    monkeypatch.setenv("PATH", "/usr/bin:/bin")
    monkeypatch.delenv("MARSHAL_NO_PATH_FIX", raising=False)
    monkeypatch.setattr(
        env_mod, "user_path", lambda **_: "/usr/bin:/opt/homebrew/bin"
    )

    merge_user_path()
    assert os.environ["PATH"] == "/usr/bin:/bin:/opt/homebrew/bin"


# --- end-to-end: merged PATH actually reaches an agent subprocess -------------------------


def test_merged_path_reaches_agent_subprocess(
    monkeypatch: pytest.MonkeyPatch, tmp_path: Path
) -> None:
    # The whole reason merge_user_path exists is so an MCP-spawned opencode CLI (or any
    # agent backend) can be located when the host's PATH was stripped. Prove the chain holds
    # end-to-end: strip PATH, run merge_user_path with a fake user_path, then spawn a child
    # via subprocess.Popen(env=child_env()) and assert the child sees the merged PATH.
    monkeypatch.setenv("PATH", "/usr/bin:/bin")
    monkeypatch.delenv("MARSHAL_NO_PATH_FIX", raising=False)
    monkeypatch.setattr(
        env_mod, "user_path", lambda **_: "/opt/homebrew/bin:/Users/chiru/.local/bin"
    )
    assert merge_user_path() is True

    sentinel_dir = tmp_path / "fake-bin"
    sentinel_dir.mkdir()
    probe = sentinel_dir / "probe.py"
    probe.write_text("import os; print(os.environ.get('PATH', ''))", encoding="utf-8")

    # The child's PATH must contain BOTH the original (preserved) dirs AND the newly-merged
    # user dirs - if only the original survived, the opencode-at-/opt/homebrew/bin case is
    # still broken in fleet runs.
    proc = subprocess.run(
        [sys.executable, str(probe)],
        capture_output=True,
        text=True,
        env=child_env(),
        timeout=10,
    )
    assert proc.returncode == 0, proc.stderr
    seen = proc.stdout.strip()
    assert "/usr/bin" in seen and "/bin" in seen, seen
    assert "/opt/homebrew/bin" in seen, seen
    assert "/Users/chiru/.local/bin" in seen, seen


def test_merged_path_propagates_through_service_init(
    monkeypatch: pytest.MonkeyPatch, tmp_path: Path
) -> None:
    # MarshalService.__init__ calls merge_user_path itself (defense-in-depth for library
    # users who construct a service without going through mcp_server.main or cli.main). Even
    # if both entry points are bypassed, the service picks up the user PATH.
    from marshal_engine.config import ClientConfig, FleetConfig
    from marshal_engine.service import MarshalService

    # A no-op empty config is enough to exercise __init__'s path recovery.
    cfg = FleetConfig(clients={"x": ClientConfig(name="x", backend="opencode")})
    (tmp_path / "fleet.config.yaml").write_text("clients: {}\n", encoding="utf-8")
    (tmp_path / "repo").mkdir()
    monkeypatch.setenv("PATH", "/usr/bin:/bin")
    monkeypatch.delenv("MARSHAL_NO_PATH_FIX", raising=False)
    monkeypatch.setattr(
        env_mod, "user_path", lambda **_: "/opt/homebrew/bin"
    )

    # Construct; this should trigger merge_user_path internally.
    MarshalService(tmp_path / "repo", cfg, config_path=tmp_path / "fleet.config.yaml")

    assert "/opt/homebrew/bin" in os.environ["PATH"]
