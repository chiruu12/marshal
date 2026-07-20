"""Goose backend (block/goose - Rust-based headless agent).

Invocation reference (goose run):

    goose run [--json] [--yes] [--model MODEL] -- "<PROMPT>"

Goose is a pure headless CLI with explicit permission gates and structured output:
  * `--json` outputs newline-delimited JSON events with type, text, tokens, cost
  * `--yes` enables auto-approval of actions (headless mode; no prompts)
  * Exits non-zero on failure
  * Output includes token counts and cost (native usage tracking available)

Notes:
  * Goose is Rust-based and offers strong permission semantics (unlike some competitors)
  * Each event is a JSON object on its own line (NDJSON format)
  * Cost/tokens available in final event; can be streamed as-you-go or at end
  * ~50k GitHub stars, AAIF-backed, production-grade
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


class GooseBackend(CodingAgentBackend):
    name = "goose"
    binary = "goose"
    capabilities = Capabilities(
        json_output=True,
        stream_json=True,
        sessions=False,  # Goose doesn't expose session resumption yet
        server_mode=False,  # No warm-server mode documented
        native_usage=True,  # Reports tokens + cost in JSON output
        permission_modes=frozenset(
            {PermissionMode.READ_ONLY, PermissionMode.SAFE_EDIT, PermissionMode.YOLO}
        ),
    )

    _PERMISSION: dict[PermissionMode, list[str]] = {
        PermissionMode.READ_ONLY: ["--plan"],  # Read-only / planning mode
        PermissionMode.SAFE_EDIT: [],  # Default interactive (user can approve individually)
        PermissionMode.YOLO: ["--yes"],  # Auto-approve all actions
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
            raise ValueError(f"goose: unsupported permission mode {mode!r}") from None

    def build_invocation(self, task: TaskSpec, opts: RunOpts) -> list[str]:
        argv = [self.binary, "run", "--json"]
        argv += self.map_permission(opts.permission)
        if opts.model:
            argv += ["--model", opts.model]
        # Goose uses `--` to separate flags from the prompt
        argv.append("--")
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
        events = _parse_ndjson(raw_stdout)

        text_parts: list[str] = []
        usage = UsageRecord(backend=self.name, source=UsageSource.UNAVAILABLE)
        error_msg: str | None = None
        found_usage = False

        for ev in events:
            # Error event
            if ev.get("type") == "error":
                error_msg = ev.get("message") or str(ev)

            # Text output event
            if ev.get("type") in ("text", "output"):
                txt = ev.get("text") or ev.get("output")
                if isinstance(txt, str):
                    text_parts.append(txt)

            # Completion event with tokens/cost
            if ev.get("type") in ("completion", "done", "finish"):
                # Extract usage info if present
                tokens = ev.get("tokens", {})
                if isinstance(tokens, dict):
                    usage.input_tokens += int(tokens.get("input", 0) or 0)
                    usage.output_tokens += int(tokens.get("output", 0) or 0)
                    found_usage = True

                cost = ev.get("cost")
                if cost is not None and isinstance(cost, (int, float)):
                    usage.cost_usd += float(cost)
                    found_usage = True

        # Set usage source based on what was reported
        if found_usage:
            usage.source = UsageSource.NATIVE

        ok = exit_code == 0 and error_msg is None
        return AgentResult(
            status=RunStatus.SUCCEEDED if ok else RunStatus.FAILED,
            text="".join(text_parts).strip(),
            usage=usage if found_usage else None,
            error=error_msg if not ok else None,
            exit_code=exit_code,
            raw_stdout=raw_stdout,
            raw_stderr=raw_stderr,
        )


def _parse_ndjson(raw: str) -> list[dict[str, Any]]:
    """Parse newline-delimited JSON from Goose output."""
    events: list[dict[str, Any]] = []
    for line in raw.splitlines():
        line = line.strip()
        if not line:
            continue
        if not line.startswith("{"):
            continue
        try:
            obj = json.loads(line)
        except json.JSONDecodeError:
            continue
        if isinstance(obj, dict):
            events.append(obj)
    return events
