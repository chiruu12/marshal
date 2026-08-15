"""Adding entries to a git checkout's local exclude file.

`.git/info/exclude` is the per-clone, uncommitted counterpart to `.gitignore`. Marshal writes there
rather than to the repo's `.gitignore` because that file belongs to the user: editing it would put
an unexplained diff in their working tree and, on a shared repo, a merge conflict. The exclude file
is local, so the same rule can be applied in every clone without anyone reviewing a change they did
not make.

Both callers are tidiness, never correctness - a run's product must not depend on an entry landing
here - so the fail-open helper is the one to reach for outside provisioning.
"""

from __future__ import annotations

import subprocess
from pathlib import Path

from .env import DETACHED_STDIO


class GitExcludeError(RuntimeError):
    """The checkout's exclude file could not be located."""


def append_git_exclude(checkout: Path, entry: str) -> None:
    """Add ``entry`` to ``checkout``'s ``info/exclude`` if it is not already listed.

    Asks git for the path rather than assuming ``.git/info/exclude``: in a linked worktree ``.git``
    is a file, and the exclude file lives in the shared common dir under a different path. Raises
    ``GitExcludeError`` when git cannot answer - use ``try_append_git_exclude`` where the entry is a
    nicety and a failure must not surface.
    """
    proc = subprocess.run(
        ["git", "-C", str(checkout), "rev-parse", "--git-path", "info/exclude"],
        capture_output=True,
        text=True,
        check=False,
        **DETACHED_STDIO,
    )
    if proc.returncode != 0:
        raise GitExcludeError(
            f"could not resolve exclude file for {checkout}: "
            f"{proc.stderr.strip() or proc.stdout.strip()}"
        )
    exclude = Path(proc.stdout.strip())
    if not exclude.is_absolute():
        exclude = checkout / exclude
    exclude.parent.mkdir(parents=True, exist_ok=True)
    # Decoded with replacement, not strictly. The file is the user's and nothing guarantees it is
    # UTF-8 - a latin-1 line in it would otherwise raise `UnicodeDecodeError`, which is a
    # `ValueError` and so slips past an `OSError` guard, taking down a caller that was promised this
    # could not fail. Only an ASCII membership test is needed, and the append itself never decodes,
    # so replacement costs nothing and removes the failure mode rather than catching it later.
    raw = exclude.read_bytes() if exclude.exists() else b""
    existing = raw.decode("utf-8", errors="replace")
    if entry in existing.splitlines():
        return  # idempotent: re-running must not grow the file on every call
    with exclude.open("a") as fh:
        if existing and not existing.endswith("\n"):
            fh.write("\n")
        fh.write(f"{entry}\n")


def try_append_git_exclude(checkout: Path, entry: str) -> bool:
    """Best-effort ``append_git_exclude``; returns whether it worked. Never raises.

    For the case where the entry only keeps `git status` tidy. A directory that is not a repo, a
    read-only `.git`, a full disk - none of those are reasons to fail the operation the caller was
    actually doing.
    """
    try:
        append_git_exclude(checkout, entry)
    except (GitExcludeError, OSError, ValueError, subprocess.SubprocessError):
        # `ValueError` is deliberate and not defensive padding: decode faults arrive as
        # `UnicodeDecodeError`, which subclasses it rather than `OSError`. A contract of "never
        # raises" has to cover the exception the code can actually produce, not the family it
        # looks like it should belong to.
        return False
    return True
