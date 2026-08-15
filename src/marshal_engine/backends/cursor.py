"""Cursor CLI adapter (`cursor-agent`).

Invocation reference (cursor-agent, headless):

    cursor-agent -p --output-format json --trust
                 [--mode plan | --force | --yolo]
                 [--model MODEL] --workspace CWD [--resume SESSION] <PROMPT>

`-p/--print` = non-interactive. Marshal uses ``--output-format stream-json`` (NDJSON events)
and concatenates assistant ``message.content[].text`` deltas — the single-object ``json`` mode
truncates ``result`` on long runs. A terminal ``type":"result"`` event still supplies
``session_id`` / ``is_error``; when its ``result`` is shorter than the stream concat, the
stream wins.

On failure the process exits non-zero and writes to stderr; a timeout-killed run may lack a
``result`` event but partial stream text is still returned.

Notes / gaps baked in from research:
  * Cursor CLI emits per-run token counts on the terminal ``result`` event
    (``usage.{inputTokens,outputTokens,cacheReadTokens,cacheWriteTokens}``) but NO USD/cost.
    Tokens are stamped with ``source=unavailable``; ``native_usage`` stays False (that flag
    means native cost). The Cursor Admin API (Enterprise) is the deferred cost path.
  * `--force`/`--yolo` mean "allow everything not explicitly denied". For ``safe-edit``,
    ``prepare()`` writes a curated deny list into the worktree's ``.cursor/cli.json``
    (alongside ``--force``). The write is TEMPORARY: ``run()`` snapshots the file's exact
    prior state (existence, bytes, mode) and restores it before returning, so the deny
    overlay is visible to the live agent process but never to Fleet's status/diff/commit
    views. ``yolo`` intentionally skips that list.
  * There is no `--cwd`; `--workspace` sets the repo root. `--trust` avoids the trust prompt.
  * `check_available` should pin/assert a minimum version - several headless hang bugs are
    version-gated and only fixed in recent builds.
"""

from __future__ import annotations

import json
import os
import re
import shutil
import stat
import subprocess
import tempfile
from pathlib import Path
from typing import Any, ClassVar, NamedTuple

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
from ..runtime.env import DETACHED_STDIO
from .base import CodingAgentBackend, parse_jsonl

#: Curated deny tokens for ``safe-edit`` (deny beats allow). Destructive shell, secrets,
#: ``.git`` writes, and Write to the policy file itself via Cursor's permission grammar.
#: Reads of ``.cursor/cli.json`` stay allowed (reading does not disable the policy). These
#: rules are curated, not a sandbox: they do not stop arbitrary same-user shell/Python from
#: rewriting the file mid-run. The #37 snapshot/restore transaction still prevents persistence
#: after the run; the worktree remains the isolation boundary for everything else.
SAFE_EDIT_DENY: tuple[str, ...] = (
    "Shell(rm)",
    "Write(**/.env)",
    "Write(**/.env.*)",
    "Write(**/.git/**)",
    "Write(.cursor/cli.json)",
    "Write(**/.cursor/cli.json)",
    "Read(**/.env)",
    "Read(**/.env.*)",
)

#: Static fallback when ``cursor-agent models`` cannot be probed.
#: Sourced from docs/model-playbook.md (cursor / composer-2.5).
_STATIC_MODELS: tuple[str, ...] = ("composer-2.5",)


