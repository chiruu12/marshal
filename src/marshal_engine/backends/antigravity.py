"""Google Antigravity CLI adapter (`agy`).

Invocation reference (Antigravity CLI 2.0, `agy`):

    agy [--dangerously-skip-permissions] [--output-format json] [--add-dir CWD]
        [-m MODEL] [--conversation ID] -p "<PROMPT>"

`agy -p` runs one prompt non-interactively. Run with cwd = the target repo (agy operates on
its launch folder; there is no `--dir` flag).

Honest gaps from research (these shape what we expose):
  * Structured output works (agy ≥ 1.1.8): ``--output-format json`` returns
    ``{response, conversation_id, usage:{input_tokens,output_tokens,cache_read_tokens,...}}``.
    Tokens are stamped; there is NO USD/cost field, so ``source=unavailable`` and
    ``native_usage=False`` (that flag means native cost). ``stream-json`` is also parseable
    (terminal ``event:"result"`` nests the same object under ``result``).
  * Auth is OAuth-first; unattended `ANTIGRAVITY_API_KEY` is an unconfirmed upstream request.
    Expect a one-time OAuth on a persistent runner. There is no cheap dedicated
    `auth`/`status`/`whoami` CLI probe (`agy --help` has none), so `verifies_auth()` stays
    False — doctor reports CLI presence only and must not claim credentials are valid.
    Prefer an honest path-only gap over a hang-prone "starts language server" probe.
  * `agy` checks for a TTY; without one, stdout can be swallowed while exit code stays 0. A PTY
    wrapper (e.g. `script -q /dev/null`) belongs in the runner layer - TODO. Until then treat an
    empty success as suspect.
  * JSON envelopes carry ``conversation_id`` (stamped onto ``session_id``); ``sessions`` stays
    False until resume is a first-class advertised capability. ``--conversation`` is still
    passed through when the caller already has an id.
  * Only `safe-edit` and `yolo` are reliably non-prompting headless. There is no confirmed
    one-shot read-only flag (the read-only presets prompt), so READ_ONLY is unsupported here.
  * WORKSPACE TRUST (fixed 2026-06-27): headless `agy` cannot establish workspace trust without a
    TTY, so it used to write edits into its scratch dir (`~/.gemini/antigravity-cli/scratch`)
    instead of `cwd` ("you do not have an active workspace"). `prepare()` briefly registers the
    run's worktree in the host-global `~/.gemini/antigravity-cli/settings.json`
    `trustedWorkspaces` (removed when the run completes; see `run()`), and the run passes
    `--add-dir <cwd>`. VERIFIED end-to-end: edits then land in the worktree, not scratch.
    `--add-dir` alone was insufficient (the prior known limitation); the trust entry is the fix.
    RESIDUAL: if Marshal is hard-killed mid-run, teardown never runs and the worktree path stays in
    `trustedWorkspaces`. It is inert (a path string) and self-heals: once the worktree directory is
    gone - `marshal clean`, or integrate's cleanup - the next Antigravity run's dead-Marshal-path
    sweep removes it. Deliberately NOT mitigated with atexit hooks or a cross-process ledger; that
    machinery would cost more than the problem, and the fail-closed "no bookkeeping means do not
    revoke" rule is the safety bias we want.
    Note how this composes with orphan reaping: reaping stamps the record `failed` but removes
    neither the worktree nor the trust entry, so the grant outlives a run that now *reads* finished.
    Nothing is newly unsafe - it is the same residual as any hard kill - but a terminal-looking
    record makes it easy to assume the cleanup already happened. `clean` is still what reclaims it.

Models available: gemini-3.1-pro, gemini-3.5-flash, claude-sonnet-4.6, claude-opus-4.6, gpt-oss-120b.
"""

from __future__ import annotations

import contextlib
import fcntl
import json
import os
import shutil
import subprocess
import sys
import tempfile
import threading
from collections.abc import Iterator
from pathlib import Path
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
from .base import CodingAgentBackend, parse_jsonl


#: Where the agy CLI keeps its user settings (incl. `trustedWorkspaces`). An attribute on the
#: backend so tests can point it at a temp file instead of the real home.
DEFAULT_SETTINGS_PATH = Path.home() / ".gemini" / "antigravity-cli" / "settings.json"

#: Static fallback when ``agy models`` cannot be probed.
#: Sourced from this module's docstring / docs/model-playbook.md (antigravity rows).
_STATIC_MODELS: tuple[str, ...] = (
    "gemini-3.1-pro",
    "gemini-3.5-flash",
    "claude-sonnet-4.6",
    "claude-opus-4.6",
    "gpt-oss-120b",
)


