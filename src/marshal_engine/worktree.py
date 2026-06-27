"""Git worktree lifecycle for isolated parallel agent runs.

Each task runs in its own worktree + branch so the fleet works in parallel without branch
collisions, and the main branch stays untouched until an explicit integrate step. This is the
safety boundary of the whole system - keep it boring and reliable.
"""

from __future__ import annotations

import os
import subprocess
from pathlib import Path

from pydantic import BaseModel

from .env import child_env


class WorktreeError(RuntimeError):
    """A git worktree operation failed."""


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
    ) -> None:
        self.repo_root = Path(repo_root)
        self.base_dir = (
            Path(base_dir) if base_dir is not None else self.repo_root / ".marshal" / "worktrees"
        )
        self.branch_prefix = branch_prefix
        self.git_timeout_s = git_timeout_s
        # Optional command run in each fresh worktree right after `git worktree add` (e.g. provision a
        # venv). None = skip. See _run_setup for why a failure tears the worktree down and raises.
        self.setup_cmd = setup_cmd
        self.setup_timeout_s = setup_timeout_s

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

    def create(self, task_id: str, base_branch: str | None = None) -> Worktree:
        """Add a worktree for `task_id` on a fresh `<prefix>/<task_id>` branch."""
        branch = f"{self.branch_prefix}/{task_id}"
        path = self.base_dir / task_id
        self.base_dir.mkdir(parents=True, exist_ok=True)
        proc = self._git("worktree", "add", "-b", branch, str(path), base_branch or "HEAD")
        if proc.returncode != 0:
            raise WorktreeError(f"worktree add failed for {task_id!r}: {proc.stderr.strip()}")
        wt = Worktree(task_id=task_id, path=path, branch=branch)
        if self.setup_cmd:
            self._run_setup(wt)
        return wt

    def _run_setup(self, wt: Worktree) -> None:
        """Run the configured setup command inside a fresh worktree (e.g. ``uv sync``).

        Runs with the driver's VIRTUAL_ENV scrubbed (so `uv sync` provisions the worktree's own
        `.venv`, not the driver's), stdin closed, and a hard timeout - the same headless guards as
        agent runs. A non-zero exit (or missing binary / timeout) tears the half-made worktree back
        down and raises: a half-provisioned worktree would have the agent run against a broken or
        stale environment, so fail fast at create time rather than hand it a trap.
        """
        assert self.setup_cmd is not None  # guarded by the caller
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
            reason = "" if proc.returncode == 0 else _setup_reason(proc.returncode, proc.stderr, proc.stdout)
        except subprocess.TimeoutExpired:
            reason = f"timed out after {self.setup_timeout_s}s"
        except FileNotFoundError:
            reason = f"command not found: {self.setup_cmd[0]!r}"
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
        the new commit sha, or None if the worktree was clean (nothing to commit). Hooks are
        skipped (`--no-verify`) since a prompting hook would deadlock a headless run.
        """
        add = self._git("add", "-A", cwd=wt.path)
        if add.returncode != 0:
            raise WorktreeError(f"add failed for {wt.task_id!r}: {add.stderr.strip()}")
        if self._git("diff", "--cached", "--quiet", cwd=wt.path).returncode == 0:
            return None  # nothing staged -> nothing to commit
        commit = self._git("commit", "--no-verify", "-m", message, cwd=wt.path)
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

    def has_unmerged_commits(self, branch: str, target: str) -> bool:
        """True if `branch` has commits not reachable from `target` (work awaiting merge)."""
        proc = self._git("rev-list", "--count", f"{target}..{branch}")
        if proc.returncode != 0:
            return False
        return proc.stdout.strip() not in ("", "0")

    def branch_tip(self, branch: str) -> str:
        """The commit sha at the tip of `branch`."""
        return self._git("rev-parse", branch).stdout.strip()

    def merged_diff_files(self, branch: str, target: str) -> list[str]:
        """Files `branch` brings into `target` - the three-dot (merge-base) delta.

        Three-dot `target...branch` diffs from the merge-base, so it lists only what `branch`
        actually changed, not files the target modified independently (two-dot would over-report
        those - they don't land from this run).
        """
        proc = self._git("diff", "--name-only", "-z", f"{target}...{branch}")
        return [f for f in proc.stdout.split("\0") if f]

    def merge(self, branch: str, *, message: str | None = None) -> MergeResult:
        """Merge `branch` into the repo's current branch.

        Three failure shapes are distinguished: a content conflict (abort + report files, repo
        left clean); a *blocked* merge that git refused to start because the target working tree
        is dirty/colliding (no changes made -> MergeResult.blocked); any other failure raises.
        """
        args = ["merge", "--no-edit", "--no-verify"]  # --no-verify: headless, never run prompting hooks
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

    def remove(self, wt: Worktree, delete_branch: bool = True) -> None:
        proc = self._git("worktree", "remove", "--force", str(wt.path))
        if proc.returncode != 0:
            raise WorktreeError(f"worktree remove failed for {wt.task_id!r}: {proc.stderr.strip()}")
        if delete_branch and wt.branch:
            self._git("branch", "-D", wt.branch)

    def prune(self) -> None:
        """Clean up administrative files for worktrees whose directories are gone."""
        self._git("worktree", "prune")


def _setup_reason(exit_code: int, stderr: str, stdout: str) -> str:
    """A debuggable reason for a failed setup command: exit code + a short output tail."""
    tail = " ".join((stderr or stdout).strip().splitlines()[-3:])
    base = f"exited with code {exit_code}"
    return f"{base}: {tail}" if tail else base


def _from_porcelain(entry: dict[str, str]) -> Worktree:
    path = Path(entry["worktree"])
    branch = entry.get("branch", "").removeprefix("refs/heads/")
    return Worktree(task_id=path.name, path=path, branch=branch)