class CursorBackend(CodingAgentBackend):
    name = "cursor"
    binary = "cursor-agent"
    credential_env_vars = ("CURSOR_API_KEY",)
    resolves_at_mentions = True  # cursor-agent expands @path mentions in the prompt
    capabilities = Capabilities(
        json_output=True,
        native_usage=False,  # tokens yes; no USD in CLI output — Admin API cost path later
        permission_modes=frozenset(
            {PermissionMode.READ_ONLY, PermissionMode.SAFE_EDIT, PermissionMode.YOLO}
        ),
        permission_fidelity=PermissionFidelity.ENFORCED_DENIES,
    )

    _PERMISSION: ClassVar[dict[PermissionMode, list[str]]] = {
        PermissionMode.READ_ONLY: ["--mode", "plan"],
        PermissionMode.SAFE_EDIT: ["--force"],
        PermissionMode.YOLO: ["--yolo"],
    }

    # --- hooks ---------------------------------------------------------------------------

    def prepare(self, opts: RunOpts) -> None:
        """Merge the safe-edit deny list into the worktree's ``.cursor/cli.json``.

        Only ``safe-edit`` gets the curated deny list; ``yolo`` is unrestricted by design and
        ``read-only`` already uses ``--mode plan``. Merge-preserving and idempotent so a
        repo-committed cli.json's allow/deny entries are kept. The write is transient:
        ``run()`` restores the file's exact prior state before returning, so the overlay
        never appears in Fleet's status/diff/commit views. Fails closed (raises) on an
        existing malformed, unreadable, or non-object config rather than replacing it.
        """
        if opts.permission is not PermissionMode.SAFE_EDIT:
            return
        _merge_safe_edit_cli_json(Path(opts.cwd) / ".cursor" / "cli.json")

    def run(self, task: TaskSpec, opts: RunOpts) -> AgentResult:
        """Shared run loop, wrapped in a ``.cursor/cli.json`` transaction for ``safe-edit``.

        Snapshot the config's exact prior state (existence, bytes, mode), let the base loop
        call ``prepare()`` (which installs the deny overlay) and spawn the agent, then restore
        the snapshot before returning - so the overlay applies to the live process but Fleet
        never observes it as agent work (EMPTY classification, verify gate, collect, commit,
        integrate all see the original tree). Restoration is exact: an agent Write-tool edit to
        the config during the run is denied by ``SAFE_EDIT_DENY`` and any residual mid-run rewrite
        (e.g. via shell) is discarded with the overlay on restore. A restoration failure fails
        the run - never return success with Marshal's policy residue still in the worktree.
        """
        if opts.permission is not PermissionMode.SAFE_EDIT:
            return super().run(task, opts)
        path = Path(opts.cwd) / ".cursor" / "cli.json"
        try:
            snapshot = _snapshot_cli_json(path)
        except OSError as exc:
            # Without a restorable snapshot the transaction cannot hold: fail closed with the
            # file untouched and the agent process never launched.
            return AgentResult(
                status=RunStatus.FAILED,
                error=(
                    f"{self.name}: cannot snapshot existing {path} ({exc}); "
                    "fix its permissions or remove it before a safe-edit run"
                ),
            )
        restore_error: str | None = None
        try:
            result = super().run(task, opts)
        finally:
            try:
                _restore_cli_json(path, snapshot)
            except OSError as exc:
                restore_error = (
                    f"{self.name}: failed to restore {path} after the run ({exc}); "
                    "the worktree may still contain Marshal's temporary safe-edit deny "
                    "overlay - restore or remove .cursor/cli.json manually before "
                    "committing or integrating this run"
                )
        if restore_error is not None:
            result.status = RunStatus.FAILED
            result.error = f"{result.error}; {restore_error}" if result.error else restore_error
        return result

    def available_models(self) -> ModelCatalog:
        """Model ids from ``cursor-agent models``, falling back to the curated playbook list.

        Output is a header line then ``<id> - <label>`` rows (verified against the real CLI), so
        only the id before the first " - " is kept: that is what a client's ``model:`` takes. A row
        without the separator is skipped rather than guessed at.
        """
        def parse(stdout: str) -> list[str]:
            ids = []
            for line in stdout.splitlines():
                stripped = line.strip()
                if not stripped or " - " not in stripped:
                    continue  # header/blank, or a shape we do not recognise - never invent an id
                ids.append(stripped.split(" - ", 1)[0].strip())
            return ids

        return self._probe_models([self.binary, "models"], parse, _STATIC_MODELS)

    def account_info(self) -> dict[str, str] | None:
        """Auth gate via ``cursor-agent status``; plan/model via ``about`` only after auth.

        ``status --format json`` reports ``isAuthenticated`` (exit 0 even when logged out — do
        not trust the exit code alone). Logged-out ``about`` still returns ``model: "Auto"`` with
        null tier/email, so ``about`` alone must never green-light doctor. On authenticated
        status, ``about`` enriches plan/model when available; otherwise a minimal
        ``{"plan": "logged-in"}`` keeps doctor OK honest. Never raises.
        """
        if shutil.which(self.binary) is None:
            return None
        try:
            status_proc = subprocess.run(
                [self.binary, "status", "--format", "json"],
                capture_output=True,
                text=True,
                timeout=15,
                check=False,
                **DETACHED_STDIO,
            )
        except (OSError, subprocess.SubprocessError):
            return None
        if not _status_authenticated(status_proc.stdout or ""):
            return None
        # Authenticated: enrich plan/model from about when possible.
        try:
            about_proc = subprocess.run(
                [self.binary, "about", "--format", "json"],
                capture_output=True,
                text=True,
                timeout=15,
                check=False,
                **DETACHED_STDIO,
            )
        except (OSError, subprocess.SubprocessError):
            return {"plan": "logged-in"}
        if about_proc.returncode == 0:
            info = _parse_about(about_proc.stdout)
            if info is not None:
                return info
        return {"plan": "logged-in"}

    def verifies_auth(self) -> bool:
        # Auth gate is ``cursor-agent status`` / ``isAuthenticated`` (not ``about``). A None from
        # account_info() with the binary present means not authenticated — doctor must FAIL.
        return True

    def build_invocation(self, task: TaskSpec, opts: RunOpts) -> list[str]:
        argv = [self.binary, "-p", "--output-format", "stream-json", "--trust"]
        argv += self.map_permission(opts.permission)
        if opts.model:
            argv += ["--model", opts.model]
        argv += ["--workspace", str(opts.cwd)]
        if opts.session_id:
            argv += ["--resume", opts.session_id]
        argv.append(self._compose_prompt(task))
        return argv

    def parse_output(self, raw_stdout: str, raw_stderr: str, exit_code: int) -> AgentResult:
        return self._parse_event_stream(raw_stdout, raw_stderr, exit_code)

    def _parse_event_stream(
        self, raw_stdout: str, raw_stderr: str, exit_code: int
    ) -> AgentResult:
        events = parse_jsonl(raw_stdout)

        text_parts: list[str] = []
        session_id: str | None = None
        result_obj: dict[str, Any] | None = None

        for ev in events:
            sid = ev.get("session_id")
            if isinstance(sid, str):
                session_id = sid

            etype = ev.get("type")
            if etype == "assistant":
                text_parts.extend(_assistant_content_texts(ev))
            elif etype == "result":
                result_obj = ev
                sid = ev.get("session_id")
                if isinstance(sid, str):
                    session_id = sid

        if result_obj is None and not events:
            result_obj = _find_result(raw_stdout)

        stream_text = "".join(text_parts)
        result_text = ""
        is_error = False
        usage = UsageRecord(backend=self.name, source=UsageSource.UNAVAILABLE)

        if result_obj is not None:
            is_error = bool(result_obj.get("is_error", False))
            result_text = str(result_obj.get("result", "") or "")
            sid = result_obj.get("session_id")
            if isinstance(sid, str):
                session_id = sid
            _apply_cursor_usage(usage, result_obj.get("usage"))

        text = stream_text if len(stream_text) >= len(result_text) else result_text

        if exit_code != 0 or result_obj is None:
            # Carry the session id and whatever usage the stream reported. A failed run still
            # consumed tokens, and dropping them here silently under-reports spend in the ledger -
            # the one thing this project refuses to get wrong. `session_id` is likewise needed to
            # resume or investigate the failure.
            return AgentResult(
                status=RunStatus.FAILED,
                text=text,
                session_id=session_id,
                usage=usage,
                error=raw_stderr.strip() or f"cursor-agent exited {exit_code}",
                exit_code=exit_code,
                raw_stdout=raw_stdout,
                raw_stderr=raw_stderr,
            )

        return AgentResult(
            status=RunStatus.FAILED if is_error else RunStatus.EXITED_CLEAN,
            text=text,
            session_id=session_id,
            usage=usage,
            error=text if is_error else None,
            exit_code=exit_code,
            raw_stdout=raw_stdout,
            raw_stderr=raw_stderr,
        )