class AntigravityBackend(CodingAgentBackend):
    name = "antigravity"
    binary = "agy"
    # agy reads/writes one global settings file; serialize concurrent trust updates (parallel runs
    # in-process via this lock, cross-process via ``_settings_file_lock`` on a sibling sidecar).
    _settings_lock = threading.Lock()
    #: resolved cwd -> ``[marshal_introduced, in_flight_run_count]``.
    #:
    #: Two facts, because one is not enough. ``marshal_introduced`` records whether the FIRST
    #: Marshal run to want this path found it absent - if the user had already trusted it, we must
    #: never revoke it. ``in_flight_run_count`` counts every Marshal run currently relying on the
    #: entry, including runs that found it already present *because a sibling Marshal run put it
    #: there*: revoking on the first teardown would pull trust out from under a still-running agy
    #: process, silently redirecting its edits to the scratch dir. The entry is removed only when
    #: the last user finishes AND Marshal introduced it.
    #: Process-local by design — single Marshal process per host for Antigravity (see docs/usage.md).
    _trust_added: dict[str, list[object]] = {}
    #: Per-thread record of which cwds this run claimed (see `_claimed_cwds`).
    _thread_claims = threading.local()
    settings_path = DEFAULT_SETTINGS_PATH
    capabilities = Capabilities(
        json_output=True,  # --output-format json (agy ≥ 1.1.8)
        stream_json=True,  # terminal event:"result" nests the same envelope
        sessions=False,  # conversation_id is stamped; resume not advertised yet
        server_mode=False,
        native_usage=False,  # tokens yes; no USD in CLI output — stay honest
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

    def account_info(self) -> dict[str, str] | None:
        """No cheap auth/status/whoami probe on ``agy`` (``agy --help`` has none).

        Always None. Hang-prone TTY probes are refused — doctor reports CLI presence only.
        """
        return None

    def verifies_auth(self) -> bool:
        # Honest gap: no authenticated-only probe; a None from account_info is "unsupported",
        # not "logged out". Doctor must not claim credentials are valid from PATH alone.
        return False

    def available_models(self) -> list[str]:
        """Model ids from ``agy models``, or the static playbook/docstring fallback.

        One model id per stdout line (verified against the real CLI). Never raises; never
        returns None — on any probe failure the curated static list is used.
        """
        if shutil.which(self.binary) is None:
            return list(_STATIC_MODELS)
        try:
            proc = subprocess.run(
                [self.binary, "models"],
                capture_output=True,
                text=True,
                timeout=20,
                stdin=subprocess.DEVNULL,
            )
        except (OSError, subprocess.SubprocessError):
            return list(_STATIC_MODELS)
        if proc.returncode != 0:
            return list(_STATIC_MODELS)
        models = [line.strip() for line in (proc.stdout or "").splitlines() if line.strip()]
        return models or list(_STATIC_MODELS)

    def build_invocation(self, task: TaskSpec, opts: RunOpts) -> list[str]:
        argv = [self.binary]
        argv += self.map_permission(opts.permission)
        # Structured envelope: response text + per-run token usage (no USD).
        argv += ["--output-format", "json"]
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

    def _claimed_cwds(self) -> dict[str, int]:
        """How many trust claims THIS thread currently holds, per cwd.

        A count, not a flag: one thread can legitimately hold more than one claim on the same path
        (nested or sequential runs in a single worker), and collapsing those to a boolean would
        strand the entry - the first release would clear the marker and the second would no-op.

        Per-thread, because a run's `prepare()` and its teardown happen on the same thread (the
        base run loop calls `prepare` then the finally path) while the backend instance itself is
        shared by every concurrent run. Without it, a run whose `prepare()` failed before
        registering would still decrement — and could revoke — a *sibling* run's live claim.
        """
        claims: dict[str, int] | None = getattr(self._thread_claims, "cwds", None)
        if claims is None:
            claims = {}
            self._thread_claims.cwds = claims
        return claims

    def prepare(self, opts: RunOpts) -> None:
        """Register the run's worktree as a trusted agy workspace so headless edits land in `cwd`.

        Without a trust entry, headless `agy` writes into its scratch dir instead of `cwd` (it cannot
        establish workspace trust without a TTY). This writes the host-global agy settings file
        (``settings_path``, default ``~/.gemini/antigravity-cli/settings.json``); ``run()`` removes
        the entry when the run completes. Merge-preserving, atomic, and idempotent; dead worktree
        paths are pruned as a backstop. Serialized for parallel runs. Fails closed on a malformed or
        unreadable settings file (preserved byte-for-byte).
        """
        key = str(Path(opts.cwd).resolve())
        # ONE critical section: mutate the settings file and record provenance under the same
        # in-process lock + cross-process flock. Splitting them inverted provenance under
        # concurrency - the run that merely *observed* a freshly added entry could register first
        # and record it as user-owned, after which nobody would ever revoke Marshal's own grant.
        # The flock also stops two Marshal processes from interleaving the trustedWorkspaces RMW
        # and dropping each other's grant.
        with self._settings_lock, _settings_file_lock(self.settings_path):
            added = _trust_workspace_locked(self.settings_path, Path(opts.cwd))
            state = self._trust_added.get(key)
            if state is None:
                self._trust_added[key] = [added, 1]
            else:
                # A sibling run already holds it; join as a user without changing provenance.
                state[1] = int(state[1]) + 1  # type: ignore[call-overload]
            mine = self._claimed_cwds()
            mine[key] = mine.get(key, 0) + 1

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
            self.release_trust(opts)

    def release_trust(self, opts: RunOpts) -> None:
        """Drop this run's claim on the trust entry, revoking it when the last claim goes.

        Split out of ``run()`` so the teardown contract is reachable without spawning a process -
        overlapping runs cannot be exercised end-to-end in a unit test, and duplicating this logic
        in a test would mean the test could not catch a change here.
        """
        key = str(Path(opts.cwd).resolve())
        # Only release what THIS run actually claimed. A `prepare()` that raised before registering
        # never claimed anything, and its teardown must not decrement a sibling's live claim.
        mine = self._claimed_cwds()
        if mine.get(key, 0) <= 0:
            return
        if mine[key] == 1:
            del mine[key]
        else:
            mine[key] -= 1
        # Drop the claim AND remove the settings entry under ONE in-process lock + cross-process
        # flock. Doing the bookkeeping, unlocking, then removing the entry left a handoff gap: a
        # new run's prepare() could slot in, see the entry still present, record it as user-owned -
        # and then this teardown deleted it, leaving that run with no trust at all and its agy
        # edits redirected to the scratch dir. The flock covers the same gap across processes.
        with self._settings_lock, _settings_file_lock(self.settings_path):
            state = self._trust_added.get(key)
            # No bookkeeping at all means nothing we did introduced this entry, so it is the
            # user's and must stay. Defaulting the other way ("assume we added it") destroys user
            # trust silently and permanently; a stray entry is recoverable, and the
            # Marshal-worktree sweep collects it later.
            if state is None:
                return
            state[1] = int(state[1]) - 1  # type: ignore[call-overload]
            if int(state[1]) > 0:  # type: ignore[call-overload]
                return  # a sibling run still needs the entry
            del self._trust_added[key]
            if bool(state[0]):  # only if WE introduced it
                _untrust_workspace_locked(self.settings_path, Path(opts.cwd))

    def parse_output(self, raw_stdout: str, raw_stderr: str, exit_code: int) -> AgentResult:
        if exit_code != 0:
            # Best-effort: a failed run may still have emitted a partial JSON envelope with
            # tokens / conversation id — keep them for the ledger and investigation.
            envelope = _extract_agy_envelope(raw_stdout)
            usage = UsageRecord(backend=self.name, source=UsageSource.UNAVAILABLE)
            session_id: str | None = None
            text = ""
            if envelope is not None:
                text = _agy_response_text(envelope)
                session_id = _agy_conversation_id(envelope)
                _apply_agy_usage(usage, envelope.get("usage"))
            return AgentResult(
                status=RunStatus.FAILED,
                text=text,
                session_id=session_id,
                usage=usage,
                error=raw_stderr.strip() or f"agy exited {exit_code}",
                exit_code=exit_code,
                raw_stdout=raw_stdout,
                raw_stderr=raw_stderr,
            )
        envelope = _extract_agy_envelope(raw_stdout)
        usage = UsageRecord(backend=self.name, source=UsageSource.UNAVAILABLE)
        if envelope is None:
            # Envelope drift / non-JSON: fall back to plain-text (at least as good as before).
            return AgentResult(
                status=RunStatus.EXITED_CLEAN,
                text=raw_stdout.strip(),
                usage=usage,
                exit_code=exit_code,
                raw_stdout=raw_stdout,
                raw_stderr=raw_stderr,
            )
        _apply_agy_usage(usage, envelope.get("usage"))
        return AgentResult(
            status=RunStatus.EXITED_CLEAN,
            text=_agy_response_text(envelope),
            session_id=_agy_conversation_id(envelope),
            usage=usage,
            exit_code=exit_code,
            raw_stdout=raw_stdout,
            raw_stderr=raw_stderr,
        )


# --- module helpers ----------------------------------------------------------------------


def _extract_agy_envelope(raw: str) -> dict[str, Any] | None:
    """Pull the terminal result object from ``json`` or ``stream-json`` stdout.

    Loose dict parse (adapter convention): tolerate missing keys and envelope drift. Prefer a
    single-object ``json`` document; for NDJSON take the last ``event == "result"`` and unwrap
    its nested ``result`` when present. Pure.
    """
    stripped = raw.strip()
    if not stripped:
        return None
    try:
        whole = json.loads(stripped)
    except json.JSONDecodeError:
        whole = None
    if isinstance(whole, dict):
        # Single-object json mode, or a lone stream event that is itself the result wrapper.
        if whole.get("event") == "result":
            nested = whole.get("result")
            return nested if isinstance(nested, dict) else whole
        return whole
    found: dict[str, Any] | None = None
    for ev in parse_jsonl(raw):
        if ev.get("event") == "result":
            nested = ev.get("result")
            found = nested if isinstance(nested, dict) else ev
    return found


def _agy_response_text(envelope: dict[str, Any]) -> str:
    """Final assistant text from an agy result envelope. Missing/wrong-type → empty string."""
    resp = envelope.get("response")
    if isinstance(resp, str):
        return resp.strip()
    return ""


def _agy_conversation_id(envelope: dict[str, Any]) -> str | None:
    sid = envelope.get("conversation_id")
    return sid if isinstance(sid, str) and sid else None


def _apply_agy_usage(usage: UsageRecord, usage_raw: object) -> None:
    """Stamp token counts from agy's ``usage`` block; source stays unavailable (no USD).

    Do not trust any cost-like key without verification — tokens only. ``thinking_tokens`` /
    ``total_tokens`` are ignored (no UsageRecord fields; total would double-count).
    """
    if not isinstance(usage_raw, dict):
        return
    inp = usage_raw.get("input_tokens", usage_raw.get("inputTokens"))
    out = usage_raw.get("output_tokens", usage_raw.get("outputTokens"))
    cache_read = usage_raw.get("cache_read_tokens", usage_raw.get("cacheReadTokens"))
    if isinstance(inp, int) and not isinstance(inp, bool) and inp > 0:
        usage.input_tokens += inp
    if isinstance(out, int) and not isinstance(out, bool) and out > 0:
        usage.output_tokens += out
    if isinstance(cache_read, int) and not isinstance(cache_read, bool) and cache_read > 0:
        usage.cache_read_tokens += cache_read


def _settings_lock_path(settings_path: Path) -> Path:
    """Sidecar for the settings flock: ``settings.lock`` next to ``settings.json``.

    Named without a ``settings.json*`` prefix so leftover-temp assertions that scan for that
    prefix do not mistake the durable lock file for an orphaned write temp. flock auto-releases
    on process death - no stale-lock reaper.
    """
    return settings_path.with_name(settings_path.stem + ".lock")


@contextlib.contextmanager
def _settings_file_lock(settings_path: Path) -> Iterator[None]:
    """Exclusive ``flock`` for the host-global settings.json read-modify-write.

    Scope is the RMW critical section only (prepare/release_trust / trust helpers) - never held
    across a backend run. Pairs with ``AntigravityBackend._settings_lock`` (thread lock first,
    then flock).
    """
    settings_path.parent.mkdir(parents=True, exist_ok=True)
    with open(_settings_lock_path(settings_path), "a+", encoding="utf-8") as guard:
        fcntl.flock(guard.fileno(), fcntl.LOCK_EX)
        try:
            yield
        finally:
            fcntl.flock(guard.fileno(), fcntl.LOCK_UN)


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
    """Locking wrapper kept for callers that do not already hold ``lock`` / the file lock."""
    with lock, _settings_file_lock(settings_path):
        return _trust_workspace_locked(settings_path, cwd)


def _trust_workspace_locked(settings_path: Path, cwd: Path) -> bool:
    """Add `cwd` to agy's `trustedWorkspaces` in `settings_path`, preserving other settings.

    Merge-preserving (other keys untouched), idempotent (no duplicate entry), and atomic (unique
    temp + replace, so a concurrent agy read never sees a torn file even if a writer dies
    between write + replace). Dead paths are pruned so the trust list stays bounded to live
    worktrees. Caller holds the in-process lock and the settings flock. Fails closed on a
    malformed or unreadable existing file.
    """
    cwd_str = str(cwd.resolve())
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
    """Locking wrapper kept for callers that do not already hold ``lock`` / the file lock."""
    with lock, _settings_file_lock(settings_path):
        _untrust_workspace_locked(settings_path, cwd)


def _untrust_workspace_locked(settings_path: Path, cwd: Path) -> None:
    """Best-effort: remove `cwd` from `trustedWorkspaces`. Never raises. Caller holds the lock."""
    cwd_str = str(cwd.resolve())
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
