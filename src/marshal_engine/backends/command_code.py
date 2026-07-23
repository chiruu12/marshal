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

from ..types import (
    AgentResult,
    Capabilities,
    PermissionFidelity,
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
        permission_fidelity=PermissionFidelity.BOUNDARY_ONLY,
    )

    # Headless `-p` auto-accept still BLOCKS the write/shell tools (the confirmation has no TTY to
    # answer), so safe-edit maps to --yolo with the git worktree as the enforced boundary (same stance
    # as cursor/opencode). read-only uses plan mode (no edits).
    _PERMISSION: dict[PermissionMode, list[str]] = {
        PermissionMode.READ_ONLY: ["--permission-mode", "plan"],
        PermissionMode.SAFE_EDIT: ["--yolo"],
        PermissionMode.YOLO: ["--yolo"],
    }

    # --- hooks ---------------------------------------------------------------------------

    def account_info(self) -> dict[str, str] | None:
        """Auth via ``command-code status --json`` (config.json alone is **not** an auth probe).

        Require ``authenticated`` strictly ``True``. Surfaces provider/model when present.
        Never raises.
        """
        if shutil.which(self.binary) is None:
            return None
        try:
            proc = subprocess.run(
                [self.binary, "status", "--json"],
                capture_output=True,
                text=True,
                timeout=15,
            )
        except (OSError, subprocess.SubprocessError):
            return None
        return _parse_status_json(proc.stdout or "")

    def verifies_auth(self) -> bool:
        # ``command-code status`` is authenticated-only; config.json presence ≠ logged in.
        return True

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


def _parse_status_json(raw: str) -> dict[str, str] | None:
    """Parse ``command-code status --json``. None unless ``authenticated`` is strictly ``True``."""
    raw = raw.strip()
    if not raw:
        return None
    try:
        obj = json.loads(raw)
    except json.JSONDecodeError:
        return None
    if not isinstance(obj, dict) or obj.get("authenticated") is not True:
        return None
    info: dict[str, str] = {}
    provider = obj.get("provider")
    model = obj.get("model")
    user = obj.get("user")
    if isinstance(provider, str) and provider:
        info["plan"] = provider
    elif isinstance(user, str) and user:
        info["plan"] = user
    if isinstance(model, str) and model:
        info["model"] = model
    return info or {"plan": "logged-in"}
