"""Command Code CLI adapter (`command-code -p`).

Invocation reference (command-code 0.40.x), headless:

    command-code -p "<PROMPT>" --skip-onboarding -t --max-turns N
                 [--permission-mode plan|auto-accept | --yolo] [-m MODEL]

`-p/--print` runs non-interactively: it prints the final assistant response to stdout and exits.
There is no JSON output for `-p` and no token/cost accounting in the output, so usage is reported
as `unavailable` - Command Code is a hosted account whose spend lives in its own dashboard, not the
CLI output (claiming a $0 cost it never reported would be a lie).

Headless hygiene baked into every invocation:
  * `--skip-onboarding` skips the interactive taste-onboarding step (it would block an automated run).
  * `-t/--trust` auto-trusts the project so the first-run permission prompt can't deadlock a run
    that has no stdin.
  * `--max-turns N` caps the agent loop; the shared runner's wall-clock timeout is the hard bound,
    this just stops a cheap runaway. Exit code 8 means the cap was hit (task likely incomplete).

The CLI operates on the current working directory (the worktree, set by the shared runner) - there
is no `--workspace`/`-C` flag, so `build_invocation` passes no directory and relies on `opts.cwd`.
"""

from __future__ import annotations

import json
import re
import shutil
import subprocess
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

#: Agent-loop turn cap. The runner's wall-clock timeout is the real bound; this guards a runaway.
_MAX_TURNS = 50

#: Exit code Command Code returns when `--max-turns` is hit (per `command-code --help`).
_CAP_HIT_EXIT = 8

_ANSI = re.compile(r"\x1b\[[0-9;?]*[ -/]*[@-~]")


class CommandCodeBackend(CodingAgentBackend):
    name = "command-code"
    binary = "command-code"
    capabilities = Capabilities(
        json_output=False,
        stream_json=False,
        sessions=False,  # -p prints plain text; no session id is surfaced to resume from
        server_mode=False,
        native_usage=False,  # hosted account: no tokens/cost in CLI output -> reported unavailable
        permission_modes=frozenset(
            {PermissionMode.READ_ONLY, PermissionMode.SAFE_EDIT, PermissionMode.YOLO}
        ),
    )

    _PERMISSION: dict[PermissionMode, list[str]] = {
        PermissionMode.READ_ONLY: ["--permission-mode", "plan"],
        PermissionMode.SAFE_EDIT: ["--permission-mode", "auto-accept"],
        PermissionMode.YOLO: ["--yolo"],
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

    def account_info(self) -> dict[str, str] | None:
        """Provider + default model from `~/.commandcode/config.json` (no network, never raises).

        Honest account context for `marshal doctor` - the hosted provider and the user's default
        model - not a usage record. Returns None if the file is missing or unparseable.
        """
        cfg = Path.home() / ".commandcode" / "config.json"
        try:
            data = json.loads(cfg.read_text(encoding="utf-8"))
        except (OSError, ValueError):
            return None
        if not isinstance(data, dict):
            return None
        info: dict[str, str] = {}
        provider = data.get("provider")
        model = data.get("model")
        if isinstance(provider, str) and provider:
            info["plan"] = provider
        if isinstance(model, str) and model:
            info["model"] = model
        return info or None

    def map_permission(self, mode: PermissionMode) -> list[str]:
        try:
            return list(self._PERMISSION[mode])
        except KeyError:
            raise ValueError(f"command-code: unsupported permission mode {mode!r}") from None

    def build_invocation(self, task: TaskSpec, opts: RunOpts) -> list[str]:
        argv = [
            self.binary,
            "-p",
            self._compose_prompt(task),
            "--skip-onboarding",
            "-t",
            "--max-turns",
            str(_MAX_TURNS),
        ]
        argv += self.map_permission(opts.permission)
        if opts.model:
            argv += ["-m", opts.model]
        return argv

    @staticmethod
    def _compose_prompt(task: TaskSpec) -> str:
        prompt = task.goal
        if task.context_files:
            files = "\n".join(f"- {f}" for f in task.context_files)
            prompt = f"{prompt}\n\nRelevant files:\n{files}"
        return prompt

    def parse_output(self, raw_stdout: str, raw_stderr: str, exit_code: int) -> AgentResult:
        text = _ANSI.sub("", raw_stdout).strip()
        usage = UsageRecord(backend=self.name, source=UsageSource.UNAVAILABLE)

        if exit_code == _CAP_HIT_EXIT:
            return AgentResult(
                status=RunStatus.FAILED,
                text=text,
                usage=usage,
                error=f"command-code hit the --max-turns cap ({_MAX_TURNS}); task may be incomplete",
                exit_code=exit_code,
                raw_stdout=raw_stdout,
                raw_stderr=raw_stderr,
            )

        ok = exit_code == 0
        return AgentResult(
            status=RunStatus.SUCCEEDED if ok else RunStatus.FAILED,
            text=text,
            usage=usage,
            error=None if ok else (raw_stderr.strip() or f"command-code exited {exit_code}"),
            exit_code=exit_code,
            raw_stdout=raw_stdout,
            raw_stderr=raw_stderr,
        )
