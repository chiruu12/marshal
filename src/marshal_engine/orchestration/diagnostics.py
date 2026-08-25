"""Driver-facing explanations for the ways a run can go wrong after the agent is done.

Every function here builds a *message*, not a decision - nothing in this module changes state. They
live together because they share one editorial rule: report what was measured, and offer the likely
cause rather than asserting one. A confident wrong cause is worse than a vague right one, which is
exactly the failure ``_orphaned_base_diagnosis`` exists to remove.
"""

from __future__ import annotations

from ..runtime.state import RunRecord
from ..runtime.worktree import WorktreeError, WorktreeManager


def _base_branch_drift_warning(rec: RunRecord | None, target: str) -> tuple[bool, str]:
    """Warn when integrate's target differs from the branch the run was spawned from."""
    if rec is None or rec.base_branch is None or rec.base_branch == target:
        return False, ""
    return True, f"warning: run was based on {rec.base_branch!r}, merging into {target!r}"


def _orphaned_base_diagnosis(
    worktrees: WorktreeManager, rec: RunRecord | None, target: str
) -> str:
    """Explain a conflict whose real cause is that the run's base is reachable from nothing.

    Rewriting history (amend, squash, soft-reset-and-recommit) while an agent works leaves its
    branch hanging off a commit no longer reachable from anything. Every file then reads as changed
    on both sides, so git reports conflicts in files the agent never touched - and the conflict list
    actively misleads, because the real cause is not in it.

    Two conditions, and BOTH are required:

    1. the base is not reachable from the merge target - otherwise there is no base problem at all;
    2. no surviving ref reaches it either. A run spawned with `base_branch` onto another branch
       also fails (1) while being entirely healthy, so testing only (1) would announce a problem
       for a supported flow and misdirect the very conflict this exists to explain.

    The message REPORTS the observation and OFFERS the likely causes rather than asserting one.
    `base_branch` is passed through verbatim, so it may be a tag, a raw sha, or a branch since
    deleted - all of which reach this state with no rewrite involved. Naming a rewrite as fact
    would put a confident wrong cause where a vague right one belongs, which is the failure this
    whole diagnosis exists to remove. What is *measured* - nothing reaches this base - holds in
    every one of those cases, and so does the remedy, which is why the message leads with it.

    Reachability, not existence: the reflog keeps an orphaned commit alive as an object for a good
    while, so an existence check answers "fine" exactly when the diagnosis is most needed.
    """
    if rec is None or not rec.base_commit:
        return ""
    try:
        if worktrees.is_ancestor(rec.base_commit, target):
            return ""
        if worktrees.any_user_ref_contains(rec.base_commit):
            return ""
    except WorktreeError:
        return ""
    return (
        f"the commit this run was based on ({rec.base_commit[:12]}) is reachable from no branch or "
        "tag, so git is merging against a base that is no longer in history and the conflicting "
        "files above are probably not the real cause. Usually this means history was rewritten "
        "(amend / squash / reset) while the run was in flight; a deleted base branch, or a "
        "`base_branch` naming a commit that was never on one, do it too. Re-run the task on the "
        "current branch, or cherry-pick the run's own commits onto it."
    )


def _deferred_provision_error(exc: BaseException) -> str:
    """Phase-named error for a spawn-path provision/setup failure (never a bare str(exc))."""
    msg = str(exc)
    if isinstance(exc, WorktreeError) and "worktree setup" in msg:
        return f"fleet: setup: {exc}"
    return f"fleet: provision: {exc}"


def _worktree_gone_message(rec: RunRecord) -> str:
    """Driver-facing reason when collect/integrate/commit hit a torn-down worktree."""
    if rec.error:
        return rec.error
    path = rec.worktree or ""
    return f"worktree for run {rec.run_id!r} no longer exists: {path}"


def _live_agent_message(rec: RunRecord) -> str:
    """Driver-facing reason when a record reads terminal but its agent is still writing.

    `cancel_run` on a run this process did not start stamps a terminal status without being able
    to signal the process group, so the record can read `cancelled` while the agent keeps editing
    its worktree. Says what to do about it: the run cannot be waited on through Marshal, because
    the status it would be waited on for is already stamped.
    """
    return (
        f"run {rec.run_id!r} reads {rec.status!r}, but its agent is still alive at pid {rec.pid}. "
        f"A terminal status can be stamped without the process having been signalled (a cancel "
        f"from another Marshal process cannot signal it), so the worktree may still be mid-write "
        f"and committing it now could capture half-written files. Wait for the process to exit or "
        f"stop it yourself, then retry."
    )
