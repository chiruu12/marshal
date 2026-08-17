"""Integration tests for WorktreeManager against a real temporary git repo."""

from __future__ import annotations

import os
import shutil
import signal
import subprocess
import sys
import time
from pathlib import Path

import pytest

from marshal_engine.runtime.worktree import WorktreeError, WorktreeManager


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
        "foo/bar",
        r"foo\bar",
        "",
        ".",
        "..",
        ".hidden",
        "-leading-dash",
        "with spaces",
        "café",
        "a\nb",
        "a\x00b",
        "a" * 129,
    ],
)
def test_create_rejects_path_traversal_in_task_id(repo: Path, bad_id: str) -> None:
    # Driver / MCP / workflow `task_id` values must never escape `.marshal/worktrees/`. Rejection
    # is Marshal-owned (charset + containment) *before* any git op — not git ref-format accident.
    m = WorktreeManager(repo)
    base_resolved = m.base_dir.resolve()
    calls: list[tuple] = []
    real_git = m._git

    def spy_git(*args: str, **kwargs: object) -> object:
        calls.append(args)
        return real_git(*args, **kwargs)

    m._git = spy_git  # type: ignore[method-assign]
    with pytest.raises(WorktreeError, match="unsafe worktree id|outside base dir"):
        m.create(bad_id)
    assert calls == [], "git must not run for a rejected id"
    # Fail-closed before mkdir: base_dir must not appear for a rejected id.
    assert not m.base_dir.exists()
    # the escape target, if it would have been a child of the parent, must not exist either
    for candidate in (base_resolved.parent / "escape", base_resolved.parent / "evil", repo / "escape"):
        assert not candidate.exists()


def test_create_rejects_absolute_path_join_escape(repo: Path, tmp_path: Path) -> None:
    # On POSIX, `base_dir / "/abs"` discards base_dir — prove we refuse before git.
    m = WorktreeManager(repo)
    abs_id = str(tmp_path / "outside_wt")
    with pytest.raises(WorktreeError, match="unsafe"):
        m.create(abs_id)
    assert not Path(abs_id).exists()


@pytest.mark.parametrize(
    "good_id",
    ["normal", "a.b-c_d", "abc123.phase", "review.candidate", "command-code", "x" * 128],
)
def test_create_accepts_safe_ids(repo: Path, good_id: str) -> None:
    m = WorktreeManager(repo)
    wt = m.create(good_id)
    assert wt.path.exists()
    assert wt.path.resolve().is_relative_to(m.base_dir.resolve())
    assert wt.task_id == good_id


def test_create_refuses_symlink_escape_under_valid_id(repo: Path) -> None:
    # A pre-seeded symlink named with a *valid* id that points outside base_dir must not create.
    m = WorktreeManager(repo)
    m.base_dir.mkdir(parents=True, exist_ok=True)
    outside = repo / "outside_target"
    outside.mkdir()
    link = m.base_dir / "escape"
    link.symlink_to(outside)
    calls: list[tuple] = []

    def boom(*_a: object, **_k: object) -> None:
        calls.append(())
        raise AssertionError("git must not run")

    m._git = boom  # type: ignore[method-assign]
    with pytest.raises(WorktreeError, match="outside base dir"):
        m.create("escape")
    assert calls == []
    assert list(outside.iterdir()) == []


def test_create_accepts_normal_ids_after_a_traversal_attempt(repo: Path) -> None:
    # A rejected traversal attempt must leave no orphan state - the manager is reusable.
    m = WorktreeManager(repo)
    with pytest.raises(WorktreeError, match="unsafe"):
        m.create("../escape")
    wt = m.create("normal-after")
    assert wt.path.exists()
    assert wt.branch == "marshal/normal-after"


def test_remove_refuses_path_outside_base_dir(repo: Path) -> None:
    from marshal_engine.runtime.worktree import Worktree

    m = WorktreeManager(repo)
    outside = repo / "not_a_worktree"
    outside.mkdir()
    marker = outside / "keep_me"
    marker.write_text("safe\n")
    poisoned = Worktree(task_id="x", path=outside, branch="marshal/x")
    with pytest.raises(WorktreeError, match="outside base dir"):
        m.remove(poisoned)
    assert marker.exists()


def test_discard_refuses_path_outside_base_dir(repo: Path) -> None:
    m = WorktreeManager(repo)
    outside = repo / "not_a_worktree"
    outside.mkdir()
    marker = outside / "keep_me"
    marker.write_text("safe\n")
    with pytest.raises(WorktreeError, match="outside base dir"):
        m.discard(outside, "marshal/x")
    assert marker.exists()


