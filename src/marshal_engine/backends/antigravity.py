"""Google Antigravity CLI adapter (`agy`).

Invocation reference (Antigravity CLI 2.0, `agy`):

    agy [-m MODEL] [--dangerously-skip-permissions] [--conversation ID] -p "<PROMPT>"

`agy -p` runs one prompt non-interactively. Run with cwd = the target repo (agy operates on
its launch folder; there is no `--dir` flag).

Honest gaps from research (these shape what we expose):
  * NO reliable structured output yet - `--output-format json` is reported broken, so we parse
    PLAIN TEXT stdout. native_usage = False (no tokens/cost available headless).
  * Auth is OAuth-first; unattended `ANTIGRAVITY_API_KEY` is an unconfirmed upstream request.
    Expect a one-time OAuth on a persistent runner. There is no cheap dedicated
    `auth`/`status`/`whoami` CLI probe (`agy --help` has none), so `verifies_auth()` stays
    False — doctor reports CLI presence only and must not claim credentials are valid.
    Prefer an honest path-only gap over a hang-prone "starts language server" probe.
  * `agy` checks for a TTY; without one, stdout can be swallowed while exit code stays 0. A PTY
    wrapper (e.g. `script -q /dev/null`) belongs in the runner layer - TODO. Until then treat an
    empty success as suspect.
  * No headless session-id capture (`-p` doesn't return its conversation id), so sessions=False;
    `--conversation` is passed through only if the caller already has an id.
  * Only `safe-edit` and `yolo` are reliably non-prompting headless. There is no confirmed
    one-shot read-only flag (the read-only presets prompt), so READ_ONLY is unsupported here.
  * WORKSPACE TRUST (fixed 2026-06-27): headless `agy` cannot establish workspace trust without a
    TTY, so it used to write edits into its scratch dir (`~/.gemini/antigravity-cli/scratch`)
    instead of `cwd` ("you do not have an active workspace"). `prepare()` briefly registers the
    run's worktree in the host-global `~/.gemini/antigravity-cli/settings.json`
    `trustedWorkspaces` (removed when the run completes; see `run()`), and the run passes
    `--add-dir <cwd>`. VERIFIED end-to-end: edits then land in the worktree, not scratch.
    `--add-dir` alone was insufficient (the prior known limitation); the trust entry is the fix.

Models available: gemini-3.1-pro, gemini-3.5-flash, claude-sonnet-4.6, claude-opus-4.6, gpt-oss-120b.
"""

from __future__ import annotations

import json
import os
import sys
import tempfile
import threading
from pathlib import Path

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


#: Where the agy CLI keeps its user settings (incl. `trustedWorkspaces`). An attribute on the
#: backend so tests can point it at a temp file instead of the real home.
DEFAULT_SETTINGS_PATH = Path.home() / ".gemini" / "antigravity-cli" / "settings.json"


