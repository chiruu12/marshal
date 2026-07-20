"""Goose backend (block/goose - Rust-based headless agent).

Invocation reference (goose ≥ 1.43 ``goose run``):

    GOOSE_MODE=auto|chat goose run --output-format stream-json --no-session \\
        [--provider PROVIDER] [--model MODEL] -t "<PROMPT>"

Goose is a headless CLI with structured output and mode-based permissions:
  * ``--output-format stream-json`` emits NDJSON events (``message`` / ``complete`` / …)
  * ``--output-format json`` emits one final JSON object (also accepted by ``parse_output``)
  * Prompt via ``-t`` / ``--text`` (not a bare ``--`` positional)
  * Headless auto-approve is ``GOOSE_MODE=auto`` (there is no ``--yes`` flag anymore)
  * Read-only / no-tools is ``GOOSE_MODE=chat``
  * ``--no-session`` keeps automated runs from writing session DB noise
  * Model field ``provider/model`` (e.g. ``cursor-agent/auto``) maps to ``--provider`` + ``--model``
    (both sides required; trailing/leading slash forms raise ``ValueError``). A bare model name is
    ``--model`` only.
  * Exits non-zero on hard failure; auth/provider errors may still exit 0 with an error message
    in the assistant text — ``parse_output`` treats those as FAILED when obvious. Plain-text
    failures on stdout (e.g. ``Unknown provider``) are also lifted into ``AgentResult.error``.

Notes:
  * Permission tiers map to ``GOOSE_MODE`` via ``prepare()`` (env), not argv flags.
  * For Cursor-backed Goose, authenticate with ``cursor-agent login`` (Goose shells out to it).
  * Token/cost fields are often null depending on provider; usage is native when present.
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
from .base import CodingAgentBackend, parse_jsonl

# Goose modes (env GOOSE_MODE). approve/smart_approve hang or fail closed without a TTY.
_GOOSE_MODE: dict[PermissionMode, str] = {
    PermissionMode.READ_ONLY: "chat",
    PermissionMode.SAFE_EDIT: "auto",
    PermissionMode.YOLO: "auto",
}


class GooseBackend(CodingAgentBackend):
    name = "goose"
    binary = "goose"
    capabilities = Capabilities(
        json_output=True,
        stream_json=True,
        sessions=False,  # Marshal runs are one-shot with --no-session
        server_mode=False,
        native_usage=True,  # when the provider reports tokens/cost in stream/json output
        permission_modes=frozenset(
            {PermissionMode.READ_ONLY, PermissionMode.SAFE_EDIT, PermissionMode.YOLO}
        ),
    )

    # Permission is env-driven (GOOSE_MODE); argv stays flag-free for these tiers.
    _PERMISSION: dict[PermissionMode, list[str]] = {
        PermissionMode.READ_ONLY: [],
        PermissionMode.SAFE_EDIT: [],
        PermissionMode.YOLO: [],
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

    def prepare(self, opts: RunOpts) -> None:
        """Stamp GOOSE_MODE for headless permission semantics (argv has no --yes/--plan)."""
        mode = _GOOSE_MODE.get(opts.permission, "auto")
        opts.extra_env = {**opts.extra_env, "GOOSE_MODE": mode}

    def build_invocation(self, task: TaskSpec, opts: RunOpts) -> list[str]:
        argv = [
            self.binary,
            "run",
            "--output-format",
            "stream-json",
            "--no-session",
        ]
        argv += self.map_permission(opts.permission)
        provider, model = _split_provider_model(opts.model)
        if provider:
            argv += ["--provider", provider]
        if model:
            argv += ["--model", model]
        argv += ["-t", self._compose_prompt(task)]
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
        if not events:
            bulk = _try_load_json_object(raw_stdout)
            if bulk is not None:
                events = [bulk]

        text_parts: list[str] = []
        usage = UsageRecord(backend=self.name, source=UsageSource.UNAVAILABLE)
        error_msg: str | None = None
        found_usage = False

        for ev in events:
            # Bulk ``--output-format json`` document
            if "messages" in ev and isinstance(ev.get("messages"), list):
                for msg in ev["messages"]:
                    if not isinstance(msg, dict):
                        continue
                    if msg.get("role") == "assistant":
                        text_parts.extend(_content_texts(msg.get("content")))
                meta_raw = ev.get("metadata")
                meta: dict[str, Any] = meta_raw if isinstance(meta_raw, dict) else {}
                found_usage = _apply_token_fields(usage, meta) or found_usage
                status = meta.get("status")
                if isinstance(status, str) and status.lower() in {"failed", "error"}:
                    error_msg = error_msg or f"goose status={status}"
                continue

            ev_type = ev.get("type")

            if ev_type == "error":
                error_msg = ev.get("message") or str(ev)
                continue

            if ev_type == "message":
                message_raw = ev.get("message")
                message: dict[str, Any] = message_raw if isinstance(message_raw, dict) else {}
                if message.get("role") == "assistant":
                    text_parts.extend(_content_texts(message.get("content")))
                continue

            if ev_type in ("complete", "completion", "done", "finish"):
                found_usage = _apply_token_fields(usage, ev) or found_usage
                tokens = ev.get("tokens")
                if isinstance(tokens, dict):
                    usage.input_tokens += int(tokens.get("input", 0) or 0)
                    usage.output_tokens += int(tokens.get("output", 0) or 0)
                    if usage.input_tokens or usage.output_tokens:
                        found_usage = True
                # cost / total_tokens already handled by _apply_token_fields when present on ev
                continue

        text = "".join(text_parts).strip()
        if error_msg is None:
            error_msg = _auth_or_fatal_error_from_text(text)
        if error_msg is None and exit_code != 0:
            # Provider/config failures often land as plain text on stdout (no JSON events).
            error_msg = _plain_failure_from_streams(raw_stdout, raw_stderr)

        if found_usage:
            usage.source = UsageSource.NATIVE

        ok = exit_code == 0 and error_msg is None
        return AgentResult(
            status=RunStatus.SUCCEEDED if ok else RunStatus.FAILED,
            text=text,
            usage=usage if found_usage else None,
            error=error_msg if not ok else None,
            exit_code=exit_code,
            raw_stdout=raw_stdout,
            raw_stderr=raw_stderr,
        )


def _split_provider_model(raw: str | None) -> tuple[str | None, str | None]:
    """Map Marshal ``model`` to Goose ``--provider`` / ``--model``.

    ``cursor-agent/auto`` -> (``cursor-agent``, ``auto``). A bare model with no slash is
    ``--model`` only (Goose's configured ``active_provider`` applies). Empty -> neither.

    When a ``/`` is present, both sides must be non-empty after strip. Trailing/leading slash
    forms (``cursor-agent/``, ``/auto``) raise ``ValueError`` so callers fail fast instead of
    emitting incomplete ``--provider`` / ``--model`` argv.
    """
    if not raw:
        return None, None
    text = raw.strip()
    if not text:
        return None, None
    if "/" not in text:
        return None, text
    provider_part, _, model_part = text.partition("/")
    provider = provider_part.strip()
    model = model_part.strip()
    if not provider or not model:
        empty = "provider" if not provider else "model"
        raise ValueError(
            f"goose model {raw!r} is malformed: use 'provider/model' "
            f"(e.g. 'cursor-agent/auto') or a bare model name with no '/'; "
            f"got empty {empty} around '/'"
        )
    return provider, model


def _try_load_json_object(raw: str) -> dict[str, Any] | None:
    blob = raw.strip()
    if not blob.startswith("{"):
        return None
    try:
        obj = json.loads(blob)
    except json.JSONDecodeError:
        return None
    return obj if isinstance(obj, dict) else None


def _content_texts(content: object) -> list[str]:
    out: list[str] = []
    if isinstance(content, str):
        out.append(content)
        return out
    if not isinstance(content, list):
        return out
    for part in content:
        if isinstance(part, str):
            out.append(part)
        elif isinstance(part, dict) and part.get("type") == "text":
            txt = part.get("text")
            if isinstance(txt, str):
                out.append(txt)
    return out


def _apply_token_fields(usage: UsageRecord, payload: dict[str, Any]) -> bool:
    """Pull token/cost fields from a complete event or metadata blob. Returns True if any set."""
    found = False
    inp = payload.get("input_tokens")
    out = payload.get("output_tokens")
    if isinstance(inp, int) and inp > 0:
        usage.input_tokens += inp
        found = True
    if isinstance(out, int) and out > 0:
        usage.output_tokens += out
        found = True
    # Some providers only report a single total; attribute to output so the ledger is non-zero.
    if not found:
        total = payload.get("total_tokens")
        if isinstance(total, int) and total > 0:
            usage.output_tokens += total
            found = True
    cost = payload.get("cost")
    if isinstance(cost, (int, float)):
        usage.cost_usd += float(cost)
        found = True
    return found


def _auth_or_fatal_error_from_text(text: str) -> str | None:
    """Goose sometimes exits 0 while embedding auth failures in the assistant message."""
    lower = text.lower()
    needles = (
        "authentication error",
        "not logged in",
        "please run 'cursor-agent login'",
        "please run \"cursor-agent login\"",
        "invalid api key",
        "unauthorized",
    )
    for needle in needles:
        if needle in lower:
            # Prefer a short first line when the model wrapped the error.
            first = text.strip().splitlines()[0].strip() if text.strip() else text
            return first[:500] or text[:500]
    return None


def _plain_failure_from_streams(stdout: str, stderr: str) -> str | None:
    """Surface non-JSON failure text Goose prints on stdout (or stderr) when exit ≠ 0.

    Live example (unknown provider): ``error: Error Unknown provider: …`` on stdout with an
    empty stderr — without this, ``parse_output`` left ``error=None`` and base.run only
    reported ``goose: exited with code 1``.
    """
    for blob in (stdout, stderr):
        picked = _prefer_error_line(blob)
        if picked:
            return picked
    return None


def _prefer_error_line(blob: str, limit: int = 500) -> str | None:
    """Prefer an ``error:`` / ``Error `` line; else a short non-empty tail. Skip JSON lines."""
    lines = [ln.strip() for ln in blob.splitlines() if ln.strip()]
    if not lines:
        return None
    for ln in lines:
        if ln.startswith("{"):
            continue
        lower = ln.lower()
        if lower.startswith("error:") or lower.startswith("error "):
            return ln[:limit]
        if "unknown provider" in lower or "goose configure" in lower:
            return ln[:limit]
    # No structured cue — take the last few non-JSON lines as a truncated tail.
    plain = [ln for ln in lines if not ln.startswith("{")]
    if not plain:
        return None
    return " ".join(plain[-3:])[:limit]
