"""Integration tests for WorktreeManager against a real temporary git repo."""

from __future__ import annotations

import subprocess
from pathlib import Path

import pytest

from marshal_engine.worktree import WorktreeError, WorktreeManager


def _init_repo(root: Path) -> None:
    def git(*args: str) -> None:
        subprocess.run(["git", "-C", str(root), *args], check=True, capture_output=True, text=True)

    git("init", "-q")
    git("config", "user.email", "test@example.com")
    git("config", "user.name", "test")
    (root / "README.md").write_text("hello\n")
    git("add", "-A")
    git("commit", "-q", "-m", "init")


@pytest.fixture
def repo(tmp_path: Path) -> Path:
    _init_repo(tmp_path)
    return tmp_path


def test_create_makes_isolated_worktree(repo: Path) -> None:
    m = WorktreeManager(repo)
    wt = m.create("task1")
    assert wt.path.exists()
    assert wt.branch == "marshal/task1"
    assert (wt.path / "README.md").exists()  # has the repo content


def test_changed_files_detects_edits_and_additions(repo: Path) -> None:
    m = WorktreeManager(repo)
    wt = m.create("task2")
    (wt.path / "new.txt").write_text("x")
    (wt.path / "README.md").write_text("changed\n")
    changed = set(m.changed_files(wt))
    assert "new.txt" in changed
    assert "README.md" in changed


def test_changed_files_handles_spaces_and_unicode(repo: Path) -> None:
    m = WorktreeManager(repo)
    wt = m.create("weird")
    (wt.path / "my file.txt").write_text("x")  # space -> would be C-quoted without -z
    (wt.path / "café.txt").write_text("y")      # non-ASCII -> would be octal-escaped without -z
    changed = set(m.changed_files(wt))
    assert "my file.txt" in changed   # returned verbatim, not '"my file.txt"'
    assert "café.txt" in changed


def test_diff_includes_tracked_and_untracked(repo: Path) -> None:
    m = WorktreeManager(repo)
    wt = m.create("task_diff")
    (wt.path / "README.md").write_text("changed\n")  # tracked modification
    (wt.path / "new.txt").write_text("brand new\n")   # untracked addition
    diff = m.diff(wt)
    assert "changed" in diff          # the tracked edit shows
    assert "new.txt" in diff          # the untracked file shows (git diff HEAD alone misses it)
    assert "brand new" in diff


def test_list_includes_created_worktree(repo: Path) -> None:
    m = WorktreeManager(repo)
    wt = m.create("task3")
    paths = {w.path.resolve() for w in m.list()}
    assert wt.path.resolve() in paths


def test_remove_deletes_worktree_and_branch(repo: Path) -> None:
    m = WorktreeManager(repo)
    wt = m.create("task4")
    assert wt.path.exists()
    m.remove(wt)
    assert not wt.path.exists()
    branches = subprocess.run(
        ["git", "-C", str(repo), "branch", "--list", "marshal/task4"],
        capture_output=True,
        text=True,
    ).stdout
    assert "marshal/task4" not in branches


def test_create_duplicate_raises(repo: Path) -> None:
    m = WorktreeManager(repo)
    m.create("dup")
    with pytest.raises(WorktreeError):
        m.create("dup")


def test_current_branch_returns_checked_out_branch(repo: Path) -> None:
    m = WorktreeManager(repo)
    assert m.current_branch()  # e.g. "main" or "master"


def test_commit_all_and_merge_round_trip(repo: Path) -> None:
    m = WorktreeManager(repo)
    wt = m.create("feat")
    (wt.path / "feature.txt").write_text("new feature\n")
    sha = m.commit_all(wt, "add feature")
    assert sha  # a commit was made
    assert m.commit_all(wt, "noop") is None  # clean worktree -> nothing to commit
    result = m.merge(wt.branch)
    assert result.ok
    assert (repo / "feature.txt").exists()  # landed in the main checkout


def test_merge_conflict_aborts_and_reports(repo: Path) -> None:
    m = WorktreeManager(repo)
    wt_a = m.create("a")
    (wt_a.path / "README.md").write_text("from a\n")
    m.commit_all(wt_a, "a")
    wt_b = m.create("b")
    (wt_b.path / "README.md").write_text("from b\n")
    m.commit_all(wt_b, "b")
    assert m.merge(wt_a.branch).ok            # first lands cleanly
    conflict = m.merge(wt_b.branch)           # second conflicts on README.md
    assert not conflict.ok
    assert "README.md" in conflict.conflicts
    assert (repo / "README.md").read_text() == "from a\n"  # aborted -> main untouched
