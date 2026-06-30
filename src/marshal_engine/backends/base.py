"""The Marshal backend base class - the cornerstone of the engine.

Every headless coding agent (Cursor, OpenCode, Codex, Gemini, ...) is a subclass that
implements four pure-or-simple hooks. The base class owns the one thing that must never
be gotten wrong: spawning the process with a hard timeout and no stdin.

Design rules (see docs/design.md):
  * `build_invocation` and `map_permission` are PURE functions returning argv / flags.
    They must be unit-testable without spawning a process. This is where contract tests live.
  * `run()` is concrete and shared: it builds the argv, runs it in `opts.cwd` with an
    external timeout and stdin closed, then delegates to `parse_output`.
  * The backend does NOT manage worktrees. The fleet/worktree layer creates the worktree
    and passes it as `opts.cwd`. Backends are stateless and isolated.
"""

from __future__ import annotations

import os
import signal
import subprocess
import sys
import time
from abc import ABC, abstractmethod

from ..env import child_env
from ..types import AgentResult, Capabilities, PermissionMode, RunOpts, RunStatus, TaskSpec, UsageRecord


class CodingAgentBackend(ABC):
    """Abstract base for a headless coding-agent backend."""

    #: short stable id, e.g. "cursor" | "opencode" | "codex"
    name: str
    #: the executable to invoke, e.g. "cursor-agent" | "opencode" | "codex"
    binary: str
    #: feature flags; subclasses set this so the orchestrator can degrade gracefully
    capabilities: Capabilities

    # --- hooks subclasses must implement -------------------------------------------------

    @abstractmethod
    def check_available(self) -> bool:
        """Return True if the binary is installed, authenticated, and a supported version.

        Implementations should probe `binary --version` (and pin/assert a minimum where
        hangs/bugs are version-gated) plus verify credentials are present.
        """

    @abstractmethod
    def build_invocation(self, task: TaskSpec, opts: RunOpts) -> list[str]:
        """Pure function: (task, opts) -> argv. No side effects, no process spawning."""

    @abstractmethod
    def map_permission(self, mode: PermissionMode) -> list[str]:
        """Pure function: a normalized permission tier -> this backend's native flags."""

    @abstractmethod
    def parse_output(self, raw_stdout: str, raw_stderr: str, exit_code: int) -> AgentResult:
        """Normalize this backend's raw output into an AgentResult.

        Must treat a non-zero exit (or unparseable output) as failure, and populate
        usage/session_id/files_changed where the backend exposes them.
        """

    # --- optional hook -------------------------------------------------------------------

    def extract_usage(self, result: AgentResult) -> UsageRecord | None:
        """Return the usage record for a run. Default: whatever parse_output captured.

        Backends without in-output usage (e.g. Cursor) override this to fetch from an
        admin API or estimate from a price table, tagging the record's `source` accordingly.
        """
        return result.usage

    def account_info(self) -> dict[str, str] | None:
        """Return human-readable account facts (e.g. plan tier, default model), or None.

        This is account *metadata* a CLI can report cheaply - NOT a usage record, so it never
        touches the cost ledger. Backends that expose it (e.g. Cursor's `about`) override this;
        the default is None. Implementations must be side-effect-light and never raise: return
        None on any failure (missing binary, unauthenticated, unparseable output).
        """
        return None

    def verifies_auth(self) -> bool:
        """True if account_info() doubles as an authenticated-only probe.

        When True, a None from account_info() *while the binary is on PATH* reliably means "not
        logged in" (not "metadata unsupported") - so `marshal doctor` reports the backend as
        present-but-unauthenticated rather than green-lighting it. This closes the gap where a CLI
        passes `--version` (unauthenticated) but dies on the first real run. Default False: most
        backends have no cheap authed probe, so doctor reports CLI presence without claiming the
        credentials are valid.
        """
        return False

    def prepare(self, opts: RunOpts) -> None:
        """Optional per-run setup, run just before the process is spawned (default: no-op).

        A seam for backend-specific preconditions that aren't pure argv - e.g. Antigravity
        registering the run's worktree as a trusted workspace so its headless edits land in `cwd`
        instead of a scratch dir. Keep it fast and idempotent; a failure here fails the run.
        """

    # --- shared, concrete run loop -------------------------------------------------------

    def run(self, task: TaskSpec, opts: RunOpts) -> AgentResult:
        """Build the invocation and execute it with a hard timeout and no stdin.

        This is the single chokepoint that defends the two universal headless footguns:
        the process is killed if it exceeds `opts.timeout_s`, and stdin is closed so an
        unexpected interactive prompt fails fast instead of deadlocking forever. On timeout the
        whole process *group* is killed (`start_new_session` + `os.killpg`), so agent grandchildren
        (subagents, MCP servers, tool shells) are not orphaned.
        """
        argv = self.build_invocation(task, opts)
        # Scrub the driver's VIRTUAL_ENV/PYTHONHOME so the agent's tooling (uv/python) resolves the
        # worktree's own environment, not the driver's - otherwise `uv run pytest` tests stale code.
        env = child_env(opts.extra_env)
        start = time.monotonic()

        def _elapsed_ms() -> int:
            return int((time.monotonic() - start) * 1000)

        try:
            self.prepare(opts)
        except Exception as exc:  # noqa: BLE001 - a prepare failure is a run failure, not a crash
            return AgentResult(
                status=RunStatus.FAILED,
                error=f"{self.name}: prepare failed: {exc}",
                duration_ms=_elapsed_ms(),
            )

        try:
            proc = subprocess.Popen(
                argv,
                cwd=str(opts.cwd),
                env=env,
                stdin=subprocess.DEVNULL,     # headless: never wait on stdin
                stdout=subprocess.PIPE,
                stderr=subprocess.PIPE,
                text=True,
                start_new_session=True,        # own process group so a timeout can kill the tree
            )
        except FileNotFoundError:
            return AgentResult(
                status=RunStatus.FAILED,
                error=f"{self.name}: binary {self.binary!r} not found on PATH",
                duration_ms=_elapsed_ms(),
            )

        # Notify the caller of the child pid (for later cancellation via process-group signal).
        # Recording the pid is best-effort: if the callback raises, do NOT let it escape here - that
        # would skip communicate()/the timeout and leak the live process. The run proceeds (still
        # timed + killed); only later cancel-by-pid is unavailable for this run.
        if opts.on_pid is not None:
            try:
                opts.on_pid(proc.pid)
            except Exception as exc:  # noqa: BLE001 - never leak the process over a pid-record failure
                print(f"[marshal] {self.name}: on_pid callback failed: {exc}", file=sys.stderr)

        # start_new_session makes the child its own group leader, so its pgid == its pid. Capture
        # it now, while the leader is alive - resolving it later (after a fast leader exit) can race
        # a zombie and strand the group.
        pgid = proc.pid

        try:
            out, err = proc.communicate(timeout=opts.timeout_s)
        except subprocess.TimeoutExpired:
            _kill_process_group(pgid)
            out, err = _drain(proc)  # bounded: a setsid-escaped survivor holding the pipe can't hang us
            return AgentResult(
                status=RunStatus.TIMED_OUT,
                error=f"{self.name}: timed out after {opts.timeout_s}s",
                session_id=opts.session_id,
                usage=self._recover_partial_usage(out, err),
                raw_stdout=out,
                raw_stderr=err,
                duration_ms=_elapsed_ms(),
            )

        result = self.parse_output(out, err, proc.returncode)
        result.duration_ms = _elapsed_ms()
        if result.status is RunStatus.FAILED and not result.error:
            # parse_output found no reason (e.g. the backend errored on stderr, not in its JSON
            # stream). Surface the exit code + a stderr tail so a failure is never a silent "failed".
            result.error = _failure_reason(self.name, proc.returncode, err)
        return result

    def _recover_partial_usage(self, stdout: str, stderr: str) -> UsageRecord | None:
        """Best-effort: salvage usage from a timed-out run's partial output. Never raises.

        Tokens are real spend even if the run was killed mid-stream, so recovering them keeps the
        cost ledger honest. A recovery failure must never mask the timeout - all errors are swallowed.
        """
        if not stdout.strip():
            return None
        try:
            return self.parse_output(stdout, stderr, 0).usage
        except Exception:  # noqa: BLE001 - recovery is best-effort and must not mask the timeout
            return None


