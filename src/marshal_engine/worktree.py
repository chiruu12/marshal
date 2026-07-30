"""Git worktree lifecycle for isolated parallel agent runs.

Each task runs in its own worktree + branch so the fleet works in parallel without branch
collisions, and the main branch stays untouched until an explicit integrate step. This is the
safety boundary of the whole system - keep it boring and reliable.
"""

from __future__ import annotations

import contextlib
import os
import re
import shutil
import signal
import subprocess
import time
from collections.abc import Callable
from pathlib import Path

from pydantic import BaseModel

from .config import setup_command_refusal
from .env import child_env
from .layout import worktrees_dir


class WorktreeError(RuntimeError):
    """A git worktree operation failed."""


# Cap on the verify-command output kept on a run record (chars, from the END - failures print
# last). The full stdout/stderr of the agent itself is persisted separately by the run-log store.
_VERIFY_OUTPUT_CAP = 4000

# Fail-closed id rules for worktree directory names / run ids / driver task_ids.
# Charset keeps workflow `hex.label` and backend-shaped segments (`command-code`) valid;
# leading `.`/`-`, separators, unicode, and over-length ids are rejected (never rewritten).
# task_id (grouping key) is capped tighter so composed run_id `task.backend.<uuid8>` fits
# under MAX_WORKTREE_ID_LEN (backends today ≤ ~12 chars).
MAX_WORKTREE_ID_LEN = 128
MAX_TASK_ID_LEN = 64
_SAFE_ID_RE = re.compile(r"^[A-Za-z0-9][A-Za-z0-9._-]*$")

# Full or abbreviated git object id (sha-1 / sha-256 hex). Used to refuse a ref name that a
# failed `git rev-parse` echoed back on stdout posing as a tip sha.
_GIT_OBJECT_ID_RE = re.compile(r"^[0-9a-f]{7,64}$")


def is_git_object_id(value: str) -> bool:
    """True if `value` looks like a git object id (lowercase hex sha, full or abbreviated)."""
    return bool(_GIT_OBJECT_ID_RE.fullmatch(value))


def _unsafe_id_reason(kind: str, value: str, *, max_len: int) -> str | None:
    """The refusal reason if `value` is not a safe flat path segment, else ``None``.

    ``kind`` is the id's role in the message ("worktree id" / "run_id") so each caller keeps
    its own wording while the RULES live in exactly one place.
    """
    if not value:
        return f"unsafe {kind}: empty"
    if len(value) > max_len:
        return f"unsafe {kind}: {value!r} exceeds max length {max_len}"
    if value in (".", "..") or not _SAFE_ID_RE.fullmatch(value):
        return (
            f"unsafe {kind}: {value!r} "
            f"(must match {_SAFE_ID_RE.pattern}, no leading '.' or '-')"
        )
    return None


def validate_worktree_id(task_id: str, *, max_len: int = MAX_WORKTREE_ID_LEN) -> str:
    """Return `task_id` if it is a safe flat path segment; raise ``WorktreeError`` otherwise.

    Allowed: ``[A-Za-z0-9._-]``, must start with alphanumeric, length 1..``max_len``.
    Rejects empty / ``.`` / ``..`` / leading ``.`` or ``-`` / slashes / spaces / unicode.
    Fail closed — never sanitize-rewrites the input.
    """
    reason = _unsafe_id_reason("worktree id", task_id, max_len=max_len)
    if reason is not None:
        raise WorktreeError(reason)
    return task_id


def validate_run_id(run_id: str) -> str:
    """Return `run_id` if it is a safe flat path segment; raise ``ValueError`` otherwise.

    A run_id becomes the ledger filename (``runs/<run_id>.json``) and the log filename
    (``logs/<run_id>.log``), and the workspace registry stats it against every registered
    repo's ledger to find its owner - so an unvalidated id is both a path-traversal read
    and a cross-workspace tenant escape. Same fail-closed rules as ``validate_worktree_id``
    (a production run_id is ``task.backend.<uuid8>``, which fits by construction), but
    raises ``ValueError``: the input-validation error type of the state/MCP boundary,
    matching how an invalid ``task_id`` surfaces there (see ``TaskSpec``).
    """
    reason = _unsafe_id_reason("run_id", run_id, max_len=MAX_WORKTREE_ID_LEN)
    if reason is not None:
        raise ValueError(reason)
    return run_id


