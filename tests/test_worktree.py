"""Integration tests for WorktreeManager against a real temporary git repo."""

from __future__ import annotations

import shutil
import subprocess
import sys
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


def test_setup_runs_command_in_worktree(repo: Path) -> None:
    # setup() runs setup_cmd in the worktree (a separate step from create, so it can run unlocked).
    m = WorktreeManager(repo, setup_cmd=[sys.executable, "-c", "open('marker', 'w').write('ok')"])
    wt = m.create("setup_ok")
    assert not (wt.path / "marker").exists()  # create() alone does NOT provision
    m.setup(wt)
    assert (wt.path / "marker").read_text() == "ok"  # ran with cwd = the worktree


def test_setup_is_noop_without_setup_cmd(repo: Path) -> None:
    m = WorktreeManager(repo)  # no setup_cmd
    wt = m.create("nosetup")
    m.setup(wt)  # no-op; the worktree survives
    assert wt.path.exists()


def test_setup_failure_tears_down_and_raises(repo: Path) -> None:
    m = WorktreeManager(repo, setup_cmd=[sys.executable, "-c", "import sys; sys.exit(1)"])
    wt = m.create("setup_fail")
    with pytest.raises(WorktreeError, match="setup"):
        m.setup(wt)
    # the worktree was torn down, so no orphan dir and the id is reusable
    assert not (m.base_dir / "setup_fail").exists()
    branches = subprocess.run(
        ["git", "-C", str(repo), "branch", "--list", "marshal/setup_fail"],
        capture_output=True,
        text=True,
    ).stdout
    assert "marshal/setup_fail" not in branches


def test_setup_missing_binary_raises(repo: Path) -> None:
    m = WorktreeManager(repo, setup_cmd=["marshal-no-such-binary-xyz123"])
    wt = m.create("setup_nobin")
    with pytest.raises(WorktreeError, match="not found"):
        m.setup(wt)
    assert not (m.base_dir / "setup_nobin").exists()  # torn down


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


def test_discard_removes_worktree_and_branch(repo: Path) -> None:
    m = WorktreeManager(repo)
    wt = m.create("disc1")
    m.discard(wt.path, wt.branch)
    assert not wt.path.exists()
    branches = subprocess.run(
        ["git", "-C", str(repo), "branch", "--list", "marshal/disc1"],
        capture_output=True, text=True,
    ).stdout
    assert "marshal/disc1" not in branches


def test_discard_reclaims_dir_when_git_admin_entry_corrupt(repo: Path) -> None:
    # The dir survives but git's admin entry is gone (a prior partial prune): `git worktree remove`
    # refuses ("not a working tree"). discard must still reclaim the disk-heavy dir, not raise.
    m = WorktreeManager(repo)
    wt = m.create("disc3")
    shutil.rmtree(repo / ".git" / "worktrees" / "disc3")  # corrupt: drop the admin entry, keep dir
    assert wt.path.exists()
    m.discard(wt.path, wt.branch)  # must not raise
    assert not wt.path.exists()    # dir reclaimed via the rmtree fallback


def test_discard_tolerates_already_gone_worktree(repo: Path) -> None:
    # Batch cleanup must handle a worktree dir that's already gone (manually deleted / partial
    # prior clean): no raise, and the dangling branch is still deleted.
    m = WorktreeManager(repo)
    wt = m.create("disc2")
    shutil.rmtree(wt.path)  # nuke the dir behind git's back
    m.discard(wt.path, wt.branch)  # must not raise
    branches = subprocess.run(
        ["git", "-C", str(repo), "branch", "--list", "marshal/disc2"],
        capture_output=True, text=True,
    ).stdout
    assert "marshal/disc2" not in branches


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


def test_merge_aborts_an_in_progress_merge_and_reports_blocked(repo: Path) -> None:
    m = WorktreeManager(repo)

    def g(*a: str) -> str:
        return subprocess.run(
            ["git", "-C", str(repo), *a], check=True, capture_output=True, text=True
        ).stdout

    wt = m.create("late")
    (wt.path / "x.txt").write_text("x")
    m.commit_all(wt, "x")
    # seed a real in-progress merge in the main checkout (clean, --no-commit -> MERGE_HEAD set)
    base = g("rev-parse", "--abbrev-ref", "HEAD").strip()
    g("checkout", "-b", "sibling")
    (repo / "sibling.txt").write_text("s")
    g("add", "-A")
    g("commit", "-m", "sibling")
    g("checkout", base)
    g("merge", "--no-commit", "--no-ff", "sibling")
    assert m._merge_in_progress()

    result = m.merge(wt.branch)            # git refuses (merge in progress); merge() aborts it
    assert not result.ok and result.blocked
    assert not m._merge_in_progress()      # repo returned to a clean state, not left mid-merge


def test_abort_merge_raises_when_abort_fails(repo: Path, monkeypatch: pytest.MonkeyPatch) -> None:
    m = WorktreeManager(repo)

    def g(*a: str) -> None:
        subprocess.run(["git", "-C", str(repo), *a], check=True, capture_output=True, text=True)

    base = subprocess.run(
        ["git", "-C", str(repo), "rev-parse", "--abbrev-ref", "HEAD"], capture_output=True, text=True
    ).stdout.strip()
    g("checkout", "-b", "sib")
    (repo / "s.txt").write_text("s")
    g("add", "-A")
    g("commit", "-m", "s")
    g("checkout", base)
    g("merge", "--no-commit", "--no-ff", "sib")  # a real in-progress merge to abort
    assert m._merge_in_progress()

    real_git = m._git

    def fake_git(*args: str, cwd: Path | None = None) -> subprocess.CompletedProcess[str]:
        if args[:2] == ("merge", "--abort"):  # simulate a held index.lock / failed abort
            return subprocess.CompletedProcess(list(args), 1, "", "fatal: index.lock exists")
        return real_git(*args, cwd=cwd)

    monkeypatch.setattr(m, "_git", fake_git)
    with pytest.raises(WorktreeError):  # abort failed + still mid-merge -> honest hard error
        m._abort_merge("marshal/whatever")


def test_commit_all_skips_pre_commit_hook(repo: Path) -> None:
    # A prompting/failing pre-commit hook would block a headless run; commit_all passes --no-verify.
    hook = repo / ".git" / "hooks" / "pre-commit"
    hook.parent.mkdir(parents=True, exist_ok=True)
    hook.write_text("#!/bin/sh\nexit 1\n")  # would fail the commit if it ran
    hook.chmod(0o755)
    m = WorktreeManager(repo)
    wt = m.create("hooked")
    (wt.path / "f.txt").write_text("x")
    assert m.commit_all(wt, "commit despite failing hook")  # --no-verify bypassed it -> a sha


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
