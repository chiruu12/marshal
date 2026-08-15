"""The Marshal backend base class - the cornerstone of the engine.

Every headless coding agent (Cursor, OpenCode, Codex, Gemini, ...) is a subclass that
implements four pure-or-simple hooks. The base class owns the one thing that must never
be gotten wrong: spawning the process with a hard timeout and no stdin.

Design rules (see docs/design.md):
  * `build_invocation` and `map_permission` are PURE functions returning argv / flags.
    They must be unit-testable without spawning a process. This is where contract tests live.
  * `run()` is concrete and shared: it builds the argv, runs it in `opts.cwd` with an
    external timeout and stdin closed, then delegates to `parse_output`.
  * The backend does NOT manage worktrees. The fleet/worktree layer creates the worktree
    and passes it as `opts.cwd`. Backends are stateless and isolated.
"""

from __future__ import annotations

import errno
import json
import os
import shutil
import signal
import subprocess
import sys
import time
from abc import ABC, abstractmethod
from collections.abc import Callable, Sequence
from typing import Any, ClassVar

from ..core.types import (
    AgentResult,
    Capabilities,
    ModelCatalog,
    ModelSource,
    PermissionMode,
    RunOpts,
    RunStatus,
    TaskSpec,
    UsageRecord,
)
from ..runtime.env import DETACHED_STDIO, child_env, redact_secrets

_VERSION_PROBE_TIMEOUT_S = 15.0


def parse_jsonl(raw: str) -> list[dict[str, Any]]:
    """Parse newline-delimited JSON objects from a backend's stdout stream."""
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


