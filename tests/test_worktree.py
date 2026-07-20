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


# --- path-traversal security: task_id is a public input, so it must be sanitized -----------


@pytest.mark.parametrize(
    "bad_id",
    [
        "../escape",
        "../../etc/evil",
        "ok/../../escape",
        "/absolute/path",
        "with spaces/../../escape",
    ],
)
def test_create_rejects_path_traversal_in_task_id(repo: Path, bad_id: str) -> None:
    # The MCP surface (and workflows) accept an arbitrary `task_id` from the driver / spec. A
    # `..` segment, an absolute path, or a slash that escapes the base dir must NOT be allowed
    # to write the worktree anywhere on disk - that would be a real path-traversal hole.
    m = WorktreeManager(repo)
    base_resolved = m.base_dir.resolve()
    with pytest.raises(WorktreeError):
        m.create(bad_id)
    # nothing landed outside the base dir
    assert (base_resolved.parent).exists()  # the parent dir was never the target
    # the escape target, if it would have been a child of the parent, must not exist either
    for candidate in (base_resolved.parent / "escape", base_resolved.parent / "evil"):
        assert not candidate.exists()


def test_create_accepts_normal_ids_after_a_traversal_attempt(repo: Path) -> None:
    # A rejected traversal attempt must leave no orphan state - the manager is reusable.
    m = WorktreeManager(repo)
    with pytest.raises(WorktreeError):
        m.create("../escape")
    wt = m.create("normal-after")
    assert wt.path.exists()
    assert wt.branch == "marshal/normal-after"


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
    m = WorktreeManager(
        repo,
        setup_cmd=["marshal-no-such-binary-xyz123"],
        allow_unsafe_commands=True,
    )
    wt = m.create("setup_nobin")
    with pytest.raises(WorktreeError, match="not found"):
        m.setup(wt)
    assert not (m.base_dir / "setup_nobin").exists()  # torn down


def test_setup_refuses_non_allowlisted_without_opt_in(repo: Path) -> None:
    m = WorktreeManager(repo, setup_cmd=["curl", "https://example.invalid"])
    wt = m.create("refuse_setup")
    with pytest.raises(WorktreeError, match="allowlist|allow_unsafe_commands"):
        m.setup(wt)
    assert not (m.base_dir / "refuse_setup").exists()


def test_setup_allows_shell_with_opt_in(repo: Path) -> None:
    m = WorktreeManager(
        repo,
        setup_cmd=["sh", "-c", "echo ok > marker"],
        allow_unsafe_commands=True,
    )
    wt = m.create("shell_ok")
    m.setup(wt)
    assert (wt.path / "marker").read_text() == "ok\n"
    m.remove(wt)


def test_setup_allowlisted_basename_runs_without_opt_in(repo: Path) -> None:
    # python/python3 (and versioned python3.N via sys.executable) are allowlisted.
    m = WorktreeManager(
        repo, setup_cmd=[sys.executable, "-c", "open('marker', 'w').write('ok')"]
    )
    assert m.allow_unsafe_commands is False
    wt = m.create("allow_py")
    m.setup(wt)
    assert (wt.path / "marker").read_text() == "ok"
    m.remove(wt)


# --- verify: the post-run gate (never raises, never tears down) ----------------------------


def test_verify_runs_in_worktree_and_passes(repo: Path) -> None:
    m = WorktreeManager(
        repo, verify_cmd=[sys.executable, "-c", "open('gate-ran', 'w').write('ok'); print('gate ok')"]
    )
    wt = m.create("verify_ok")
    ok, output = m.verify(wt)
    assert ok is True
    assert (wt.path / "gate-ran").exists()  # ran with cwd = the worktree
    assert "gate ok" in output


def test_verify_is_noop_without_cmd(repo: Path) -> None:
    m = WorktreeManager(repo)
    wt = m.create("verify_unset")
    assert m.verify(wt) == (True, "")


def test_verify_failure_keeps_worktree(repo: Path) -> None:
    # Unlike setup, a failed verify NEVER tears down - the diff must stay reviewable.
    m = WorktreeManager(
        repo, verify_cmd=[sys.executable, "-c", "import sys; print('broke the build'); sys.exit(3)"]
    )
    wt = m.create("verify_fail")
    ok, output = m.verify(wt)
    assert ok is False
    assert "verify exited 3" in output
    assert "broke the build" in output
    assert wt.path.exists()  # kept for review


def test_verify_missing_binary_reports_not_raises(repo: Path) -> None:
    m = WorktreeManager(
        repo,
        verify_cmd=["marshal-no-such-binary-xyz123"],
        allow_unsafe_commands=True,
    )
    wt = m.create("verify_nobin")
    ok, output = m.verify(wt)
    assert ok is False
    assert "could not run" in output
    assert wt.path.exists()


