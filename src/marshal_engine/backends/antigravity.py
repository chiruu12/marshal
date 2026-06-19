"""Google Antigravity CLI adapter (`agy`).

Invocation reference (Antigravity CLI 2.0, `agy`):

    agy [-m MODEL] [--dangerously-skip-permissions] [--conversation ID] -p "<PROMPT>"

`agy -p` runs one prompt non-interactively. Run with cwd = the target repo (agy operates on
its launch folder; there is no `--dir` flag).

Honest gaps from research (these shape what we expose):
  * NO reliable structured output yet — `--output-format json` is reported broken, so we parse
    PLAIN TEXT stdout. native_usage = False (no tokens/cost available headless).
  * Auth is OAuth-first; unattended `ANTIGRAVITY_API_KEY` is an unconfirmed upstream request.
    Expect a one-time OAuth on a persistent runner.
  * `agy` checks for a TTY; without one, stdout can be swallowed while exit code stays 0. A PTY
    wrapper (e.g. `script -q /dev/null`) belongs in the runner layer — TODO. Until then treat an
    empty success as suspect.
  * No headless session-id capture (`-p` doesn't return its conversation id), so sessions=False;
    `--conversation` is passed through only if the caller already has an id.
  * Only `safe-edit` and `yolo` are reliably non-prompting headless. There is no confirmed
    one-shot read-only flag (the read-only presets prompt), so READ_ONLY is unsupported here.
  * VERIFIED 2026-06-19: reply/read works headless, but file WRITES can divert to agy's scratch
    workspace (`~/.gemini/antigravity-cli/scratch`) instead of `cwd` when it cannot establish
    workspace trust headlessly — so worktree-isolated edits do NOT reliably land in the target
    dir yet. Needs a workspace-trust / PTY workaround before write use is dependable.

Models available: gemini-3.1-pro, gemini-3.5-flash, claude-sonnet-4.6, claude-opus-4.6, gpt-oss-120b.
"""

from __future__ import annotations

import shutil
import subprocess

from ..types import (
    AgentResult,
    Capabilities,
    PermissionMode,
    RunOpts,
    RunStatus,
    TaskSpec,
    UsageRecord,
    UsageSource,
)
from .base import CodingAgentBackend


class AntigravityBackend(CodingAgentBackend):
    name = "antigravity"
    binary = "agy"
    capabilities = Capabilities(
        json_output=False,  # --output-format json reported broken; text output only
        stream_json=False,
        sessions=False,  # -p does not return its conversation id
        server_mode=False,
        native_usage=False,  # no tokens/cost available headless
        permission_modes=frozenset({PermissionMode.SAFE_EDIT, PermissionMode.YOLO}),
    )

    # safe-edit and yolo both map to skip-permissions today: the default preset prompts (which
    # deadlocks headless), and there is no distinct one-shot safe-edit flag. Tighter scoping
    # comes from /config presets via the engine config layer later.
    _PERMISSION: dict[PermissionMode, list[str]] = {
        PermissionMode.SAFE_EDIT: ["--dangerously-skip-permissions"],
        PermissionMode.YOLO: ["--dangerously-skip-permissions"],
    }

    # --- hooks ---------------------------------------------------------------------------

    def check_available(self) -> bool:
        if shutil.which(self.binary) is None:
            return False
        try:
            proc = subprocess.run(
                [self.binary, "--version"], capture_output=True, text=True, timeout=15
            )
        except (OSError, subprocess.SubprocessError):
            return False
        return proc.returncode == 0

    def map_permission(self, mode: PermissionMode) -> list[str]:
        try:
            return list(self._PERMISSION[mode])
        except KeyError:
            raise ValueError(
                f"antigravity: permission mode {mode!r} is not supported headless "
                "(only safe-edit and yolo)"
            ) from None

    def build_invocation(self, task: TaskSpec, opts: RunOpts) -> list[str]:
        argv = [self.binary]
        argv += self.map_permission(opts.permission)
        if opts.model:
            argv += ["-m", opts.model]
        if opts.session_id:
            argv += ["--conversation", opts.session_id]
        # -p must come last with the prompt as its trailing argument.
        argv += ["-p", self._compose_prompt(task)]
        return argv

    @staticmethod
    def _compose_prompt(task: TaskSpec) -> str:
        prompt = task.goal
        if task.context_files:
            files = "\n".join(f"- {f}" for f in task.context_files)
            prompt = f"{prompt}\n\nRelevant files:\n{files}"
        return prompt

    def parse_output(self, raw_stdout: str, raw_stderr: str, exit_code: int) -> AgentResult:
        if exit_code != 0:
            return AgentResult(
                status=RunStatus.FAILED,
                error=raw_stderr.strip() or f"agy exited {exit_code}",
                exit_code=exit_code,
                raw_stdout=raw_stdout,
                raw_stderr=raw_stderr,
            )
        # Plain-text output; no machine-readable usage/session available.
        return AgentResult(
            status=RunStatus.SUCCEEDED,
            text=raw_stdout.strip(),
            usage=UsageRecord(backend=self.name, source=UsageSource.UNAVAILABLE),
            exit_code=exit_code,
            raw_stdout=raw_stdout,
            raw_stderr=raw_stderr,
        )
