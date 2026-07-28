"""Google Gemini CLI adapter (`gemini -p`).

Invocation reference (google-gemini/gemini-cli docs — NOT live-verified here; `gemini` was
absent on PATH in the adapter worktree):

    gemini -p "<PROMPT>" --output-format json [--model MODEL]
           --approval-mode <plan|auto_edit|yolo>
           [--resume SESSION] [--skip-trust]

Headless mode is triggered by `-p`/`--prompt` (required when stdin is closed). `--output-format
json` emits one JSON object on stdout:

    {"response":"<text>","stats":{...},"error":null}

Documented `stats` shapes vary by CLI version: newer builds nest per-model token counts under
`stats.models.<name>.tokens` (`prompt`, `candidates`, `cached`, …); older docs show flat
`stats.session` / `stats.model` turn counts without tokens. The parser accepts both and never
fabricates cost — Gemini JSON documents tokens but not USD, so usage stays `unavailable` for
cost even when tokens are present.

`--approval-mode default` prompts for tool approval and deadlocks headless runs; only the three
non-prompting modes below are mapped. `permission_fidelity` is `boundary-only` (no Marshal deny
layer; worktree isolation is the safety boundary).

Doctor stays path-only: no cheap authenticated-only probe is wired (verify `gemini auth` or
equivalent against a real install before adding `account_info`/`verifies_auth`).
"""

from __future__ import annotations

import json
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


class GeminiBackend(CodingAgentBackend):
    name = "gemini"
    binary = "gemini"
    capabilities = Capabilities(
        json_output=True,
        stream_json=False,
        sessions=True,  # `--resume` accepts a session id (documented CLI reference)
        server_mode=False,
        native_usage=True,  # tokens in JSON stats when present; cost is never reported
        permission_modes=frozenset(
            {PermissionMode.READ_ONLY, PermissionMode.SAFE_EDIT, PermissionMode.YOLO}
        ),
        permission_fidelity=PermissionFidelity.BOUNDARY_ONLY,
    )

    # Marshal tiers -> Gemini `--approval-mode` (documented choices; verify against `gemini --help`).
    _PERMISSION: dict[PermissionMode, list[str]] = {
        PermissionMode.READ_ONLY: ["--approval-mode", "plan"],
        PermissionMode.SAFE_EDIT: ["--approval-mode", "auto_edit"],
        PermissionMode.YOLO: ["--approval-mode", "yolo"],
    }

    # --- hooks ---------------------------------------------------------------------------

    def build_invocation(self, task: TaskSpec, opts: RunOpts) -> list[str]:
        argv = [self.binary, "-p", self._compose_prompt(task), "--output-format", "json"]
        argv += self.map_permission(opts.permission)
        # VERIFY: `--skip-trust` is documented for headless workspace trust; confirm on a real CLI.
        argv += ["--skip-trust"]
        if opts.model:
            argv += ["--model", opts.model]
        if opts.session_id:
            argv += ["--resume", opts.session_id]
        return argv

    def parse_output(self, raw_stdout: str, raw_stderr: str, exit_code: int) -> AgentResult:
        obj = _parse_result(raw_stdout)
        if obj is None:
            return AgentResult(
                status=RunStatus.FAILED,
                exit_code=exit_code,
                raw_stdout=raw_stdout,
                raw_stderr=raw_stderr,
            )

        err = obj.get("error")
        error_msg: str | None = None
        if isinstance(err, dict):
            msg = err.get("message")
            if isinstance(msg, str) and msg.strip():
                error_msg = msg.strip()
            elif err.get("type"):
                error_msg = str(err["type"])

        response = obj.get("response")
        text = response.strip() if isinstance(response, str) else ""
        session_id = _extract_session_id(obj.get("stats"))
        usage = _extract_usage(obj.get("stats"), self.name)

        ok = exit_code == 0 and error_msg is None
        if not ok and not error_msg:
            error_msg = text or "gemini reported an error"

        return AgentResult(
            status=RunStatus.SUCCEEDED if ok else RunStatus.FAILED,
            text=text,
            session_id=session_id,
            usage=usage,
            error=error_msg if not ok else None,
            exit_code=exit_code,
            raw_stdout=raw_stdout,
            raw_stderr=raw_stderr,
        )


# --- module helpers ----------------------------------------------------------------------


def _parse_result(raw: str) -> dict[str, Any] | None:
    """Parse the single JSON object `--output-format json` emits. None if unparseable."""
    raw = raw.strip()
    if not raw:
        return None
    try:
        obj = json.loads(raw)
        return obj if isinstance(obj, dict) else None
    except json.JSONDecodeError:
        start = raw.find("{")
        if start <= 0:
            return None
        try:
            obj = json.loads(raw[start:])
        except json.JSONDecodeError:
            return None
        return obj if isinstance(obj, dict) else None


def _extract_session_id(stats: Any) -> str | None:
    """Best-effort session id from stats; field names unverified without a live CLI."""
    if not isinstance(stats, dict):
        return None
    for key in ("sessionId", "session_id", "id"):
        val = stats.get(key)
        if isinstance(val, str) and val:
            return val
    session = stats.get("session")
    if isinstance(session, dict):
        for key in ("sessionId", "session_id", "id"):
            val = session.get(key)
            if isinstance(val, str) and val:
                return val
    return None


def _extract_usage(stats: Any, backend_name: str) -> UsageRecord | None:
    """Build usage from JSON stats. Tokens when reported; cost always unavailable."""
    if not isinstance(stats, dict):
        return None
    input_tokens = 0
    output_tokens = 0
    cache_read = 0

    models = stats.get("models")
    if isinstance(models, dict):
        for model_stats in models.values():
            if not isinstance(model_stats, dict):
                continue
            tokens = model_stats.get("tokens")
            if not isinstance(tokens, dict):
                continue
            input_tokens += int(tokens.get("prompt", 0) or 0)
            output_tokens += int(tokens.get("candidates", 0) or 0)
            cache_read += int(tokens.get("cached", 0) or 0)

    if input_tokens + output_tokens + cache_read <= 0:
        return None

    return UsageRecord(
        backend=backend_name,
        input_tokens=input_tokens,
        output_tokens=output_tokens,
        cache_read_tokens=cache_read,
        cost_usd=0.0,
        source=UsageSource.UNAVAILABLE,
    )