def test_discard_refuses_base_dir_itself(repo: Path) -> None:
    # Equality with base_dir must not count as "under" — otherwise discard's rmtree fallback
    # would wipe the shared worktrees root and every sibling run.
    m = WorktreeManager(repo)
    m.base_dir.mkdir(parents=True)
    sibling = m.base_dir / "keep"
    sibling.mkdir()
    (sibling / "marker").write_text("alive\n")
    with pytest.raises(WorktreeError, match="outside base dir"):
        m.discard(m.base_dir, None)
    assert (sibling / "marker").exists()
    with pytest.raises(WorktreeError, match="outside base dir"):
        m.discard(m.base_dir / ".", None)
    assert (sibling / "marker").exists()


def _ensure_deletable_main(repo: Path) -> None:
    """Leave HEAD on a side branch so unguarded `git branch -D main` would actually delete it."""
    subprocess.run(
        ["git", "-C", str(repo), "branch", "-M", "main"],
        check=True, capture_output=True, text=True,
    )
    subprocess.run(
        ["git", "-C", str(repo), "checkout", "-b", "operator"],
        check=True, capture_output=True, text=True,
    )


def _branch_list(repo: Path, name: str) -> str:
    return subprocess.run(
        ["git", "-C", str(repo), "branch", "--list", name],
        capture_output=True, text=True,
        check=False,
    ).stdout


def test_discard_refuses_branch_outside_prefix(repo: Path) -> None:
    # Path is contained; branch must be too. A poisoned "main" must not be force-deleted.
    _ensure_deletable_main(repo)
    m = WorktreeManager(repo)
    wt = m.create("poison-br")
    with pytest.raises(WorktreeError, match="outside managed prefix"):
        m.discard(wt.path, "main")
    assert "main" in _branch_list(repo, "main")
    assert not wt.path.exists()  # worktree still reclaimed; only -D is refused


def test_discard_still_deletes_managed_branch(repo: Path) -> None:
    m = WorktreeManager(repo)
    wt = m.create("managed-ok")
    m.discard(wt.path, "marshal/managed-ok")
    assert not wt.path.exists()
    assert "marshal/managed-ok" not in _branch_list(repo, "marshal/managed-ok")


def test_remove_refuses_base_dir_itself(repo: Path) -> None:
    from marshal_engine.runtime.worktree import Worktree

    m = WorktreeManager(repo)
    m.base_dir.mkdir(parents=True)
    poisoned = Worktree(task_id="x", path=m.base_dir, branch="marshal/x")
    with pytest.raises(WorktreeError, match="outside base dir"):
        m.remove(poisoned)


def test_validate_worktree_id_happy_and_task_cap() -> None:
    from marshal_engine.runtime.worktree import MAX_TASK_ID_LEN, validate_worktree_id

    assert validate_worktree_id("a.b-c_d") == "a.b-c_d"
    with pytest.raises(WorktreeError, match="max length"):
        validate_worktree_id("a" * (MAX_TASK_ID_LEN + 1), max_len=MAX_TASK_ID_LEN)


@pytest.mark.parametrize(
    "good_id",
    [
        "t1.echo.ab12cd34",  # task.backend.<uuid8> - the production shape
        "deadbeef.first.command-code.00ff00ff",  # dotted workflow task + dashed backend
        "a.b-c_d.x",
        "x" * 128,  # at the shared cap (a composed run_id fits under MAX_WORKTREE_ID_LEN)
    ],
)
def test_validate_run_id_accepts_production_shapes(good_id: str) -> None:
    from marshal_engine.runtime.worktree import validate_run_id

    assert validate_run_id(good_id) == good_id


