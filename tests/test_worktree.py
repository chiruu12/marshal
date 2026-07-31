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
    from marshal_engine.worktree import Worktree

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
    from marshal_engine.worktree import Worktree

    m = WorktreeManager(repo)
    m.base_dir.mkdir(parents=True)
    poisoned = Worktree(task_id="x", path=m.base_dir, branch="marshal/x")
    with pytest.raises(WorktreeError, match="outside base dir"):
        m.remove(poisoned)


def test_validate_worktree_id_happy_and_task_cap() -> None:
    from marshal_engine.worktree import MAX_TASK_ID_LEN, validate_worktree_id

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
    from marshal_engine.worktree import validate_run_id

    assert validate_run_id(good_id) == good_id


@pytest.mark.parametrize(
    "bad_id",
    ["", ".", "..", "../x", "foo/bar", "a\\b", ".hidden", "-lead", "café", "a\x00b", "a" * 129],
)
def test_validate_run_id_refuses_unsafe_ids(bad_id: str) -> None:
    from marshal_engine.worktree import validate_run_id

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
    from marshal_engine.env import redact_secrets
    from marshal_engine.worktree import _VERIFY_OUTPUT_CAP

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


# --- failure atomicity (#143) -----------------------------------------------------------------


def test_setup_oserror_tears_down_and_raises(repo: Path, monkeypatch: pytest.MonkeyPatch) -> None:
    """A generic OSError from the setup binary must become WorktreeError with teardown (M6)."""
    m = WorktreeManager(
        repo,
        setup_cmd=[sys.executable, "-c", "pass"],
    )
    wt = m.create("setup_eacces")
    real_run = subprocess.run

    def boom(*args: object, **kwargs: object) -> subprocess.CompletedProcess[str]:
        cmd = args[0] if args else kwargs.get("args")
        if isinstance(cmd, (list, tuple)) and cmd and cmd[0] == sys.executable:
            raise PermissionError("injected EACCES")
        return real_run(*args, **kwargs)  # type: ignore[arg-type]

    monkeypatch.setattr(subprocess, "run", boom)
    with pytest.raises(WorktreeError, match="could not run"):
        m.setup(wt)
    assert not (m.base_dir / "setup_eacces").exists()
    branches = subprocess.run(
        ["git", "-C", str(repo), "branch", "--list", "marshal/setup_eacces"],
        capture_output=True,
        text=True,
    ).stdout
    assert "marshal/setup_eacces" not in branches


def test_verify_oserror_reports_not_raises(repo: Path, monkeypatch: pytest.MonkeyPatch) -> None:
    """verify() already catches OSError; pin the path so a PermissionError stays soft (M6)."""
    m = WorktreeManager(
        repo,
        verify_cmd=[sys.executable, "-c", "pass"],
    )
    wt = m.create("verify_eacces")
    real_run = subprocess.run

    def boom(*args: object, **kwargs: object) -> subprocess.CompletedProcess[str]:
        cmd = args[0] if args else kwargs.get("args")
        if isinstance(cmd, (list, tuple)) and cmd and cmd[0] == sys.executable:
            raise PermissionError("injected EACCES")
        return real_run(*args, **kwargs)  # type: ignore[arg-type]

    monkeypatch.setattr(subprocess, "run", boom)
    ok, output = m.verify(wt)
    assert ok is False
    assert "could not run" in output
    assert wt.path.exists()  # verify never tears down


def test_create_failure_deletes_leaked_branch(repo: Path, monkeypatch: pytest.MonkeyPatch) -> None:
    """A failed `git worktree add -b` must not leave the branch behind (M7)."""
    m = WorktreeManager(repo)
    real_git = m._git

    def flaky(*args: str, cwd: Path | None = None) -> subprocess.CompletedProcess[str]:
        if args[:2] == ("worktree", "add") and "-b" in args:
            # Reproduce git's leak: create the branch, then fail the worktree checkout.
            branch = args[args.index("-b") + 1]
            real_git("branch", branch, cwd=cwd)
            return subprocess.CompletedProcess(
                args=["git", *args], returncode=1, stdout="", stderr="simulated worktree add fail"
            )
        return real_git(*args, cwd=cwd)

    monkeypatch.setattr(m, "_git", flaky)
    with pytest.raises(WorktreeError, match="worktree add failed"):
        m.create("leak_branch")
    branches = subprocess.run(
        ["git", "-C", str(repo), "branch", "--list", "marshal/leak_branch"],
        capture_output=True,
        text=True,
    ).stdout
    assert "marshal/leak_branch" not in branches
    # Retry on the same id must succeed (the whole point of deleting the leaked branch).
    monkeypatch.undo()
    wt = m.create("leak_branch")
    assert wt.path.exists()
    m.remove(wt)