class CodingAgentBackend(ABC):
    """Abstract base for a headless coding-agent backend."""

    #: short stable id, e.g. "cursor" | "opencode" | "codex"
    name: str
    #: the executable to invoke, e.g. "cursor-agent" | "opencode" | "codex"
    binary: str
    #: feature flags; subclasses set this so the orchestrator can degrade gracefully
    capabilities: Capabilities
    #: normalized permission tier -> native argv flags; subclasses populate this table
    _PERMISSION: ClassVar[dict[PermissionMode, list[str]]] = {}
    #: Parent env vars this backend may need for CLI auth (API keys). Only this backend's
    #: run forwards them; a cursor child never sees ``ANTHROPIC_API_KEY``. Empty by default.
    credential_env_vars: ClassVar[tuple[str, ...]] = ()
    #: True if this CLI resolves ``@path`` mentions in a prompt into file content. Those backends
    #: get ``@file`` mentions for ``context_files``; the rest get a plain bulleted list, which is
    #: inert text everywhere and cannot be mistaken for an unresolved mention.
    resolves_at_mentions: ClassVar[bool] = False

    # --- hooks subclasses must implement -------------------------------------------------

    def check_available(self) -> bool:
        """Return True if ``binary`` is on PATH and responds to ``--version``.

        This is a presence probe only - it does not verify authentication or pin a minimum
        version. Backends that require a version floor (e.g. Antigravity ≥ 1.1.8 for JSON
        output) override this so availability matches what the adapter will actually invoke.
        Backends with a cheap authenticated probe override ``account_info()`` and set
        ``verifies_auth()`` so ``marshal doctor`` can distinguish "installed" from "logged in".
        """
        if shutil.which(self.binary) is None:
            return False
        try:
            proc = subprocess.run(
                [self.binary, "--version"],
                capture_output=True,
                text=True,
                timeout=_VERSION_PROBE_TIMEOUT_S,
                check=False,
                **DETACHED_STDIO,
            )
        except (OSError, subprocess.SubprocessError):
            return False
        return proc.returncode == 0

    def unavailable_detail(self) -> str:
        """Doctor/CLI detail when ``check_available()`` is False.

        Default covers the presence-only probe. Adapters with a version floor override to name
        the minimum so a found-but-too-old CLI is not misreported as "not on PATH".
        """
        return "CLI not on PATH / not runnable"

    @abstractmethod
    def build_invocation(self, task: TaskSpec, opts: RunOpts) -> list[str]:
        """Pure function: (task, opts) -> argv. No side effects, no process spawning."""

    def map_permission(self, mode: PermissionMode) -> list[str]:
        """Pure function: a normalized permission tier -> this backend's native flags."""
        try:
            return list(self._PERMISSION[mode])
        except KeyError:
            raise ValueError(self._unsupported_permission_error(mode)) from None

    def _unsupported_permission_error(self, mode: PermissionMode) -> str:
        return f"{self.name}: unsupported permission mode {mode!r}"

    @abstractmethod
    def parse_output(self, raw_stdout: str, raw_stderr: str, exit_code: int) -> AgentResult:
        """Normalize this backend's raw output into an AgentResult.

        Must treat a non-zero exit (or unparseable output) as failure, and populate
        usage/session_id where the backend exposes them. Must be pure - no subprocesses.
        """

    # --- optional hooks ------------------------------------------------------------------

    def _compose_prompt(self, task: TaskSpec) -> str:
        """Build the agent prompt from the task goal and optional context files / read_paths.

        The only per-backend difference is how `context_files` are named, so that is a declared
        flag (`resolves_at_mentions`) rather than an override. Cursor and Goose previously
        carried byte-identical copies of this method purely to change that one line - two places
        for a `read_paths` wording fix to be applied, and one to be forgotten in.
        """
        prompt = task.goal
        if task.context_files:
            if self.resolves_at_mentions:
                mentions = " ".join(f"@{f}" for f in task.context_files)
                prompt = f"{prompt}\n\nRelevant context: {mentions}"
            else:
                files = "\n".join(f"- {f}" for f in task.context_files)
                prompt = f"{prompt}\n\nRelevant files:\n{files}"
        if task.read_paths:
            prompt = (
                f"{prompt}\n\nRead-only reference material is available under "
                f".marshal-context/ (do not modify those files)."
            )
        return prompt

    def finalize(self, result: AgentResult) -> AgentResult:
        """Post-success hook called by ``run()`` after ``parse_output`` on genuine completion.

        Default is identity. Backends that need a follow-up subprocess (e.g. OpenCode's
        ``opencode export`` reconciliation) override this so ``parse_output`` stays pure
        and the timeout-recovery path never triggers hidden work.
        """
        return result

    def extract_usage(self, result: AgentResult) -> UsageRecord | None:
        """Return the usage record for a run. Default: whatever parse_output captured.

        Backends without in-output usage (e.g. Cursor) may override this to fetch from a provider
        admin API, tagging the record's `source` accordingly. Estimating is not an option: a run
        whose cost no one reported stays `unavailable`, never a guessed figure.
        """
        return result.usage

    def account_info(self) -> dict[str, str] | None:
        """Return human-readable account facts (e.g. plan tier, default model), or None.

        This is account *metadata* a CLI can report cheaply - NOT a usage record, so it never
        touches the cost ledger. Backends that expose it (e.g. Cursor's `about`) override this;
        the default is None. Implementations must be side-effect-light and never raise: return
        None on any failure (missing binary, unauthenticated, unparseable output).
        """
        return None

    def available_models(self) -> ModelCatalog:
        """Model ids this backend can run, tagged with where the answer came from.

        A **read-only convenience**, so a driver does not have to leave the tool and shell out to
        `cursor-agent models` to find out what it may configure - which is exactly what happened in
        the field, on both sides of a single day.

        The `source` is the point. `PROBED` means the CLI answered just now; `STATIC` means the
        CLI could not be asked and this is a curated list that may name a model the account
        cannot actually run; `UNAVAILABLE` means there is nothing to report. Returning a bare
        list conflated the first two, so a fallback printed by a backend that was not even
        installed was indistinguishable from a live answer.

        Implementations must be side-effect-light and never raise: degrade to `STATIC`, or to the
        default `UNAVAILABLE`, on any failure (missing binary, unauthenticated, unparseable output).

        This never feeds routing. Clients own backend+model; this is a catalogue you read, and the
        distinction is what keeps a probe from quietly becoming configuration.
        """
        return ModelCatalog()

    def _probe_models(
        self,
        argv: list[str],
        parse: Callable[[str], list[str]],
        static: Sequence[str],
        *,
        timeout_s: float = 20.0,
    ) -> ModelCatalog:
        """Shared probe-then-fall-back-to-curated path for adapters whose CLI can list models.

        Every failure mode lands on the same answer - the curated list, tagged `STATIC` - so no
        adapter has to remember to degrade honestly on its own. `parse` yielding nothing counts
        as a failure: a CLI whose output shape changed upstream would otherwise report an empty
        live catalogue, which reads as "this backend has no models".
        """
        if shutil.which(self.binary) is None:
            return ModelCatalog(models=list(static), source=ModelSource.STATIC)
        try:
            proc = subprocess.run(
                argv,
                capture_output=True,
                text=True,
                timeout=timeout_s,
                check=False,
                **DETACHED_STDIO,
            )
        except (OSError, subprocess.SubprocessError):
            return ModelCatalog(models=list(static), source=ModelSource.STATIC)
        if proc.returncode != 0:
            return ModelCatalog(models=list(static), source=ModelSource.STATIC)
        try:
            models = parse(proc.stdout or "")
        except Exception:  # noqa: BLE001 - a parser fault must not take the listing down
            models = []
        if not models:
            return ModelCatalog(models=list(static), source=ModelSource.STATIC)
        return ModelCatalog(models=models, source=ModelSource.PROBED)

    def verifies_auth(self) -> bool:
        """True if account_info() doubles as an authenticated-only probe.

        When True, a None from account_info() *while the binary is on PATH* reliably means "not
        logged in" (not "metadata unsupported") - so `marshal doctor` reports the backend as
        present-but-unauthenticated rather than green-lighting it. This closes the gap where a CLI
        passes `--version` (unauthenticated) but dies on the first real run. Default False: most
        backends have no cheap authed probe, so doctor reports CLI presence without claiming the
        credentials are valid.
        """
        return False

    def prepare(self, opts: RunOpts) -> None:
        """Optional per-run setup, run just before the process is spawned (default: no-op).

        A seam for backend-specific preconditions that aren't pure argv - e.g. Antigravity
        registering the run's worktree as a trusted workspace so its headless edits land in `cwd`
        instead of a scratch dir. Keep it fast and idempotent; a failure here fails the run.
        """

    # --- shared, concrete run loop -------------------------------------------------------

    def run(self, task: TaskSpec, opts: RunOpts) -> AgentResult:
        """Build the invocation and execute it with a hard timeout and no stdin.

        This is the single chokepoint that defends the two universal headless footguns:
        the process is killed if it exceeds `opts.timeout_s`, and stdin is closed so an
        unexpected interactive prompt fails fast instead of deadlocking forever. On timeout the
        whole process *group* is killed (`start_new_session` + `os.killpg`), so agent grandchildren
        (subagents, MCP servers, tool shells) are not orphaned.
        """
        start = time.monotonic()

        def _elapsed_ms() -> int:
            return int((time.monotonic() - start) * 1000)

        # prepare() may stamp opts.extra_env (OpenCode OPENCODE_CONFIG_CONTENT, Goose GOOSE_MODE)
        # or write worktree config (Cursor deny list, Antigravity trust). It must run before we
        # snapshot argv/env for the child, or those mutations are silently dropped.
        try:
            self.prepare(opts)
        except Exception as exc:  # noqa: BLE001 - a prepare failure is a run failure, not a crash
            return AgentResult(
                status=RunStatus.FAILED,
                error=f"{self.name}: prepare failed: {exc}",
                duration_ms=_elapsed_ms(),
            )

        argv = self.build_invocation(task, opts)
        # Allowlisted child env: operational base + this backend's credentials only. Unrelated
        # secrets (AWS_*, GH_TOKEN, another backend's API key) never reach the agent.
        env = child_env(
            opts.extra_env,
            client=opts.client_env,
            credentials=self.credential_env_vars,
        )

        try:
            proc = subprocess.Popen(
                argv,
                cwd=str(opts.cwd),
                env=env,
                stdout=subprocess.PIPE,
                stderr=subprocess.PIPE,
                text=True,
                # Carries the process-group split the timeout kill depends on: `_kill_tree`
                # killpg's this pid, which is the group leader only because of setsid.
                **DETACHED_STDIO,
            )
        except OSError as exc:
            # FileNotFoundError, PermissionError (EACCES), ETXTBSY, etc. — never escape run().
            return AgentResult(
                status=RunStatus.FAILED,
                error=_spawn_os_error(self.name, self.binary, exc),
                duration_ms=_elapsed_ms(),
            )

        # Notify the caller of the child pid (for later cancellation via process-group signal).
        # Recording the pid is best-effort: if the callback raises, do NOT let it escape here - that
        # would skip communicate()/the timeout and leak the live process. The run proceeds (still
        # timed + killed); only later cancel-by-pid is unavailable for this run.
        if opts.on_pid is not None:
            try:
                opts.on_pid(proc.pid)
            except Exception as exc:  # noqa: BLE001 - never leak the process over a pid-record failure
                print(f"[marshal] {self.name}: on_pid callback failed: {exc}", file=sys.stderr)

        # start_new_session makes the child its own group leader, so its pgid == its pid. Capture
        # it now, while the leader is alive - resolving it later (after a fast leader exit) can race
        # a zombie and strand the group.
        pgid = proc.pid

        def _notify_exit() -> None:
            """Tell the caller the child is reaped, so its pid may now be recycled by the OS."""
            if opts.on_exit is None:
                return
            try:
                opts.on_exit()
            except Exception as exc:  # noqa: BLE001 - never let a callback break a finished run
                print(f"[marshal] {self.name}: on_exit callback failed: {exc}", file=sys.stderr)

        try:
            out, err = proc.communicate(timeout=opts.timeout_s)
        except subprocess.TimeoutExpired:
            _kill_process_group(pgid)
            out, err = _drain(proc)  # bounded: a setsid-escaped survivor holding the pipe can't hang us
            _notify_exit()
            return AgentResult(
                status=RunStatus.TIMED_OUT,
                error=f"{self.name}: timed out after {opts.timeout_s}s",
                session_id=opts.session_id,
                usage=self._recover_partial_usage(out, err),
                raw_stdout=out,
                raw_stderr=err,
                duration_ms=_elapsed_ms(),
            )

        _notify_exit()
        result = self.parse_output(out, err, proc.returncode)
        result = self.finalize(result)
        result.duration_ms = _elapsed_ms()
        if result.status is RunStatus.FAILED and not result.error:
            # parse_output found no reason (e.g. the backend errored outside its JSON stream).
            # Consult stderr first, then stdout — Goose and others bury provider failures on stdout.
            result.error = _failure_reason(self.name, proc.returncode, err, out)
        elif (
            result.status is RunStatus.FAILED
            and result.error
            and _looks_like_auth_failure(result.error)
            and _AUTH_ALLOWLIST_HINT not in result.error
        ):
            result.error = f"{result.error} ({_AUTH_ALLOWLIST_HINT})"
        return result

    def _recover_partial_usage(self, stdout: str, stderr: str) -> UsageRecord | None:
        """Best-effort: salvage usage from a timed-out run's partial output. Never raises.

        Tokens are real spend even if the run was killed mid-stream, so recovering them keeps the
        cost ledger honest. A recovery failure must never mask the timeout - all errors are swallowed.
        """
        if not stdout.strip():
            return None
        try:
            return self.parse_output(stdout, stderr, 0).usage
        except Exception:  # noqa: BLE001 - recovery is best-effort and must not mask the timeout
            return None


