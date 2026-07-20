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
from .base import CodingAgentBackend, parse_jsonl


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

    # Headless runs close stdin, so interactive approve-per-tool would deadlock. safe-edit and
    # yolo both use `--yes`; the git worktree is the enforced boundary (same stance as
    # command-code / cursor / opencode).
    _PERMISSION: dict[PermissionMode, list[str]] = {
        PermissionMode.READ_ONLY: ["--plan"],
        PermissionMode.SAFE_EDIT: ["--yes"],
        PermissionMode.YOLO: ["--yes"],
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
        events = parse_jsonl(raw_stdout)

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