def test_create_failure_preserves_preexisting_branch(repo: Path) -> None:
    """A failed add must NOT `branch -D` a pre-existing same-named branch (M7 data-loss)."""
    m = WorktreeManager(repo)

    def git(*args: str) -> str:
        return subprocess.run(
            ["git", "-C", str(repo), *args],
            check=True,
            capture_output=True,
            text=True,
        ).stdout.strip()

    # Unmerged work on the exact branch name create() would use.
    git("checkout", "-b", "marshal/preexist")
    (repo / "important.txt").write_text("keep me\n")
    git("add", "important.txt")
    git("commit", "-m", "unmerged work")
    tip = git("rev-parse", "HEAD")
    git("checkout", "-")  # back to the original branch

    with pytest.raises(WorktreeError, match="worktree add failed"):
        m.create("preexist")  # -b rejects: branch already exists

    assert git("rev-parse", "marshal/preexist") == tip  # tip (and unmerged work) survived
    listed = subprocess.run(
        ["git", "-C", str(repo), "branch", "--list", "marshal/preexist"],
        capture_output=True,
        text=True,
    ).stdout
    assert "marshal/preexist" in listed
    assert not (repo / "important.txt").exists()  # main checkout untouched


def test_create_failure_cleanup_timeout_does_not_mask_add_error(
    repo: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    """A TimeoutExpired / WorktreeError from best-effort `-D` must not replace the add error."""
    m = WorktreeManager(repo)
    real_git = m._git

    def flaky(*args: str, cwd: Path | None = None) -> subprocess.CompletedProcess[str]:
        if args[:2] == ("worktree", "add") and "-b" in args:
            branch = args[args.index("-b") + 1]
            real_git("branch", branch, cwd=cwd)
            return subprocess.CompletedProcess(
                args=["git", *args], returncode=1, stdout="", stderr="simulated worktree add fail"
            )
        if args[:2] == ("branch", "-D"):
            # What `_git` raises when the cleanup itself times out.
            raise WorktreeError("git 'branch -D marshal/mask_me' timed out after 30s")
        return real_git(*args, cwd=cwd)

    monkeypatch.setattr(m, "_git", flaky)
    with pytest.raises(WorktreeError, match="worktree add failed.*simulated worktree add fail"):
        m.create("mask_me")


def test_create_failure_skips_delete_on_already_exists_despite_stale_probe(
    repo: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    """TOCTOU: probe says absent, but add fails with 'already exists' → never `-D` (M7)."""
    m = WorktreeManager(repo)
    real_git = m._git
    # Concurrent creator's branch (exists before add; probe will lie and say absent).
    real_git("branch", "marshal/race")
    tip = real_git("rev-parse", "marshal/race").stdout.strip()
    deleted: list[str] = []

    def flaky(*args: str, cwd: Path | None = None) -> subprocess.CompletedProcess[str]:
        if args[:2] == ("show-ref", "--verify"):
            # Stale probe: report absent even though the concurrent branch is already there.
            return subprocess.CompletedProcess(
                args=["git", *args], returncode=1, stdout="", stderr=""
            )
        if args[:2] == ("worktree", "add") and "-b" in args:
            branch = args[args.index("-b") + 1]
            return subprocess.CompletedProcess(
                args=["git", *args],
                returncode=128,
                stdout="",
                stderr=f"fatal: a branch named '{branch}' already exists\n",
            )
        if args[:2] == ("branch", "-D"):
            deleted.append(args[2] if len(args) > 2 else "")
            return real_git(*args, cwd=cwd)
        return real_git(*args, cwd=cwd)

    monkeypatch.setattr(m, "_git", flaky)
    with pytest.raises(WorktreeError, match="already exists"):
        m.create("race")
    assert deleted == [], f"branch -D must not run on already-exists; got {deleted!r}"
    assert real_git("rev-parse", "marshal/race").stdout.strip() == tip


def test_merged_diff_files_raises_on_git_failure(
    repo: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    """merged_diff_files must raise WorktreeError on git failure, matching merged_diff (M5)."""
    m = WorktreeManager(repo)
    real_git = m._git

    def flaky(*args: str, cwd: Path | None = None) -> subprocess.CompletedProcess[str]:
        if args[:1] == ("diff",) and "--name-only" in args:
            return subprocess.CompletedProcess(
                args=["git", *args], returncode=128, stdout="", stderr="fatal: bad revision"
            )
        return real_git(*args, cwd=cwd)

    monkeypatch.setattr(m, "_git", flaky)
    with pytest.raises(WorktreeError, match="could not list files"):
        m.merged_diff_files("marshal/missing", "main")


def test_branch_tip_raises_on_unresolvable_ref(repo: Path) -> None:
    """Failed rev-parse must raise — never return the ref name as if it were a sha (#173)."""
    m = WorktreeManager(repo)
    missing = "definitely/not/a/ref"
    # Pre-fix: git echoed the argument on stdout and branch_tip returned it unchecked.
    raw = subprocess.run(
        ["git", "-C", str(repo), "rev-parse", missing],
        capture_output=True,
        text=True,
    )
    assert raw.returncode != 0
    assert raw.stdout.strip() == missing
    with pytest.raises(WorktreeError, match="could not resolve tip"):
        tip = m.branch_tip(missing)
        raise AssertionError(f"branch_tip must not return the ref name; got {tip!r}")


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
