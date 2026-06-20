"""OpenAI Codex CLI adapter (`codex exec`).

Invocation reference (codex-cli 0.138.0):

    codex exec --json --color never --skip-git-repo-check
               -s <read-only|workspace-write|danger-full-access>
               [-m MODEL] [-C CWD] [resume <session>] <PROMPT>

`--json` emits JSONL events on stdout. Event shapes confirmed from a live probe:

    {"type":"thread.started","thread_id":"<uuid>"}
    {"type":"turn.started"}
    {"type":"turn.failed","error":{"message":"..."}}
    {"type":"error","message":"..."}

The success-path message/token events are parsed best-effort below and should be
re-verified against a live *successful* run (the probe account had hit its usage limit).
The shared runner closes stdin, so Codex's "reading from stdin" path hits EOF immediately
rather than blocking.
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


class CodexBackend(CodingAgentBackend):
    name = "codex"
    binary = "codex"
    capabilities = Capabilities(
        json_output=True,
        stream_json=True,
        sessions=True,
        server_mode=False,
        native_usage=True,  # token counts come from JSON events; cost is estimated via price table
        permission_modes=frozenset(
            {PermissionMode.READ_ONLY, PermissionMode.SAFE_EDIT, PermissionMode.YOLO}
        ),
    )

    _SANDBOX: dict[PermissionMode, list[str]] = {
        PermissionMode.READ_ONLY: ["--sandbox", "read-only"],
        PermissionMode.SAFE_EDIT: ["--sandbox", "workspace-write"],
        PermissionMode.YOLO: ["--dangerously-bypass-approvals-and-sandbox"],
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
            return list(self._SANDBOX[mode])
        except KeyError:
            raise ValueError(f"codex: unsupported permission mode {mode!r}") from None

    def build_invocation(self, task: TaskSpec, opts: RunOpts) -> list[str]:
        argv = [self.binary, "exec", "--json", "--color", "never", "--skip-git-repo-check"]
        argv += self.map_permission(opts.permission)
        if opts.model:
            argv += ["-m", opts.model]
        argv += ["-C", str(opts.cwd)]
        if opts.session_id:
            argv += ["resume", opts.session_id]
        argv.append(self._compose_prompt(task))
        return argv

    @staticmethod
    def _compose_prompt(task: TaskSpec) -> str:
        prompt = task.goal
        if task.context_files:
            files = "\n".join(f"- {f}" for f in task.context_files)
            prompt = f"{prompt}\n\nRelevant files:\n{files}"
        return prompt

    def parse_output(self, raw_stdout: str, raw_stderr: str, exit_code: int) -> AgentResult:
        events = _parse_jsonl(raw_stdout)

        session_id: str | None = None
        text_parts: list[str] = []
        error_msg: str | None = None
        usage = UsageRecord(backend=self.name, source=UsageSource.UNAVAILABLE)
        found_tokens = False

        for ev in events:
            etype = ev.get("type", "")
            if etype == "thread.started":
                session_id = ev.get("thread_id") or session_id
            elif etype in ("error", "turn.failed"):
                err = ev.get("error")
                if isinstance(err, dict):
                    error_msg = err.get("message") or error_msg
                error_msg = error_msg or ev.get("message")
            else:
                # best-effort success-path extraction (re-verify schema against a live run)
                txt = _extract_text(ev)
                if txt:
                    text_parts.append(txt)
                tok = _extract_tokens(ev)
                if tok is not None:
                    usage.input_tokens += tok["input"]
                    usage.output_tokens += tok["output"]
                    usage.cache_read_tokens += tok["cache_read"]
                    found_tokens = True

        # Codex reports tokens but never a cost, so its source stays UNAVAILABLE; the fleet's pricing
        # step estimates cost from these tokens (-> ESTIMATED when the model is priced). Claiming
        # NATIVE here would assert a $0 cost the backend never reported.

        ok = exit_code == 0 and error_msg is None
        return AgentResult(
            status=RunStatus.SUCCEEDED if ok else RunStatus.FAILED,
            text="\n".join(text_parts).strip(),
            session_id=session_id,
            usage=usage if found_tokens else None,
            error=error_msg if not ok else None,
            exit_code=exit_code,
            raw_stdout=raw_stdout,
            raw_stderr=raw_stderr,
        )


# --- module helpers ----------------------------------------------------------------------


def _parse_jsonl(raw: str) -> list[dict[str, Any]]:
    events: list[dict[str, Any]] = []
    for line in raw.splitlines():
        line = line.strip()
        if not line.startswith("{"):
            continue
        try:
            obj = json.loads(line)
        except json.JSONDecodeError:
            continue
        if isinstance(obj, dict):
            events.append(obj)
    return events


def _extract_text(ev: dict[str, Any]) -> str | None:
    if ev.get("type", "") not in ("agent_message", "item.completed", "turn.completed", "assistant"):
        return None
    for key in ("message", "text", "last_agent_message", "content"):
        val = ev.get(key)
        if isinstance(val, str) and val.strip():
            return val
    item = ev.get("item")
    if isinstance(item, dict):
        for key in ("text", "message", "content"):
            val = item.get(key)
            if isinstance(val, str) and val.strip():
                return val
    return None


def _extract_tokens(ev: dict[str, Any]) -> dict[str, int] | None:
    usage = ev.get("usage")
    src: dict[str, Any] = usage if isinstance(usage, dict) else ev
    if not any(k in src for k in ("input_tokens", "output_tokens")):
        return None
    return {
        "input": int(src.get("input_tokens", 0) or 0),
        "output": int(src.get("output_tokens", 0) or 0),
        "cache_read": int(src.get("cached_input_tokens", src.get("cache_read_tokens", 0)) or 0),
    }
