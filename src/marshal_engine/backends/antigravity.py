"""Google Antigravity CLI adapter (`agy`).

`agy` is the CLI surface of **Antigravity 2.0** (Google, 19 May 2026), which ships as four
things: the desktop app, this Go CLI (successor to Gemini CLI), an SDK, and a managed agent
service. The CLI versions INDEPENDENTLY of the product generation — "Antigravity 2" is the
platform, `agy --version` reads 1.1.x. Do not conflate them: the version floor below is a CLI
version, and every behaviour noted here was verified against the binary, not the announcement.

Invocation reference (verified against `agy` 1.1.13 / 1.1.14):

    agy [--mode plan | --dangerously-skip-permissions] [--disable-slash-commands]
        [--output-format json] [--print-timeout DURATION] [--add-dir CWD]
        [--model MODEL] [--conversation ID] -p "<PROMPT>"

`agy -p` runs one prompt non-interactively. Run with cwd = the target repo (agy operates on
its launch folder; there is no `--dir` flag).

Honest gaps from research (these shape what we expose):
  * Structured output works (agy ≥ 1.1.8): ``--output-format json`` returns
    ``{response, conversation_id, usage:{input_tokens,output_tokens,cache_read_tokens,...}}``.
    Tokens are stamped; there is NO USD/cost field, so ``source=unavailable`` and
    ``native_usage=False`` (that flag means native cost). ``stream-json`` is also parseable
    (terminal ``event:"result"`` nests the same object under ``result``).
    ``check_available()`` enforces this floor (unparsable / too-old → unavailable) so doctor
    and graceful client-skip never green-light a CLI that will fail every run.
  * Auth is OAuth-first, and since 1.1.13 also unattended via `GEMINI_API_KEY` (set
    `modelProvider: "gemini"` in agy's settings; `GOOGLE_GEMINI_BASE_URL` for a custom
    endpoint). `ANTIGRAVITY_API_KEY` remains an unconfirmed upstream request.
    There IS now a cheap authenticated-only probe: print-mode slash commands (agy >= 1.1.11)
    are answered by the CLI without starting an agent turn or spending quota. `account_info()`
    uses `-p "/usage" --output-format json`, VERIFIED to return `status=SUCCESS` with
    `usage.total_tokens == 0`, so `verifies_auth()` is True and doctor fails closed on a
    logged-out CLI instead of green-lighting a fan-out that dies on its first real run.
    It also carries quota headroom (weekly / 5-hour percentages) — for a backend with no USD
    to report, remaining quota is the only cost signal there is.
  * `--print-timeout` is agy's OWN print-mode deadline and it defaults to **5 minutes**, which
    silently truncated every Marshal run configured for longer — the run died at 5m with
    `error: "timeout waiting for response"` no matter what `timeout_s` said. It is now derived
    from the run's timeout and set just INSIDE it (see `_print_timeout`), so agy returns a
    parseable envelope with token counts instead of being hard-killed by the external timeout.
  * PTY: the older "agy checks for a TTY, so stdout can be swallowed while the exit code stays
    0" note is NOT reproducible on 1.1.13/1.1.14. Every headless run here goes out under
    `DETACHED_STDIO` (no TTY, stdin `/dev/null`) and the JSON envelope arrives intact, including
    end-to-end through `marshal run`. The PTY-wrapper TODO is dropped; re-open it with a
    reproduction rather than on suspicion.
  * JSON envelopes carry ``conversation_id`` (stamped onto ``session_id``). ``--conversation``
    is still passed through when the caller already has an id; resume is not first-class yet.
  * `read-only` maps to `--mode plan` (agy >= 1.1.12, which fixed `--mode` being ignored in
    headless `-p`). VERIFIED 2026-08-19 on 1.1.13: a run told to create a file returned
    `status=SUCCESS` in 6.9s with the directory still EMPTY - it plans instead of writing, and
    does not block on the "Proceed" affordance its own response text mentions. This is what
    makes Antigravity usable as a read-only reviewer in adversarial teams.
    KNOWN EDGE (verified end-to-end through `marshal run`): plan mode treats a *file write* and
    a *shell command* differently. A write is answered with a plan and `status=SUCCESS`; a shell
    command is HARD-DENIED and the run exits non-zero (`permission check failed for command
    ... user denied permission to run command`). So the tier genuinely binds - that denial is
    the guarantee working - but a reviewer prompt that reaches for `grep`/`pytest` fails the
    run rather than degrading. Prompt read-only Antigravity reviewers to read files and not to
    run commands.
  * SLASH HIJACK: print mode parses a prompt beginning with `/` as a CLI slash command, so the
    agent never runs — `-p "/usage ..."` returns `status=ERROR`, `num_turns: 0`. `_compose_prompt`
    passes `task.goal` through verbatim, so any goal starting with `/` hit this. safe-edit and
    yolo now pass `--disable-slash-commands`, verified to fix it (the same prompt then replies
    normally).
    READ-ONLY CANNOT USE THAT FLAG. agy warns `--mode plan has no effect while slash command
    expansion is disabled`, and it means it: with both flags the write was blocked only by the
    DEFAULT mode's unattended denial, not by the plan tier. That is a permission tier that does
    not bind — the exact thing MIN_AGY_VERSION was raised to prevent — so read-only keeps
    `--mode plan` and instead REFUSES a slash-leading prompt up front, in `build_invocation`,
    before a worktree is spent. A leading space does not escape the parse (verified).
  * `safe-edit` stays on `--dangerously-skip-permissions`, and this is now a TESTED REJECTION
    rather than an untried idea. `--mode accept-edits` looks like the tighter mapping. It is not
    usable headless: probed WITH a real `trustedWorkspaces` entry, the agent reached for a shell
    command (`echo "HELLO" > PROOF.md`), accept-edits denies shell with nobody to approve, and
    the run exited 1 having written nothing. Probed WITHOUT the trust entry it did the opposite -
    wrote the file and still returned `status=ERROR` ("not a valid artifact path"), a run that
    succeeded on disk and reads as failed. Neither outcome is a permission tier. Do not re-litigate
    this without new upstream behaviour; the agent's freedom to choose shell over the edit tool is
    the blocker, not the flag.
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

Verified-and-declined, so nobody re-probes them:
  * `--sandbox` ("terminal restrictions") is NOT a filesystem boundary. Under it the agent still
    created `/tmp/agy-escape-probe.md`, outside the worktree entirely. It buys Marshal nothing
    the worktree does not already provide, and claiming a sandbox we do not have would be worse
    than claiming none (see issue #175: the worktree is a git-branch boundary, not a jail).
  * `agy models --output-format json` DOES NOT EXIST despite the 1.1.12 changelog announcing it:
    the subcommand's parser rejects the flag (`flags provided but not defined: -output-format`,
    exit 1). This is the same class of upstream drift the ZCode adapter documents - help text and
    release notes advertising flags the binary refuses - so the tab-splitting text parse stays.
    Do not "upgrade" it to JSON without re-probing the actual binary.
  * `--json-schema` works: the envelope gains a `structured_output` dict that really does match
    the schema. Deliberately NOT wired up. Marshal's `output_schema` is a backend-agnostic
    contract in `orchestration/structured.py` (prompt instruction, then extract + validate from
    the final message); routing one backend around it would fork a cross-cutting contract for a
    single adapter's convenience. The existing path already covers agy.

Model ids carry an effort suffix (agy 1.1.13). A BARE family id is REJECTED:
`--model gemini-3.5-flash` fails with `invalid model selection ... requires --effort
(available: low, medium, high)`, so pin the suffixed id. Marshal does not synthesise an
`--effort` value — which effort to spend is the caller's call, not a default worth guessing.
"""

