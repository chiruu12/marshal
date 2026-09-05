"""Keeping Marshal's own state out of the user's `git status`."""

from __future__ import annotations

import os
import subprocess
import threading
from pathlib import Path

import pytest

from marshal_engine.runtime.git_exclude import (
    GitExcludeError,
    append_git_exclude,
    try_append_git_exclude,
)


def _init(root: Path) -> None:
    for args in (("init", "-q"), ("config", "user.email", "t@x"), ("config", "user.name", "t")):
        subprocess.run(["git", "-C", str(root), *args], check=True, capture_output=True)


def _exclude_text(repo: Path) -> str:
    path = repo / ".git" / "info" / "exclude"
    return path.read_text() if path.exists() else ""


def test_the_entry_lands_in_the_local_exclude_not_the_users_gitignore(tmp_path: Path) -> None:
    """`.gitignore` belongs to the user.

    Editing it would put a diff they did not write into their working tree, and a merge conflict
    into a shared repo. The exclude file is per-clone and uncommitted, so the same rule applies
    everywhere without anyone reviewing a change Marshal made on their behalf.
    """
    _init(tmp_path)
    append_git_exclude(tmp_path, ".marshal/")

    assert ".marshal/" in _exclude_text(tmp_path).splitlines()
    assert not (tmp_path / ".gitignore").exists()


def test_appending_twice_does_not_grow_the_file(tmp_path: Path) -> None:
    """Called on every Fleet construction, so a repeat must be a no-op rather than a duplicate."""
    _init(tmp_path)
    for _ in range(5):
        append_git_exclude(tmp_path, ".marshal/")

    assert _exclude_text(tmp_path).splitlines().count(".marshal/") == 1


def test_an_existing_exclude_file_is_preserved(tmp_path: Path) -> None:
    """The file is the user's too - other entries in it must survive, with no line joined."""
    _init(tmp_path)
    exclude = tmp_path / ".git" / "info" / "exclude"
    exclude.parent.mkdir(parents=True, exist_ok=True)
    exclude.write_text("*.log\nbuild/")  # no trailing newline, deliberately

    append_git_exclude(tmp_path, ".marshal/")

    lines = _exclude_text(tmp_path).splitlines()
    assert lines == ["*.log", "build/", ".marshal/"]


def test_concurrent_appends_do_not_duplicate_the_entry(tmp_path: Path) -> None:
    """REGRESSION: read-check-append without a lock let concurrent writers each miss the other's
    line and append duplicates, breaking the idempotency the function documents."""
    _init(tmp_path)
    barrier = threading.Barrier(8)
    errors: list[BaseException] = []

    def worker() -> None:
        try:
            # Generous: the barrier only exists to maximise contention, and its timeout is not
            # what this test is asserting. A loaded machine (a parallel fleet, a busy CI runner)
            # can take seconds just to schedule all eight threads, and a BrokenBarrierError there
            # was collected as a worker failure - reporting a lock defect for a slow scheduler.
            barrier.wait(timeout=60)
            append_git_exclude(tmp_path, ".marshal/")
        except BaseException as exc:  # noqa: BLE001 - collect for the main thread
            errors.append(exc)

    threads = [threading.Thread(target=worker) for _ in range(8)]
    for thread in threads:
        thread.start()
    for thread in threads:
        thread.join()
    assert not errors, f"workers failed: {errors!r}"
    assert _exclude_text(tmp_path).splitlines().count(".marshal/") == 1


def test_a_directory_that_is_not_a_repo_is_reported_not_raised(tmp_path: Path) -> None:
    """The strict form raises so provisioning can refuse; the best-effort form must not."""
    plain = tmp_path / "not_a_repo"
    plain.mkdir()

    with pytest.raises(GitExcludeError):
        append_git_exclude(plain, ".marshal/")

    assert try_append_git_exclude(plain, ".marshal/") is False  # never raises


def test_a_fleet_leaves_the_repo_status_clean(tmp_path: Path) -> None:
    """The user-visible claim, end to end.

    Marshal's state directory appearing as untracked noise is how a ledger and logs end up in
    someone's commit after a `git add -A`.
    """
    from marshal_engine.orchestration.fleet import Fleet

    _init(tmp_path)
    (tmp_path / "README.md").write_text("hi\n")
    subprocess.run(["git", "-C", str(tmp_path), "add", "-A"], check=True, capture_output=True)
    subprocess.run(["git", "-C", str(tmp_path), "commit", "-qm", "init"], check=True, capture_output=True)

    Fleet(tmp_path, {})
    (tmp_path / ".marshal" / "runs").mkdir(parents=True, exist_ok=True)
    (tmp_path / ".marshal" / "runs" / "r.json").write_text("{}")

    status = subprocess.run(
        ["git", "-C", str(tmp_path), "status", "--porcelain"],
        capture_output=True, text=True, check=True,
    ).stdout
    assert status.strip() == "", f"expected a clean status, got: {status!r}"


