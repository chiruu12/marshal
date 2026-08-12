"""Tests for child-process environment hygiene (allowlist + user-PATH recovery)."""

from __future__ import annotations

import os
import subprocess
import sys
from pathlib import Path

import pytest

import marshal_engine.runtime.env as env_mod
from marshal_engine.backends.base import CodingAgentBackend
from marshal_engine.backends.claude_code import ClaudeCodeBackend
from marshal_engine.backends.cursor import CursorBackend
from marshal_engine.runtime.env import child_env, merge_user_path, redact_secrets, user_path
from marshal_engine.runtime.logs import RunLogStore
from marshal_engine.core.types import (
    AgentResult,
    Capabilities,
    PermissionMode,
    RunOpts,
    RunStatus,
    TaskSpec,
)


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


# --- child_env: allowlist (operational base + per-backend credentials) --------------------


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


def test_child_env_strips_marshal_session_vars(monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setenv("MARSHAL_CONFIG", "/driver/fleet.config.yaml")
    monkeypatch.setenv("MARSHAL_REPO", "/driver/repo")
    env = child_env()
    assert "MARSHAL_CONFIG" not in env
    assert "MARSHAL_REPO" not in env


def test_child_env_marshal_prefix_still_scrubbed_not_prefix_siblings(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    # MARSHAL_* is always dropped. Near-miss names are not special-cased either: they fall under
    # the allowlist (dropped unless operational / credential / client / extra). Updated from the
    # pre-allowlist test that asserted ambient inheritance of MARSHALL_X / NOT_MARSHAL_CONFIG.
    monkeypatch.setenv("MARSHAL_CONFIG", "/driver/fleet.config.yaml")
    monkeypatch.setenv("MARSHALL_X", "ambient")
    monkeypatch.setenv("NOT_MARSHAL_CONFIG", "ambient")
    env = child_env()
    assert "MARSHAL_CONFIG" not in env
    assert "MARSHALL_X" not in env
    assert "NOT_MARSHAL_CONFIG" not in env
    # Client escape hatch can still pass a non-secret near-miss name.
    env2 = child_env(client={"MARSHALL_X": "via-client", "NOT_MARSHAL_CONFIG": "via-client"})
    assert env2["MARSHALL_X"] == "via-client"
    assert env2["NOT_MARSHAL_CONFIG"] == "via-client"


def test_child_env_extra_overrides_marshal_scrub(monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setenv("MARSHAL_CONFIG", "/driver/fleet.config.yaml")
    env = child_env({"MARSHAL_CONFIG": "/worktree/fleet.config.yaml"})
    assert env["MARSHAL_CONFIG"] == "/worktree/fleet.config.yaml"


def test_child_env_applies_client_vars(monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setenv("FOO", "driver-value")
    # FOO is not on the allowlist; client env is the escape hatch that still delivers it.
    env = child_env(client={"FOO": "client-value"})
    assert env["FOO"] == "client-value"
    assert "FOO" not in child_env()  # ambient FOO is dropped without client/extra


def test_child_env_client_cannot_resurrect_virtual_env(monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setenv("VIRTUAL_ENV", "/driver/.venv")
    env = child_env(client={"VIRTUAL_ENV": "/client/.venv"})
    assert "VIRTUAL_ENV" not in env


def test_child_env_extra_still_overrides_after_client(monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setenv("VIRTUAL_ENV", "/driver/.venv")
    env = child_env({"VIRTUAL_ENV": "/wanted/.venv"}, client={"VIRTUAL_ENV": "/client/.venv"})
    assert env["VIRTUAL_ENV"] == "/wanted/.venv"


def test_child_env_drops_unrelated_secrets(monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setenv("AWS_SECRET_ACCESS_KEY", "aws-secret-value-long")
    monkeypatch.setenv("GH_TOKEN", "ghp_unrelated_token_value")
    monkeypatch.setenv("EASTROUTER_API_KEY", "east-secret-value")
    env = child_env(credentials=("CURSOR_API_KEY",))
    assert "AWS_SECRET_ACCESS_KEY" not in env
    assert "GH_TOKEN" not in env
    assert "EASTROUTER_API_KEY" not in env


def test_child_env_forwards_only_requested_credentials(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    monkeypatch.setenv("ANTHROPIC_API_KEY", "sk-ant-parent-value-long")
    monkeypatch.setenv("CURSOR_API_KEY", "cursor-parent-value-long")
    cursor_env = child_env(credentials=CursorBackend.credential_env_vars)
    assert "CURSOR_API_KEY" in cursor_env
    assert "ANTHROPIC_API_KEY" not in cursor_env
    claude_env = child_env(credentials=ClaudeCodeBackend.credential_env_vars)
    assert "ANTHROPIC_API_KEY" in claude_env
    assert "CURSOR_API_KEY" not in claude_env


def test_child_env_keeps_home(monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setenv("HOME", "/Users/test")
    assert child_env()["HOME"] == "/Users/test"


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
    # Pin which() so the fake candidates "exist" on any OS (Linux CI has no /bin/zsh).
    monkeypatch.setattr(env_mod.shutil, "which", lambda cmd: cmd)

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
    # Pin which() so the fake candidates "exist" on any OS (Linux CI has no /bin/zsh).
    monkeypatch.setattr(env_mod.shutil, "which", lambda cmd: cmd)

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
    # Pin which() so the fake candidates "exist" on any OS (Linux CI has no /bin/zsh).
    monkeypatch.setattr(env_mod.shutil, "which", lambda cmd: cmd)

    assert user_path(fallback_dirs=()) is None  # no fallback dirs -> a genuine miss


def test_user_path_returns_none_on_subprocess_exception(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    import subprocess

    def fake_run(*args, **kwargs):  # noqa: ANN001, ARG001
        raise subprocess.SubprocessError("boom")

    monkeypatch.setattr(env_mod.subprocess, "run", fake_run)
    monkeypatch.setattr(env_mod, "_SHELL_CANDIDATES", ("/bin/zsh",))
    # Pin which() so the fake candidates "exist" on any OS (Linux CI has no /bin/zsh).
    monkeypatch.setattr(env_mod.shutil, "which", lambda cmd: cmd)

    assert user_path(fallback_dirs=()) is None


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
    # Pin which() so the fake candidates "exist" on any OS (Linux CI has no /bin/zsh).
    monkeypatch.setattr(env_mod.shutil, "which", lambda cmd: cmd)

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
    # Pin which() so the fake candidates "exist" on any OS (Linux CI has no /bin/zsh).
    monkeypatch.setattr(env_mod.shutil, "which", lambda cmd: cmd)

    assert user_path(fallback_dirs=()) is None
    assert user_path(fallback_dirs=()) is None
    assert call_count == 1


def test_user_path_falls_back_to_known_dirs(
    monkeypatch: pytest.MonkeyPatch, tmp_path: Path
) -> None:
    # Every shell probe fails, but the fallback dirs that exist on disk are still returned (and
    # cached) - a stripped-PATH launch must not become a silent permanent miss.
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
    # Pin which() so the fake candidates "exist" on any OS (Linux CI has no /bin/zsh).
    monkeypatch.setattr(env_mod.shutil, "which", lambda cmd: cmd)
    real = tmp_path / "bin"
    real.mkdir()
    missing = tmp_path / "does-not-exist"

    path = user_path(fallback_dirs=(str(real), str(missing)))
    assert path == str(real)  # only the dir that exists; the missing one is excluded
    assert user_path(fallback_dirs=(str(real), str(missing))) == str(real)  # cached
    assert call_count == 1


def test_merge_user_path_appends_fallback_when_shells_fail(
    monkeypatch: pytest.MonkeyPatch, tmp_path: Path
) -> None:
    # End-to-end through the real user_path(): shells all fail, the module-level fallback dirs
    # (monkeypatched to a tmp dir) land on os.environ['PATH'].
    from unittest.mock import MagicMock

    def fake_run(argv, *args, **kwargs):  # noqa: ANN001, ARG001
        m = MagicMock()
        m.returncode = 1
        m.stdout = ""
        m.stderr = ""
        return m

    monkeypatch.setattr(env_mod.subprocess, "run", fake_run)
    monkeypatch.setattr(env_mod, "_SHELL_CANDIDATES", ("/bin/zsh",))
    # Pin which() so the fake candidates "exist" on any OS (Linux CI has no /bin/zsh).
    monkeypatch.setattr(env_mod.shutil, "which", lambda cmd: cmd)
    userbin = tmp_path / "userbin"
    userbin.mkdir()
    monkeypatch.setattr(env_mod, "_FALLBACK_USER_DIRS", (str(userbin),))
    monkeypatch.setenv("PATH", "/usr/bin")
    monkeypatch.delenv("MARSHAL_NO_PATH_FIX", raising=False)

    assert merge_user_path() is True
    assert os.environ["PATH"] == f"/usr/bin:{userbin}"


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
        check=False,
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
    from marshal_engine.core.config import ClientConfig, FleetConfig
    from marshal_engine.interfaces.service import MarshalService

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


# --- per-client env reaches a REAL child process ------------------------------------------


class _EnvProbe(CodingAgentBackend):
    """Writes one env var's value to a file, so a test can assert on the real child env."""

    name = "envprobe"
    binary = "python"
    capabilities = Capabilities()

    def __init__(self, var: str, out: Path) -> None:
        self._var, self._out = var, out

    def check_available(self) -> bool:
        return True

    def build_invocation(self, task: TaskSpec, opts: RunOpts) -> list[str]:
        script = (
            f"import os,pathlib; "
            f"pathlib.Path({str(self._out)!r}).write_text(os.environ.get({self._var!r},'<unset>'))"
        )
        return [sys.executable, "-c", script]

    def map_permission(self, mode: PermissionMode) -> list[str]:
        return []

    def parse_output(self, raw_stdout: str, raw_stderr: str, exit_code: int) -> AgentResult:
        return AgentResult(
            status=RunStatus.EXITED_CLEAN if exit_code == 0 else RunStatus.FAILED,
            text=raw_stdout,
            exit_code=exit_code,
        )


def _probe_run(tmp_path: Path, var: str, client_env: dict[str, str]) -> str:
    out = tmp_path / f"seen-{var}-{len(client_env)}-{abs(hash(tuple(sorted(client_env.items()))))}"
    backend = _EnvProbe(var, out)
    opts = RunOpts(cwd=tmp_path, permission=PermissionMode.SAFE_EDIT, client_env=client_env)
    backend.run(TaskSpec(id="probe", goal="x"), opts)
    return out.read_text()


def test_client_env_reaches_a_real_child_process(tmp_path: Path) -> None:
    assert _probe_run(tmp_path, "CODEX_HOME", {"CODEX_HOME": "/tmp/home-a"}) == "/tmp/home-a"


def test_two_clients_on_one_backend_do_not_leak_env(tmp_path: Path) -> None:
    """The motivating case: same backend, different provider homes, no cross-contamination."""
    a = _probe_run(tmp_path, "CODEX_HOME", {"CODEX_HOME": "/tmp/home-a"})
    b = _probe_run(tmp_path, "CODEX_HOME", {"CODEX_HOME": "/tmp/home-b"})
    plain = _probe_run(tmp_path, "CODEX_HOME", {})
    assert (a, b) == ("/tmp/home-a", "/tmp/home-b")
    assert plain == "<unset>", "a client with no env: must not inherit a sibling client's value"


# --- allowlist: real spawned children -----------------------------------------------------


class _CredProbe(CodingAgentBackend):
    """Real child that prints one env var; credential allowlist is injectable."""

    name = "credprobe"
    binary = "python"
    capabilities = Capabilities()
    credential_env_vars: tuple[str, ...] = ()

    def __init__(self, var: str, out: Path, *, credentials: tuple[str, ...] = ()) -> None:
        self._var, self._out = var, out
        # Instance override so each probe can declare a different allowlist.
        self.credential_env_vars = credentials

    def check_available(self) -> bool:
        return True

    def build_invocation(self, task: TaskSpec, opts: RunOpts) -> list[str]:
        script = (
            f"import os,pathlib; "
            f"pathlib.Path({str(self._out)!r}).write_text("
            f"os.environ.get({self._var!r},'<unset>'))"
        )
        return [sys.executable, "-c", script]

    def map_permission(self, mode: PermissionMode) -> list[str]:
        return []

    def parse_output(self, raw_stdout: str, raw_stderr: str, exit_code: int) -> AgentResult:
        return AgentResult(
            status=RunStatus.EXITED_CLEAN if exit_code == 0 else RunStatus.FAILED,
            text=raw_stdout,
            exit_code=exit_code,
        )


def _cred_probe_run(
    tmp_path: Path,
    var: str,
    *,
    credentials: tuple[str, ...] = (),
    client_env: dict[str, str] | None = None,
) -> str:
    out = tmp_path / f"cred-{var}-{'-'.join(credentials) or 'none'}"
    backend = _CredProbe(var, out, credentials=credentials)
    opts = RunOpts(
        cwd=tmp_path,
        permission=PermissionMode.SAFE_EDIT,
        client_env=client_env or {},
    )
    backend.run(TaskSpec(id="probe", goal="x"), opts)
    return out.read_text()


def test_real_child_does_not_see_unrelated_secrets(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    monkeypatch.setenv("AWS_SECRET_ACCESS_KEY", "aws-secret-value-long-enough")
    monkeypatch.setenv("GH_TOKEN", "ghp_unrelated_token_value_xx")
    assert _cred_probe_run(tmp_path, "AWS_SECRET_ACCESS_KEY") == "<unset>"
    assert _cred_probe_run(tmp_path, "GH_TOKEN") == "<unset>"


def test_real_child_keeps_operational_path_and_home(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    monkeypatch.setenv("PATH", "/usr/bin:/bin:/custom/bin")
    monkeypatch.setenv("HOME", "/Users/allowlist-home")
    assert _cred_probe_run(tmp_path, "PATH") == "/usr/bin:/bin:/custom/bin"
    assert _cred_probe_run(tmp_path, "HOME") == "/Users/allowlist-home"


def test_cursor_child_does_not_see_anthropic_key(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    monkeypatch.setenv("ANTHROPIC_API_KEY", "sk-ant-should-not-reach-cursor")
    monkeypatch.setenv("CURSOR_API_KEY", "cursor-key-value-long-enough")
    assert (
        _cred_probe_run(
            tmp_path, "ANTHROPIC_API_KEY", credentials=CursorBackend.credential_env_vars
        )
        == "<unset>"
    )
    assert (
        _cred_probe_run(
            tmp_path, "CURSOR_API_KEY", credentials=CursorBackend.credential_env_vars
        )
        == "cursor-key-value-long-enough"
    )


def test_claude_code_child_sees_anthropic_key(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    monkeypatch.setenv("ANTHROPIC_API_KEY", "sk-ant-claude-child-sees-this")
    assert (
        _cred_probe_run(
            tmp_path,
            "ANTHROPIC_API_KEY",
            credentials=ClaudeCodeBackend.credential_env_vars,
        )
        == "sk-ant-claude-child-sees-this"
    )


# --- log / text redaction -----------------------------------------------------------------


def test_redact_secrets_replaces_credential_values(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    secret = "sk-ant-redact-me-please-xx"
    monkeypatch.setenv("ANTHROPIC_API_KEY", secret)
    text = f"running with key={secret} and done"
    out = redact_secrets(text, credential_names=["ANTHROPIC_API_KEY"])
    assert secret not in out
    assert "[redacted:ANTHROPIC_API_KEY]" in out
    assert "running with key=" in out


def test_redact_secrets_skips_short_values(monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setenv("ANTHROPIC_API_KEY", "short")  # < 8 chars
    text = "the value short appears in ordinary prose often"
    assert redact_secrets(text, credential_names=["ANTHROPIC_API_KEY"]) == text


def test_redact_secrets_leaves_ordinary_output_alone(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    monkeypatch.setenv("ANTHROPIC_API_KEY", "sk-ant-unique-secret-value")
    text = "tests passed\nall green\n"
    assert redact_secrets(text, credential_names=["ANTHROPIC_API_KEY"]) == text


def test_run_log_store_redacts_credential_in_stdout(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    secret = "sk-ant-log-persist-secret-xx"
    monkeypatch.setenv("ANTHROPIC_API_KEY", secret)
    store = RunLogStore(tmp_path / "logs")
    store.write("r1", f"env dump: ANTHROPIC_API_KEY={secret}\n", "ok\n")
    text = store.read("r1")
    assert text is not None
    assert secret not in text
    assert "[redacted:ANTHROPIC_API_KEY]" in text
    assert "env dump:" in text
    assert "--- stderr ---\nok" in text


def test_redact_before_truncate_removes_boundary_straddle(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """Value-based redaction must see the whole secret; truncate-then-redact leaks a prefix."""
    secret = "sk-ant-straddle-secret-xx"
    monkeypatch.setenv("ANTHROPIC_API_KEY", secret)
    cap = 16_000
    keep_prefix = 10  # first chars of the secret land just before the cut
    pad = "p" * (cap - keep_prefix)
    raw = pad + secret + "trailer"
    # Broken order (the defect): prefix of the secret survives in the retained window.
    broken = redact_secrets(raw[:cap], credential_names=["ANTHROPIC_API_KEY"])
    assert secret[:keep_prefix] in broken
    # Correct order: redact on the full string, then cap.
    fixed = redact_secrets(raw, credential_names=["ANTHROPIC_API_KEY"])[:cap]
    assert secret not in fixed
    assert secret[:keep_prefix] not in fixed
    # Marker may itself be clipped by the cap; that is fine — no raw fragment remains.
    assert "[redacted:" in fixed