def test_verify_refuses_non_allowlisted_without_opt_in(repo: Path) -> None:
    m = WorktreeManager(repo, verify_cmd=["sh", "-c", "exit 0"])
    wt = m.create("verify_refuse")
    ok, output = m.verify(wt)
    assert ok is False
    assert "refused" in output
    assert "allow_unsafe_commands" in output
    assert wt.path.exists()
    m.remove(wt)


def test_verify_allows_shell_with_opt_in(repo: Path) -> None:
    m = WorktreeManager(
        repo,
        verify_cmd=["sh", "-c", "echo gate; exit 0"],
        allow_unsafe_commands=True,
    )
    wt = m.create("verify_shell")
    ok, output = m.verify(wt)
    assert ok is True
    assert "gate" in output
    m.remove(wt)


def test_verify_timeout_reports_not_raises(repo: Path) -> None:
    m = WorktreeManager(
        repo,
        verify_cmd=[sys.executable, "-c", "import time; time.sleep(30)"],
        setup_timeout_s=1,  # verify reuses the setup timeout knob
    )
    wt = m.create("verify_slow")
    ok, output = m.verify(wt)
    assert ok is False
    assert "timed out after 1s" in output
    assert wt.path.exists()


def test_verify_output_keeps_the_tail(repo: Path) -> None:
    # Failures print last: a long run's output is truncated from the front, keeping the summary.
    m = WorktreeManager(
        repo,
        verify_cmd=[
            sys.executable,
            "-c",
            "import sys; print('x' * 6000); print('TAIL-MARKER'); sys.exit(1)",
        ],
    )
    wt = m.create("verify_long")
    ok, output = m.verify(wt)
    assert ok is False
    assert "TAIL-MARKER" in output
    assert len(output) < 4200  # capped (plus the small exit-code prefix)
    assert "..." in output  # truncation is visible


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


def test_commit_all_runs_pre_commit_hook_when_opted_in(repo: Path) -> None:
    hook = repo / ".git" / "hooks" / "pre-commit"
    hook.parent.mkdir(parents=True, exist_ok=True)
    hook.write_text("#!/bin/sh\nexit 1\n")
    hook.chmod(0o755)
    m = WorktreeManager(repo, integrate_run_hooks=True)
    wt = m.create("hooked-run")
    (wt.path / "f.txt").write_text("x")
    with pytest.raises(WorktreeError, match="commit failed"):
        m.commit_all(wt, "should fail when hook runs")


def test_merge_skips_commit_msg_hook_by_default(repo: Path) -> None:
    # Fast-forward merges skip commit-msg; diverge main so a merge commit would run the hook.
    hook = repo / ".git" / "hooks" / "commit-msg"
    hook.parent.mkdir(parents=True, exist_ok=True)
    hook.write_text("#!/bin/sh\nexit 1\n")
    hook.chmod(0o755)
    m = WorktreeManager(repo)
    wt = m.create("merge-hook-skip")
    (wt.path / "g.txt").write_text("y")
    m.commit_all(wt, "feature")
    (repo / "README.md").write_text("main moved\n")
    subprocess.run(
        ["git", "-C", str(repo), "add", "-A"],
        check=True,
        capture_output=True,
        text=True,
    )
    subprocess.run(
        ["git", "-C", str(repo), "commit", "--no-verify", "-m", "main"],
        check=True,
        capture_output=True,
        text=True,
    )
    assert m.merge(wt.branch).ok  # --no-verify bypassed the failing commit-msg hook


def test_merge_runs_commit_msg_hook_when_opted_in(repo: Path) -> None:
    # Fast-forward merges skip commit-msg; diverge main so merge creates a commit.
    hook = repo / ".git" / "hooks" / "commit-msg"
    hook.parent.mkdir(parents=True, exist_ok=True)
    hook.write_text("#!/bin/sh\nexit 1\n")
    hook.chmod(0o755)
    m = WorktreeManager(repo, integrate_run_hooks=True)
    wt = m.create("merge-hook-run")
    (wt.path / "g.txt").write_text("y")
    # commit_all also runs hooks when opted in; commit with --no-verify via a skip manager.
    m_skip = WorktreeManager(repo)
    m_skip.commit_all(wt, "feature")
    (repo / "README.md").write_text("main moved\n")
    subprocess.run(
        ["git", "-C", str(repo), "add", "-A"],
        check=True,
        capture_output=True,
        text=True,
    )
    subprocess.run(
        ["git", "-C", str(repo), "commit", "--no-verify", "-m", "main"],
        check=True,
        capture_output=True,
        text=True,
    )
    result = m.merge(wt.branch)
    # A failing commit-msg leaves the merge unfinished; WorktreeManager aborts and reports blocked.
    assert not result.ok
    assert result.blocked
    assert "Not committing merge" in (result.message or "") or result.message


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
