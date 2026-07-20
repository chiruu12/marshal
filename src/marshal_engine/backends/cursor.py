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
  * Cursor CLI emits NO tokens or cost in its output - usage is reported as unavailable here.
    The account-level Cursor Admin API (team/enterprise, per service-account) is wired later.
  * `--force`/`--yolo` mean "allow everything not explicitly denied". For ``safe-edit``,
    ``prepare()`` writes a curated deny list into the worktree's ``.cursor/cli.json``
    (alongside ``--force``). ``yolo`` intentionally skips that list.
  * There is no `--cwd`; `--workspace` sets the repo root. `--trust` avoids the trust prompt.
  * `check_available` should pin/assert a minimum version - several headless hang bugs are
    version-gated and only fixed in recent builds.
"""

from __future__ import annotations

import json
import os
import re
import shutil
import subprocess
import tempfile
from pathlib import Path
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

#: Curated deny tokens for ``safe-edit`` (deny beats allow). Destructive shell, secrets, and
#: ``.git`` writes - the worktree remains the isolation boundary for everything else.
SAFE_EDIT_DENY: tuple[str, ...] = (
    "Shell(rm)",
    "Write(**/.env)",
    "Write(**/.env.*)",
    "Write(**/.git/**)",
    "Read(**/.env)",
    "Read(**/.env.*)",
)


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

    def prepare(self, opts: RunOpts) -> None:
        """Merge the safe-edit deny list into the worktree's ``.cursor/cli.json``.

        Only ``safe-edit`` gets the curated deny list; ``yolo`` is unrestricted by design and
        ``read-only`` already uses ``--mode plan``. Merge-preserving and idempotent so a
        repo-committed cli.json's allow/deny entries are kept.
        """
        if opts.permission is not PermissionMode.SAFE_EDIT:
            return
        _merge_safe_edit_cli_json(Path(opts.cwd) / ".cursor" / "cli.json")

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

    def account_info(self) -> dict[str, str] | None:
        """Plan tier + default model from `cursor-agent about`. Cursor exposes no quota/usage API
        for an individual account, but it does report the subscription tier and current model -
        honest account context (never a usage record). Returns None on any failure."""
        if shutil.which(self.binary) is None:
            return None
        try:
            proc = subprocess.run(
                [self.binary, "about", "--format", "json"],
                capture_output=True,
                text=True,
                timeout=15,
            )
        except (OSError, subprocess.SubprocessError):
            return None
        if proc.returncode != 0:
            return None
        return _parse_about(proc.stdout)

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


def _merge_safe_edit_cli_json(path: Path) -> None:
    """Union ``SAFE_EDIT_DENY`` into ``path``'s ``permissions.deny``, preserving other keys.

    Atomic write (unique temp + ``os.replace``) so a concurrent reader never sees a torn file.
    """
    data: dict[str, Any] = {}
    if path.exists():
        try:
            loaded = json.loads(path.read_text(encoding="utf-8"))
            if isinstance(loaded, dict):
                data = loaded
        except (json.JSONDecodeError, OSError):
            data = {}
    perms_raw = data.get("permissions")
    perms: dict[str, Any] = perms_raw if isinstance(perms_raw, dict) else {}
    deny_raw = perms.get("deny")
    existing = [d for d in deny_raw if isinstance(d, str)] if isinstance(deny_raw, list) else []
    for rule in SAFE_EDIT_DENY:
        if rule not in existing:
            existing.append(rule)
    perms["deny"] = existing
    data["permissions"] = perms
    path.parent.mkdir(parents=True, exist_ok=True)
    fd, tmp_str = tempfile.mkstemp(dir=str(path.parent), prefix=f"{path.name}.", suffix=".tmp")
    try:
        with os.fdopen(fd, "w", encoding="utf-8") as f:
            f.write(json.dumps(data, indent=2) + "\n")
        os.replace(tmp_str, path)
    except BaseException:
        try:
            os.unlink(tmp_str)
        except OSError:
            pass
        raise


def _parse_about(raw: str) -> dict[str, str] | None:
    """Extract ``{plan, model}`` from ``cursor-agent about`` output.

    JSON (``--format json``) is preferred; a text fallback parses the human table so a future
    default-format change can't silently drop the signal. Pure - unit-tested without a subprocess.
    """
    raw = raw.strip()
    if not raw:
        return None
    info: dict[str, str] = {}
    try:
        obj = json.loads(raw)
    except json.JSONDecodeError:
        obj = None
    if isinstance(obj, dict):
        tier = obj.get("subscriptionTier")
        model = obj.get("model")
        if isinstance(tier, str) and tier:
            info["plan"] = tier
        if isinstance(model, str) and model:
            info["model"] = model
        return info or None
    labels = {"subscription tier": "plan", "model": "model"}
    for line in raw.splitlines():
        # Split "Key: value" or "Key    value" into label + value; match the WHOLE label so
        # "Modeling foo" / "Subscription Tierx" don't false-positive on a prefix.
        parts = re.split(r"\s*:\s*|\s{2,}", line.strip(), maxsplit=1)
        if len(parts) != 2:
            continue
        key = " ".join(parts[0].lower().split())
        value = parts[1].strip()
        if value and key in labels:
            info[labels[key]] = value
    return info or None


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
