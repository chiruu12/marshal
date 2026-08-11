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

from ..core.config import setup_command_refusal
from .env import child_env, redact_secrets
from ..core.ids import MAX_TASK_ID_LEN as MAX_TASK_ID_LEN
from ..core.ids import MAX_WORKTREE_ID_LEN as MAX_WORKTREE_ID_LEN
from ..core.ids import validate_run_id as validate_run_id
from ..core.ids import validate_worktree_id as _validate_worktree_id
from ..core.layout import legacy_worktrees_dir, runs_root


class WorktreeError(RuntimeError):
    """A git worktree operation failed."""


# Cap on the verify-command output kept on a run record (chars, from the END - failures print
# last). The full stdout/stderr of the agent itself is persisted separately by the run-log store.
_VERIFY_OUTPUT_CAP = 4000

# Id rules live in ``ids`` (one implementation). Constants / validate_run_id are re-exported;
# validate_worktree_id wraps ValueError → WorktreeError for this module's callers.

# Full or abbreviated git object id (sha-1 / sha-256 hex). Used to refuse a ref name that a
# failed `git rev-parse` echoed back on stdout posing as a tip sha.
_GIT_OBJECT_ID_RE = re.compile(r"^[0-9a-f]{7,64}$")


def is_git_object_id(value: str) -> bool:
    """True if `value` looks like a git object id (lowercase hex sha, full or abbreviated)."""
    return bool(_GIT_OBJECT_ID_RE.fullmatch(value))