# --- module helpers ----------------------------------------------------------------------


class _CliJsonSnapshot(NamedTuple):
    """Exact prior state of ``.cursor/cli.json``: ``file_bytes is None`` = did not exist."""

    file_bytes: bytes | None
    mode: int | None
    dir_existed: bool


def _require_safe_cli_json_paths(path: Path) -> None:
    """Refuse to read/write through a symlinked ``.cursor/`` or symlinked/non-regular ``cli.json``.

    ``Path`` helpers follow directory symlinks, so ``mkstemp(dir=parent)`` / ``unlink`` /
    ``os.replace`` on ``path`` would operate on the link target - escaping the worktree and
    potentially clobbering another Cursor config. Call this at snapshot, merge, AND restore
    time: an agent can replace ``.cursor/`` with a symlink after prepare and before finally.
    """
    parent = path.parent
    if parent.is_symlink():
        raise OSError(
            f"{parent} is a symlink; refusing to read/write a safe-edit overlay through it"
        )
    if not os.path.lexists(path):
        return
    st = path.lstat()
    if stat.S_ISLNK(st.st_mode):
        raise OSError(
            f"{path} is a symlink; refusing to snapshot/replace it for a safe-edit run"
        )
    if not stat.S_ISREG(st.st_mode):
        raise OSError(
            f"{path} is not a regular file; refusing to snapshot/replace it for a "
            "safe-edit run"
        )