class AntigravityBackend(CodingAgentBackend):
    name = "antigravity"
    binary = "agy"
    # agy reads/writes one global settings file; serialize concurrent trust updates (parallel runs).
    _settings_lock = threading.Lock()
    #: cwd -> did THIS run add the trust entry? Teardown revokes only what we granted, so a
    #: workspace the user had already trusted survives the run. Class-level like the lock: one
    #: backend instance serves every run, and parallel runs each key on their own worktree.
    _trust_added: dict[str, bool] = {}
    settings_path = DEFAULT_SETTINGS_PATH
    capabilities = Capabilities(
        json_output=False,  # --output-format json reported broken; text output only
        stream_json=False,
        sessions=False,  # -p does not return its conversation id
        server_mode=False,
        native_usage=False,  # no tokens/cost available headless
        permission_modes=frozenset({PermissionMode.SAFE_EDIT, PermissionMode.YOLO}),
        permission_fidelity=PermissionFidelity.BOUNDARY_ONLY,
    )

    # safe-edit and yolo both map to skip-permissions today: the default preset prompts (which
    # deadlocks headless), and there is no distinct one-shot safe-edit flag. Tighter scoping
    # comes from /config presets via the engine config layer later.
    _PERMISSION: dict[PermissionMode, list[str]] = {
        PermissionMode.SAFE_EDIT: ["--dangerously-skip-permissions"],
        PermissionMode.YOLO: ["--dangerously-skip-permissions"],
    }

    # --- hooks ---------------------------------------------------------------------------

    def _unsupported_permission_error(self, mode: PermissionMode) -> str:
        return (
            f"antigravity: permission mode {mode!r} is not supported headless "
            "(only safe-edit and yolo)"
        )

    def build_invocation(self, task: TaskSpec, opts: RunOpts) -> list[str]:
        argv = [self.binary]
        argv += self.map_permission(opts.permission)
        # Add the worktree to the active workspace; paired with the trust entry prepare() writes,
        # this makes edits land in cwd instead of agy's scratch dir.
        argv += ["--add-dir", str(opts.cwd)]
        if opts.model:
            argv += ["-m", opts.model]
        if opts.session_id:
            argv += ["--conversation", opts.session_id]
        # -p must come last with the prompt as its trailing argument.
        argv += ["-p", self._compose_prompt(task)]
        return argv

    def prepare(self, opts: RunOpts) -> None:
        """Register the run's worktree as a trusted agy workspace so headless edits land in `cwd`.

        Without a trust entry, headless `agy` writes into its scratch dir instead of `cwd` (it cannot
        establish workspace trust without a TTY). This writes the host-global agy settings file
        (``settings_path``, default ``~/.gemini/antigravity-cli/settings.json``); ``run()`` removes
        the entry when the run completes. Merge-preserving, atomic, and idempotent; dead worktree
        paths are pruned as a backstop. Serialized for parallel runs. Fails closed on a malformed or
        unreadable settings file (preserved byte-for-byte).
        """
        added = _trust_workspace(self.settings_path, Path(opts.cwd), self._settings_lock)
        with self._settings_lock:
            self._trust_added[str(Path(opts.cwd).resolve())] = added

    def run(self, task: TaskSpec, opts: RunOpts) -> AgentResult:
        """Shared run loop, wrapped in a host-global ``trustedWorkspaces`` transaction.

        ``prepare()`` adds this run's worktree to agy's global settings; the finally path removes
        only that path before returning - so the trust grant lasts for the run, not indefinitely.
        Teardown is best-effort: a missing or unreadable settings file at cleanup time warns on
        stderr and does not affect the run result.
        """
        try:
            return super().run(task, opts)
        finally:
            key = str(Path(opts.cwd).resolve())
            with self._settings_lock:
                added = self._trust_added.pop(key, True)
            if added:
                _untrust_workspace(self.settings_path, Path(opts.cwd), self._settings_lock)

    def parse_output(self, raw_stdout: str, raw_stderr: str, exit_code: int) -> AgentResult:
        if exit_code != 0:
            return AgentResult(
                status=RunStatus.FAILED,
                error=raw_stderr.strip() or f"agy exited {exit_code}",
                exit_code=exit_code,
                raw_stdout=raw_stdout,
                raw_stderr=raw_stderr,
            )
        # Plain-text output; no machine-readable usage/session available.
        return AgentResult(
            status=RunStatus.SUCCEEDED,
            text=raw_stdout.strip(),
            usage=UsageRecord(backend=self.name, source=UsageSource.UNAVAILABLE),
            exit_code=exit_code,
            raw_stdout=raw_stdout,
            raw_stderr=raw_stderr,
        )


# --- module helpers ----------------------------------------------------------------------


def _load_settings_object(settings_path: Path) -> dict[str, object]:
    """Load ``settings_path`` as a JSON object. Raises ``RuntimeError`` on any read/parse failure."""
    if not settings_path.exists():
        return {}
    try:
        raw = settings_path.read_text(encoding="utf-8")
    except OSError as exc:
        raise RuntimeError(
            f"existing {settings_path} is unreadable ({exc}); fix its permissions or remove it "
            "before an antigravity run"
        ) from exc
    try:
        loaded = json.loads(raw)
    except json.JSONDecodeError as exc:
        raise RuntimeError(
            f"existing {settings_path} is not valid JSON ({exc}); fix or remove it before an "
            "antigravity run - refusing to overwrite it"
        ) from exc
    if not isinstance(loaded, dict):
        raise RuntimeError(
            f"existing {settings_path} is valid JSON but not an object; fix or remove it before "
            "an antigravity run - refusing to overwrite it"
        )
    return loaded


