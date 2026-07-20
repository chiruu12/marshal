"""OpenCode adapter (`opencode run`).

Invocation reference (opencode CLI):

    opencode run --format json
                 [--agent plan | --dangerously-skip-permissions]
                 [-m provider/model] --dir CWD [-s SESSION] <PROMPT>

`--format json` emits an NDJSON event stream. Event shapes (from research):
  * text part      -> {"part":{"type":"text","text":"..."}}            (final message = concat)
  * step finish    -> {"part":{"type":"step-finish","reason":"stop",
                                "cost":<usd>,"tokens":{"input":..,"output":..,
                                                       "reasoning":..,"cache":{"read":..,"write":..}}}}
  * error          -> {"error":{"name":"...","data":{"message":"..."}}}

Notes / gaps baked in from research (verify against a live run):
  * The JSON stream can drop the final `step-finish` event, so cost/tokens may be incomplete;
    Phase 2 reconciles from `~/.local/share/opencode/storage` / `opencode export`.
  * `opencode serve` (HTTP, 127.0.0.1:4096) is a faster warm-server path - added later
    (capabilities.server_mode = True).
  * serve+attach hangs if any permission is `ask`; for `run` headless we keep stdin closed
    (shared runner) so a stray prompt fails fast instead of deadlocking. ``prepare()`` stamps
    an engine-managed permission snippet via ``OPENCODE_CONFIG_CONTENT`` (``question: deny``
    always for write tiers; curated bash/edit/read denies for ``safe-edit``). Skip-permissions
    does NOT cover the ``question`` tool - the managed config closes that gap.
  * Canonical repo moved sst/opencode -> anomalyco/opencode; npm package still `opencode-ai`.
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

#: Env var OpenCode reads as an inline JSON config override (high precedence; no worktree dirty).
_OPENCODE_CONFIG_CONTENT = "OPENCODE_CONFIG_CONTENT"


def permission_config_for(mode: PermissionMode) -> dict[str, Any] | None:
    """Pure builder: permission snippet stamped for a write-tier run, or None for read-only.

    ``safe-edit`` gets curated denies (``question``, ``external_directory``, ``rm``, ``.env``,
    ``.git``). ``yolo`` still denies ``question`` so headless runs cannot deadlock on a prompt,
    but otherwise stays unrestricted. Never emits ``ask`` (stdin is closed).
    """
    if mode is PermissionMode.READ_ONLY:
        return None
    if mode is PermissionMode.YOLO:
        return {
            "$schema": "https://opencode.ai/config.json",
            "permission": {
                "*": "allow",
                "question": "deny",
            },
        }
    # SAFE_EDIT
    return {
        "$schema": "https://opencode.ai/config.json",
        "permission": {
            "*": "allow",
            "question": "deny",
            "external_directory": "deny",
            "bash": {
                "*": "allow",
                "rm": "deny",
                "rm *": "deny",
            },
            "edit": {
                "*": "allow",
                "*.env": "deny",
                "*.env.*": "deny",
                ".git/*": "deny",
                ".git/**": "deny",
            },
            "read": {
                "*": "allow",
                "*.env": "deny",
                "*.env.*": "deny",
                "*.env.example": "allow",
            },
        },
    }


class OpenCodeBackend(CodingAgentBackend):
    name = "opencode"
    binary = "opencode"
    capabilities = Capabilities(
        json_output=True,
        stream_json=True,
        sessions=True,
        server_mode=True,  # `opencode serve` - warm-server fast path wired in a later phase
        native_usage=True,
        permission_modes=frozenset(
            {PermissionMode.READ_ONLY, PermissionMode.SAFE_EDIT, PermissionMode.YOLO}
        ),
    )

    _PERMISSION: dict[PermissionMode, list[str]] = {
        PermissionMode.READ_ONLY: ["--agent", "plan"],
        PermissionMode.SAFE_EDIT: ["--dangerously-skip-permissions"],
        PermissionMode.YOLO: ["--dangerously-skip-permissions"],
    }

    # --- hooks ---------------------------------------------------------------------------

    def prepare(self, opts: RunOpts) -> None:
        """Stamp ``OPENCODE_CONFIG_CONTENT`` with the permission snippet for this tier.

        Argv still uses ``--dangerously-skip-permissions`` for write tiers; the managed config
        is what makes ``safe-edit`` distinct (curated denies + ``question: deny``).
        """
        cfg = permission_config_for(opts.permission)
        if cfg is None:
            return
        opts.extra_env = {
            **opts.extra_env,
            _OPENCODE_CONFIG_CONTENT: json.dumps(cfg, separators=(",", ":")),
        }

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
            raise ValueError(f"opencode: unsupported permission mode {mode!r}") from None

    def build_invocation(self, task: TaskSpec, opts: RunOpts) -> list[str]:
        argv = [self.binary, "run", "--format", "json"]
        argv += self.map_permission(opts.permission)
        if opts.model:
            argv += ["-m", opts.model]
        argv += ["--dir", str(opts.cwd)]
        if opts.session_id:
            argv += ["-s", opts.session_id]
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

        text_parts: list[str] = []
        session_id: str | None = None
        error_msg: str | None = None
        usage = UsageRecord(backend=self.name, source=UsageSource.UNAVAILABLE)
        found_usage = False
        found_cost = False

        for ev in events:
            err = ev.get("error")
            if isinstance(err, dict):
                data = err.get("data")
                if isinstance(data, dict) and data.get("message"):
                    error_msg = str(data["message"])
                else:
                    error_msg = error_msg or (str(err["name"]) if err.get("name") else None)

            sid = ev.get("sessionID") or ev.get("session_id")
            if isinstance(sid, str):
                session_id = sid

            part = ev.get("part")
            if not isinstance(part, dict):
                continue
            ptype = part.get("type")
            if ptype == "text":
                txt = part.get("text")
                if isinstance(txt, str):
                    text_parts.append(txt)
            elif ptype in ("step-finish", "step_finish"):
                tokens_raw = part.get("tokens")
                tokens: dict[str, Any] = tokens_raw if isinstance(tokens_raw, dict) else {}
                cache_raw = tokens.get("cache")
                cache: dict[str, Any] = cache_raw if isinstance(cache_raw, dict) else {}
                usage.input_tokens += int(tokens.get("input", 0) or 0)
                usage.output_tokens += int(tokens.get("output", 0) or 0)
                usage.cache_read_tokens += int(cache.get("read", 0) or 0)
                cost = part.get("cost")
                if cost is not None:
                    usage.cost_usd += float(cost or 0)
                    found_cost = True
                found_usage = True

        # NATIVE only when the backend reported a POSITIVE cost. A reported $0 alongside consumed
        # tokens means the model is unpriced (e.g. a custom OpenAI-compatible provider opencode has no
        # price table for, like EastRouter) - NOT a free run - so it stays UNAVAILABLE rather than
        # claiming a fake $0. Tokens without any cost field also stay UNAVAILABLE (priced from the table).
        if found_cost and usage.cost_usd > 0:
            usage.source = UsageSource.NATIVE

        ok = exit_code == 0 and error_msg is None
        return AgentResult(
            status=RunStatus.SUCCEEDED if ok else RunStatus.FAILED,
            text="".join(text_parts).strip(),
            session_id=session_id,
            usage=usage if found_usage else None,
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