def _snapshot_cli_json(path: Path) -> _CliJsonSnapshot:
    """Capture ``path``'s exact state (existence, bytes, permission bits) for later restore.

    Fail closed on symlinks and non-regular files: ``path.exists()`` follows links, so a
    naive read/replace would turn a symlink into a regular file (or destroy a broken
    link) and still report success - leaving a false worktree delta. Same for a
    symlinked ``.cursor/`` parent, which would write the overlay outside the worktree.
    """
    _require_safe_cli_json_paths(path)
    parent = path.parent
    dir_existed = parent.is_dir()
    try:
        st = path.lstat()
    except FileNotFoundError:
        return _CliJsonSnapshot(None, None, dir_existed)
    return _CliJsonSnapshot(path.read_bytes(), stat.S_IMODE(st.st_mode), dir_existed)


def _restore_cli_json(path: Path, snapshot: _CliJsonSnapshot) -> None:
    """Put ``path`` back into its snapshotted state, byte-for-byte.

    Absent before -> remove the generated file, and remove ``.cursor/`` only when this run
    created it AND it is empty (agent-created content in there is real work - keep it; git
    never reports an empty directory, so a leftover empty dir is not a worktree delta either).
    Present before -> rewrite the original bytes + mode atomically (temp + ``os.replace``).

    Re-validates paths before any write/unlink so a mid-run swap of ``.cursor/`` for a
    symlink cannot redirect restore outside the worktree (fail the run instead).
    """
    _require_safe_cli_json_paths(path)
    if snapshot.file_bytes is None:
        try:
            path.unlink()
        except FileNotFoundError:
            pass
        if not snapshot.dir_existed:
            try:
                os.rmdir(path.parent)
            except OSError:
                pass  # non-empty (agent work) or already gone - both fine
        return
    path.parent.mkdir(parents=True, exist_ok=True)
    # Parent may have been recreated; refuse again before mkstemp/replace follow it.
    _require_safe_cli_json_paths(path)
    fd, tmp_str = tempfile.mkstemp(dir=str(path.parent), prefix=f"{path.name}.", suffix=".tmp")
    try:
        with os.fdopen(fd, "wb") as f:
            f.write(snapshot.file_bytes)
        if snapshot.mode is not None:
            os.chmod(tmp_str, snapshot.mode)
        os.replace(tmp_str, path)
    except BaseException:
        try:
            os.unlink(tmp_str)
        except OSError:
            pass
        raise