def _kill_process_group(pgid: int, grace_s: float = 0.5) -> None:
    """SIGTERM then unconditionally SIGKILL the child's whole process group.

    `pgid` is the leader pid (the child was started with `start_new_session=True`). After SIGTERM
    we wait a short grace for cooperative shutdown, then SIGKILL the *whole group* regardless of
    whether the leader itself already exited - escalation must depend on the group dying, not on the
    leader being reaped, or a SIGTERM-ignoring grandchild survives. A grandchild that escaped the
    session (`setsid`) cannot be reached here; the bounded drain in `run()` is what keeps such a
    survivor from hanging the engine.
    """
    try:
        os.killpg(pgid, signal.SIGTERM)
    except (ProcessLookupError, PermissionError):
        return  # group already gone
    time.sleep(grace_s)
    try:
        os.killpg(pgid, signal.SIGKILL)
    except (ProcessLookupError, PermissionError):
        return


def _drain(proc: subprocess.Popen[str]) -> tuple[str, str]:
    """Collect remaining output and reap, bounded so a surviving pipe-holder can't block forever."""
    try:
        out, err = proc.communicate(timeout=2)
        return out or "", err or ""
    except subprocess.TimeoutExpired as exc:
        proc.poll()  # non-blocking: reap the killed leader now (a setsid'd survivor keeps the pipe)
        return _as_text(exc.stdout), _as_text(exc.stderr)