@pytest.mark.parametrize(
    "bad_id",
    ["", ".", "..", "../x", "foo/bar", "a\\b", ".hidden", "-lead", "café", "a\x00b", "a" * 129],
)
def test_validate_run_id_refuses_unsafe_ids(bad_id: str) -> None:
    from marshal_engine.runtime.worktree import validate_run_id

    # ValueError, not WorktreeError: the state/MCP boundary's input-validation error type,
    # matching how an invalid task_id surfaces there (TaskSpec wraps WorktreeError -> ValueError).
    with pytest.raises(ValueError, match="unsafe run_id"):
        validate_run_id(bad_id)


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
        check=False,
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
    # Static allowlist refusal fails at construction — never creates a worktree/branch.
    with pytest.raises(WorktreeError, match="allowlist|allow_unsafe_commands"):
        WorktreeManager(repo, setup_cmd=["curl", "https://example.invalid"])
    assert not (WorktreeManager(repo).base_dir / "refuse_setup").exists()
    listed = subprocess.run(
        ["git", "-C", str(repo), "worktree", "list", "--porcelain"],
        capture_output=True,
        text=True,
        check=True,
    ).stdout
    assert "refuse_setup" not in listed
    branches = subprocess.run(
        ["git", "-C", str(repo), "branch", "--list", "marshal/refuse_setup"],
        capture_output=True,
        text=True,
        check=True,
    ).stdout
    assert branches.strip() == ""


def test_setup_runtime_backstop_still_refuses_mutated_cmd(repo: Path) -> None:
    # Defense in depth: setup() still refuses if setup_cmd is mutated after a clean __init__.
    m = WorktreeManager(repo)
    m.setup_cmd = ["curl", "https://example.invalid"]
    wt = m.create("refuse_runtime")
    with pytest.raises(WorktreeError, match="allowlist|allow_unsafe_commands"):
        m.setup(wt)
    assert not (m.base_dir / "refuse_runtime").exists()


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
    # Same construction gate as setup — refused verify never reaches create/verify.
    with pytest.raises(WorktreeError, match="allowlist|allow_unsafe_commands"):
        WorktreeManager(repo, verify_cmd=["sh", "-c", "exit 0"])


def test_verify_runtime_backstop_still_refuses_mutated_cmd(repo: Path) -> None:
    m = WorktreeManager(repo)
    m.verify_cmd = ["sh", "-c", "exit 0"]
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


def test_verify_output_redacts_secret_straddling_tail_cap(
    repo: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    """Redact before the verify tail cut so a straddling credential leaves no fragment."""
    from marshal_engine.runtime.env import redact_secrets
    from marshal_engine.runtime.worktree import _VERIFY_OUTPUT_CAP

    secret = "sk-ant-verify-straddle-x"
    monkeypatch.setenv("ANTHROPIC_API_KEY", secret)
    # First `keep_prefix` chars sit just before the tail boundary; the rest is retained.
    keep_prefix = 10
    # len(secret) + len(suffix) - keep_prefix == CAP
    suffix_len = _VERIFY_OUTPUT_CAP + keep_prefix - len(secret)
    assert suffix_len > 0
    body = ("v" * 500) + secret + ("s" * suffix_len)
    leaked_suffix = secret[keep_prefix:]
    broken = redact_secrets(body[-_VERIFY_OUTPUT_CAP:], credential_names=["ANTHROPIC_API_KEY"])
    assert leaked_suffix in broken  # truncate-then-redact would persist this fragment

    script = f"import sys; print({body!r}); sys.exit(1)"
    m = WorktreeManager(repo, verify_cmd=[sys.executable, "-c", script])
    wt = m.create("verify_straddle")
    ok, output = m.verify(wt)
    assert ok is False
    assert secret not in output
    assert leaked_suffix not in output


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
        check=False,
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
        check=False,
    ).stdout
    assert "marshal/disc1" not in branches


def test_discard_reclaims_a_run_dir_that_is_no_longer_a_git_repo(repo: Path) -> None:
    # A killed agent (or a half-finished clean) can leave the dir present but its .git gone, so git
    # refuses to treat it as a repo at all. Reclaiming the disk-heavy dir is the point of discard,
    # so it must not raise and must not leave the dir behind.
    m = WorktreeManager(repo)
    wt = m.create("disc3")
    shutil.rmtree(wt.path / ".git")
    assert wt.path.exists()
    m.discard(wt.path, wt.branch)  # must not raise
    assert not wt.path.exists()


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
        check=False,
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
        ["git", "-C", str(repo), "rev-parse", "--abbrev-ref", "HEAD"], capture_output=True, text=True,
        check=False,
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


# --- failure atomicity (#143) -----------------------------------------------------------------