def _ensure_under_base(path: Path, base_dir: Path) -> Path:
    """Resolve `path` and require a strict descendant of `base_dir` (symlink-aware).

    Equality with ``base_dir`` is refused: ``discard(base_dir)`` must not rmtree the shared
    worktrees root (and every sibling worktree under it).
    """
    resolved = path.resolve()
    base = base_dir.resolve()
    if resolved == base or not resolved.is_relative_to(base):
        raise WorktreeError(
            f"worktree path {str(resolved)!r} is outside base dir {str(base)!r}"
        )
    return resolved


def _ensure_managed_branch(branch: str, branch_prefix: str) -> str:
    """Return `branch` if it is under ``branch_prefix/``; raise ``WorktreeError`` otherwise.

    Symmetrical with ``_ensure_under_base``: create always names branches
    ``{prefix}/{task_id}``, so ``git branch -D`` must refuse anything else. A poisoned run
    record (``branch: main``) must not delete the operator's branch on clean.
    """
    prefix = f"{branch_prefix}/"
    if not branch.startswith(prefix) or branch == prefix:
        raise WorktreeError(
            f"branch {branch!r} is outside managed prefix {branch_prefix!r}"
        )
    return branch


def _restore_writable_dirs(root: Path) -> None:
    """Add owner-write on directories under ``root`` so remove/rmtree can unlink entries.

    Read-only *files* can still be unlinked from a writable directory; directories without the
    write bit cannot, which strands the worktree when ``ignore_errors`` cleanup reports success.
    Applied to the whole tree (not a ``.marshal-context`` special case) so any read-only content
    is reclaimable.
    """
    if not root.exists():
        return
    for dirpath, _dirnames, _filenames in os.walk(root):
        try:
            mode = os.stat(dirpath).st_mode
            if mode & 0o200 == 0:
                os.chmod(dirpath, mode | 0o200)
        except OSError:
            continue


class Worktree(BaseModel):
    task_id: str
    path: Path
    branch: str


class MergeResult(BaseModel):
    """Outcome of merging a worktree branch back into the current branch."""

    ok: bool
    conflicts: list[str] = []
    message: str = ""
    blocked: bool = False  # merge could not start (dirty/colliding target); nothing was changed


