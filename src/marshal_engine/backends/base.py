"""The Marshal backend base class — the cornerstone of the engine.

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
import subprocess
from abc import ABC, abstractmethod

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

    # --- shared, concrete run loop -------------------------------------------------------

    def run(self, task: TaskSpec, opts: RunOpts) -> AgentResult:
        """Build the invocation and execute it with a hard timeout and no stdin.

        This is the single chokepoint that defends the two universal headless footguns:
        the process is killed if it exceeds `opts.timeout_s`, and stdin is closed so an
        unexpected interactive prompt fails fast instead of deadlocking forever.
        """
        argv = self.build_invocation(task, opts)
        env = {**os.environ, **opts.extra_env}

        try:
            proc = subprocess.run(
                argv,
                cwd=str(opts.cwd),
                env=env,
                stdin=subprocess.DEVNULL,     # headless: never wait on stdin
                capture_output=True,
                text=True,
                timeout=opts.timeout_s,        # hard timeout — kills the child on expiry
                start_new_session=True,        # own process group (group-kill hardening: TODO runner.py)
            )
        except subprocess.TimeoutExpired as exc:
            return AgentResult(
                status=RunStatus.TIMED_OUT,
                error=f"{self.name}: timed out after {opts.timeout_s}s",
                session_id=opts.session_id,
                raw_stdout=_as_text(exc.stdout),
                raw_stderr=_as_text(exc.stderr),
            )
        except FileNotFoundError:
            return AgentResult(
                status=RunStatus.FAILED,
                error=f"{self.name}: binary {self.binary!r} not found on PATH",
            )

        return self.parse_output(proc.stdout, proc.stderr, proc.returncode)


def _as_text(value: object) -> str:
    if value is None:
        return ""
    if isinstance(value, bytes):
        return value.decode("utf-8", "replace")
    return str(value)