@pytest.mark.skipif(os.geteuid() == 0, reason="root ignores file permission bits")
def test_a_fleet_still_builds_when_the_exclude_cannot_be_written(tmp_path: Path) -> None:
    """Tidiness must never become a new way for a run to fail.

    Skipped as root, where `chmod 0o444` is not enforced: the write would simply succeed, the
    unwritable case this test names would never arise, and it would pass without testing it.
    """
    from marshal_engine.orchestration.fleet import Fleet

    _init(tmp_path)
    exclude = tmp_path / ".git" / "info" / "exclude"
    exclude.parent.mkdir(parents=True, exist_ok=True)
    exclude.touch()
    exclude.chmod(0o444)  # git init leaves this file present; make it unwritable
    # Assert the precondition actually holds, so a platform where the chmod is ignored fails
    # loudly here instead of reporting a pass for a guard it never reached.
    assert not os.access(exclude, os.W_OK), "precondition: the exclude file must be unwritable"
    try:
        Fleet(tmp_path, {})  # must not raise
    finally:
        exclude.chmod(0o644)


def test_a_non_utf8_exclude_file_does_not_break_anything(tmp_path: Path) -> None:
    """The file is the user's and nothing guarantees it is UTF-8.

    A latin-1 byte in it raises `UnicodeDecodeError` — which subclasses `ValueError`, not
    `OSError`, so it slipped straight past the guard and would have aborted `Fleet` construction
    despite the "never fails a run" contract. Reading bytes and decoding with replacement removes
    the failure rather than catching it downstream.
    """
    _init(tmp_path)
    exclude = tmp_path / ".git" / "info" / "exclude"
    exclude.parent.mkdir(parents=True, exist_ok=True)
    exclude.write_bytes(b"caf\xe9/\n")  # latin-1, not valid UTF-8

    append_git_exclude(tmp_path, ".marshal/")  # must not raise

    assert b".marshal/" in exclude.read_bytes()
    assert b"caf\xe9/" in exclude.read_bytes()  # the user's line survives byte-for-byte


def test_a_fleet_builds_over_a_non_utf8_exclude_file(tmp_path: Path) -> None:
    """The contract the caller was actually given."""
    from marshal_engine.orchestration.fleet import Fleet

    _init(tmp_path)
    exclude = tmp_path / ".git" / "info" / "exclude"
    exclude.parent.mkdir(parents=True, exist_ok=True)
    exclude.write_bytes(b"\xff\xfe binary junk\n")

    Fleet(tmp_path, {})  # must not raise


def test_the_entry_is_on_disk_before_the_lock_is_released(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    """REGRESSION: the lock must cover the write, not just the decision to write.

    `fh` is buffered, so unlocking in the `finally` and letting the flush land at `with`-block
    close leaves a window where the lock is free but the bytes are not on disk. A waiter admitted
    there reads a file without the entry and appends a second copy - the duplicate the lock is
    there to prevent. This widens that window deterministically instead of hoping a loaded CI
    runner hits it, which is how the concurrency test above failed.
    """
    import fcntl as _fcntl

    from marshal_engine.runtime import git_exclude as mod

    _init(tmp_path)
    real_flock = _fcntl.flock
    admitted: list[str] = []

    def slow_unlock(fd: int, op: int) -> None:
        real_flock(fd, op)
        if op == _fcntl.LOCK_UN and not admitted:
            # Stand in for the waiter the kernel would admit here: read the file the way
            # append_git_exclude does, at the instant the lock is released.
            admitted.append(_exclude_text(tmp_path))

    monkeypatch.setattr(mod.fcntl, "flock", slow_unlock)
    mod.append_git_exclude(tmp_path, ".marshal/")

    assert admitted, "the unlock path never ran - the test no longer exercises the window"
    assert ".marshal/" in admitted[0].splitlines(), (
        "the lock was released before the entry reached disk; a waiter admitted here would "
        f"append a duplicate. Saw: {admitted[0]!r}"
    )
