"""Git worktree lifecycle for isolated parallel agent runs.

Each task runs in its own worktree + branch so the fleet works in parallel without branch
collisions, and the main branch stays untouched until an explicit integrate step. This is the
safety boundary of the whole system — keep it boring and reliable.
"""

from __future__ import annotations

import subprocess
from dataclasses import dataclass
from pathlib import Path


class WorktreeError(RuntimeError):
    """A git worktree operation failed."""


@dataclass
class Worktree:
    task_id: str
    path: Path
    branch: str


class WorktreeManager:
    """Create, inspect, and tear down git worktrees under a base directory."""

    def __init__(
        self,
        repo_root: Path | str,
        base_dir: Path | str | None = None,
        branch_prefix: str = "marshal",
    ) -> None:
        self.repo_root = Path(repo_root)
        self.base_dir = (
            Path(base_dir) if base_dir is not None else self.repo_root / ".marshal" / "worktrees"
        )
        self.branch_prefix = branch_prefix

    def _git(self, *args: str, cwd: Path | None = None) -> subprocess.CompletedProcess[str]:
        return subprocess.run(
            ["git", "-C", str(cwd or self.repo_root), *args],
            capture_output=True,
            text=True,
        )

    def create(self, task_id: str, base_branch: str | None = None) -> Worktree:
        """Add a worktree for `task_id` on a fresh `<prefix>/<task_id>` branch."""
        branch = f"{self.branch_prefix}/{task_id}"
        path = self.base_dir / task_id
        self.base_dir.mkdir(parents=True, exist_ok=True)
        proc = self._git("worktree", "add", "-b", branch, str(path), base_branch or "HEAD")
        if proc.returncode != 0:
            raise WorktreeError(f"worktree add failed for {task_id!r}: {proc.stderr.strip()}")
        return Worktree(task_id=task_id, path=path, branch=branch)

    def changed_files(self, wt: Worktree) -> list[str]:
        """Paths changed inside the worktree (uncommitted), via `git status --porcelain`."""
        proc = self._git("status", "--porcelain", cwd=wt.path)
        if proc.returncode != 0:
            raise WorktreeError(f"status failed for {wt.task_id!r}: {proc.stderr.strip()}")
        files: list[str] = []
        for line in proc.stdout.splitlines():
            entry = line[3:].strip() if len(line) > 3 else line.strip()
            if not entry:
                continue
            if " -> " in entry:  # renames: "old -> new"
                entry = entry.split(" -> ", 1)[1]
            files.append(entry)
        return files

    def diff(self, wt: Worktree) -> str:
        """Unified diff of all uncommitted work in the worktree, including new files.

        `git diff HEAD` alone misses untracked files an agent created — the common case — so
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
            # `git diff --no-index` exits 1 when files differ (always, vs /dev/null) — not an error.
            added = self._git("diff", "--no-index", "--", "/dev/null", path, cwd=wt.path)
            parts.append(added.stdout)
        return "".join(parts)

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


def _from_porcelain(entry: dict[str, str]) -> Worktree:
    path = Path(entry["worktree"])
    branch = entry.get("branch", "").removeprefix("refs/heads/")
    return Worktree(task_id=path.name, path=path, branch=branch)
