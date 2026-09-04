"""Integration tests for the shared CodingAgentBackend.run() chokepoint.

Verifies the two invariants that the base class must enforce for every backend: a hard
timeout that kills the child, and stdin closed so an interactive prompt can't deadlock.
Uses a dummy backend over the local Python interpreter - portable, fast, no real CLIs.
"""

from __future__ import annotations

import contextlib
import errno
import subprocess
import sys
import time
from pathlib import Path

import pytest

from marshal_engine import (
    AgentResult,
    Capabilities,
    PermissionMode,
    RunOpts,
    RunStatus,
    TaskSpec,
    UsageRecord,
    UsageSource,
)
from marshal_engine.backends.base import CodingAgentBackend
from marshal_engine.core.types import ProgressTimeout


class _Dummy(CodingAgentBackend):
    name = "dummy"
    capabilities = Capabilities()

    def __init__(self, argv: list[str]) -> None:
        self._argv = argv
        self.binary = argv[0]

    def check_available(self) -> bool:
        return True

    def build_invocation(self, task: TaskSpec, opts: RunOpts) -> list[str]:
        return self._argv

    def map_permission(self, mode: PermissionMode) -> list[str]:
        return []

    def parse_output(self, raw_stdout: str, raw_stderr: str, exit_code: int) -> AgentResult:
        return AgentResult(
            status=RunStatus.EXITED_CLEAN if exit_code == 0 else RunStatus.FAILED,
            text=raw_stdout.strip(),
            exit_code=exit_code,
            raw_stdout=raw_stdout,
            raw_stderr=raw_stderr,
        )


def _task() -> TaskSpec:
    return TaskSpec(id="t", goal="g")


def test_run_success(tmp_path: Path) -> None:
    b = _Dummy([sys.executable, "-c", "print('hi')"])
    res = b.run(_task(), RunOpts(cwd=tmp_path))
    assert res.status is RunStatus.EXITED_CLEAN
    assert res.text == "hi"


