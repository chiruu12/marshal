"""Claude Code CLI adapter (`claude -p`).

Invocation reference (claude 2.1.170):

    claude -p --output-format json [--model MODEL]
              --permission-mode <plan|acceptEdits|bypassPermissions>
              [--resume SESSION] <PROMPT>

Run with `cwd` set to the worktree (the shared runner does this), so edits land in the
isolated worktree. `--output-format json` emits a single JSON result object on stdout:

    {"type":"result","subtype":"success","is_error":false,"result":"<final text>",
     "session_id":"<uuid>","total_cost_usd":0.0123,"duration_ms":4567,"num_turns":3,
     "usage":{"input_tokens":100,"output_tokens":200,
              "cache_creation_input_tokens":50,"cache_read_input_tokens":1000}}

Claude Code reports `total_cost_usd` directly, so usage is `native` (honest cost, no
estimation) - unlike Cursor. The shared runner closes stdin, so any interactive prompt hits
EOF instead of blocking; the permission modes below are all non-prompting.

Runtime behaviour verified by a live probe (2026-06-26): `acceptEdits` in `-p` mode does not
block with stdin closed, edits land in the worktree `cwd` (not a diverted dir - the bug that
affects Antigravity), and the JSON `usage`/`total_cost_usd` schema matches the parsing below.
The pure hooks (`build_invocation`/`map_permission`/`parse_output`) are contract-tested offline.
"""

from __future__ import annotations

import json
import shutil
import subprocess
from typing import Any

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


class ClaudeCodeBackend(CodingAgentBackend):
    name = "claude-code"
    binary = "claude"
    capabilities = Capabilities(
        json_output=True,
        stream_json=True,
        sessions=True,
        server_mode=False,
        native_usage=True,  # emits total_cost_usd + tokens in its JSON result
        permission_modes=frozenset(
            {PermissionMode.READ_ONLY, PermissionMode.SAFE_EDIT, PermissionMode.YOLO}
        ),
        permission_fidelity=PermissionFidelity.BOUNDARY_ONLY,
    )

    # Marshal's three tiers -> Claude Code's non-prompting permission modes. `default`/`auto`
    # prompt for approval and would deadlock a stdin-less run, so they are never used.
    _PERMISSION: dict[PermissionMode, list[str]] = {
        PermissionMode.READ_ONLY: ["--permission-mode", "plan"],
        PermissionMode.SAFE_EDIT: ["--permission-mode", "acceptEdits"],
        PermissionMode.YOLO: ["--permission-mode", "bypassPermissions"],
    }

    # --- hooks ---------------------------------------------------------------------------

    def account_info(self) -> dict[str, str] | None:
        """Auth via ``claude auth status`` (default ``--json``); map ``subscriptionType`` → plan.

        Fail closed when ``loggedIn`` is not strictly ``True``. Never raises.
        """
        if shutil.which(self.binary) is None:
            return None
        try:
            proc = subprocess.run(
                [self.binary, "auth", "status"],
                capture_output=True,
                text=True,
                timeout=15,
            )
        except (OSError, subprocess.SubprocessError):
            return None
        return _parse_auth_status(proc.stdout or "")

    def verifies_auth(self) -> bool:
        # ``claude auth status`` reports ``loggedIn``; None with binary present → not authenticated.
        return True

    def build_invocation(self, task: TaskSpec, opts: RunOpts) -> list[str]:
        argv = [self.binary, "-p", "--output-format", "json"]
        argv += self.map_permission(opts.permission)
        if opts.model:
            argv += ["--model", opts.model]
        if opts.session_id:
            argv += ["--resume", opts.session_id]
        argv.append(self._compose_prompt(task))
        return argv

    def parse_output(self, raw_stdout: str, raw_stderr: str, exit_code: int) -> AgentResult:
        obj = _parse_result(raw_stdout)
        if obj is None:
            # No parseable result object (auth failure, crash, empty). base.run() fills the reason
            # from the exit code + stderr tail, so a failure is never a silent success.
            return AgentResult(
                status=RunStatus.FAILED,
                exit_code=exit_code,
                raw_stdout=raw_stdout,
                raw_stderr=raw_stderr,
            )

        is_error = bool(obj.get("is_error", False))
        text = obj.get("result")
        text = text.strip() if isinstance(text, str) else ""
        session_id = obj.get("session_id")
        session_id = session_id if isinstance(session_id, str) else None

        ok = exit_code == 0 and not is_error
        return AgentResult(
            status=RunStatus.SUCCEEDED if ok else RunStatus.FAILED,
            text=text,
            session_id=session_id,
            usage=_extract_usage(obj, self.name),
            error=(text or obj.get("subtype") or "claude-code reported an error") if not ok else None,
            exit_code=exit_code,
            raw_stdout=raw_stdout,
            raw_stderr=raw_stderr,
        )


# --- module helpers ----------------------------------------------------------------------


def _parse_auth_status(raw: str) -> dict[str, str] | None:
    """Parse ``claude auth status`` JSON. None unless ``loggedIn`` is strictly ``True``."""
    raw = raw.strip()
    if not raw:
        return None
    try:
        obj = json.loads(raw)
    except json.JSONDecodeError:
        return None
    if not isinstance(obj, dict) or obj.get("loggedIn") is not True:
        return None
    info: dict[str, str] = {}
    sub = obj.get("subscriptionType")
    if isinstance(sub, str) and sub:
        info["plan"] = sub
    email = obj.get("email")
    if isinstance(email, str) and email and "plan" not in info:
        info["plan"] = "logged-in"
    return info or {"plan": "logged-in"}


def _parse_result(raw: str) -> dict[str, Any] | None:
    """Parse the single JSON result object `--output-format json` emits. None if unparseable."""
    raw = raw.strip()
    if not raw:
        return None
    try:
        obj = json.loads(raw)
        return obj if isinstance(obj, dict) else None
    except json.JSONDecodeError:
        # Defensive: salvage the object if anything preceded it on the stream.
        start = raw.find("{")
        if start <= 0:
            return None
        try:
            obj = json.loads(raw[start:])
        except json.JSONDecodeError:
            return None
        return obj if isinstance(obj, dict) else None


def _extract_usage(obj: dict[str, Any], backend_name: str) -> UsageRecord | None:
    """Build a usage record from the result. NATIVE only when a real cost was reported.

    Claiming NATIVE without a reported cost would assert a $0 the backend never claimed, so an
    absent `total_cost_usd` stays `unavailable` (tokens kept) - the engine's honesty rule.
    """
    raw_cost = obj.get("total_cost_usd")
    if isinstance(raw_cost, (int, float)) and not isinstance(raw_cost, bool):
        cost_usd, has_cost = float(raw_cost), True
    else:
        cost_usd, has_cost = 0.0, False
    usage = obj.get("usage")
    usage = usage if isinstance(usage, dict) else {}
    input_tokens = int(usage.get("input_tokens", 0) or 0)
    output_tokens = int(usage.get("output_tokens", 0) or 0)
    cache_read = int(usage.get("cache_read_input_tokens", 0) or 0)
    if not has_cost and input_tokens + output_tokens <= 0:
        return None
    return UsageRecord(
        backend=backend_name,
        input_tokens=input_tokens,
        output_tokens=output_tokens,
        cache_read_tokens=cache_read,
        cost_usd=cost_usd,
        source=UsageSource.NATIVE if has_cost else UsageSource.UNAVAILABLE,
    )