def _atomic_write_settings(settings_path: Path, data: dict[str, object]) -> None:
    """Atomically replace ``settings_path`` with ``data`` (unique temp + ``os.replace``)."""
    settings_path.parent.mkdir(parents=True, exist_ok=True)
    fd, tmp_str = tempfile.mkstemp(
        dir=str(settings_path.parent), prefix=f"{settings_path.name}.", suffix=".tmp"
    )
    try:
        with os.fdopen(fd, "w", encoding="utf-8") as f:
            f.write(json.dumps(data, indent=2))
        os.replace(tmp_str, settings_path)
    except BaseException:
        try:
            os.unlink(tmp_str)
        except OSError:
            pass
        raise


def _is_marshal_worktree(path: str) -> bool:
    """True for a path that looks like one of Marshal's own worktrees.

    Used to bound the dead-path sweep. A user's own trusted path may be *temporarily* unavailable
    (unmounted volume, external drive, network share); deleting it because it does not exist right
    now silently destroys their agy trust config. Only Marshal's own leftovers are swept.
    """
    return f"{os.sep}.marshal{os.sep}worktrees{os.sep}" in path


def _trust_workspace(settings_path: Path, cwd: Path, lock: threading.Lock) -> bool:
    """Add `cwd` to agy's `trustedWorkspaces` in `settings_path`, preserving other settings.

    Merge-preserving (other keys untouched), idempotent (no duplicate entry), and atomic (unique
    temp + replace, so a concurrent agy read never sees a torn file even if a writer dies
    between write + replace). Dead paths are pruned so the trust list stays bounded to live
    worktrees. The lock serializes concurrent writers (parallel runs all share this one
    global file). Fails closed on a malformed or unreadable existing file.
    """
    cwd_str = str(cwd.resolve())
    with lock:
        data = _load_settings_object(settings_path)
        existing = data.get("trustedWorkspaces")
        trusted = [t for t in existing if isinstance(t, str)] if isinstance(existing, list) else []
        already_trusted = cwd_str in trusted
        # Keep every other entry; sweep only Marshal's OWN dead worktrees (a crashed run's
        # leftover). A user path that is merely unavailable right now is never touched.
        kept = [
            t
            for t in trusted
            if t != cwd_str and not (_is_marshal_worktree(t) and not Path(t).exists())
        ]
        data["trustedWorkspaces"] = [*kept, cwd_str]
        _atomic_write_settings(settings_path, data)
        # Report whether WE introduced this entry, so teardown never revokes trust the user
        # granted themselves before Marshal ever ran.
        return not already_trusted


def _untrust_workspace(settings_path: Path, cwd: Path, lock: threading.Lock) -> None:
    """Best-effort: remove `cwd` from `trustedWorkspaces`. Never raises."""
    cwd_str = str(cwd.resolve())
    with lock:
        if not settings_path.exists():
            print(
                f"[marshal] antigravity: {settings_path} missing during trust cleanup; "
                "skipping untrust",
                file=sys.stderr,
            )
            return
        try:
            raw = settings_path.read_text(encoding="utf-8")
        except OSError as exc:
            print(
                f"[marshal] antigravity: cannot read {settings_path} for trust cleanup ({exc}); "
                "leaving file untouched",
                file=sys.stderr,
            )
            return
        try:
            loaded = json.loads(raw)
        except json.JSONDecodeError as exc:
            print(
                f"[marshal] antigravity: {settings_path} is not valid JSON during trust cleanup "
                f"({exc}); leaving file untouched",
                file=sys.stderr,
            )
            return
        if not isinstance(loaded, dict):
            print(
                f"[marshal] antigravity: {settings_path} is not a JSON object during trust "
                "cleanup; leaving file untouched",
                file=sys.stderr,
            )
            return
        existing = loaded.get("trustedWorkspaces")
        if not isinstance(existing, list):
            return
        trusted = [t for t in existing if isinstance(t, str)]
        if cwd_str not in trusted:
            return
        # Remove ONLY this run's path. No general dead-path sweep here: another entry that is
        # temporarily unavailable belongs to the user, and dropping it would silently revoke
        # trust they granted themselves.
        loaded["trustedWorkspaces"] = [t for t in trusted if t != cwd_str]
        try:
            _atomic_write_settings(settings_path, loaded)
        except OSError as exc:
            print(
                f"[marshal] antigravity: failed to write {settings_path} during trust cleanup "
                f"({exc}); leaving file untouched",
                file=sys.stderr,
            )