def _kill_process_group(pgid: int, grace_s: float = 0.5) -> None:
    """SIGTERM then unconditionally SIGKILL the child's whole process group.

    `pgid` is the leader pid (the child was started with `start_new_session=True`). After SIGTERM
    we wait a short grace for cooperative shutdown, then SIGKILL the *whole group* regardless of
    whether the leader itself already exited - escalation must depend on the group dying, not on the
    leader being reaped, or a SIGTERM-ignoring grandchild survives. A grandchild that escaped the
    session (`setsid`) cannot be reached here; the bounded drain in `run()` is what keeps such a
    survivor from hanging the engine.
    """
    try:
        os.killpg(pgid, signal.SIGTERM)
    except (ProcessLookupError, PermissionError):
        return  # group already gone
    time.sleep(grace_s)
    try:
        os.killpg(pgid, signal.SIGKILL)
    except (ProcessLookupError, PermissionError):
        return


def _drain(proc: subprocess.Popen[str]) -> tuple[str, str]:
    """Collect remaining output and reap, bounded so a surviving pipe-holder can't block forever."""
    try:
        out, err = proc.communicate(timeout=2)
        return out or "", err or ""
    except subprocess.TimeoutExpired as exc:
        proc.poll()  # non-blocking: reap the killed leader now (a setsid'd survivor keeps the pipe)
        return _as_text(exc.stdout), _as_text(exc.stderr)


def _as_text(value: object) -> str:
    if value is None:
        return ""
    if isinstance(value, bytes):
        return value.decode("utf-8", "replace")
    return str(value)


def _failure_reason(name: str, exit_code: int, stderr: str) -> str:
    """A debuggable reason for a failed run that parse_output couldn't explain: exit code + stderr tail."""
    tail = " ".join(stderr.strip().splitlines()[-3:])
    reason = f"{name}: exited with code {exit_code}"
    return f"{reason}: {tail}" if tail else reason