from __future__ import annotations

import contextlib
import fcntl
import json
import os
import re
import shutil
import subprocess
import sys
import tempfile
import threading
from collections.abc import Iterator
from pathlib import Path
from typing import Any, ClassVar

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

#: Where the agy CLI keeps its user settings (incl. `trustedWorkspaces`). An attribute on the
#: backend so tests can point it at a temp file instead of the real home.
DEFAULT_SETTINGS_PATH = Path.home() / ".gemini" / "antigravity-cli" / "settings.json"

#: Minimum ``agy`` this adapter will drive. Availability fails closed below it so doctor /
#: client-skip never treat a too-old CLI as runnable.
#:
#: 1.1.8 brought ``--output-format json``; the floor is 1.1.12 because that release fixed
#: ``--mode`` being SILENTLY IGNORED in headless ``-p``. That makes it a safety floor, not a
#: feature floor: on an older CLI a ``read-only`` run would drop back to the default mode and
#: could write. A permission tier that does not bind is worse than one that is unavailable.
MIN_AGY_VERSION: tuple[int, int, int] = (1, 1, 12)

#: Static fallback when ``agy models`` cannot be probed. Captured from a live ``agy models`` on
#: 1.1.13 — every id here is one the CLI accepts verbatim.
#:
#: These MUST stay suffixed. The previous list held bare family names (``gemini-3.5-flash``,
#: ``claude-sonnet-4.6``) which the CLI now rejects outright, so the fallback path handed every
#: caller a model id guaranteed to fail the run. Note ``claude-sonnet-4-6``: dashes, not dots.
_STATIC_MODELS: tuple[str, ...] = (
    "gemini-3.7-flash-high",
    "gemini-3.7-flash-medium",
    "gemini-3.7-flash-low",
    "gemini-3.5-flash-high",
    "gemini-3.5-flash-medium",
    "gemini-3.5-flash-low",
    "gemini-3.1-pro-high",
    "gemini-3.1-pro-low",
    "claude-sonnet-4-6",
    "claude-opus-4-6-thinking",
    "gpt-oss-120b-medium",
)

