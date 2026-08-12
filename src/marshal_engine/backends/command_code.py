"""Command Code CLI adapter (`command-code -p`).

Invocation reference (command-code 1.7.x), headless:

    command-code -p "<PROMPT>" --skip-onboarding -t --max-turns N --output-format json
                 [--permission-mode plan|auto-accept | --yolo] [-m MODEL]

`-p/--print` runs non-interactively and exits. With `--output-format json` it emits an NDJSON event
stream whose LAST line is the result:

    {"type":"result","subtype":"success","sessionId":…,"stopReason":"end_turn",
     "usage":{"inputTokens":…,"outputTokens":…,"cacheReadTokens":…,"cacheWriteTokens":…},
     "durationMs":…,"finalText":…}

That gives tokens and a resumable `sessionId`. It does **not** give a dollar figure, so cost stays
`unavailable` and `native_usage` stays False (that flag means native *cost*): Command Code is a
hosted account whose spend lives in its own dashboard, and multiplying tokens by a rate Marshal
guessed would be a fabricated cost, not a measured one.

Older CLIs that do not know `--output-format` print plain text; `parse_output` falls back to
scraping stdout when no result line is present, so those keep working with usage `unavailable`.

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
from typing import Any

from ..core.types import (
    AgentResult,
    Capabilities,
    ModelCatalog,
    PermissionFidelity,
    PermissionMode,
    RunOpts,
    RunStatus,
    TaskSpec,
    UsageRecord,
    UsageSource,
)
from .base import CodingAgentBackend, parse_jsonl

#: Agent-loop turn cap. The runner's wall-clock timeout is the real bound; this guards a runaway.
_MAX_TURNS = 50

#: Exit code Command Code returns when `--max-turns` is hit (per `command-code --help`).
_CAP_HIT_EXIT = 8

_ANSI = re.compile(r"\x1b\[[0-9;?]*[ -/]*[@-~]")

#: ``--list-models`` rows look like ``<id>  <description>`` (2+ spaces).
_LIST_MODELS_ROW = re.compile(r"^([A-Za-z0-9][A-Za-z0-9_./-]*)\s{2,}\S")

#: Static fallback when ``command-code --list-models`` cannot be probed.
#: Sourced from docs/model-playbook.md (command-code / zai-org/glm-5.2).
_STATIC_MODELS: tuple[str, ...] = ("zai-org/glm-5.2",)


class CommandCodeBackend(CodingAgentBackend):
    name = "command-code"
    binary = "command-code"
    credential_env_vars = ("COMMAND_CODE_API_KEY",)
    capabilities = Capabilities(
        json_output=True,
        native_usage=False,  # tokens yes, USD no -> cost stays unavailable (see module docstring)
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
                check=False,
            )
        except (OSError, subprocess.SubprocessError):
            return None
        return _parse_status_json(proc.stdout or "")

    def verifies_auth(self) -> bool:
        # ``command-code status`` is authenticated-only; config.json presence ≠ logged in.
        return True

    def available_models(self) -> ModelCatalog:
        """Model ids from ``command-code --list-models``, falling back to the curated list.

        Rows are ``<id>  <description>``; section headers (no padded description) are skipped.
        """
        return self._probe_models(
            [self.binary, "--list-models"],
            lambda stdout: _parse_list_models(_ANSI.sub("", stdout)),
            _STATIC_MODELS,
        )

    def build_invocation(self, task: TaskSpec, opts: RunOpts) -> list[str]:
        argv = [
            self.binary,
            "-p",
            self._compose_prompt(task),
            "--skip-onboarding",
            "-t",
            "--max-turns",
            str(_MAX_TURNS),
            "--output-format",
            "json",
        ]
        argv += self.map_permission(opts.permission)
        if opts.model:
            argv += ["-m", opts.model]
        return argv

    def parse_output(self, raw_stdout: str, raw_stderr: str, exit_code: int) -> AgentResult:
        clean = _ANSI.sub("", raw_stdout)
        result = _final_result(clean)
        # No result line = an older CLI that ignored `--output-format`, or a run that died before
        # emitting one. Scrape stdout as before rather than losing the agent's answer entirely.
        text = (result.get("finalText") or "").strip() if result else clean.strip()
        usage = _usage_from(self.name, result, _last_model(clean))
        session_id = result.get("sessionId") if result else None

        if exit_code == _CAP_HIT_EXIT:
            return AgentResult(
                status=RunStatus.FAILED,
                text=text,
                usage=usage,
                session_id=session_id if isinstance(session_id, str) else None,
                error=f"command-code hit the --max-turns cap ({_MAX_TURNS}); task may be incomplete",
                exit_code=exit_code,
                raw_stdout=raw_stdout,
                raw_stderr=raw_stderr,
            )

        ok = exit_code == 0
        return AgentResult(
            status=RunStatus.EXITED_CLEAN if ok else RunStatus.FAILED,
            text=text,
            usage=usage,
            session_id=session_id if isinstance(session_id, str) else None,
            error=None if ok else (raw_stderr.strip() or f"command-code exited {exit_code}"),
            exit_code=exit_code,
            raw_stdout=raw_stdout,
            raw_stderr=raw_stderr,
        )


def _final_result(clean_stdout: str) -> dict[str, Any] | None:
    """The stream's terminal ``{"type":"result"}`` object, or None when there is not one.

    Scans from the end: the result line is last, and taking the LAST match means a stray `result`
    echoed inside a tool output cannot be mistaken for the run's own.
    """
    for event in reversed(parse_jsonl(clean_stdout)):
        if event.get("type") == "result":
            return event
    return None


def _last_model(clean_stdout: str) -> str | None:
    """The model the final turn actually ran on.

    Taken from the event stream because the result line does not carry it, and the *last* one
    because a run may switch models mid-stream (fallback on a provider error).
    """
    for event in reversed(parse_jsonl(clean_stdout)):
        inner = event.get("event")
        if not isinstance(inner, dict):
            continue
        model = inner.get("model")
        if isinstance(model, str) and model:
            return model
    return None


def _usage_from(
    backend: str, result: dict[str, Any] | None, model: str | None
) -> UsageRecord:
    """Tokens from the result line. Cost is never derived - see the module docstring.

    ``source`` stays ``UNAVAILABLE`` even when tokens are present: the field describes the
    provenance of the **cost**, and Command Code reports none. Tagging this ``NATIVE`` would put a
    $0.00 into every cost rollup and make the backend that bills the most look free.
    """
    usage = UsageRecord(backend=backend, model=model, source=UsageSource.UNAVAILABLE)
    tokens = (result or {}).get("usage")
    if not isinstance(tokens, dict):
        return usage
    return usage.model_copy(
        update={
            "input_tokens": _int(tokens.get("inputTokens")),
            "output_tokens": _int(tokens.get("outputTokens")),
            "cache_read_tokens": _int(tokens.get("cacheReadTokens")),
            "cache_write_tokens": _int(tokens.get("cacheWriteTokens")),
        }
    )


def _int(value: Any) -> int:
    """Non-negative int, or 0. Upstream JSON is version-variable - never raise on a token count."""
    if isinstance(value, bool) or not isinstance(value, (int, float)):
        return 0
    return max(0, int(value))


def _parse_list_models(raw: str) -> list[str]:
    """Extract model ids from ``command-code --list-models`` text. Pure."""
    models: list[str] = []
    for line in (raw or "").splitlines():
        match = _LIST_MODELS_ROW.match(line.strip())
        if match is None:
            continue
        models.append(match.group(1))
    return models


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
