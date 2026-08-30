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
from pathlib import Path
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
    #: The curated model ids this adapter falls back to when its CLI cannot be probed - the same
    #: list handed to ``_probe_models``. Declared on the class, not just as a module constant, so
    #: ``marshal drift`` can compare what Marshal *ships* against what the CLI still *offers*: a
    #: fallback naming an id the CLI has dropped is a degrade path that guarantees a failed run.
    static_models: ClassVar[tuple[str, ...]] = ()
    #: The CLI version line this adapter was last verified against, verbatim as the binary prints
    #: it. ``None`` means no baseline was recorded, which ``marshal drift`` reports as such rather
    #: than treating as agreement. This is a maintenance fact, never a floor: a version floor is
    #: enforced in ``check_available`` (see Antigravity's ``MIN_AGY_VERSION``), whereas this only
    #: says "the build a human last watched work".
    verified_version: ClassVar[str | None] = None

    # --- hooks subclasses must implement -------------------------------------------------

    def available_for_client(self, client_env: dict[str, str] | None = None) -> bool:
        """Availability for ONE client, whose ``env:`` block may be what names the launcher.

        Default delegates to ``check_available()``, which is the right answer for every backend
        whose CLI is a normal PATH executable. ZCode overrides it: a client's ``ZCODE_BIN`` can be
        the only thing pointing at the install, and a probe that resolves a DIFFERENT launcher
        than ``build_invocation`` would is how a perfectly runnable client gets skipped as
        "unavailable" - which defeats the point of ``resolve_launcher`` existing at all.

        Kept separate from ``check_available`` rather than added to it as a parameter: that hook
        is widely overridden (every adapter and every test double may define it), and widening its
        signature would break each one for a need only one backend has.
        """
        return self.check_available()

    def check_available(self) -> bool:
        """Return True if ``binary`` is on PATH and responds to ``--version``.

        This is a presence probe only - it does not verify authentication or pin a minimum
        version. Backends that require a version floor (e.g. Antigravity, see its MIN_AGY_VERSION)
        override this so availability matches what the adapter will actually invoke.
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

    def resolve_launcher(self, client_env: dict[str, str] | None = None) -> list[str]:
        """Argv prefix that launches this backend's CLI - what ``build_invocation`` starts with.

        Default is the single PATH executable ``binary``, which is every backend whose CLI is
        installed as a normal command. Backends whose entry point is not a PATH executable
        override this: ZCode ships its headless CLI as a Node bundle *inside the desktop app*, so
        its prefix is ``["node", ".../zcode.cjs"]``. Overriding here (rather than open-coding the
        path in ``build_invocation``) keeps argv construction and availability probing agreeing on
        one answer. ``client_env`` is the per-client ``env:`` block, for adapters that let a
        client point at an explicit install.
        """
        return [self.binary]

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

        The `source` is the point. `PROBED` means the CLI answered just now; `STATIC` means no
        live probe was attempted and this is a curated list that may name a model the account
        cannot actually run; `PROBE_FAILED` means a probe ran and failed, so the same curated
        list is returned but tagged as unverified; `UNAVAILABLE` means there is nothing to
        report. Returning a bare list conflated live and curated answers, so a fallback printed
        by a backend that was not even installed was indistinguishable from a live answer.

        Implementations must be side-effect-light and never raise: degrade to `STATIC` /
        `PROBE_FAILED`, or to the default `UNAVAILABLE`, on any failure (missing binary,
        unauthenticated, unparseable output).

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

        Missing binary → curated list tagged ``STATIC`` (never asked). Any failure after the
        binary is found → same list tagged ``PROBE_FAILED`` (asked, no answer). Callers such as
        drift need that split; ``list_models`` still sees a non-live curated list either way.
        ``parse`` yielding nothing counts as a failure: a CLI whose output shape changed upstream
        would otherwise report an empty live catalogue, which reads as "this backend has no models".
        """
        if shutil.which(self.binary) is None:
            return ModelCatalog(models=list(static), source=ModelSource.STATIC)
        failed = ModelCatalog(models=list(static), source=ModelSource.PROBE_FAILED)
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
            return failed
        if proc.returncode != 0:
            return failed
        try:
            models = parse(proc.stdout or "")
        except Exception:  # noqa: BLE001 - a parser fault must not take the listing down
            models = []
        if not models:
            return failed
        return ModelCatalog(models=models, source=ModelSource.PROBED)

    def probe_version(self) -> str | None:
        """The installed CLI's own version line, or None when it is missing or not runnable.

        Deliberately returns the line *verbatim* rather than a parsed tuple. This feeds drift
        detection, where any change is worth surfacing - a build hash moving under an unchanged
        version number is exactly the kind of upstream shift that has broken adapters before, and
        parsing to a number would discard it.

        Side-effect-light and never raises, like every other probe on this class.
        """
        argv = self._version_argv()
        if argv is None:
            return None
        try:
            proc = subprocess.run(
                argv,
                capture_output=True,
                text=True,
                timeout=20.0,
                check=False,
                **DETACHED_STDIO,
            )
        except (OSError, subprocess.SubprocessError):
            return None
        if proc.returncode != 0:
            return None
        text = (proc.stdout or proc.stderr or "").strip()
        return text.splitlines()[0].strip() if text else None

    def _version_argv(self) -> list[str] | None:
        """Argv that asks this CLI its version, or None when it is not installed.

        Split out so a backend whose entry point is not a PATH executable can redirect the probe
        without restating how the answer is read. ZCode is the one that needs it: it resolves a
        launcher from env vars and app-bundle paths, and a probe that resolved a DIFFERENT
        launcher than the invocation is the exact defect ``resolve_launcher`` exists to prevent.
        """
        return None if shutil.which(self.binary) is None else [self.binary, "--version"]

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
            out, err = _wait_for_child(proc, opts)
        except subprocess.TimeoutExpired:
            _kill_process_group(pgid)
            out, err, drained = _drain(proc)  # bounded: a pipe-holder that escaped can't hang us
            # Ask whether the kill worked instead of assuming it. `_kill_process_group` swallows
            # every signalling error, so it can return having achieved nothing - a leader wedged in
            # uninterruptible I/O rides out SIGKILL, and a `killpg` refused with EPERM never
            # escalates at all.
            #
            # Two DIFFERENT things can outlive the kill, and they are not interchangeable. The
            # leader is one, and it is the one worth recording: its pid is on the record, so every
            # guard downstream can re-probe it and stop refusing once it is gone. A descendant that
            # left the group with its own `setsid` is the other, and all we ever learn about it is
            # that the pipes did not close - no pid, so nothing to probe and nothing that could
            # ever clear a block. Recording that as the same fact would strand the run's work
            # behind a refusal with no way out, so it is disclosed in the error instead and the
            # driver decides.
            survived = proc.poll() is None
            escaped = not drained
            if not survived:
                # Only claim the pid is recyclable when it actually is. Saying so over a live agent
                # tells `cancel_run` the child already exited, so it declines to signal - retiring
                # the one control left over a process Marshal just failed to stop.
                _notify_exit()
            return AgentResult(
                status=RunStatus.TIMED_OUT,
                error=_timeout_error(
                    self.name, opts.timeout_s, survived=survived, escaped=escaped
                ),
                session_id=opts.session_id,
                usage=self._recover_partial_usage(out, err),
                raw_stdout=out,
                raw_stderr=err,
                duration_ms=_elapsed_ms(),
                agent_survived_kill=survived,
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


def _newest_mtime(root: Path) -> float:
    """Newest mtime anywhere under ``root``, or 0.0 when nothing can be read.

    Skips ``.git``: git writes index/lock files on its own schedule, so counting them would
    report progress for a run that has done nothing. Errors are swallowed - a progress probe
    must never be able to end a run by failing.
    """
    newest = 0.0
    stack = [root]
    while stack:
        try:
            with os.scandir(stack.pop()) as entries:
                for entry in entries:
                    try:
                        if entry.is_dir(follow_symlinks=False):
                            if entry.name != ".git":
                                stack.append(Path(entry.path))
                            continue
                        newest = max(newest, entry.stat(follow_symlinks=False).st_mtime)
                    except OSError:
                        continue
        except OSError:
            continue
    return newest


def _wait_for_child(proc: subprocess.Popen[str], opts: RunOpts) -> tuple[str, str]:
    """Wait for the child, ending it early if it stalls and extending it while it works.

    Without a policy this is exactly ``communicate(timeout=opts.timeout_s)`` - the previous
    behaviour, byte for byte. `communicate` is kept in both paths deliberately: it owns the
    concurrent draining of stdout and stderr, and replacing it with hand-rolled incremental
    reads is precisely where a pipe-buffer deadlock comes from. Repeated calls are safe here
    because stdin is DEVNULL, so no input is ever pending between slices.

    Raises ``TimeoutExpired`` for the caller to handle, exactly as before, in both the stalled
    and the ceiling cases - a run that is ended for lack of progress IS a timeout, and gets the
    same kill, the same status and the same partial-usage recovery.
    """
    policy = opts.progress
    if policy is None or not policy.enabled:
        return proc.communicate(timeout=opts.timeout_s)

    hard_ceiling = policy.hard_ceiling_s or opts.timeout_s
    soft_deadline = policy.soft_deadline_s or opts.timeout_s
    started = time.monotonic()
    last_progress = started
    seen_mtime = _newest_mtime(opts.cwd)

    while True:
        elapsed = time.monotonic() - started
        remaining_hard = hard_ceiling - elapsed
        if remaining_hard <= 0:
            raise subprocess.TimeoutExpired(proc.args, hard_ceiling)
        # Never sleep past the ceiling, and never past the next progress measurement.
        slice_s = min(policy.poll_interval_s, remaining_hard)
        # Below the soft deadline there is nothing to extend, so wake no more often than needed.
        if elapsed < soft_deadline:
            slice_s = min(slice_s, max(soft_deadline - elapsed, 1.0), policy.poll_interval_s)
        try:
            return proc.communicate(timeout=slice_s)
        except subprocess.TimeoutExpired:
            pass
        mtime = _newest_mtime(opts.cwd)
        if mtime > seen_mtime:
            seen_mtime = mtime
            last_progress = time.monotonic()
        if time.monotonic() - last_progress >= policy.stall_s:
            # Nothing has changed under the worktree for `stall_s`. Ending now returns the
            # failure sooner and hands the rest of the cap back, instead of waiting out a clock
            # that was never evidence either way.
            raise subprocess.TimeoutExpired(proc.args, policy.stall_s)


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


_DRAIN_TIMEOUT_S = 2.0


def _timeout_error(name: str, timeout_s: float, *, survived: bool, escaped: bool) -> str:
    """The error for a timed-out run, saying what survived the kill.

    A driver's next move differs completely. If nothing survived, the worktree is a stable snapshot
    and the usual choices apply - collect it, re-run with a longer timeout. Otherwise the diff is a
    moving target and integrating it commits a half-written tree. Reporting every case as "timed
    out after 600s" hands the driver the first plan for the second situation.

    The two survivors are reported differently because only one of them can be waited out. Marshal
    holds the leader's pid, so a driver (and every guard downstream) can watch it go. An escaped
    descendant left no pid at all - the sole trace is pipes that never closed - so nothing can
    report when it stops, and saying so plainly is better than a refusal that would never lift.
    """
    base = f"{name}: timed out after {timeout_s}s"
    if survived:
        return (
            f"{base}, and the agent did NOT stop: SIGTERM then SIGKILL to its process group left "
            "it running. It may still be writing to the worktree, so its diff is not a snapshot - "
            "do not integrate or clean this run on the strength of the timeout alone. Stop the "
            "process yourself (its pid is on the run record) and re-check before acting on the "
            "worktree."
        )
    if escaped:
        return (
            f"{base}. The agent itself is gone, but something it started had left the process "
            "group (its own setsid) and was still holding the run's output pipes, so the group "
            "kill never reached it. Marshal has no pid for it and cannot tell you when it stops. "
            "It is usually a server or helper rather than an editor, but check the worktree is "
            "quiet before integrating this run."
        )
    return base


def _drain(proc: subprocess.Popen[str]) -> tuple[str, str, bool]:
    """Collect remaining output and reap, bounded so a surviving pipe-holder can't block forever.

    The third value says whether the pipes actually closed. Hitting the bound is not just a
    give-up: the pipes are inherited by every descendant, so something is still holding the write
    end - a process that escaped the group via its own ``setsid`` and cannot be signalled through
    the group at all. That is the one signal ``run()`` has that a writer outlived the kill even
    though the leader it can poll is gone, so it is reported rather than swallowed.
    """
    try:
        out, err = proc.communicate(timeout=_DRAIN_TIMEOUT_S)
        return out or "", err or "", True
    except subprocess.TimeoutExpired as exc:
        proc.poll()  # non-blocking: reap the killed leader now (a setsid'd survivor keeps the pipe)
        return _as_text(exc.stdout), _as_text(exc.stderr), False


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
        if lower.startswith(("error:", "error ")):
            return ln[:limit]
    return " ".join(lines[-3:])[:limit]
