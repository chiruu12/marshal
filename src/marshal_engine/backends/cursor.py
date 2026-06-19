"""Cursor CLI adapter (`cursor-agent`).

Invocation reference (cursor-agent, headless):

    cursor-agent -p --output-format json --trust
                 [--mode plan | --force | --yolo]
                 [--model MODEL] --workspace CWD [--resume SESSION] <PROMPT>

`-p/--print` = non-interactive. `--output-format json` emits a single result object:

    {"type":"result","subtype":"success","is_error":false,"duration_ms":...,
     "result":"<final text>","session_id":"<uuid>"}

On failure the process exits non-zero and writes to stderr with no JSON object on stdout.

Notes / gaps baked in from research:
  * Cursor CLI emits NO tokens or cost in its output — usage is reported as unavailable here.
    The account-level Cursor Admin API (team/enterprise, per service-account) is wired later.
  * `--force`/`--yolo` mean "allow everything not explicitly denied"; tighter scoping is a
    deny-list in `.cursor/cli.json` applied by the engine, not an argv flag.
  * There is no `--cwd`; `--workspace` sets the repo root. `--trust` avoids the trust prompt.
  * `check_available` should pin/assert a minimum version — several headless hang bugs are
    version-gated and only fixed in recent builds.
"""

from __future__ import annotations

import json
import shutil
import subprocess
from typing import Any

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


class CursorBackend(CodingAgentBackend):
    name = "cursor"
    binary = "cursor-agent"
    capabilities = Capabilities(
        json_output=True,
        stream_json=True,
        sessions=True,
        server_mode=False,
        native_usage=False,  # no tokens/cost in CLI output; admin-API path added later
        permission_modes=frozenset(
            {PermissionMode.READ_ONLY, PermissionMode.SAFE_EDIT, PermissionMode.YOLO}
        ),
    )

    _PERMISSION: dict[PermissionMode, list[str]] = {
        PermissionMode.READ_ONLY: ["--mode", "plan"],
        PermissionMode.SAFE_EDIT: ["--force"],
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

    def map_permission(self, mode: PermissionMode) -> list[str]:
        try:
            return list(self._PERMISSION[mode])
        except KeyError:
            raise ValueError(f"cursor: unsupported permission mode {mode!r}") from None

    def build_invocation(self, task: TaskSpec, opts: RunOpts) -> list[str]:
        argv = [self.binary, "-p", "--output-format", "json", "--trust"]
        argv += self.map_permission(opts.permission)
        if opts.model:
            argv += ["--model", opts.model]
        argv += ["--workspace", str(opts.cwd)]
        if opts.session_id:
            argv += ["--resume", opts.session_id]
        argv.append(self._compose_prompt(task))
        return argv

    @staticmethod
    def _compose_prompt(task: TaskSpec) -> str:
        prompt = task.goal
        if task.context_files:
            mentions = " ".join(f"@{f}" for f in task.context_files)
            prompt = f"{prompt}\n\nRelevant context: {mentions}"
        return prompt

    def parse_output(self, raw_stdout: str, raw_stderr: str, exit_code: int) -> AgentResult:
        obj = _find_result(raw_stdout)

        if exit_code != 0 or obj is None:
            return AgentResult(
                status=RunStatus.FAILED,
                error=raw_stderr.strip() or f"cursor-agent exited {exit_code}",
                exit_code=exit_code,
                raw_stdout=raw_stdout,
                raw_stderr=raw_stderr,
            )

        is_error = bool(obj.get("is_error", False))
        text = str(obj.get("result", "") or "")
        sid = obj.get("session_id")
        session_id = sid if isinstance(sid, str) else None

        return AgentResult(
            status=RunStatus.FAILED if is_error else RunStatus.SUCCEEDED,
            text=text,
            session_id=session_id,
            usage=UsageRecord(backend=self.name, source=UsageSource.UNAVAILABLE),
            error=text if is_error else None,
            exit_code=exit_code,
            raw_stdout=raw_stdout,
            raw_stderr=raw_stderr,
        )


# --- module helpers ----------------------------------------------------------------------


def _find_result(raw: str) -> dict[str, Any] | None:
    """Return the `type == "result"` object from the JSON output (last one wins)."""
    found: dict[str, Any] | None = None
    for line in raw.splitlines():
        line = line.strip()
        if not line.startswith("{"):
            continue
        try:
            obj = json.loads(line)
        except json.JSONDecodeError:
            continue
        if isinstance(obj, dict) and obj.get("type") == "result":
            found = obj
    if found is None:
        try:
            whole = json.loads(raw.strip())
        except json.JSONDecodeError:
            return None
        if isinstance(whole, dict):
            found = whole
    return found