def validate_worktree_id(task_id: str, *, max_len: int = MAX_WORKTREE_ID_LEN) -> str:
    """Return `task_id` if safe; raise ``WorktreeError`` otherwise (wraps ``ids`` ValueError)."""
    try:
        return _validate_worktree_id(task_id, max_len=max_len)
    except ValueError as exc:
        raise WorktreeError(str(exc)) from exc


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
    """Create, inspect, and tear down one isolated checkout per run, under a base directory.

    Each run gets its own **clone** (`git clone --local`), not a linked worktree. The difference is
    the git admin directory: linked worktrees share the main repo's `.git`, so an agent could write
    a hook or a command-executing config key (`core.hooksPath`, `filter.*.smudge`, ...) there and
    have it execute during a LATER, unrelated run - and survive cleanup, because tearing down a
    worktree never rewrites shared git state (#180). A clone has its own `.git`, so there is nothing
    shared to poison - including the object store, which is copied rather than hardlinked so a run
    cannot reach the driver's objects through a shared inode.

    A run's commits are published back into the main repo when they are committed, which is what
    keeps every branch operation here (merge, ancestry, unmerged counts) a plain local-branch
    operation against the driver's repo.

    This is an isolation boundary for git state and for the run's own working tree. It is **not** a
    filesystem sandbox: nothing here stops an agent writing to an absolute path elsewhere on the
    host (#175). Do not describe it as one.
    """

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
        self.base_dir = Path(base_dir) if base_dir is not None else runs_root(self.repo_root)
        # Runs made by an older Marshal live under `<repo>/.marshal/worktrees`. Their records hold
        # absolute paths there, and teardown refuses any path outside a known base - so without this
        # an upgrade would strand every existing run's directory as permanently un-cleanable. Only
        # ever read: nothing new is created here.
        self.legacy_base_dir = legacy_worktrees_dir(self.repo_root) if base_dir is None else None
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
        if path.exists():
            raise WorktreeError(
                f"run directory for {task_id!r} already exists at {path}; clean it before reusing "
                "the id"
            )
        # The DRIVER's repo decides whether this branch name is free, and it is checked before
        # anything is created. A clone would not refuse on its own: `clone` puts the repo's branches
        # under refs/remotes/, so `checkout -b` in the clone would happily shadow a name that is
        # already taken here and the collision would only surface later, at publish. Refusing up
        # front also means the pre-existing branch is never a candidate for cleanup - the run that
        # did not start cannot delete work it did not create.
        if self._git("show-ref", "--verify", "--quiet", f"refs/heads/{branch}").returncode == 0:
            raise WorktreeError(
                f"branch {branch!r} already exists; refusing to reuse it for {task_id!r} - it may "
                "hold unmerged work. Clean the earlier run or choose another task id."
            )
        base = self.resolve_base_branch(base_branch)
        # Pinned to a commit in the DRIVER's repo, because a clone does not have the repo's branches
        # as local ones - they arrive under refs/remotes/ - so a base like another run's
        # `marshal/<id>` branch (how `spawn(base_branch=...)` chains off earlier work) is not a
        # checkoutable name inside the clone. The commit always is. `base` itself stays the name the
        # caller gave, since that is what the run record reports as its base.
        resolved = self._git("rev-parse", "--verify", "--quiet", f"{base}^{{commit}}")
        if resolved.returncode != 0 or not resolved.stdout.strip():
            raise WorktreeError(
                f"base {base!r} for {task_id!r} is not a commit in this repo"
            )
        base_commit = resolved.stdout.strip()

        # `--no-hardlinks` is the security-relevant flag, not an optimisation knob. A plain
        # `--local` clone HARDLINKS the object store, and a hardlink is the same inode: an agent
        # that chmods a loose object in its own clone and writes to it corrupts the DRIVER's object
        # database through the shared inode (verified - `git fsck` on the repo reports the damage
        # afterwards). Copying costs disk proportional to history, which is the price of the run
        # being unable to reach back into the repo at all. `--no-checkout` because the branch is
        # created below; checking out the base first would only be thrown away.
        clone = self._git(
            "clone", "--local", "--no-hardlinks", "--no-checkout", "--quiet",
            str(self.repo_root), str(path),
        )
        if clone.returncode != 0:
            shutil.rmtree(path, ignore_errors=True)  # a partial clone must not look like a run dir
            raise WorktreeError(f"clone failed for {task_id!r}: {clone.stderr.strip()}")

        # The clone carries every branch the repo had, so an id whose branch is already taken fails
        # HERE, inside the run's own clone, and the pre-existing branch is never touched. That is the
        # same refusal a linked `worktree add -b` gave, minus the leaked-branch cleanup it needed:
        # nothing outside this directory was created, so tearing the directory down is the whole
        # rollback.
        wt = Worktree(task_id=task_id, path=path, branch=branch)
        checkout = self._git("checkout", "--quiet", "-b", branch, base_commit, cwd=path)
        if checkout.returncode != 0:
            shutil.rmtree(path, ignore_errors=True)
            raise WorktreeError(
                f"could not start {task_id!r} from {base!r} ({base_commit[:12]}): "
                f"{checkout.stderr.strip()}"
            )

        self._inherit_identity(path)

        if self.integrate_run_hooks:
            _copy_repo_hooks(self.repo_root, path)

        # Published while still empty so the branch exists in the driver's repo from `create`, as it
        # did when this was a linked worktree. Callers ask about it before the run has committed
        # anything - `branch_tip`, `has_unmerged_commits` - and a branch that materialised only on
        # first commit would make those fail rather than answer "no commits yet".
        self._publish(wt, base_commit)
        return wt

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
        # Redact before the tail cut: a credential straddling the cap would otherwise leak a prefix.
        output = redact_secrets(output)
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
            # Nothing of OURS to commit - but the agent may have committed on its own, and those
            # commits live only in the clone until they are published. Returning here without
            # publishing is how self-committed work went missing: the branch existed in the repo,
            # pointing at the base, so the work read as "no changes" rather than as an error.
            self._publish(wt, "self-committed")
            return None
        commit_args = ["commit"]
        if not self.integrate_run_hooks:
            commit_args.append("--no-verify")
        commit_args += ["-m", message]
        commit = self._git(*commit_args, cwd=wt.path)
        if commit.returncode != 0:
            raise WorktreeError(f"commit failed for {wt.task_id!r}: {commit.stderr.strip()}")
        sha = self._git("rev-parse", "HEAD", cwd=wt.path).stdout.strip()
        self._publish(wt, sha)
        return sha

    def _inherit_identity(self, clone_path: Path) -> None:
        """Carry the repo's committer identity into a run's clone.

        A linked worktree read the repo's LOCAL config; a clone does not - `git clone` copies
        objects and refs, not `.git/config`. So a repo that sets `user.email` per-repo (rather than
        globally) produced runs whose every commit failed with "Author identity unknown". That is a
        real setup, not a corner: per-repo identity is how one keeps work and personal commits apart.

        Only the identity is copied, deliberately. Cloning the whole local config would drag along
        keys that name paths in the operator's repo (`core.hooksPath`, `filter.*`), which is exactly
        the shared-execution surface this class exists to stop pointing at.
        """
        for key in ("user.name", "user.email"):
            # Read with the repo as cwd so the answer is the EFFECTIVE value - repo-local if set,
            # otherwise the global the clone would have found anyway. Absent stays absent: git's own
            # "Author identity unknown" is the right error, and inventing one would forge authorship.
            value = self._git("config", "--get", key).stdout.strip()
            if value:
                self._git("config", key, value, cwd=clone_path)

    def publish(self, wt: Worktree) -> None:
        """Make the run's branch in the driver's repo match its clone.

        Called before anything READS that branch. A run's commits - including ones the agent made
        itself, which never pass through `commit_all` - exist only in the clone until this runs.
        """
        self._publish(wt, "publish")

    def _publish(self, wt: Worktree, what: str) -> None:
        """Copy the run's branch out of its clone and into the driver's repo.

        The run works in a clone, so its commits start out reachable from nothing in the main repo -
        while `merge`, `unmerged_commit_count`, `is_ancestor` and the rest all operate there, on a
        plain local branch. Publishing here is what keeps that true: the branch is a normal branch by
        the time anything downstream looks for it, so isolating the run changed where work happens
        and not how it is integrated.

        Fetched from the clone as a plain object transfer. `--no-tags` because a run has no business
        adding tags to the driver's repo. The refspec IS forced, because the clone is authoritative
        for this one branch: an agent that amends or rebases its own commits - normal behaviour -
        leaves a non-fast-forward update, and refusing it would strand real work behind a stale ref.
        Forcing is safe here only because the name was proven free in the driver's repo before the
        run started (see `create`), so this can never overwrite a branch some other run owns.
        """
        _ensure_managed_branch(wt.branch, self.branch_prefix)
        proc = self._git(
            "fetch", "--no-tags", "--quiet", str(wt.path),
            f"+refs/heads/{wt.branch}:refs/heads/{wt.branch}",
        )
        if proc.returncode != 0:
            raise WorktreeError(
                f"could not publish {wt.task_id!r}'s branch ({what[:12]}) to the repo: "
                f"{proc.stderr.strip()}"
            )

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

    def is_ancestor(self, commit: str, target: str) -> bool:
        """Whether `commit` is reachable from `target`. False when either is unknown.

        `git merge-base --is-ancestor` exits 1 for "not an ancestor" and 128 for a bad object, and
        both mean "not reachable" for the caller's purposes.
        """
        return self._git("merge-base", "--is-ancestor", commit, target).returncode == 0

    def any_user_ref_contains(self, commit: str) -> bool:
        """Whether any ref the USER owns still reaches `commit`. Marshal's own are excluded.

        This is what separates "the base was orphaned out from under this run" from "this run was
        deliberately based on another branch": an orphaned base is reachable from no surviving ref,
        while a divergent base is still contained by the branch it was cut from. Reachability from
        the merge target alone cannot tell those apart, and `base_branch` chaining is supported.

        Every ref under ``branch_prefix/`` is skipped, not merely the asking run's own branch. Run
        branches are cut FROM the base, so they contain it by construction - and a fan-out leaves
        siblings sharing one base, so counting them would let any concurrent run silence the
        diagnosis for all of them. That is the case this matters in: rewriting history under a
        1-run fleet is a nuisance, under an 8-run fleet it is eight confusing conflicts.

        Deliberately ref-based, not object-based: the reflog keeps an orphaned commit alive as an
        object long after every ref has moved off it.
        """
        proc = self._git("for-each-ref", "--contains", commit, "--format=%(refname)")
        if proc.returncode != 0:
            return False
        managed = f"refs/heads/{self.branch_prefix}/"
        return any(
            ref.strip() and not ref.strip().startswith(managed)
            for ref in proc.stdout.splitlines()
        )

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

    def _delete_managed_branch(self, branch: str) -> None:
        """Force-delete a Marshal-managed branch; refuse anything outside ``branch_prefix``."""
        _ensure_managed_branch(branch, self.branch_prefix)
        self._git("branch", "-D", branch)

    def _ensure_removable(self, path: Path) -> Path:
        """Require ``path`` to sit under a base dir Marshal owns - the current one or the legacy one.

        Teardown deletes directories, so the containment check is what stops a poisoned or stale
        record from pointing cleanup at the host. Accepting the legacy base as well is what lets a
        repo that ran an older Marshal still clean up the runs it left behind.
        """
        try:
            return _ensure_under_base(path, self.base_dir)
        except WorktreeError:
            if self.legacy_base_dir is None:
                raise
            return _ensure_under_base(path, self.legacy_base_dir)

    def remove(self, wt: Worktree, delete_branch: bool = True) -> None:
        """Reclaim a run's directory (and by default its branch).

        A clone is a self-contained directory, so removing it is `rmtree` and nothing else - there
        is no admin entry in the main repo to unregister, which is precisely the shared state that
        made a worktree teardown able to leave things behind.
        """
        self._ensure_removable(wt.path)
        if wt.path.exists():
            _restore_writable_dirs(wt.path)
            try:
                shutil.rmtree(wt.path)
            except OSError as exc:
                raise WorktreeError(f"could not remove run dir for {wt.task_id!r}: {exc}") from exc
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
        self._ensure_removable(p)
        if p.exists():
            _restore_writable_dirs(p)
            # `ignore_errors` because reclaiming the disk is the whole point: a run dir left
            # half-readable by a killed agent must not strand under an `errors` entry forever.
            shutil.rmtree(p, ignore_errors=True)
        # Still pruned, though a clone registers nothing: a repo that ran an older Marshal can hold
        # admin entries for LINKED worktrees under this same base dir, and this is what reaps them.
        self.prune()
        if branch:
            self._delete_managed_branch(branch)


def _copy_repo_hooks(repo_root: Path, clone_path: Path) -> None:
    """Copy the repo's hooks into a run's clone. Only called when ``integrate_run_hooks`` is set.

    A clone does not inherit hooks, which is mostly the point - but an operator who explicitly opted
    into running their hooks on a run's commit means their hooks, and silently not running them
    would be a gate that reports success without ever having executed. Copied, never linked: the
    run gets its own copy to execute, so an agent editing it cannot reach back into the repo's.
    """
    src = repo_root / ".git" / "hooks"
    dest = clone_path / ".git" / "hooks"
    if not src.is_dir():
        return
    # `symlinks=False` DEREFERENCES: a hook that is a symlink to a file outside the repo would
    # otherwise be recreated as that same absolute link inside a directory the agent can write, so
    # writing "its own" hook would overwrite the operator's real file - and that edit outlives the
    # run. Copying contents means the run gets something it can only damage for itself.
    with contextlib.suppress(OSError):
        shutil.copytree(src, dest, dirs_exist_ok=True, symlinks=False)


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