_DEFAULT_UNAVAILABLE = "CLI not on PATH / not runnable"


class AntigravityBackend(CodingAgentBackend):
    name = "antigravity"
    binary = "agy"
    credential_env_vars = ("ANTIGRAVITY_API_KEY", "GEMINI_API_KEY")
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
    _trust_added: ClassVar[dict[str, list[object]]] = {}
    #: Per-thread record of which cwds this run claimed (see `_claimed_cwds`).
    _thread_claims = threading.local()
    settings_path = DEFAULT_SETTINGS_PATH
    capabilities = Capabilities(
        json_output=True,  # --output-format json (agy >= 1.1.8; see MIN_AGY_VERSION)
        native_usage=False,  # tokens yes; no USD in CLI output — stay honest
        permission_modes=frozenset(
            {PermissionMode.READ_ONLY, PermissionMode.SAFE_EDIT, PermissionMode.YOLO}
        ),
        permission_fidelity=PermissionFidelity.BOUNDARY_ONLY,
    )

    # read-only is a real tier here: `--mode plan` produces a plan and writes nothing (verified;
    # see module docstring). safe-edit and yolo still share skip-permissions — the default preset
    # prompts (which deadlocks headless), and `--mode accept-edits` is not yet trustworthy
    # headless (it wrote the file and still reported ERROR). Do not collapse read-only into
    # those two: it is the mode that lets Antigravity join review teams without write access.
    _PERMISSION: ClassVar[dict[PermissionMode, list[str]]] = {
        PermissionMode.READ_ONLY: ["--mode", "plan"],
        PermissionMode.SAFE_EDIT: ["--dangerously-skip-permissions"],
        PermissionMode.YOLO: ["--dangerously-skip-permissions"],
    }

    # --- hooks ---------------------------------------------------------------------------

    def check_available(self) -> bool:
        """True only when ``agy`` is on PATH and meets ``MIN_AGY_VERSION``.

        A presence-only probe would green-light a CLI that ignores ``--mode`` (so a read-only
        run could write) or predates ``--output-format json`` (so every run fails to parse).
        Unparsable ``--version`` output fails closed.
        """
        return self._probe_availability()[0]

    def unavailable_detail(self) -> str:
        """Doctor detail: names the version floor when the CLI is present but unusable."""
        return self._probe_availability()[1]

    def _probe_availability(self) -> tuple[bool, str]:
        if shutil.which(self.binary) is None:
            return False, _DEFAULT_UNAVAILABLE
        try:
            proc = subprocess.run(
                [self.binary, "--version"],
                capture_output=True,
                text=True,
                timeout=15,
                check=False,
                **DETACHED_STDIO,
            )
        except (OSError, subprocess.SubprocessError):
            return False, _DEFAULT_UNAVAILABLE
        if proc.returncode != 0:
            return False, _DEFAULT_UNAVAILABLE
        raw = f"{proc.stdout or ''}\n{proc.stderr or ''}"
        ver = _parse_agy_version(raw)
        floor = _fmt_agy_version(MIN_AGY_VERSION)
        if ver is None:
            return False, (
                f"agy version unparsable (need ≥ {floor} for --mode and --output-format json)"
            )
        if ver < MIN_AGY_VERSION:
            return False, (
                f"agy {_fmt_agy_version(ver)} too old "
                f"(need ≥ {floor} for --mode and --output-format json)"
            )
        return True, ""

    def account_info(self) -> dict[str, str] | None:
        """Authenticated-only probe via print-mode ``/usage`` (agy >= 1.1.11). Never raises.

        ``agy -p "/usage" --output-format json`` is answered by the CLI itself: no agent turn,
        no quota, ``usage.total_tokens == 0`` — but it still needs credentials, which is what
        makes it an auth gate rather than another PATH check. The returned ``plan`` string
        carries weekly quota headroom, the closest thing this backend has to a cost signal.
        """
        if shutil.which(self.binary) is None:
            return None
        try:
            proc = subprocess.run(
                [self.binary, "-p", "/usage", "--output-format", "json"],
                capture_output=True,
                text=True,
                timeout=30,
                check=False,
                **DETACHED_STDIO,
            )
        except (OSError, subprocess.SubprocessError):
            return None
        if proc.returncode != 0:
            return None
        return _parse_agy_usage_command(proc.stdout or "")

    def verifies_auth(self) -> bool:
        # True since the print-mode /usage probe landed: a None from account_info() now really
        # does mean "not authenticated (or the probe failed)", so doctor may fail closed on it.
        return True

    def available_models(self) -> ModelCatalog:
        """Model ids from ``agy models``, falling back to the curated list.

        Rows are ``id<TAB>Human Label``; only the id half is a model. See
        ``_parse_agy_models`` for why keeping the whole line was a live bug.
        """
        return self._probe_models(
            [self.binary, "models"],
            _parse_agy_models,
            _STATIC_MODELS,
        )

    def build_invocation(self, task: TaskSpec, opts: RunOpts) -> list[str]:
        prompt = self._compose_prompt(task)
        argv = [self.binary]
        argv += self.map_permission(opts.permission)
        if opts.permission is PermissionMode.READ_ONLY:
            # `--disable-slash-commands` would silently un-bind `--mode plan`, so read-only pays
            # for its enforcement by refusing the prompts the flag would have protected.
            _refuse_slash_leading_prompt(prompt)
        else:
            argv += ["--disable-slash-commands"]
        # Structured envelope: response text + per-run token usage (no USD).
        argv += ["--output-format", "json"]
        # agy's own print-mode deadline. Left at its 5-minute default it silently capped every
        # longer run, so the engine's timeout never meant what it said.
        argv += ["--print-timeout", _print_timeout(opts.timeout_s)]
        # Add the worktree to the active workspace; paired with the trust entry prepare() writes,
        # this makes edits land in cwd instead of agy's scratch dir.
        argv += ["--add-dir", str(opts.cwd)]
        if opts.model:
            # Long form only: agy dropped the `-m` short alias by 1.1.13, and its Go flag parser
            # rejects an unknown flag by printing usage and exiting non-zero — so a model-pinned
            # run failed with the CLI's help text instead of ever reaching the model.
            argv += ["--model", opts.model]
        if opts.session_id:
            argv += ["--conversation", opts.session_id]
        # -p must come last with the prompt as its trailing argument.
        argv += ["-p", prompt]
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
        if opts.permission is PermissionMode.READ_ONLY:
            # A plan-mode run cannot write, so it has nothing to gain from a trust entry — and a
            # read-only fan-out is exactly where mutating a HOST-GLOBAL settings file is least
            # welcome. `--add-dir` (still passed) is what lets it read the worktree; trust only
            # governs writes. `release_trust()` carries the symmetric guard — see the note there
            # for why the per-thread claim map is not enough on its own.
            return
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
        if opts.permission is PermissionMode.READ_ONLY:
            # Symmetric with `prepare()`: a read-only run never took a claim, so it must never
            # drop one. The per-thread claim map alone does NOT provide this, because it is keyed
            # by cwd rather than by run - a read-only run sharing a thread and a cwd with a
            # write run would release the WRITE run's claim and revoke trust underneath it,
            # silently redirecting that run's edits to agy's scratch dir.
            return
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