class WorktreeManager:
    """Create, inspect, and tear down git worktrees under a base directory."""

    def __init__(
        self,
        repo_root: Path | str,
        base_dir: Path | str | None = None,
        branch_prefix: str = "marshal",
        git_timeout_s: int = 120,
        setup_cmd: list[str] | None = None,
        setup_timeout_s: int = 600,
        verify_cmd: list[str] | None = None,
        allow_unsafe_commands: bool = False,
        integrate_run_hooks: bool = False,
    ) -> None:
        self.repo_root = Path(repo_root)
        self.base_dir = Path(base_dir) if base_dir is not None else worktrees_dir(self.repo_root)
        self.branch_prefix = branch_prefix
        self.git_timeout_s = git_timeout_s
        # Optional command run in each fresh worktree right after `git worktree add` (e.g. provision a
        # venv). None = skip. See setup() for why a failure tears the worktree down and raises.
        self.setup_cmd = setup_cmd
        self.setup_timeout_s = setup_timeout_s
        # Optional gate command run in the worktree after a run that would otherwise succeed (e.g.
        # the repo's full test suite). None = skip. See verify() - a failure never tears down.
        # Post-agent: cwd content may be agent-authored (allowlist is not a sandbox).
        self.verify_cmd = verify_cmd
        # When false, setup/verify refuse non-allowlisted basenames / relative path argv[0]
        # (see config.setup_command_refusal). Basename screen only — not a sandbox for args.
        self.allow_unsafe_commands = allow_unsafe_commands
        # When false (default), commit/merge pass --no-verify so prompting hooks cannot deadlock
        # headless integrate and so agent-touched hook scripts are not executed. True = run hooks;
        # only for known non-interactive hooks with trusted provenance.
        self.integrate_run_hooks = integrate_run_hooks
        # Fail closed at construction so a refused setup/verify never reaches `git worktree add`
        # (load_config already rejects the same static error for YAML). Runtime checks remain.
        for label, cmd in (("setup_cmd", setup_cmd), ("verify_cmd", verify_cmd)):
            if not cmd:
                continue
            refused = setup_command_refusal(cmd, allow_unsafe=allow_unsafe_commands)
            if refused:
                raise WorktreeError(f"{label}: {refused}")

    def git_read(self, *args: str) -> subprocess.CompletedProcess[str]:
        """Run a READ-ONLY git command on the repo through the same guarded wrapper as everything else.

        The public seam for callers outside this module (``MarshalService.diff_range``) that need a
        guarded git read: closed stdin, hard timeout, ``GIT_TERMINAL_PROMPT=0``. Reaching for the
        private ``_git`` across two composition layers worked, but it made a rename here a silent
        breakage there that no type-checker would catch. Nothing about this method enforces
        read-only-ness - it is the caller's contract, which is why the name says so.
        """
        return self._git(*args)

    def _git(self, *args: str, cwd: Path | None = None) -> subprocess.CompletedProcess[str]:
        # These git calls run on the driver's checkout (commit/merge/status), so they get the same
        # headless guards as agent runs: stdin closed + a hard timeout so a credential/lock/hook
        # prompt fails fast instead of hanging the driver. GIT_TERMINAL_PROMPT=0 turns an auth
        # prompt into an error rather than a wait.
        try:
            return subprocess.run(
                ["git", "-C", str(cwd or self.repo_root), *args],
                capture_output=True,
                text=True,
                stdin=subprocess.DEVNULL,
                # LC_ALL=C keeps git's messages in English so stderr matching (e.g. the
                # blocked-merge detection in merge()) is stable across locales.
                env={**os.environ, "GIT_TERMINAL_PROMPT": "0", "LC_ALL": "C"},
                timeout=self.git_timeout_s,
            )
        except subprocess.TimeoutExpired as exc:
            raise WorktreeError(
                f"git {' '.join(args)!r} timed out after {self.git_timeout_s}s"
            ) from exc

    def resolve_base_branch(self, base_branch: str | None) -> str:
        """The ref recorded for a run: caller's branch name, or HEAD resolved when omitted."""
        if base_branch is not None:
            return base_branch
        proc = self._git("rev-parse", "--abbrev-ref", "HEAD")
        if proc.returncode != 0:
            raise WorktreeError(f"could not resolve base branch: {proc.stderr.strip()}")
        ref = proc.stdout.strip()
        if ref != "HEAD":
            return ref
        return self._git("rev-parse", "HEAD").stdout.strip()

    def create(self, task_id: str, base_branch: str | None = None) -> Worktree:
        """Add a worktree for `task_id` on a fresh `<prefix>/<task_id>` branch (git op only).

        Provisioning (the optional ``setup_cmd``, e.g. ``uv sync``) is a SEPARATE step - call
        ``setup(wt)`` afterwards. The fleet serializes only this git op (it's milliseconds) and runs
        ``setup()`` OUTSIDE that lock, so a parallel fan-out provisions worktrees concurrently
        instead of one-at-a-time behind the lock.

        ``task_id`` is validated (charset + length) and the resolved path is required to stay under
        ``base_dir`` *before* any git op — Marshal-owned, not git-accidental.
        """
        task_id = validate_worktree_id(task_id)
        branch = f"{self.branch_prefix}/{task_id}"
        path = self.base_dir / task_id
        _ensure_under_base(path, self.base_dir)
        self.base_dir.mkdir(parents=True, exist_ok=True)
        # Fast-path probe: skip leaked-branch cleanup when the ref already existed. Not
        # authoritative alone (TOCTOU: another process can create the same name between probe
        # and add) — the add's own stderr is the source of truth below.
        branch_existed = (
            self._git("show-ref", "--verify", "--quiet", f"refs/heads/{branch}").returncode == 0
        )
        proc = self._git("worktree", "add", "-b", branch, str(path), base_branch or "HEAD")
        if proc.returncode != 0:
            # NEVER delete a branch this add attempt did not create. Git's atomic decision at
            # add time is authoritative: "already exists" means the branch is foreign (or left
            # by an earlier attempt) — deleting it is the data-loss vector. The show-ref probe
            # is only a fast-path for the pre-existing case.
            add_out = f"{proc.stderr}\n{proc.stdout}"
            already_exists = "already exists" in add_out
            if not branch_existed and not already_exists:
                # Best-effort only: never let cleanup mask the original add failure (e.g. a
                # TimeoutExpired from `branch -D` surfacing as WorktreeError).
                with contextlib.suppress(Exception):
                    self._delete_managed_branch(branch)
            raise WorktreeError(f"worktree add failed for {task_id!r}: {proc.stderr.strip()}")
        return Worktree(task_id=task_id, path=path, branch=branch)

    def setup(
        self,
        wt: Worktree,
        *,
        on_pid: Callable[[int], None] | None = None,
        on_exit: Callable[[], None] | None = None,
    ) -> None:
        """Provision a fresh worktree by running the configured ``setup_cmd`` (no-op if unset).

        Runs with the driver's VIRTUAL_ENV scrubbed (so `uv sync` provisions the worktree's own
        `.venv`, not the driver's), stdin closed, and a hard timeout - the same headless guards as
        agent runs. The child is started in its own process group (``start_new_session``) so a
        timeout — or ``cancel_run`` via ``on_pid`` — can ``killpg`` the whole tree. ``on_pid`` /
        ``on_exit`` mirror the agent-run hooks: publish the pid for cancel, then mark it reaped.
        Safe to run concurrently across worktrees (each is a distinct dir), so the fleet calls it
        OUTSIDE the create lock and a fan-out provisions in parallel. A non-zero exit (or missing
        binary / timeout) tears the half-made worktree back down and raises: a half-provisioned
        worktree would have the agent run against a broken or stale environment, so fail fast
        rather than hand it a trap.
        """
        if not self.setup_cmd:
            return
        refused = setup_command_refusal(self.setup_cmd, allow_unsafe=self.allow_unsafe_commands)
        if refused:
            reason = refused
        elif on_pid is not None or on_exit is not None:
            # Cancel-aware path: own process group + pid hooks so cancel_run can killpg.
            reason = self._run_setup_cmd(wt, on_pid=on_pid, on_exit=on_exit)
        else:
            # Default path keeps subprocess.run (tests/monkeypatches target it; no cancel hooks).
            reason = self._run_setup_cmd_simple(wt)
        if reason:
            # Best-effort teardown so a failed setup doesn't strand an orphan worktree (and a retry
            # can reuse the task_id); never let teardown mask the original setup failure.
            try:
                self.remove(wt)
            except WorktreeError:
                pass
            raise WorktreeError(
                f"worktree setup {self.setup_cmd!r} failed for {wt.task_id!r}: {reason}"
            )

    def _run_setup_cmd_simple(self, wt: Worktree) -> str:
        """Run ``setup_cmd`` via ``subprocess.run`` (no cancel hooks)."""
        assert self.setup_cmd is not None
        try:
            proc = subprocess.run(
                self.setup_cmd,
                cwd=str(wt.path),
                capture_output=True,
                text=True,
                stdin=subprocess.DEVNULL,
                env=child_env(),
                timeout=self.setup_timeout_s,
            )
            return (
                ""
                if proc.returncode == 0
                else _setup_reason(proc.returncode, proc.stderr, proc.stdout)
            )
        except subprocess.TimeoutExpired:
            return f"timed out after {self.setup_timeout_s}s"
        except FileNotFoundError:
            return f"command not found: {self.setup_cmd[0]!r}"
        except (OSError, subprocess.SubprocessError) as exc:
            # Match verify(): a generic OSError (EACCES on the binary, etc.) must become a
            # WorktreeError with teardown, not escape as a raw crash that strands the worktree.
            return f"could not run {self.setup_cmd[0]!r}: {exc}"

    def _run_setup_cmd(
        self,
        wt: Worktree,
        *,
        on_pid: Callable[[int], None] | None,
        on_exit: Callable[[], None] | None,
    ) -> str:
        """Run ``setup_cmd`` in its own process group with optional cancel hooks."""
        assert self.setup_cmd is not None
        try:
            proc = subprocess.Popen(
                self.setup_cmd,
                cwd=str(wt.path),
                stdout=subprocess.PIPE,
                stderr=subprocess.PIPE,
                text=True,
                stdin=subprocess.DEVNULL,
                env=child_env(),
                start_new_session=True,
            )
        except FileNotFoundError:
            return f"command not found: {self.setup_cmd[0]!r}"
        except OSError as exc:
            return f"could not run {self.setup_cmd[0]!r}: {exc}"

        if on_pid is not None:
            try:
                on_pid(proc.pid)
            except Exception:  # noqa: BLE001 - never leak the process over a pid-record failure
                pass
        pgid = proc.pid
        out, err = "", ""
        try:
            out, err = proc.communicate(timeout=self.setup_timeout_s)
        except subprocess.TimeoutExpired:
            _kill_setup_process_group(pgid)
            try:
                out, err = proc.communicate(timeout=2)
            except subprocess.TimeoutExpired:
                proc.poll()
                out, err = "", ""
            return f"timed out after {self.setup_timeout_s}s"
        finally:
            if on_exit is not None:
                with contextlib.suppress(Exception):
                    on_exit()
        if proc.returncode == 0:
            return ""
        return _setup_reason(proc.returncode, err, out)

    def verify(self, wt: Worktree) -> tuple[bool, str]:
        """Run the configured ``verify_cmd`` in the worktree; ``(ok, tail-truncated output)``.

        The post-run counterpart of ``setup()``: same headless guards (VIRTUAL_ENV scrubbed, stdin
        closed, hard timeout - reusing ``setup_timeout_s``), but it NEVER raises and NEVER tears
        the worktree down - a failed gate still holds reviewable work, and the diff must survive
        for the driver to inspect. Cwd content may be agent-authored (allowlist ≠ sandbox).
        ``(True, "")`` when no command is configured.
        """
        if not self.verify_cmd:
            return True, ""
        refused = setup_command_refusal(self.verify_cmd, allow_unsafe=self.allow_unsafe_commands)
        if refused:
            return False, f"verify refused: {refused}"
        try:
            proc = subprocess.run(
                self.verify_cmd,
                cwd=str(wt.path),
                capture_output=True,
                text=True,
                stdin=subprocess.DEVNULL,
                env=child_env(),
                timeout=self.setup_timeout_s,
            )
        except subprocess.TimeoutExpired:
            return False, f"verify timed out after {self.setup_timeout_s}s"
        except (OSError, subprocess.SubprocessError) as exc:
            return False, f"verify could not run {self.verify_cmd[0]!r}: {exc}"
        output = "\n".join(part for part in (proc.stdout, proc.stderr) if part).strip()
        # Failures print last: keep the TAIL, where test runners put the summary.
        if len(output) > _VERIFY_OUTPUT_CAP:
            output = "..." + output[-_VERIFY_OUTPUT_CAP:]
        if proc.returncode == 0:
            return True, output
        return False, f"verify exited {proc.returncode}\n{output}".strip()

    def changed_files(self, wt: Worktree) -> list[str]:
        """Paths changed inside the worktree (uncommitted).

        Uses `git status --porcelain -z` so paths are emitted verbatim and NUL-delimited - names
        with spaces or non-ASCII are returned as-is, not C-quoted (`"my file.txt"`). With `-z` a
        rename/copy emits the new path in the status record followed by the old path as its own
        NUL field, which is skipped.
        """
        proc = self._git("status", "--porcelain", "-z", cwd=wt.path)
        if proc.returncode != 0:
            raise WorktreeError(f"status failed for {wt.task_id!r}: {proc.stderr.strip()}")
        tokens = proc.stdout.split("\0")
        files: list[str] = []
        i = 0
        while i < len(tokens):
            tok = tokens[i]
            if not tok:
                i += 1
                continue
            status, path = tok[:2], tok[3:]
            if path:
                files.append(path)
            i += 2 if status and status[0] in ("R", "C") else 1  # rename/copy: skip the old-path field
        return files

    def diff(self, wt: Worktree) -> str:
        """Unified diff of all uncommitted work in the worktree, including new files.

        `git diff HEAD` alone misses untracked files an agent created - the common case - so
        those are appended as against-/dev/null diffs. Read-only: the index is not modified.
        """
        parts: list[str] = []
        tracked = self._git("diff", "HEAD", cwd=wt.path)
        if tracked.returncode != 0:
            raise WorktreeError(f"diff failed for {wt.task_id!r}: {tracked.stderr.strip()}")
        if tracked.stdout:
            parts.append(tracked.stdout)
        listing = self._git("ls-files", "--others", "--exclude-standard", "-z", cwd=wt.path)
        for path in listing.stdout.split("\0"):
            if not path:
                continue
            # `git diff --no-index` exits 1 when files differ (always, vs /dev/null) - not an error.
            added = self._git("diff", "--no-index", "--", "/dev/null", path, cwd=wt.path)
            parts.append(added.stdout)
        return "".join(parts)

    def commit_all(self, wt: Worktree, message: str) -> str | None:
        """Stage and commit everything in the worktree onto its branch.

        Agents leave their work uncommitted; integrating it means committing it first. Returns
        the new commit sha, or None if the worktree was clean (nothing to commit). By default
        hooks are skipped (`--no-verify`) so a prompting hook cannot deadlock a headless run and
        so possibly agent-modified hook scripts are not executed; set
        ``integrate_run_hooks=True`` only for non-interactive hooks with trusted provenance.
        """
        add = self._git("add", "-A", cwd=wt.path)
        if add.returncode != 0:
            raise WorktreeError(f"add failed for {wt.task_id!r}: {add.stderr.strip()}")
        if self._git("diff", "--cached", "--quiet", cwd=wt.path).returncode == 0:
            return None  # nothing staged -> nothing to commit
        commit_args = ["commit"]
        if not self.integrate_run_hooks:
            commit_args.append("--no-verify")
        commit_args += ["-m", message]
        commit = self._git(*commit_args, cwd=wt.path)
        if commit.returncode != 0:
            raise WorktreeError(f"commit failed for {wt.task_id!r}: {commit.stderr.strip()}")
        return self._git("rev-parse", "HEAD", cwd=wt.path).stdout.strip()

    def current_branch(self) -> str:
        """The branch currently checked out in the main repo (the merge target).

        Raises on a detached HEAD: merging into a non-branch would leave the merge commit
        reachable from no branch (orphaned on the next checkout), so integrate must refuse.
        """
        proc = self._git("rev-parse", "--abbrev-ref", "HEAD")
        if proc.returncode != 0:
            raise WorktreeError(f"could not resolve current branch: {proc.stderr.strip()}")
        branch = proc.stdout.strip()
        if branch == "HEAD":
            raise WorktreeError("repo is in detached HEAD; check out a branch before integrating")
        return branch

    def unmerged_commit_count(self, branch: str, target: str) -> int:
        """How many commits `branch` has that are not reachable from `target`.

        Raises on a git error rather than returning 0: callers use this to decide whether work
        exists, and a silent 0 would report "empty" (and drop committed work) when we actually
        cannot tell.
        """
        proc = self._git("rev-list", "--count", f"{target}..{branch}")
        if proc.returncode != 0:
            raise WorktreeError(
                f"could not count unmerged commits {target}..{branch}: {proc.stderr.strip()}"
            )
        raw = proc.stdout.strip()
        return int(raw) if raw else 0

    def has_unmerged_commits(self, branch: str, target: str) -> bool:
        """True if `branch` has commits not reachable from `target` (work awaiting merge)."""
        return self.unmerged_commit_count(branch, target) > 0

    def branch_tip(self, branch: str) -> str:
        """The commit sha at the tip of `branch`.

        Raises on a git error rather than returning the ref name: a failed ``rev-parse`` echoes
        its argument on stdout (exit 128), which would poison ``base_commit`` and the then-chain
        no-diff check (both sides equal the branch name → false "produced no diff").
        """
        proc = self._git("rev-parse", branch)
        if proc.returncode != 0:
            # Match merged_diff_files: a git failure must not masquerade as a successful tip.
            raise WorktreeError(
                f"could not resolve tip of {branch!r}: {proc.stderr.strip()}"
            )
        tip = proc.stdout.strip()
        if not is_git_object_id(tip):
            raise WorktreeError(f"tip of {branch!r} is not a commit sha: {tip!r}")
        return tip

    def merged_diff_files(self, branch: str, target: str) -> list[str]:
        """Files `branch` brings into `target` - the three-dot (merge-base) delta.

        Three-dot `target...branch` diffs from the merge-base, so it lists only what `branch`
        actually changed, not files the target modified independently (two-dot would over-report
        those - they don't land from this run).
        """
        proc = self._git("diff", "--name-only", "-z", f"{target}...{branch}")
        if proc.returncode != 0:
            # Match merged_diff: a git failure must not silently report changed_files=[] for a
            # merged run (integrate would then claim nothing landed).
            raise WorktreeError(
                f"could not list files for {target}...{branch}: {proc.stderr.strip()}"
            )
        return [f for f in proc.stdout.split("\0") if f]

    def merged_diff(self, branch: str, target: str) -> str:
        """Unified diff of commits on `branch` since the merge-base with `target` (three-dot)."""
        proc = self._git("diff", f"{target}...{branch}")
        if proc.returncode != 0:
            raise WorktreeError(
                f"could not diff {target}...{branch}: {proc.stderr.strip()}"
            )
        return proc.stdout

    def merge(self, branch: str, *, message: str | None = None) -> MergeResult:
        """Merge `branch` into the repo's current branch.

        Three failure shapes are distinguished: a content conflict (abort + report files, repo
        left clean); a *blocked* merge that git refused to start because the target working tree
        is dirty/colliding (no changes made -> MergeResult.blocked); any other failure raises.
        """
        # Default --no-verify: headless, never run prompting hooks. Opt in via integrate_run_hooks.
        args = ["merge", "--no-edit"]
        if not self.integrate_run_hooks:
            args.append("--no-verify")
        if message is not None:
            args += ["-m", message]
        args.append(branch)
        proc = self._git(*args)
        if proc.returncode == 0:
            return MergeResult(ok=True, message=proc.stdout.strip())
        conflicts = self._conflicted_files()
        if conflicts:
            self._abort_merge(branch)
            return MergeResult(ok=False, conflicts=conflicts, message=proc.stdout.strip())
        stderr = proc.stderr.strip()
        if self._merge_in_progress():
            # git started a merge it couldn't finish (no content conflict): abort so the repo is
            # left clean, and report blocked (recoverable on retry) rather than raising mid-merge.
            self._abort_merge(branch)
            return MergeResult(ok=False, blocked=True, message=stderr or proc.stdout.strip())
        if "overwritten by merge" in stderr or "Aborting" in stderr:
            # git refused before starting (dirty/colliding target). No merge state to abort.
            return MergeResult(ok=False, blocked=True, message=stderr)
        raise WorktreeError(f"merge of {branch!r} failed: {stderr or proc.stdout.strip()}")

    def _abort_merge(self, branch: str) -> None:
        """Abort an in-progress merge and verify the repo is clean again.

        `git merge --abort` can itself fail (a held index.lock, or a `_git` timeout). If it does,
        the checkout is left mid-merge - so we raise a hard error rather than let the caller report
        a clean, recoverable result over a dirty repo.
        """
        ab = self._git("merge", "--abort")
        if ab.returncode != 0 or self._merge_in_progress():
            raise WorktreeError(
                f"merge of {branch!r} left mid-merge; abort failed: "
                f"{ab.stderr.strip() or 'still in progress'}"
            )

    def _merge_in_progress(self) -> bool:
        return self._git("rev-parse", "-q", "--verify", "MERGE_HEAD").returncode == 0

    def _conflicted_files(self) -> list[str]:
        # -z: verbatim, NUL-delimited paths (no C-quoting of spaces/non-ASCII names).
        proc = self._git("diff", "--name-only", "--diff-filter=U", "-z")
        return [f for f in proc.stdout.split("\0") if f]

    def list(self) -> list[Worktree]:
        """All worktrees known to the repo (includes the main checkout)."""
        proc = self._git("worktree", "list", "--porcelain")
        worktrees: list[Worktree] = []
        current: dict[str, str] = {}
        for line in proc.stdout.splitlines():
            if not line.strip():
                if current.get("worktree"):
                    worktrees.append(_from_porcelain(current))
                current = {}
                continue
            key, _, val = line.partition(" ")
            current[key] = val
        if current.get("worktree"):
            worktrees.append(_from_porcelain(current))
        return worktrees

    def _delete_managed_branch(self, branch: str) -> None:
        """Force-delete a Marshal-managed branch; refuse anything outside ``branch_prefix``."""
        _ensure_managed_branch(branch, self.branch_prefix)
        self._git("branch", "-D", branch)

    def remove(self, wt: Worktree, delete_branch: bool = True) -> None:
        _ensure_under_base(wt.path, self.base_dir)
        _restore_writable_dirs(wt.path)
        proc = self._git("worktree", "remove", "--force", str(wt.path))
        if proc.returncode != 0:
            raise WorktreeError(f"worktree remove failed for {wt.task_id!r}: {proc.stderr.strip()}")
        if delete_branch and wt.branch:
            self._delete_managed_branch(wt.branch)

    def prune(self) -> None:
        """Clean up administrative files for worktrees whose directories are gone."""
        self._git("worktree", "prune")

    def discard(self, path: Path | str, branch: str | None) -> None:
        """Tear down a finished run's worktree + branch, tolerant of a half-gone worktree.

        Unlike `remove` (which needs a live worktree), this handles batch cleanup of a worktree in
        any state: already gone (manually deleted / a prior partial clean), live, or *corrupt* (the
        dir survives but git's admin entry was pruned, so `git worktree remove` refuses with "not a
        working tree"). Reclaiming the disk is the whole point, so when git won't remove a still-
        present dir we fall back to a best-effort `rmtree`. Then `prune` the admin files and delete
        the branch (git `-D` failures ignored - already gone, or checked out in a live worktree).
        The immutable usage ledger and the run-state record are NOT touched here.

        Paths outside ``base_dir`` are refused before any remove/rmtree (poisoned state must not
        delete host directories). A branch outside ``branch_prefix/`` is refused *after* the
        worktree dir is reclaimed so cleanup is not stranded — only the `-D` is skipped.
        Owner-write is restored on directories first so read-only trees (e.g. provisioned
        ``read_paths`` at 0o555) cannot strand the worktree.
        """
        p = Path(path)
        _ensure_under_base(p, self.base_dir)
        if p.exists():
            _restore_writable_dirs(p)
            rm = self._git("worktree", "remove", "--force", str(p))
            if rm.returncode != 0 and p.exists():
                # git refused (corrupt/missing admin entry); the dir is now just a plain directory -
                # reclaim it directly so the disk isn't stranded under an `errors` entry forever.
                shutil.rmtree(p, ignore_errors=True)
        self.prune()
        if branch:
            self._delete_managed_branch(branch)


def _kill_setup_process_group(pgid: int, grace_s: float = 0.5) -> None:
    """SIGTERM then SIGKILL the setup child's process group (same shape as agent timeout kills)."""
    try:
        os.killpg(pgid, signal.SIGTERM)
    except (ProcessLookupError, PermissionError):
        return
    time.sleep(grace_s)
    try:
        os.killpg(pgid, signal.SIGKILL)
    except (ProcessLookupError, PermissionError):
        return


def _setup_reason(exit_code: int, stderr: str, stdout: str) -> str:
    """A debuggable reason for a failed setup command: exit code + a short output tail."""
    tail = " ".join((stderr or stdout).strip().splitlines()[-3:])
    base = f"exited with code {exit_code}"
    return f"{base}: {tail}" if tail else base


def _from_porcelain(entry: dict[str, str]) -> Worktree:
    path = Path(entry["worktree"])
    branch = entry.get("branch", "").removeprefix("refs/heads/")
    return Worktree(task_id=path.name, path=path, branch=branch)
