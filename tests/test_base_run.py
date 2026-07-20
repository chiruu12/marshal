"""Integration tests for the shared CodingAgentBackend.run() chokepoint.

Verifies the two invariants that the base class must enforce for every backend: a hard
timeout that kills the child, and stdin closed so an interactive prompt can't deadlock.
Uses a dummy backend over the local Python interpreter - portable, fast, no real CLIs.
"""

from __future__ import annotations

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
            status=RunStatus.SUCCEEDED if exit_code == 0 else RunStatus.FAILED,
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
    assert res.status is RunStatus.SUCCEEDED
    assert res.text == "hi"


def test_run_scrubs_driver_virtual_env(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> None:
    # The driver runs inside its own venv; the agent child must NOT inherit VIRTUAL_ENV, or its
    # `uv run` would resolve the driver's install instead of the worktree's (testing stale code).
    monkeypatch.setenv("VIRTUAL_ENV", "/driver/.venv")
    b = _Dummy([sys.executable, "-c", "import os; print(os.environ.get('VIRTUAL_ENV', 'UNSET'))"])
    res = b.run(_task(), RunOpts(cwd=tmp_path))
    assert res.status is RunStatus.SUCCEEDED
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
    assert res.status is RunStatus.SUCCEEDED
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
    assert res.status is RunStatus.SUCCEEDED
    assert res.text == "from-prepare"


def test_run_prepare_failure_is_a_failed_result(tmp_path: Path) -> None:
    class _BadPrep(_Dummy):
        def prepare(self, opts: RunOpts) -> None:
            raise RuntimeError("trust failed")

    res = _BadPrep([sys.executable, "-c", "print('hi')"]).run(_task(), RunOpts(cwd=tmp_path))
    assert res.status is RunStatus.FAILED
    assert "prepare failed" in (res.error or "") and "trust failed" in (res.error or "")


def test_run_timeout_kills_process(tmp_path: Path) -> None:
    b = _Dummy([sys.executable, "-c", "import time; time.sleep(30)"])
    res = b.run(_task(), RunOpts(cwd=tmp_path, timeout_s=1))
    assert res.status is RunStatus.TIMED_OUT
    assert "timed out" in (res.error or "")


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
    assert res.status is RunStatus.SUCCEEDED
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
            status=RunStatus.SUCCEEDED if exit_code == 0 else RunStatus.FAILED,
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
