"""Fail-closed path-segment id rules for worktree dirs, run ids, and task ids.

Shared by core types (TaskSpec) and the worktree/state/log boundaries so the charset
and length checks live in exactly one place — a security boundary (path-traversal refusal).
"""

from __future__ import annotations

import re

# Charset keeps workflow `hex.label` and backend-shaped segments (`command-code`) valid;
# leading `.`/`-`, separators, unicode, and over-length ids are rejected (never rewritten).
# task_id (grouping key) is capped tighter so composed run_id `task.backend.<uuid8>` fits
# under MAX_WORKTREE_ID_LEN (backends today ≤ ~12 chars).
MAX_WORKTREE_ID_LEN = 128
MAX_TASK_ID_LEN = 64
_SAFE_ID_RE = re.compile(r"^[A-Za-z0-9][A-Za-z0-9._-]*$")


def _unsafe_id_reason(kind: str, value: str, *, max_len: int) -> str | None:
    """The refusal reason if `value` is not a safe flat path segment, else ``None``.

    ``kind`` is the id's role in the message ("worktree id" / "run_id") so each caller keeps
    its own wording while the RULES live in exactly one place.
    """
    if not value:
        return f"unsafe {kind}: empty"
    if len(value) > max_len:
        return f"unsafe {kind}: {value!r} exceeds max length {max_len}"
    if value in (".", "..") or not _SAFE_ID_RE.fullmatch(value):
        return (
            f"unsafe {kind}: {value!r} "
            f"(must match {_SAFE_ID_RE.pattern}, no leading '.' or '-')"
        )
    return None


def validate_worktree_id(task_id: str, *, max_len: int = MAX_WORKTREE_ID_LEN) -> str:
    """Return `task_id` if it is a safe flat path segment; raise ``ValueError`` otherwise.

    Allowed: ``[A-Za-z0-9._-]``, must start with alphanumeric, length 1..``max_len``.
    Rejects empty / ``.`` / ``..`` / leading ``.`` or ``-`` / slashes / spaces / unicode.
    Fail closed — never sanitize-rewrites the input.
    """
    reason = _unsafe_id_reason("worktree id", task_id, max_len=max_len)
    if reason is not None:
        raise ValueError(reason)
    return task_id


def validate_run_id(run_id: str) -> str:
    """Return `run_id` if it is a safe flat path segment; raise ``ValueError`` otherwise.

    A run_id becomes the ledger filename (``runs/<run_id>.json``) and the log filename
    (``logs/<run_id>.log``), and the workspace registry stats it against every registered
    repo's ledger to find its owner - so an unvalidated id is both a path-traversal read
    and a cross-workspace tenant escape. Same fail-closed rules as ``validate_worktree_id``
    (a production run_id is ``task.backend.<uuid8>``, which fits by construction).
    """
    reason = _unsafe_id_reason("run_id", run_id, max_len=MAX_WORKTREE_ID_LEN)
    if reason is not None:
        raise ValueError(reason)
    return run_id