def test_setup_oserror_tears_down_and_raises(repo: Path, monkeypatch: pytest.MonkeyPatch) -> None:
    """A generic OSError from the setup binary must become WorktreeError with teardown (M6)."""
    m = WorktreeManager(
        repo,
        setup_cmd=[sys.executable, "-c", "pass"],
    )
    wt = m.create("setup_eacces")
    # Patched on Popen, not run: setup/verify spawn through `_run_group` so a timeout can kill the
    # child's whole process group, and `subprocess.run` is no longer on that path at all.
    real_popen = subprocess.Popen

    def boom(*args: object, **kwargs: object) -> "subprocess.Popen[str]":
        cmd = args[0] if args else kwargs.get("args")
        if isinstance(cmd, (list, tuple)) and cmd and cmd[0] == sys.executable:
            raise PermissionError("injected EACCES")
        return real_popen(*args, **kwargs)  # type: ignore[arg-type]

    monkeypatch.setattr(subprocess, "Popen", boom)
    with pytest.raises(WorktreeError, match="could not run"):
        m.setup(wt)
    assert not (m.base_dir / "setup_eacces").exists()
    branches = subprocess.run(
        ["git", "-C", str(repo), "branch", "--list", "marshal/setup_eacces"],
        capture_output=True,
        text=True,
        check=False,
    ).stdout
    assert "marshal/setup_eacces" not in branches


def test_verify_oserror_reports_not_raises(repo: Path, monkeypatch: pytest.MonkeyPatch) -> None:
    """verify() already catches OSError; pin the path so a PermissionError stays soft (M6)."""
    m = WorktreeManager(
        repo,
        verify_cmd=[sys.executable, "-c", "pass"],
    )
    wt = m.create("verify_eacces")
    # Patched on Popen, not run: setup/verify spawn through `_run_group` so a timeout can kill the
    # child's whole process group, and `subprocess.run` is no longer on that path at all.
    real_popen = subprocess.Popen

    def boom(*args: object, **kwargs: object) -> "subprocess.Popen[str]":
        cmd = args[0] if args else kwargs.get("args")
        if isinstance(cmd, (list, tuple)) and cmd and cmd[0] == sys.executable:
            raise PermissionError("injected EACCES")
        return real_popen(*args, **kwargs)  # type: ignore[arg-type]

    monkeypatch.setattr(subprocess, "Popen", boom)
    ok, output = m.verify(wt)
    assert ok is False
    assert "could not run" in output
    assert wt.path.exists()  # verify never tears down


def test_create_refuses_a_taken_branch_and_leaves_its_work_untouched(repo: Path) -> None:
    """The data-loss case: a same-named branch may hold unmerged work (M7).

    A run must never adopt or delete it. With a clone this is checked in the DRIVER's repo before
    anything is created, because a clone would not refuse on its own - `clone` puts the repo's
    branches under refs/remotes/, so `checkout -b` inside it would shadow the taken name and the
    collision would surface only later.
    """
    m = WorktreeManager(repo)

    def git(*args: str) -> str:
        return subprocess.run(
            ["git", "-C", str(repo), *args], check=True, capture_output=True, text=True
        ).stdout.strip()

    git("checkout", "-b", "marshal/preexist")
    (repo / "important.txt").write_text("keep me\n")
    git("add", "important.txt")
    git("commit", "-m", "unmerged work")
    tip = git("rev-parse", "HEAD")
    git("checkout", "-")

    with pytest.raises(WorktreeError, match="already exists"):
        m.create("preexist")

    assert git("rev-parse", "marshal/preexist") == tip  # tip (and its unmerged work) survived
    assert "marshal/preexist" in git("branch", "--list", "marshal/preexist")


