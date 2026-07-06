"""Google Antigravity CLI adapter (`agy`).

Invocation reference (Antigravity CLI 2.0, `agy`):

    agy [-m MODEL] [--dangerously-skip-permissions] [--conversation ID] -p "<PROMPT>"

`agy -p` runs one prompt non-interactively. Run with cwd = the target repo (agy operates on
its launch folder; there is no `--dir` flag).

Honest gaps from research (these shape what we expose):
  * NO reliable structured output yet - `--output-format json` is reported broken, so we parse
    PLAIN TEXT stdout. native_usage = False (no tokens/cost available headless).
  * Auth is OAuth-first; unattended `ANTIGRAVITY_API_KEY` is an unconfirmed upstream request.
    Expect a one-time OAuth on a persistent runner.
  * `agy` checks for a TTY; without one, stdout can be swallowed while exit code stays 0. A PTY
    wrapper (e.g. `script -q /dev/null`) belongs in the runner layer - TODO. Until then treat an
    empty success as suspect.
  * No headless session-id capture (`-p` doesn't return its conversation id), so sessions=False;
    `--conversation` is passed through only if the caller already has an id.
  * Only `safe-edit` and `yolo` are reliably non-prompting headless. There is no confirmed
    one-shot read-only flag (the read-only presets prompt), so READ_ONLY is unsupported here.
  * WORKSPACE TRUST (fixed 2026-06-27): headless `agy` cannot establish workspace trust without a
    TTY, so it used to write edits into its scratch dir (`~/.gemini/antigravity-cli/scratch`)
    instead of `cwd` ("you do not have an active workspace"). `prepare()` now pre-registers the
    run's worktree in `~/.gemini/antigravity-cli/settings.json` `trustedWorkspaces`, and the run
    passes `--add-dir <cwd>`. VERIFIED end-to-end: edits then land in the worktree, not scratch.
    `--add-dir` alone was insufficient (the prior known limitation); the trust entry is the fix.

Models available: gemini-3.1-pro, gemini-3.5-flash, claude-sonnet-4.6, claude-opus-4.6, gpt-oss-120b.
"""

from __future__ import annotations

import json
import os
import shutil
import subprocess
import tempfile
import threading
from pathlib import Path

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


#: Where the agy CLI keeps its user settings (incl. `trustedWorkspaces`). An attribute on the
#: backend so tests can point it at a temp file instead of the real home.
DEFAULT_SETTINGS_PATH = Path.home() / ".gemini" / "antigravity-cli" / "settings.json"


class AntigravityBackend(CodingAgentBackend):
    name = "antigravity"
    binary = "agy"
    # agy reads/writes one global settings file; serialize concurrent trust updates (parallel runs).
    _settings_lock = threading.Lock()
    settings_path = DEFAULT_SETTINGS_PATH
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
        # Add the worktree to the active workspace; paired with the trust entry prepare() writes,
        # this makes edits land in cwd instead of agy's scratch dir.
        argv += ["--add-dir", str(opts.cwd)]
        if opts.model:
            argv += ["-m", opts.model]
        if opts.session_id:
            argv += ["--conversation", opts.session_id]
        # -p must come last with the prompt as its trailing argument.
        argv += ["-p", self._compose_prompt(task)]
        return argv

    def prepare(self, opts: RunOpts) -> None:
        """Register the run's worktree as a trusted agy workspace so headless edits land in `cwd`.

        Without a trust entry, headless `agy` writes into its scratch dir instead of `cwd` (it cannot
        establish workspace trust without a TTY). Merge-preserving, atomic, and idempotent; dead
        worktree paths are pruned so the list stays bounded. Serialized for parallel runs.
        """
        _trust_workspace(self.settings_path, Path(opts.cwd), self._settings_lock)

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


# --- module helpers ----------------------------------------------------------------------


def _trust_workspace(settings_path: Path, cwd: Path, lock: threading.Lock) -> None:
    """Add `cwd` to agy's `trustedWorkspaces` in `settings_path`, preserving other settings.

    Merge-preserving (other keys untouched), idempotent (no duplicate entry), and atomic (unique
    temp + replace, so a concurrent agy read never sees a torn file even if a writer dies
    between write + replace). Dead paths are pruned so the trust list stays bounded to live
    worktrees. The lock serializes concurrent writers (parallel runs all share this one
    global file).
    """
    cwd_str = str(cwd.resolve())
    with lock:
        data: dict[str, object] = {}
        if settings_path.exists():
            try:
                loaded = json.loads(settings_path.read_text(encoding="utf-8"))
                if isinstance(loaded, dict):
                    data = loaded
            except (json.JSONDecodeError, OSError):
                data = {}  # a malformed file is replaced, not trusted
        existing = data.get("trustedWorkspaces")
        trusted = [t for t in existing if isinstance(t, str)] if isinstance(existing, list) else []
        # Keep this worktree + any other still-existing trusted paths; drop dead ones.
        kept = [t for t in trusted if t != cwd_str and Path(t).exists()]
        data["trustedWorkspaces"] = [*kept, cwd_str]
        settings_path.parent.mkdir(parents=True, exist_ok=True)
        # Atomic temp + replace. The temp MUST be uniquely named (mkstemp) so a partial write
        # that crashes between write_text and replace can't be confused with a later writer's
        # temp - a fixed `.tmp` filename would also race under any future code path that
        # releases the lock between phases.
        fd, tmp_str = tempfile.mkstemp(
            dir=str(settings_path.parent), prefix=f"{settings_path.name}.", suffix=".tmp"
        )
        try:
            with os.fdopen(fd, "w", encoding="utf-8") as f:
                f.write(json.dumps(data, indent=2))
            os.replace(tmp_str, settings_path)
        except BaseException:
            # Never leave a half-written temp file under a unique name (mkstemp gives us the
            # chance to clean up properly - a fixed name would mask the next writer).
            try:
                os.unlink(tmp_str)
            except OSError:
                pass
            raise