def _spawn_os_error(name: str, binary: str, exc: OSError) -> str:
    """Actionable AgentResult.error when Popen cannot start the backend binary."""
    if isinstance(exc, FileNotFoundError) or exc.errno == errno.ENOENT:
        return f"{name}: binary {binary!r} not found on PATH"
    if isinstance(exc, PermissionError) or exc.errno == errno.EACCES:
        return f"{name}: binary {binary!r} not executable (permission denied)"
    if exc.errno == errno.ETXTBSY:
        return f"{name}: binary {binary!r} busy (text file busy; try again later)"
    detail = exc.strerror or str(exc)
    if exc.errno is not None:
        return f"{name}: failed to spawn {binary!r}: [Errno {exc.errno}] {detail}"
    return f"{name}: failed to spawn {binary!r}: {detail}"


def _as_text(value: object) -> str:
    if value is None:
        return ""
    if isinstance(value, bytes):
        return value.decode("utf-8", "replace")
    return str(value)


_AUTH_FAILURE_MARKERS = (
    "auth",
    "unauthoriz",
    "unauthenticated",
    "not logged in",
    "please log in",
    "invalid api key",
    "invalid_api_key",
    "api key",
    "authentication",
    "401",
    "403",
)


def _looks_like_auth_failure(text: str) -> bool:
    lower = text.lower()
    return any(marker in lower for marker in _AUTH_FAILURE_MARKERS)