#: Seconds shaved off the run timeout when setting agy's own ``--print-timeout``.
#:
#: Small and fixed: the point is only to order the two deadlines, not to reserve real work time.
#: When agy hits its deadline first it returns a JSON envelope — status, error, and the token
#: counts spent so far — where Marshal's external kill leaves a signalled process and no usage.
#: Landing the same second is a coin flip, so we take the cheap side of it.
_PRINT_TIMEOUT_GRACE_S = 5


def _print_timeout(timeout_s: int) -> str:
    """Run timeout -> the Go duration for ``--print-timeout``, set just inside ours. Pure.

    Floored at 1s so a very short timeout still produces a valid duration rather than ``0s``
    (which agy reads as "no deadline") or a negative one.
    """
    return f"{max(1, timeout_s - _PRINT_TIMEOUT_GRACE_S)}s"


def _refuse_slash_leading_prompt(prompt: str) -> None:
    """Raise if a read-only prompt would be eaten by agy's slash-command parser. Pure.

    Fails in ``build_invocation`` — before a worktree is created — because the alternative is a
    run that reports ERROR with ``num_turns: 0`` and looks like an agent failure rather than a
    prompt that was never delivered. Leading whitespace does not escape the parse (verified), so
    it is stripped before the check.
    """
    if not prompt.lstrip().startswith("/"):
        return
    raise ValueError(
        "antigravity: a read-only prompt must not begin with '/' — agy parses it as a CLI slash "
        "command and the agent never runs (status=ERROR, num_turns=0). The usual fix, "
        "--disable-slash-commands, is unavailable here: agy warns '--mode plan has no effect "
        "while slash command expansion is disabled', which would leave the read-only tier "
        "unenforced. Reword the goal so it does not start with '/', or run it as safe-edit."
    )