def _merge_safe_edit_cli_json(path: Path) -> None:
    """Union ``SAFE_EDIT_DENY`` into ``path``'s ``permissions.deny``, preserving other keys.

    Atomic write (unique temp + ``os.replace``) so a concurrent reader never sees a torn file.
    Fails closed on an existing malformed, unreadable, non-object, symlink, or non-regular
    document (and on a symlinked ``.cursor/`` parent): raising here (surfaced by the base run
    loop as a failed run) beats silently replacing a user's config with a Marshal-generated
    one - the original path is left untouched.
    """
    data: dict[str, Any] = {}
    try:
        _require_safe_cli_json_paths(path)
    except OSError as exc:
        raise RuntimeError(str(exc)) from exc
    if os.path.lexists(path):
        try:
            raw = path.read_text(encoding="utf-8")
        except OSError as exc:
            raise RuntimeError(
                f"existing {path} is unreadable ({exc}); fix its permissions or remove it "
                "before a safe-edit run"
            ) from exc
        try:
            loaded = json.loads(raw)
        except json.JSONDecodeError as exc:
            raise RuntimeError(
                f"existing {path} is not valid JSON ({exc}); fix or remove it before a "
                "safe-edit run - refusing to overwrite it"
            ) from exc
        if not isinstance(loaded, dict):
            raise RuntimeError(
                f"existing {path} is valid JSON but not an object; fix or remove it before "
                "a safe-edit run - refusing to overwrite it"
            )
        data = loaded
    perms_raw = data.get("permissions")
    perms: dict[str, Any] = perms_raw if isinstance(perms_raw, dict) else {}
    deny_raw = perms.get("deny")
    existing = [d for d in deny_raw if isinstance(d, str)] if isinstance(deny_raw, list) else []
    for rule in SAFE_EDIT_DENY:
        if rule not in existing:
            existing.append(rule)
    perms["deny"] = existing
    # Recent Cursor CLIs schema-validate the file and REQUIRE permissions.allow as an array —
    # a deny-only document fails validation and the agent never launches. An empty list adds no
    # grants (--force still means "allow everything not explicitly denied"); an existing list
    # is preserved untouched.
    allow_raw = perms.get("allow")
    perms["allow"] = allow_raw if isinstance(allow_raw, list) else []
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


def _status_authenticated(raw: str) -> bool:
    """True only when ``status --format json`` has ``isAuthenticated`` strictly ``True``.

    Logged-out CLIs still exit 0 with ``isAuthenticated: false`` — never infer auth from exit
    code alone. Unparseable / missing / wrong-type fields are not authenticated. Pure.
    """
    raw = raw.strip()
    if not raw:
        return False
    try:
        obj = json.loads(raw)
    except json.JSONDecodeError:
        return False
    return isinstance(obj, dict) and obj.get("isAuthenticated") is True


def _parse_about(raw: str) -> dict[str, str] | None:
    """Extract ``{plan, model}`` from ``cursor-agent about`` for post-auth enrichment.

    JSON (``--format json``) is preferred; a text fallback parses the human table so a future
    default-format change can't silently drop the signal. Requires an auth-adjacent signal
    (non-empty ``subscriptionTier`` and/or ``userEmail`` in JSON, or a Subscription Tier line
    in text) — bare ``model: "Auto"`` with null tier/email (the live logged-out shape) must
    not look like a successful account probe. Pure - unit-tested without a subprocess.
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
        email = obj.get("userEmail")
        model = obj.get("model")
        has_auth_signal = (isinstance(tier, str) and bool(tier)) or (
            isinstance(email, str) and bool(email)
        )
        if not has_auth_signal:
            return None
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
    # Text path: require a plan (tier) so a lone "Model: Auto" line cannot green-light.
    if "plan" not in info:
        return None
    return info or None


def _assistant_content_texts(ev: dict[str, Any]) -> list[str]:
    """Extract assistant text deltas from a Cursor stream-json ``assistant`` event."""
    message_raw = ev.get("message")
    message: dict[str, Any] = message_raw if isinstance(message_raw, dict) else {}
    if message.get("role") != "assistant":
        return []
    return _content_texts(message.get("content"))


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


def _apply_cursor_usage(usage: UsageRecord, usage_raw: object) -> None:
    """Stamp token counts from a ``result`` event when present; source stays unavailable.

    Cursor reports tokens but never USD in the CLI envelope — cost provenance stays
    ``unavailable`` (do not invent a $0). Cache read/write are additive optional fields.
    """
    if not isinstance(usage_raw, dict):
        return
    inp = usage_raw.get("inputTokens", usage_raw.get("input_tokens"))
    out = usage_raw.get("outputTokens", usage_raw.get("output_tokens"))
    cache_read = usage_raw.get("cacheReadTokens", usage_raw.get("cache_read_tokens"))
    cache_write = usage_raw.get("cacheWriteTokens", usage_raw.get("cache_write_tokens"))
    if isinstance(inp, int) and inp > 0:
        usage.input_tokens += inp
    if isinstance(out, int) and out > 0:
        usage.output_tokens += out
    if isinstance(cache_read, int) and cache_read > 0:
        usage.cache_read_tokens += cache_read
    if isinstance(cache_write, int) and cache_write > 0:
        usage.cache_write_tokens += cache_write


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