def test_a_failed_create_leaves_no_branch_and_the_id_stays_reusable(
    repo: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    """The leak case: a half-finished create must not strand the branch name it was going to use.

    Under a clone the branch is born inside the run's own directory, so tearing that directory down
    IS the rollback - there is no separate branch in the driver's repo to clean up, and therefore no
    window in which cleanup could delete the wrong one.
    """
    m = WorktreeManager(repo)
    real_git = m._git

    def flaky(*args: str, cwd: Path | None = None) -> subprocess.CompletedProcess[str]:
        if args[:1] == ("checkout",) and "-b" in args:
            return subprocess.CompletedProcess(
                args=["git", *args], returncode=1, stdout="", stderr="simulated checkout fail"
            )
        return real_git(*args, cwd=cwd)

    monkeypatch.setattr(m, "_git", flaky)
    with pytest.raises(WorktreeError, match="simulated checkout fail"):
        m.create("leak_branch")

    branches = subprocess.run(
        ["git", "-C", str(repo), "branch", "--list", "marshal/leak_branch"],
        capture_output=True, text=True,
        check=False,
    ).stdout
    assert "marshal/leak_branch" not in branches
    assert not (m.base_dir / "leak_branch").exists()  # no half-built run dir left behind

    monkeypatch.undo()
    wt = m.create("leak_branch")  # the id is reusable, which is the point
    assert wt.path.exists()
    m.remove(wt)


def test_create_never_deletes_a_branch(repo: Path, monkeypatch: pytest.MonkeyPatch) -> None:
    """Structural guard on the M7 data-loss vector, not a scenario.

    The old add-then-clean-up path had to reason about WHICH branch a failure left behind; deleting
    the wrong one destroyed unmerged work. Cloning removes the need to delete anything at all, so
    assert the capability is simply absent - no failure mode can reintroduce it.
    """
    m = WorktreeManager(repo)
    real_git = m._git
    deletes: list[tuple[str, ...]] = []

    def watched(*args: str, cwd: Path | None = None) -> subprocess.CompletedProcess[str]:
        if args[:2] == ("branch", "-D"):
            deletes.append(args)
        return real_git(*args, cwd=cwd)

    monkeypatch.setattr(m, "_git", watched)
    m.create("no_delete")
    with pytest.raises(WorktreeError):
        m.create("no_delete")  # second create fails: dir and branch are both taken

    assert deletes == []


def test_branch_tip_returns_sha_for_real_branch(repo: Path) -> None:
    m = WorktreeManager(repo)
    wt = m.create("tip_ok")
    assert wt.branch is not None
    tip = m.branch_tip(wt.branch)
    assert len(tip) == 40
    assert all(c in "0123456789abcdef" for c in tip)
    head = subprocess.run(
        ["git", "-C", str(repo), "rev-parse", wt.branch],
        check=True,
        capture_output=True,
        text=True,
    ).stdout.strip()
    assert tip == head


def test_branch_tip_rejects_non_sha_stdout(
    repo: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    """Even exit 0 must not accept a non-sha tip (future git quirk / mis-parse)."""
    m = WorktreeManager(repo)
    real_git = m._git

    def weird(*args: str, cwd: Path | None = None) -> subprocess.CompletedProcess[str]:
        if args[:1] == ("rev-parse",):
            return subprocess.CompletedProcess(
                args=["git", *args], returncode=0, stdout="marshal/not-a-sha\n", stderr=""
            )
        return real_git(*args, cwd=cwd)

    monkeypatch.setattr(m, "_git", weird)
    with pytest.raises(WorktreeError, match="not a commit sha"):
        m.branch_tip("marshal/whatever")


def test_setup_on_pid_publishes_and_on_exit_fires(repo: Path) -> None:
    """setup() process-group spawn publishes pid via on_pid and reaps via on_exit (#146 cancel hook)."""
    seen: dict[str, object] = {}

    def on_pid(pid: int) -> None:
        seen["pid"] = pid

    def on_exit() -> None:
        seen["exited"] = True

    m = WorktreeManager(
        repo,
        setup_cmd=[sys.executable, "-c", "open('marker','w').write('ok')"],
    )
    wt = m.create("setup_hooks")
    m.setup(wt, on_pid=on_pid, on_exit=on_exit)
    assert isinstance(seen.get("pid"), int) and seen["pid"] > 0
    assert seen.get("exited") is True
    assert (wt.path / "marker").read_text() == "ok"


# --- #180: a run's git admin state is its own -------------------------------------------------


def test_a_run_cannot_reach_the_repos_git_admin_state(repo: Path) -> None:
    """The whole reason runs are clones rather than linked worktrees.

    A linked worktree's `.git` is a FILE pointing into the main repo's `.git`, so writing a hook or
    a command-executing config key from inside a run landed in shared state and fired during a
    LATER, unrelated run - and survived cleanup, because tearing down a worktree never rewrote it.
    A clone has its own `.git` directory, so the same writes reach nothing but the run itself.
    """
    m = WorktreeManager(repo)
    wt = m.create("own_git")

    git_dir = wt.path / ".git"
    assert git_dir.is_dir(), "a linked worktree's .git is a file into the shared common dir"
    assert git_dir.resolve().is_relative_to(wt.path.resolve())

    # What an agent would write. Both land inside the run and nowhere else.
    (git_dir / "hooks").mkdir(exist_ok=True)
    (git_dir / "hooks" / "post-checkout").write_text("#!/bin/sh\necho pwned\n")
    subprocess.run(
        ["git", "-C", str(wt.path), "config", "core.hooksPath", str(git_dir / "hooks")],
        check=True, capture_output=True, text=True,
    )

    assert not (repo / ".git" / "hooks" / "post-checkout").exists()
    repo_config = (repo / ".git" / "config").read_text()
    assert "hooksPath" not in repo_config

    # And the poison does not outlive the run: teardown is a directory removal, so there is no
    # residue left in shared state for the next run to execute.
    m.discard(wt.path, wt.branch)
    assert "hooksPath" not in (repo / ".git" / "config").read_text()
    assert not (repo / ".git" / "hooks" / "post-checkout").exists()


def test_a_runs_commits_reach_the_repo_as_a_normal_branch(repo: Path) -> None:
    """Isolation must not change how work is integrated.

    The run commits inside its own clone, where nothing in the driver's repo can see it. Publishing
    on commit is what keeps every downstream branch operation - merge, ancestry, unmerged counts -
    a plain local-branch operation against the repo, exactly as it was.
    """
    m = WorktreeManager(repo)
    wt = m.create("publish")
    (wt.path / "new.txt").write_text("agent work\n")
    sha = m.commit_all(wt, "agent work")

    assert sha is not None
    assert m.branch_tip(wt.branch) == sha       # visible in the driver's repo, by branch name
    assert m.has_unmerged_commits(wt.branch, m.current_branch())
    assert "new.txt" in m.merged_diff_files(wt.branch, m.current_branch())


def test_a_run_can_commit_when_the_identity_is_only_repo_local(
    repo: Path, monkeypatch: pytest.MonkeyPatch, tmp_path: Path
) -> None:
    """A clone does not inherit `.git/config`, so the committer identity has to be carried over.

    Per-repo identity is a normal setup - it is how people keep work and personal commits apart -
    and under a linked worktree it came for free. Without this, every commit in every run of such a
    repo fails with "Author identity unknown". Global config is pointed at an empty dir so the test
    proves the repo-local value is what gets used, rather than passing on the developer's own.
    """
    empty_home = tmp_path / "no_global"
    empty_home.mkdir()
    monkeypatch.setenv("HOME", str(empty_home))
    monkeypatch.setenv("GIT_CONFIG_GLOBAL", str(empty_home / "gitconfig"))
    monkeypatch.setenv("GIT_CONFIG_SYSTEM", str(empty_home / "gitsystem"))

    m = WorktreeManager(repo)
    wt = m.create("identity")
    (wt.path / "work.txt").write_text("done\n")

    sha = m.commit_all(wt, "agent work")
    assert sha is not None
    author = subprocess.run(
        ["git", "-C", str(wt.path), "log", "-1", "--format=%ae"],
        capture_output=True, text=True,
        check=False,
    ).stdout.strip()
    assert author == "test@example.com"  # the repo's identity, not a fabricated one


def test_a_runs_clone_does_not_inherit_the_repos_local_config_wholesale(repo: Path) -> None:
    """Only the identity crosses over.

    Copying the whole local config would carry keys that name paths in the operator's repo -
    `core.hooksPath`, `filter.*` - re-pointing a run at the very shared execution surface that
    isolating it was meant to remove.
    """
    subprocess.run(
        ["git", "-C", str(repo), "config", "core.hooksPath", str(repo / ".git" / "hooks")],
        check=True, capture_output=True, text=True,
    )
    m = WorktreeManager(repo)
    wt = m.create("noconfig")

    clone_config = (wt.path / ".git" / "config").read_text()
    assert "hooksPath" not in clone_config
    assert "user" in clone_config  # identity did cross over


def test_a_run_cannot_corrupt_the_repos_objects_through_a_shared_inode(repo: Path) -> None:
    """A hardlinked object store would leave a writable path back into the driver's repo.

    `git clone --local` hardlinks objects by default, and a hardlink is the same inode. Git writes
    objects read-only, but the agent OWNS the file in its own clone, so it can chmod and write -
    and the driver's object database changes with it (`git fsck` then reports the damage). Copying
    is what makes the run's objects the run's own.
    """
    m = WorktreeManager(repo)
    wt = m.create("objects")

    objects = [p for p in (wt.path / ".git" / "objects").rglob("*") if p.is_file()]
    assert objects, "no objects to check - the clone would not be a useful repo"
    for obj in objects:
        assert obj.stat().st_nlink == 1, f"{obj} is hardlinked into the driver's repo"

    # The attack itself: take one object, make it writable, scribble on it.
    target = objects[0]
    relative = target.relative_to(wt.path / ".git" / "objects")
    twin = repo / ".git" / "objects" / relative
    before = twin.read_bytes()
    target.chmod(0o644)
    target.write_bytes(b"CORRUPTED")

    assert twin.read_bytes() == before  # the driver's copy is untouched
    fsck = subprocess.run(
        ["git", "-C", str(repo), "fsck"], capture_output=True, text=True,
        check=False,
    )
    assert "error" not in fsck.stderr.lower()


def test_copied_hooks_cannot_be_used_to_overwrite_a_file_outside_the_run(repo: Path) -> None:
    """A hook that is a symlink must be copied by CONTENT, never recreated as a link.

    Preserving the link would put an agent-writable path inside the run pointing at an operator's
    file elsewhere on disk - so the agent writing "its own" hook would overwrite the real one, and
    that edit outlives the run.
    """
    outside = repo.parent / "operator_hook.sh"
    outside.write_text("#!/bin/sh\necho operator\n")
    hooks = repo / ".git" / "hooks"
    hooks.mkdir(parents=True, exist_ok=True)
    (hooks / "pre-commit").symlink_to(outside)

    m = WorktreeManager(repo, integrate_run_hooks=True)
    wt = m.create("hooklink")

    copied = wt.path / ".git" / "hooks" / "pre-commit"
    assert copied.exists()
    assert not copied.is_symlink()

    copied.write_text("#!/bin/sh\necho pwned\n")  # what an agent would do
    assert outside.read_text() == "#!/bin/sh\necho operator\n"


# --- #175: a run's directory is not inside the repo it is working on ---------------------------


def test_a_run_directory_is_outside_the_repo(repo: Path) -> None:
    """The `../..` reach, removed.

    While run dirs sat at `<repo>/.marshal/worktrees/<id>`, climbing three levels from the agent's
    cwd landed in the operator's live checkout - and `.marshal/runs`, the ledger. No exploit was
    needed for that, just a relative path. This does not make the agent unable to write elsewhere on
    the host; it removes the case where wandering upward lands somewhere costly by accident.
    """
    m = WorktreeManager(repo)
    wt = m.create("outside")

    resolved = wt.path.resolve()
    assert not resolved.is_relative_to(repo.resolve())
    # The repo is not an ancestor, so no number of `..` from inside the run passes THROUGH the
    # checkout or the ledger - which is exactly what the old `<repo>/.marshal/worktrees/<id>`
    # location could not say.
    assert repo.resolve() not in resolved.parents
    assert (repo / ".marshal").resolve() not in resolved.parents


def test_the_fleet_puts_runs_outside_the_repo_too(repo: Path) -> None:
    """Through `Fleet`, the way production builds it - not just a hand-made WorktreeManager.

    This is the test that was missing: asserting the property on a directly-constructed manager
    proved the default, while `Fleet` passed its own `base_dir` and put runs back under the repo.
    The boundary has to hold on the path runs actually take.
    """
    from marshal_engine.orchestration.fleet import Fleet

    fleet = Fleet(repo, {})
    assert not fleet.worktrees.base_dir.resolve().is_relative_to(repo.resolve())
    # The ledger DOES stay with the repo - it is Marshal-written, and now out of the agent's reach.
    assert fleet.state.dir.resolve().is_relative_to(repo.resolve())


def test_a_pinned_base_dir_does_not_drag_runs_back_into_the_repo(repo: Path) -> None:
    """`base_dir` pins Marshal's state; it must not silently re-nest the agent's working tree."""
    from marshal_engine.orchestration.fleet import Fleet

    fleet = Fleet(repo, {}, base_dir=repo / ".marshal")
    assert not fleet.worktrees.base_dir.resolve().is_relative_to(repo.resolve())


def test_two_checkouts_of_the_same_project_do_not_share_a_run_tree(tmp_path: Path) -> None:
    """Keyed by resolved path, not by name.

    A worktree-per-feature setup has several directories with the SAME basename that are different
    repos. Sharing a run tree between them would collide run ids and let one repo's cleanup delete
    another's work.
    """
    from marshal_engine.core.layout import runs_root

    a = tmp_path / "a" / "project"
    b = tmp_path / "b" / "project"
    for path in (a, b):
        path.mkdir(parents=True)
        _init_repo(path)

    assert runs_root(a) != runs_root(b)
    assert runs_root(a).name.startswith("project-")  # still identifiable by eye


def test_runs_left_by_an_older_marshal_can_still_be_cleaned(repo: Path) -> None:
    """Upgrade path: old records point into `<repo>/.marshal/worktrees`.

    Teardown refuses any path outside a base dir Marshal owns - which is what stops a poisoned
    record aiming cleanup at the host. Without accepting the legacy base too, upgrading would strand
    every existing run's directory as permanently un-cleanable.
    """
    from marshal_engine.core.layout import legacy_worktrees_dir

    legacy = legacy_worktrees_dir(repo) / "old_run"
    legacy.mkdir(parents=True)
    (legacy / "leftover.txt").write_text("from a previous version\n")

    m = WorktreeManager(repo)
    m.discard(legacy, None)  # must not raise "outside base dir"
    assert not legacy.exists()


def test_cleanup_still_refuses_a_path_under_neither_base(repo: Path, tmp_path: Path) -> None:
    """The legacy allowance widens the containment check by exactly one directory, not generally."""
    m = WorktreeManager(repo)
    elsewhere = tmp_path / "somewhere_else"
    elsewhere.mkdir()
    (elsewhere / "keep").write_text("safe\n")

    with pytest.raises(WorktreeError, match="outside base dir"):
        m.discard(elsewhere, None)
    assert (elsewhere / "keep").exists()


def test_setup_timeout_kills_the_whole_process_group(
    repo: Path, tmp_path: Path
) -> None:
    # Greptile #261: with start_new_session=True, subprocess.run's timeout would kill only the
    # group leader and leave its children writing into a worktree Marshal has given up on. Spawn a
    # grandchild that outlives its parent, then assert it is gone after the timeout.
    marker = tmp_path / "grandchild.pid"
    script = (
        "import os, subprocess, sys, time\n"
        "p = subprocess.Popen([sys.executable, '-c', 'import time; time.sleep(60)'])\n"
        f"open({str(marker)!r}, 'w').write(str(p.pid))\n"
        "time.sleep(60)\n"
    )
    m = WorktreeManager(
        repo,
        setup_cmd=[sys.executable, "-c", script],
        setup_timeout_s=2.0,
    )
    wt = m.create("group_kill")
    with pytest.raises(WorktreeError, match="timed out"):
        m.setup(wt)

    assert marker.exists(), "the setup command never spawned its grandchild; the test proved nothing"
    pid = int(marker.read_text())
    deadline = time.monotonic() + 10
    while time.monotonic() < deadline:
        try:
            os.kill(pid, 0)
        except ProcessLookupError:
            break
        time.sleep(0.1)
    else:
        os.kill(pid, signal.SIGKILL)
        pytest.fail(f"grandchild {pid} survived the setup timeout; the process group was not killed")


def test_diff_raises_when_a_new_files_content_cannot_be_read(
    repo: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    """REGRESSION (#257.6): the per-untracked-file `git diff --no-index` return code was never
    checked, unlike the tracked `git diff HEAD` three lines above it. A real failure yields empty
    stdout, so the file still appeared in `changed_files` while contributing no hunk - a driver
    then reviews a filename with no content and either integrates it unread or rejects the run for
    producing an empty file. Exit 1 stays normal (files always differ against /dev/null); only
    ABOVE 1 is an error."""
    m = WorktreeManager(repo)
    wt = m.create("task_diff_broken")
    (wt.path / "new.txt").write_text("brand new\n")

    real_git = m._git

    def flaky_git(*args: str, cwd: Path | None = None) -> subprocess.CompletedProcess[str]:
        if "--no-index" in args:
            return subprocess.CompletedProcess(
                args=list(args), returncode=128, stdout="", stderr="fatal: cannot read 'new.txt'"
            )
        return real_git(*args, cwd=cwd)

    monkeypatch.setattr(m, "_git", flaky_git)
    with pytest.raises(WorktreeError) as exc:
        m.diff(wt)
    assert "new.txt" in str(exc.value), "the failure must name the file whose content is missing"


def test_diff_still_accepts_the_normal_exit_1_from_no_index(repo: Path) -> None:
    """The guard must not break the happy path: `--no-index` exits 1 whenever the files differ,
    which against /dev/null is always, so 1 can never be treated as a failure."""
    m = WorktreeManager(repo)
    wt = m.create("task_diff_exit1")
    (wt.path / "new.txt").write_text("brand new\n")
    assert "brand new" in m.diff(wt)