def _parse_agy_models(stdout: str) -> list[str]:
    """``agy models`` stdout -> model ids. Pure.

    Each row is ``id<TAB>Human Label``. The label is not part of the id, and passing the joined
    line back as a model makes the CLI reject the run with ``invalid model selection`` — so a
    driver that copied an id straight out of ``list_models`` got a guaranteed failure. Take the
    first tab-separated field only, and drop the CLI's progress line if it ever moves to stdout
    (it is on stderr as of 1.1.13, but a model id ending in an ellipsis is not a real id).
    """
    ids: list[str] = []
    for line in stdout.splitlines():
        candidate = line.split("\t", 1)[0].strip()
        if candidate and not candidate.endswith("..."):
            ids.append(candidate)
    return ids


def _parse_agy_usage_command(stdout: str) -> dict[str, str] | None:
    """``/usage`` envelope -> ``{"plan": ...}``, or None when it does not prove authentication.

    Response rows are tab-separated ``group, limit, remaining, resets_at``. Only the weekly rows
    are summarised — the five-hour window refills on its own and is noise on a doctor line. Any
    shape we do not recognise returns None, and doctor reads that as "not authenticated (or the
    probe failed)": a false negative costs a re-run, a false OK costs a whole fan-out. Pure.
    """
    envelope = _extract_agy_envelope(stdout)
    if envelope is None or envelope.get("status") != "SUCCESS":
        return None
    response = envelope.get("response")
    if not isinstance(response, str) or not response.strip():
        return None
    weekly = []
    for line in response.splitlines():
        parts = [part.strip() for part in line.split("\t")]
        if len(parts) >= 3 and "weekly" in parts[1].lower() and parts[0] and parts[2]:
            weekly.append(f"{parts[0]} {parts[2]}")
    if not weekly:
        # Authenticated (the CLI answered) but the row shape drifted. Say so plainly rather than
        # inventing a quota figure — an unparsed row is not evidence of headroom.
        return {"plan": "logged-in"}
    return {"plan": "logged-in (weekly quota left: " + ", ".join(weekly) + ")"}


def _parse_agy_version(raw: str) -> tuple[int, int, int] | None:
    """Extract ``(major, minor, patch)`` from ``agy --version`` output. Pure; None if unparsable."""
    m = re.search(r"(\d+)\.(\d+)(?:\.(\d+))?", raw)
    if m is None:
        return None
    return (int(m.group(1)), int(m.group(2)), int(m.group(3) or 0))


def _fmt_agy_version(ver: tuple[int, int, int]) -> str:
    return f"{ver[0]}.{ver[1]}.{ver[2]}"


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