_AUTH_ALLOWLIST_HINT = (
    "if this backend authenticates via an env API key, confirm that key is on its "
    "credential allowlist and present in the parent environment "
    "(see `marshal doctor` / docs/config.md); CLI login may still be required"
)


def _failure_reason(name: str, exit_code: int, stderr: str, stdout: str = "") -> str:
    """Debuggable reason when parse_output left error empty: exit code + stderr/stdout tail.

    Prefer stderr; fall back to stdout (some CLIs, notably Goose, print provider/config failures
    only on stdout). Prefer lines that look like ``error:`` / ``Error `` when present.
    Auth-shaped failures get an env-allowlist hint so a missing forwarded key is diagnosable.
    """
    tail = _failure_tail(stderr) or _failure_tail(stdout)
    reason = f"{name}: exited with code {exit_code}"
    if tail:
        reason = f"{reason}: {tail}"
    if _looks_like_auth_failure(tail or reason):
        reason = f"{reason} ({_AUTH_ALLOWLIST_HINT})"
    return reason


def _failure_tail(blob: str, limit: int = 500) -> str:
    # Redact before the length cut so a credential cannot straddle the boundary and leak a prefix.
    blob = redact_secrets(blob)
    lines = [ln.strip() for ln in blob.splitlines() if ln.strip()]
    if not lines:
        return ""
    for ln in lines:
        lower = ln.lower()
        if lower.startswith("error:") or lower.startswith("error "):
            return ln[:limit]
    return " ".join(lines[-3:])[:limit]