def test_run_scrubs_driver_virtual_env(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> None:
    # The driver runs inside its own venv; the agent child must NOT inherit VIRTUAL_ENV, or its
    # `uv run` would resolve the driver's install instead of the worktree's (testing stale code).
    monkeypatch.setenv("VIRTUAL_ENV", "/driver/.venv")
    b = _Dummy([sys.executable, "-c", "import os; print(os.environ.get('VIRTUAL_ENV', 'UNSET'))"])
    res = b.run(_task(), RunOpts(cwd=tmp_path))
    assert res.status is RunStatus.EXITED_CLEAN
    assert res.text == "UNSET"  # scrubbed from the child env
    # extra_env still wins if a caller deliberately sets it
    b2 = _Dummy([sys.executable, "-c", "import os; print(os.environ.get('VIRTUAL_ENV', 'UNSET'))"])
    res2 = b2.run(_task(), RunOpts(cwd=tmp_path, extra_env={"VIRTUAL_ENV": "/wanted"}))
    assert res2.text == "/wanted"


def test_run_calls_prepare_before_spawn(tmp_path: Path) -> None:
    calls: list[Path] = []

    class _Prep(_Dummy):
        def prepare(self, opts: RunOpts) -> None:
            calls.append(Path(opts.cwd))

    res = _Prep([sys.executable, "-c", "print('hi')"]).run(_task(), RunOpts(cwd=tmp_path))
    assert res.status is RunStatus.EXITED_CLEAN
    assert calls == [tmp_path]  # prepare ran, with the run's cwd


def test_run_prepare_extra_env_reaches_child(tmp_path: Path) -> None:
    # prepare() often stamps permission/config into opts.extra_env (OpenCode CONFIG_CONTENT,
    # Goose GOOSE_MODE). Building child_env before prepare would drop those mutations.
    class _StampEnv(_Dummy):
        def prepare(self, opts: RunOpts) -> None:
            opts.extra_env = {**opts.extra_env, "MARSHAL_PREPARE_STAMP": "from-prepare"}

    b = _StampEnv(
        [
            sys.executable,
            "-c",
            "import os; print(os.environ.get('MARSHAL_PREPARE_STAMP', 'MISSING'))",
        ]
    )
    res = b.run(_task(), RunOpts(cwd=tmp_path, extra_env={"KEEP": "1"}))
    assert res.status is RunStatus.EXITED_CLEAN
    assert res.text == "from-prepare"


def test_run_prepare_failure_is_a_failed_result(tmp_path: Path) -> None:
    class _BadPrep(_Dummy):
        def prepare(self, opts: RunOpts) -> None:
            raise RuntimeError("trust failed")

    res = _BadPrep([sys.executable, "-c", "print('hi')"]).run(_task(), RunOpts(cwd=tmp_path))
    assert res.status is RunStatus.FAILED
    assert "prepare failed" in (res.error or "") and "trust failed" in (res.error or "")


def test_run_timeout_kills_process(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    import os
    import signal

    from marshal_engine.backends import base as base_mod

    killed: list[tuple[int, int]] = []
    real_killpg = os.killpg

    def _spy_killpg(pgid: int, sig: int) -> None:
        killed.append((pgid, sig))
        real_killpg(pgid, sig)

    monkeypatch.setattr(base_mod.os, "killpg", _spy_killpg)

    child_pid: list[int] = []
    b = _Dummy([sys.executable, "-c", "import time; time.sleep(30)"])
    res = b.run(
        _task(),
        RunOpts(cwd=tmp_path, timeout_s=1, on_pid=child_pid.append),
    )
    assert res.status is RunStatus.TIMED_OUT
    assert "timed out" in (res.error or "")
    # Core invariant: timeout must signal the process group, then reap the child.
    assert child_pid, "on_pid never fired"
    assert any(sig == signal.SIGTERM for _, sig in killed), f"group never SIGTERM'd: {killed}"
    assert all(pgid == child_pid[0] for pgid, _ in killed)
    # Child was reaped (not left as a live sleep after TIMED_OUT).
    with pytest.raises(ProcessLookupError):
        os.kill(child_pid[0], 0)


def test_run_stamps_duration_on_every_path(tmp_path: Path) -> None:
    ok = _Dummy([sys.executable, "-c", "print('hi')"])
    assert ok.run(_task(), RunOpts(cwd=tmp_path)).duration_ms >= 0  # success path stamped
    slow = _Dummy([sys.executable, "-c", "import time; time.sleep(30)"])
    timed = slow.run(_task(), RunOpts(cwd=tmp_path, timeout_s=1))
    assert timed.status is RunStatus.TIMED_OUT
    assert timed.duration_ms >= 1000  # timeout path stamped (~the 1s wait)


def test_timeout_kills_whole_process_group(tmp_path: Path) -> None:
    # Outer process spawns a grandchild that would write a sentinel at +3s, then sleeps. A timeout
    # at 1s must group-kill BOTH, so the grandchild never reaches its write.
    sentinel = tmp_path / "grandchild.txt"
    inner = f"import time; time.sleep(3); open({str(sentinel)!r}, 'w').write('alive')"
    outer = (
        "import subprocess, sys, time; "
        f"subprocess.Popen([sys.executable, '-c', {inner!r}]); "
        "time.sleep(30)"
    )
    b = _Dummy([sys.executable, "-c", outer])
    res = b.run(_task(), RunOpts(cwd=tmp_path, timeout_s=1))
    assert res.status is RunStatus.TIMED_OUT
    time.sleep(3)  # past the grandchild's +3s write window
    assert not sentinel.exists()  # group was killed -> grandchild never wrote


def test_timeout_sigkills_grandchild_that_ignores_sigterm(tmp_path: Path) -> None:
    # A grandchild that ignores SIGTERM (e.g. a server doing graceful shutdown) must still be
    # SIGKILLed - escalation must depend on the group dying, not on the leader being reaped.
    sentinel = tmp_path / "ignored-sigterm.txt"
    inner = (
        "import signal, time; signal.signal(signal.SIGTERM, signal.SIG_IGN); "
        f"time.sleep(3); open({str(sentinel)!r}, 'w').write('survived')"
    )
    outer = (
        "import subprocess, sys, time; "
        f"subprocess.Popen([sys.executable, '-c', {inner!r}]); time.sleep(30)"
    )
    res = _Dummy([sys.executable, "-c", outer]).run(_task(), RunOpts(cwd=tmp_path, timeout_s=1))
    assert res.status is RunStatus.TIMED_OUT
    time.sleep(3)
    assert not sentinel.exists()  # SIGKILL escalation killed it despite SIG_IGN on SIGTERM


def test_timeout_returns_even_if_grandchild_escapes_session(tmp_path: Path) -> None:
    # A grandchild that calls setsid() escapes the group; killpg can't reach it. The bounded drain
    # must still let run() return promptly instead of blocking on the inherited pipe it holds.
    sentinel = tmp_path / "escaped.txt"
    inner = f"import os, time; os.setsid(); time.sleep(6); open({str(sentinel)!r}, 'w').write('x')"
    outer = (
        "import subprocess, sys, time; "
        f"subprocess.Popen([sys.executable, '-c', {inner!r}]); time.sleep(30)"
    )
    res = _Dummy([sys.executable, "-c", outer]).run(_task(), RunOpts(cwd=tmp_path, timeout_s=1))
    assert res.status is RunStatus.TIMED_OUT
    assert not sentinel.exists()  # run() returned without waiting for the escaped grandchild (+6s)


def test_run_stdin_closed_does_not_hang(tmp_path: Path) -> None:
    # If stdin were a TTY/open pipe this would block forever; DEVNULL gives EOF immediately.
    b = _Dummy([sys.executable, "-c", "import sys; sys.stdin.read(); print('eof-ok')"])
    res = b.run(_task(), RunOpts(cwd=tmp_path, timeout_s=10))
    assert res.status is RunStatus.EXITED_CLEAN
    assert res.text == "eof-ok"


def test_failed_run_without_error_surfaces_exit_code_and_stderr(tmp_path: Path) -> None:
    # _Dummy.parse_output returns FAILED with no error on a non-zero exit; base.run must fill a
    # debuggable reason from the exit code + stderr (so a failure is never a silent "failed").
    b = _Dummy([sys.executable, "-c", "import sys; sys.stderr.write('boom detail\\n'); sys.exit(3)"])
    res = b.run(_task(), RunOpts(cwd=tmp_path))
    assert res.status is RunStatus.FAILED
    assert res.error and "code 3" in res.error and "boom detail" in res.error


def test_failed_run_without_error_surfaces_stdout_when_stderr_empty(tmp_path: Path) -> None:
    # Goose-style: actionable failure text on stdout only. base.run must not drop it.
    b = _Dummy(
        [
            sys.executable,
            "-c",
            "import sys; sys.stdout.write('error: Error Unknown provider: fake\\n'); sys.exit(1)",
        ]
    )
    res = b.run(_task(), RunOpts(cwd=tmp_path))
    assert res.status is RunStatus.FAILED
    assert res.error and "Unknown provider" in res.error


def test_run_missing_binary(tmp_path: Path) -> None:
    b = _Dummy(["marshal-no-such-binary-xyz123"])
    res = b.run(_task(), RunOpts(cwd=tmp_path))
    assert res.status is RunStatus.FAILED
    assert "not found" in (res.error or "")


def test_run_non_executable_binary_returns_agent_result(tmp_path: Path) -> None:
    # EACCES on the backend binary must not escape run() as a raw PermissionError.
    binary = tmp_path / "noexec-backend"
    binary.write_text("#!/bin/sh\necho hi\n")
    binary.chmod(0o644)  # readable but not executable
    res = _Dummy([str(binary)]).run(_task(), RunOpts(cwd=tmp_path))
    assert res.status is RunStatus.FAILED
    err = res.error or ""
    assert "not executable" in err
    assert str(binary) in err


def test_run_popen_etxtbsy_returns_agent_result(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    def _busy(*_a: object, **_k: object) -> None:
        raise OSError(errno.ETXTBSY, "Text file busy", "busy-binary")

    monkeypatch.setattr(subprocess, "Popen", _busy)
    res = _Dummy(["busy-binary"]).run(_task(), RunOpts(cwd=tmp_path))
    assert res.status is RunStatus.FAILED
    err = res.error or ""
    assert "busy-binary" in err
    assert "busy" in err.lower() or "text file busy" in err.lower()


def test_run_popen_eacces_message_names_binary_and_cause(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    def _denied(*_a: object, **_k: object) -> None:
        raise PermissionError(errno.EACCES, "Permission denied", "locked-bin")

    monkeypatch.setattr(subprocess, "Popen", _denied)
    res = _Dummy(["locked-bin"]).run(_task(), RunOpts(cwd=tmp_path))
    assert res.status is RunStatus.FAILED
    err = res.error or ""
    assert "locked-bin" in err
    assert "not executable" in err or "permission denied" in err.lower()


def test_run_popen_other_oserror_returns_agent_result(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    def _boom(*_a: object, **_k: object) -> None:
        raise OSError(errno.EIO, "Input/output error", "weird-bin")

    monkeypatch.setattr(subprocess, "Popen", _boom)
    res = _Dummy(["weird-bin"]).run(_task(), RunOpts(cwd=tmp_path))
    assert res.status is RunStatus.FAILED
    err = res.error or ""
    assert "weird-bin" in err
    assert str(errno.EIO) in err or "Input/output error" in err


class _PartialUsage(CodingAgentBackend):
    """Flushes a usage line, then hangs - exercises partial-usage recovery on timeout."""

    name = "partial"
    capabilities = Capabilities()
    binary = sys.executable

    def check_available(self) -> bool:
        return True

    def build_invocation(self, task: TaskSpec, opts: RunOpts) -> list[str]:
        return [sys.executable, "-c", "import time; print('TOKENS=42', flush=True); time.sleep(30)"]

    def map_permission(self, mode: PermissionMode) -> list[str]:
        return []

    def parse_output(self, raw_stdout: str, raw_stderr: str, exit_code: int) -> AgentResult:
        usage = None
        for line in raw_stdout.splitlines():
            if line.startswith("TOKENS="):
                usage = UsageRecord(
                    backend="partial", input_tokens=int(line.split("=")[1]), source=UsageSource.NATIVE
                )
        return AgentResult(
            status=RunStatus.EXITED_CLEAN if exit_code == 0 else RunStatus.FAILED,
            usage=usage,
            exit_code=exit_code,
        )


def test_timeout_recovers_partial_usage(tmp_path: Path) -> None:
    res = _PartialUsage().run(_task(), RunOpts(cwd=tmp_path, timeout_s=1))
    assert res.status is RunStatus.TIMED_OUT  # status is preserved, not flipped to success
    assert res.usage is not None and res.usage.input_tokens == 42  # real spend salvaged


class _BoomParser(_PartialUsage):
    """parse_output raises - recovery must swallow it and still report the timeout."""

    def build_invocation(self, task: TaskSpec, opts: RunOpts) -> list[str]:
        return [sys.executable, "-c", "import time; print('x', flush=True); time.sleep(30)"]

    def parse_output(self, raw_stdout: str, raw_stderr: str, exit_code: int) -> AgentResult:
        raise ValueError("parser blew up")


def test_timeout_recovery_error_does_not_mask_timeout(tmp_path: Path) -> None:
    res = _BoomParser().run(_task(), RunOpts(cwd=tmp_path, timeout_s=1))
    assert res.status is RunStatus.TIMED_OUT  # recovery failure swallowed, timeout still reported
    assert res.usage is None


# --- extract_usage contract: the seam Cursor/Codex use to backfill real cost --------------


class _CapturingUsage(_Dummy):
    """A dummy that overrides extract_usage to capture the post-parse_output result it sees.

    The seam is the ONLY hook backends without in-output usage (Cursor's admin API, Codex's
    admin API, future pricing APIs) have for stamping a real cost onto a run after the
    process has exited. It MUST receive the result parse_output built - the same status, the
    same text, the same exit_code - so a backend can decide "did this run actually produce
    tokens I should charge for?" before swapping in admin-api usage.
    """

    def __init__(self) -> None:
        super().__init__([sys.executable, "-c", "print('hi')"])
        self.captured: AgentResult | None = None

    def extract_usage(self, result: AgentResult) -> UsageRecord:
        self.captured = result
        return UsageRecord(
            backend="capturing", source=UsageSource.ADMIN_API, cost_usd=0.99
        )


def test_extract_usage_default_returns_result_usage() -> None:
    # The base class default is `result.usage` - a backend that didn't override the seam
    # must still get its parse_output result.usage passed through unchanged. Locks down
    # the contract Fleet._execute relies on: `usage = backend.extract_usage(result)`.
    b = _Dummy([sys.executable, "-c", "print('hi')"])
    result = b.run(_task(), RunOpts(cwd=Path("/tmp")))
    # The default seam returns result.usage (None here because _Dummy doesn't set one)
    assert b.extract_usage(result) is result.usage


def test_failure_tail_redacts_secret_before_length_cut(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """Error tails truncate; redact must run first or a straddling credential leaks a prefix."""
    from marshal_engine.backends.base import _failure_tail
    from marshal_engine.runtime.env import redact_secrets

    secret = "sk-ant-err-tail-secret-x"
    monkeypatch.setenv("ANTHROPIC_API_KEY", secret)
    limit = 500
    keep_prefix = 10
    prefix = "error: "
    # Place the secret so its first keep_prefix chars land just before the length cut.
    pad = "e" * (limit - len(prefix) - keep_prefix)
    line = prefix + pad + secret + " trailing"
    broken = redact_secrets(line[:limit], credential_names=["ANTHROPIC_API_KEY"])
    assert secret[:keep_prefix] in broken

    out = _failure_tail(line, limit=limit)
    assert secret not in out
    assert secret[:keep_prefix] not in out
    assert "[redacted:" in out


def test_extract_usage_override_receives_post_parse_output() -> None:
    # The seam must be called with the AgentResult parse_output produced - same status, text,
    # exit_code - so a backend can condition on them. Locks down the contract Cursor's
    # admin-api fetch and Codex's account-info lookup rely on.
    backend = _CapturingUsage()
    result = backend.run(_task(), RunOpts(cwd=Path("/tmp")))
    # Fleet calls extract_usage on the result of base.run() - simulate the call here so
    # the contract test stays in test_base_run (the seam's home).
    usage = backend.extract_usage(result)
    assert abs(usage.cost_usd - 0.99) < 1e-9
    assert usage.source is UsageSource.ADMIN_API
    # the override saw the same status/text/exit_code the caller sees
    assert backend.captured is not None
    assert backend.captured.status is result.status
    assert backend.captured.text == result.text
    assert backend.captured.exit_code == result.exit_code


def test_an_ordinary_timeout_reports_the_agent_stopped(tmp_path: Path) -> None:
    """The kill lands, so the run is settled: the flag stays clear, the error says only that it
    timed out, and the caller is told the pid is reaped and reusable."""
    exited: list[bool] = []
    b = _Dummy([sys.executable, "-c", "import time; time.sleep(30)"])
    res = b.run(
        _task(),
        RunOpts(cwd=tmp_path, timeout_s=1, on_exit=lambda: exited.append(True)),
    )

    assert res.status is RunStatus.TIMED_OUT
    assert res.agent_survived_kill is False
    assert "did NOT stop" not in (res.error or ""), "claimed a survivor over a dead process"
    assert exited == [True], "a reaped child must still be reported as reaped"


def test_a_timeout_whose_kill_did_nothing_says_the_agent_is_still_running(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    """`_kill_process_group` swallows every signalling error and `_drain` gives up after two
    seconds, so both can return having achieved nothing - a `killpg` refused with EPERM never
    escalates to SIGKILL, and a leader wedged in uninterruptible I/O rides one out. The run was
    stamped `timed_out` either way, which tells a driver the worktree is a finished snapshot while
    the agent is still writing into it, and told the fleet the pid was reaped - retiring the one
    control left over a process Marshal had just failed to stop."""
    import os
    import signal

    from marshal_engine.backends import base as base_mod

    monkeypatch.setattr(base_mod, "_kill_process_group", lambda pgid, **kw: None)

    exited: list[bool] = []
    child_pid: list[int] = []
    b = _Dummy([sys.executable, "-c", "import time; time.sleep(30)"])
    try:
        res = b.run(
            _task(),
            RunOpts(
                cwd=tmp_path,
                timeout_s=1,
                on_pid=child_pid.append,
                on_exit=lambda: exited.append(True),
            ),
        )

        assert res.status is RunStatus.TIMED_OUT
        assert res.agent_survived_kill is True, "the surviving agent was recorded as stopped"
        assert "did NOT stop" in (res.error or ""), "the error read like an ordinary timeout"
        assert exited == [], "told the fleet a live agent's pid was reaped and reusable"
        assert child_pid and _pid_is_alive(child_pid[0]), "the child was not actually alive"
    finally:
        for pid in child_pid:
            with contextlib.suppress(ProcessLookupError, PermissionError, OSError):
                os.killpg(pid, signal.SIGKILL)


def _pid_is_alive(pid: int) -> bool:
    import os

    try:
        os.kill(pid, 0)
    except ProcessLookupError:
        return False
    except PermissionError:
        return True
    return True


def test_a_descendant_that_escaped_the_group_counts_as_a_survivor(tmp_path: Path) -> None:
    """`poll()` answers for the leader only. A grandchild that calls `setsid` leaves the process
    group, so the group kill never reaches it and the leader's exit proves nothing about it - yet
    it inherited the pipes and can go on writing to the worktree. The drain is the evidence: it is
    bounded precisely because such a survivor holds the write end open, so a drain that never
    finishes means somebody is still there.

    Disclosed rather than blocked. There is no pid for an escaped descendant - held pipes are the
    entire trace - so a refusal keyed on it could never be lifted, and the run's work would be
    stranded for good. `agent_survived_kill` stays False because it means the one survivor Marshal
    can actually watch stop."""
    import os
    import signal

    # Leader spawns a setsid'd grandchild that holds the inherited pipes, then exits immediately.
    inner = "import time; time.sleep(30)"
    outer = (
        "import subprocess, sys; "
        f"subprocess.Popen([sys.executable, '-c', {inner!r}], start_new_session=True); "
        "import time; time.sleep(30)"
    )
    b = _Dummy([sys.executable, "-c", outer])
    child_pid: list[int] = []
    res = b.run(_task(), RunOpts(cwd=tmp_path, timeout_s=1, on_pid=child_pid.append))

    try:
        assert res.status is RunStatus.TIMED_OUT
        assert "had left the process group" in (res.error or ""), (
            "the leader's exit was reported as proof that everything it started had stopped"
        )
        assert res.agent_survived_kill is False, (
            "an escaped descendant has no pid, so nothing could ever lift a block keyed on it"
        )
    finally:
        for pid in child_pid:
            with contextlib.suppress(ProcessLookupError, PermissionError, OSError):
                os.killpg(pid, signal.SIGKILL)


def test_a_clean_timeout_is_not_reported_as_a_survivor(tmp_path: Path) -> None:
    """The drain finishing is the normal case and must not read as evidence of anything. Treating
    it as a survivor would block every timed-out run's worktree from being committed, reviewed or
    cleaned - the failure this whole guard exists to avoid, in the opposite direction."""
    b = _Dummy([sys.executable, "-c", "import time; time.sleep(30)"])
    res = b.run(_task(), RunOpts(cwd=tmp_path, timeout_s=1))

    assert res.status is RunStatus.TIMED_OUT
    assert res.agent_survived_kill is False


# --- progress-aware timeout (#276) -------------------------------------------------------
#
# The hard ceiling is the invariant and is asserted in every one of these: the policy may only
# move a kill EARLIER or extend BELOW the ceiling, never past it.

def _sleeper(seconds: float) -> _Dummy:
    """A child that produces no output and touches nothing - i.e. reads as stalled."""
    return _Dummy([sys.executable, "-c", f"import time; time.sleep({seconds})"])


def _writer(seconds: float, path: Path) -> _Dummy:
    """A child that keeps writing to its worktree - i.e. reads as making progress."""
    code = (
        "import time, sys\n"
        f"end = time.time() + {seconds}\n"
        "i = 0\n"
        "while time.time() < end:\n"
        f"    open({str(path)!r} + str(i % 3), 'w').write(str(i))\n"
        "    i += 1\n"
        "    time.sleep(0.2)\n"
    )
    return _Dummy([sys.executable, "-c", code])


def test_the_progress_scan_is_bounded_so_the_hard_ceiling_still_holds(tmp_path: Path) -> None:
    """The scan sits between the waiter's ceiling checks, so an unbounded walk of a big worktree
    would hold the run past `hard_ceiling_s` - the one deadline that must always hold. It stops
    on the first newer entry, and gives up on a budget either way."""
    from marshal_engine.backends.base import _newest_mtime

    deep = tmp_path
    for i in range(40):
        deep = deep / f"d{i}"
    deep.mkdir(parents=True)
    for i in range(300):
        (deep / f"f{i}.txt").write_text("x")
    started = time.monotonic()
    mtime, complete = _newest_mtime(tmp_path, budget_s=0.0)
    assert time.monotonic() - started < 1.0
    assert complete is False, "an exhausted budget must report the scan as incomplete"
    assert mtime == 0.0

    # A scan that CAN finish still answers, and stops early once it has its answer.
    found, done = _newest_mtime(tmp_path)
    assert done is True and found > 0.0


def test_an_unfinished_scan_is_not_read_as_a_stall(tmp_path: Path, monkeypatch) -> None:  # noqa: ANN001
    """`complete=False` means "progress unknown", not "no progress". Killing a run for a stall on
    a scan that never finished would be a verdict the code did not earn."""
    from marshal_engine.backends import base as base_mod

    monkeypatch.setattr(base_mod, "_newest_mtime", lambda *a, **k: (0.0, False))
    policy = ProgressTimeout(enabled=True, stall_s=1, poll_interval_s=1, hard_ceiling_s=3)
    started = time.monotonic()
    res = _sleeper(30).run(_task(), RunOpts(cwd=tmp_path, timeout_s=30, progress=policy))
    elapsed = time.monotonic() - started
    # Not killed at stall_s (1s) on unknown evidence; the ceiling (3s) still ends it.
    assert res.status is RunStatus.TIMED_OUT
    assert "hard ceiling" in (res.error or ""), res.error
    assert 2.0 < elapsed < 20.0, elapsed


def test_a_run_with_no_progress_ends_at_its_soft_deadline(tmp_path: Path) -> None:
    """REGRESSION (P2): `soft_deadline_s` is documented as the FIRST deadline, at which a run
    "still making progress is extended rather than killed" - but no branch ever killed at it, so
    the key only sized the poll slice and any soft deadline below `stall_s` was decorative."""
    policy = ProgressTimeout(
        enabled=True, stall_s=30, soft_deadline_s=1, hard_ceiling_s=30, poll_interval_s=1
    )
    started = time.monotonic()
    res = _sleeper(30).run(_task(), RunOpts(cwd=tmp_path, timeout_s=30, progress=policy))
    elapsed = time.monotonic() - started
    assert res.status is RunStatus.TIMED_OUT
    assert "soft deadline" in (res.error or ""), res.error
    assert elapsed < 15, f"ran {elapsed:.1f}s; the 1s soft deadline should have ended it"


def test_a_stalled_run_is_killed_before_its_cap(tmp_path: Path) -> None:
    """The whole point: a run that stopped working must not burn the rest of the clock.

    Today every timeout in the ledger sits at the wall - the mechanism has never once ended a
    run for lack of progress, only for running out of time.
    """
    policy = ProgressTimeout(enabled=True, stall_s=1, poll_interval_s=1, hard_ceiling_s=30)
    started = time.monotonic()
    res = _sleeper(30).run(_task(), RunOpts(cwd=tmp_path, timeout_s=30, progress=policy))
    elapsed = time.monotonic() - started

    assert res.status is RunStatus.TIMED_OUT
    assert elapsed < 15, f"killed at {elapsed:.1f}s; should be seconds, not the 30s cap"


def test_a_run_making_progress_survives_past_the_soft_deadline(tmp_path: Path) -> None:
    """A productive run at the cap is exactly the case whose tokens are spent for nothing.

    `stall_s` is shorter than the run on purpose: the run survives ONLY because its writes keep
    resetting the stall clock. Take the progress signal away (stop reading mtimes, or stop
    resetting `last_progress`) and this run stalls out at 2s instead of finishing - so the test
    is evidence for the extension, not merely for the hard ceiling being far away.
    """
    policy = ProgressTimeout(
        enabled=True, stall_s=2, soft_deadline_s=1, hard_ceiling_s=30, poll_interval_s=1
    )
    res = _writer(4, tmp_path / "w").run(
        _task(), RunOpts(cwd=tmp_path, timeout_s=1, progress=policy)
    )

    # Without the extension a 1s soft deadline would have killed a run that ran for ~4s.
    assert res.status is RunStatus.EXITED_CLEAN


def test_a_stall_kill_says_it_was_a_stall_and_names_the_stall_deadline(tmp_path: Path) -> None:
    """REGRESSION (P1): the TIMED_OUT error was always built from `timeout_s`, so a stall kill
    reported "timed out after 600s" for a run stopped at 1s of silence - a duration the run never
    had, with nothing saying it had been ended for lack of progress. A driver cannot tell a stall
    from a wall-clock kill, and the two call for different fixes."""
    policy = ProgressTimeout(enabled=True, stall_s=1, poll_interval_s=1, hard_ceiling_s=30)
    res = _sleeper(30).run(_task(), RunOpts(cwd=tmp_path, timeout_s=30, progress=policy))
    assert res.status is RunStatus.TIMED_OUT
    assert "no progress" in (res.error or "")
    assert "1s" in (res.error or "") and "30s" not in (res.error or "")


def test_a_ceiling_kill_says_ceiling_and_a_plain_timeout_still_says_timed_out(
    tmp_path: Path,
) -> None:
    """The other two shapes, so the fix distinguishes rather than relabels everything."""
    policy = ProgressTimeout(
        enabled=True, stall_s=30, soft_deadline_s=1, hard_ceiling_s=2, poll_interval_s=1
    )
    res = _writer(30, tmp_path / "w").run(
        _task(), RunOpts(cwd=tmp_path, timeout_s=1, progress=policy)
    )
    assert res.status is RunStatus.TIMED_OUT
    assert "hard ceiling" in (res.error or "") and "2s" in (res.error or "")

    plain = _sleeper(30).run(_task(), RunOpts(cwd=tmp_path, timeout_s=1))
    assert plain.status is RunStatus.TIMED_OUT
    assert "timed out after 1s" in (plain.error or "")


def test_the_hard_ceiling_still_kills_a_busy_run(tmp_path: Path) -> None:
    """The invariant: a run is never extended past the ceiling, however productive it looks."""
    policy = ProgressTimeout(
        enabled=True, stall_s=30, soft_deadline_s=1, hard_ceiling_s=2, poll_interval_s=1
    )
    started = time.monotonic()
    res = _writer(30, tmp_path / "w").run(
        _task(), RunOpts(cwd=tmp_path, timeout_s=1, progress=policy)
    )
    elapsed = time.monotonic() - started

    assert res.status is RunStatus.TIMED_OUT
    assert elapsed < 20, f"ran {elapsed:.1f}s; the 2s ceiling must bound it"


def test_no_policy_leaves_the_wall_clock_exactly_as_before(tmp_path: Path) -> None:
    """Anti-blanket control: the default path must not acquire progress behaviour."""
    started = time.monotonic()
    res = _sleeper(30).run(_task(), RunOpts(cwd=tmp_path, timeout_s=2))
    elapsed = time.monotonic() - started

    assert res.status is RunStatus.TIMED_OUT
    # Killed by the plain cap at ~2s, NOT early by a stall detector that should not be running.
    assert 1.0 < elapsed < 15


def test_a_future_dated_file_cannot_pin_the_progress_signal(tmp_path: Path) -> None:
    """A file stamped ahead of the clock would otherwise be the newest mtime forever, so every
    real write compares lower and a productive run is killed as stalled."""
    import os

    from marshal_engine.backends.base import _newest_mtime

    future = tmp_path / "from-the-future"
    future.write_text("x")
    ahead = time.time() + 86_400
    os.utime(future, (ahead, ahead))
    assert _newest_mtime(tmp_path)[0] == 0.0, "a future stamp was read as progress"
    real = tmp_path / "real.py"
    real.write_text("x")
    newest = _newest_mtime(tmp_path)[0]
    assert 0.0 < newest < ahead
    assert newest == real.stat().st_mtime


def test_git_writes_do_not_count_as_agent_progress(tmp_path: Path) -> None:
    """`.git` is skipped, so a stalled agent cannot be kept alive by git's own bookkeeping.

    The progress signal is "the worktree changed". Git writes into `.git` constantly - index
    refreshes, lock files, gc - none of which is the agent doing work. If those counted, a hung
    run would look busy for as long as anything touched the repo, and the stall detector would
    never fire: the exact failure it exists to catch.
    """
    from marshal_engine.backends.base import _newest_mtime

    git = tmp_path / ".git"
    git.mkdir()
    (git / "index").write_text("x")
    nested = git / "refs" / "heads"
    nested.mkdir(parents=True)
    (nested / "main").write_text("x")

    assert _newest_mtime(tmp_path)[0] == 0.0  # nothing outside .git has been written

    tracked = tmp_path / "src.py"
    tracked.write_text("x")
    assert _newest_mtime(tmp_path)[0] > 0.0  # ... and a real file still registers


def test_an_unreadable_directory_is_not_read_as_progress(tmp_path: Path) -> None:
    """A directory we cannot scan yields no mtime rather than an exception.

    `_newest_mtime` runs on every poll of a live run. Raising here would turn a permission quirk
    into a killed run, so the walk degrades to "saw nothing" - which is the safe direction: the
    stall detector fires, and the hard ceiling is still underneath it either way.
    """
    from marshal_engine.backends.base import _newest_mtime

    blocked = tmp_path / "blocked"
    blocked.mkdir()
    (blocked / "f").write_text("x")
    blocked.chmod(0o000)
    try:
        assert _newest_mtime(tmp_path)[0] == 0.0
    finally:
        blocked.chmod(0o755)
