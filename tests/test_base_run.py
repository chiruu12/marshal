"""Integration tests for the shared CodingAgentBackend.run() chokepoint.

Verifies the two invariants that the base class must enforce for every backend: a hard
timeout that kills the child, and stdin closed so an interactive prompt can't deadlock.
Uses a dummy backend over the local Python interpreter — portable, fast, no real CLIs.
"""

from __future__ import annotations

import sys
from pathlib import Path

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


def test_run_stdin_closed_does_not_hang(tmp_path: Path) -> None:
    # If stdin were a TTY/open pipe this would block forever; DEVNULL gives EOF immediately.
    b = _Dummy([sys.executable, "-c", "import sys; sys.stdin.read(); print('eof-ok')"])
    res = b.run(_task(), RunOpts(cwd=tmp_path, timeout_s=10))
    assert res.status is RunStatus.SUCCEEDED
    assert res.text == "eof-ok"


def test_run_missing_binary(tmp_path: Path) -> None:
    b = _Dummy(["marshal-no-such-binary-xyz123"])
    res = b.run(_task(), RunOpts(cwd=tmp_path))
    assert res.status is RunStatus.FAILED
    assert "not found" in (res.error or "")


class _PartialUsage(CodingAgentBackend):
    """Flushes a usage line, then hangs — exercises partial-usage recovery on timeout."""

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
    """parse_output raises — recovery must swallow it and still report the timeout."""

    def build_invocation(self, task: TaskSpec, opts: RunOpts) -> list[str]:
        return [sys.executable, "-c", "import time; print('x', flush=True); time.sleep(30)"]

    def parse_output(self, raw_stdout: str, raw_stderr: str, exit_code: int) -> AgentResult:
        raise ValueError("parser blew up")


def test_timeout_recovery_error_does_not_mask_timeout(tmp_path: Path) -> None:
    res = _BoomParser().run(_task(), RunOpts(cwd=tmp_path, timeout_s=1))
    assert res.status is RunStatus.TIMED_OUT  # recovery failure swallowed, timeout still reported
    assert res.usage is None
