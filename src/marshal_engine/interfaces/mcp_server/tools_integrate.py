
from __future__ import annotations

from typing import TYPE_CHECKING, Annotated, Any, Literal

from pydantic import Field

from .context import ToolContext

if TYPE_CHECKING:  # the mcp SDK is an optional extra; only needed for typing here
    from mcp.server.mcpserver import MCPServer
from .schema import _DESC_RUN_ID, _DESC_WORKSPACE, _DESC_WS_HINT


def register(app: MCPServer, ctx: ToolContext) -> None:
    """Register this group's tools on ``app``."""
    ws_call = ctx.ws_call
    run_call = ctx.run_call

    @app.tool()
    async def commit_run(
        run_id: Annotated[str, Field(description=_DESC_RUN_ID)],
        message: Annotated[str | None, Field(description="Commit message (default: marshal: <run_id>).")] = None,
        workspace: Annotated[str | None, Field(description=_DESC_WS_HINT)] = None,
    ) -> dict[str, Any]:
        """Freeze a finished run's work as a commit on its OWN branch (the driver's branch is untouched).

        Use this to CHAIN dependent work: commit_run(A), then spawn(B, base_branch=A's branch) so B
        builds on A's actual output. Without it, basing a run on a prior run's branch sees only the
        spawn base (the agent left its work uncommitted). NOT a substitute for integrate - it never
        merges into your branch. To chain, use the returned `branch`/`commit` regardless of status.
        Status is one of: committed | clean (no new commit needed - tree already clean, NOT "branch
        empty") | blocked (run still running) | error (a git op needs a human, see message)."""
        return await run_call(
            run_id, workspace, lambda svc: svc.commit_run(run_id, message=message),
        )

    @app.tool()
    async def set_outcome(
        run_id: Annotated[str, Field(description=_DESC_RUN_ID)],
        outcome: Annotated[
            Literal["integrated", "rejected", "abandoned", "advisory"], Field(description=(
                "Your judgment about the WORK, distinct from the run's process `status`. "
                "`rejected` = you reviewed it and it was wrong or off-scope; `abandoned` = you "
                "gave up on it (superseded, no longer needed); `advisory` = it produced findings "
                "you USED and there was nothing to merge (a read-only review, audit, or plan "
                "panel) - use it instead of `abandoned` there, which reads as giving up and "
                "counts against the client's integration rate. `integrated` is written for you "
                "by `integrate`; passing it here is refused unless a merge actually landed."
            ))],
        note: Annotated[str | None, Field(description=(
            "Short reason, for your future self reading `routing`. Truncated at 2000 chars."
        ))] = None,
        workspace: Annotated[str | None, Field(description=_DESC_WS_HINT)] = None,
    ) -> dict[str, Any]:
        """Record whether a finished run's work was any good - the input `routing` ranks on.

        RECORD YOUR REJECTIONS. Declining to integrate leaves no trace, so an unjudged run and a
        run you threw away look identical; every routing rate is computed over judged runs only,
        and without rejections it reads 100% for everyone and means nothing.

        `integrated` is sticky - a merge commit is a fact, not an opinion - so overwriting it
        returns status `conflict` rather than an error, and the record is left alone. For the same
        reason you cannot ASSERT it: recording `integrated` on a run that was never merged is
        refused, because a permanent verdict for a merge that never happened could never be
        corrected. A run that has not finished cannot be judged at all. Re-recording the same value
        is `unchanged`. Status is one of: recorded | unchanged | conflict."""
        return await run_call(
            run_id, workspace, lambda svc: svc.set_outcome(run_id, outcome, note=note),
        )

    @app.tool()
    async def integrate(
        run_id: Annotated[str, Field(description=_DESC_RUN_ID)],
        message: Annotated[str | None, Field(description=(
            "Commit message for the run's work. Write it in the target repo's own convention - "
            "you reviewed the diff, so you are the one who knows what it did. Omitted, it falls "
            "back to 'marshal: integrate <run_id>', which describes the tooling rather than the "
            "change and usually has to be rewritten afterwards."
        ))] = None,
        cleanup: Annotated[bool, Field(description="Remove the worktree after a successful merge.")] = False,
        workspace: Annotated[str | None, Field(description=_DESC_WS_HINT)] = None,
    ) -> dict[str, Any]:
        """Merge a run's worktree branch into its workspace's current branch.

        REVIEW WHAT IT PRODUCED FIRST with collect_run - `exited_clean` means the process exited 0,
        NOT that the work is correct. Integrate one diff run at a time; skip text-only runs.
        Pass `message` to commit in the repo's own convention. Outcome status is one of: merged |
        conflict (aborted, repo left clean) | blocked (target dirty/detached, or the run is still
        running - fix and retry) | empty (no file changes to merge - an outcome, not a fault) |
        error (a git op needs a human)."""
        return await run_call(
            run_id, workspace, lambda svc: svc.integrate(run_id, message=message, cleanup=cleanup),
        )

    @app.tool()
    async def clean(
        scope: Annotated[Literal["merged", "finished", "all"], Field(description="'merged' (integrated only, safest) | 'finished' (default: merged + failed/timed_out/cancelled/empty/verify_failed; keeps un-integrated succeeded work - review verify_failed diffs before cleaning) | 'all' (every terminal run; DESTRUCTIVE - also drops un-reviewed succeeded runs' branches).")] = "finished",
        run_ids: Annotated[list[str] | None, Field(description="Clean exactly these run ids instead of by scope (a running run is refused; older_than_hours is ignored).")] = None,
        older_than_hours: Annotated[float | None, Field(description="Only clean runs that ended at least this many hours ago (ignored when run_ids is given).")] = None,
        dry_run: Annotated[bool, Field(description="Report what would be removed without touching anything.")] = False,
        workspace: Annotated[str | None, Field(description=_DESC_WORKSPACE)] = None,
    ) -> dict[str, Any]:
        """Tear down finished runs' worktrees + branches in a workspace to reclaim disk.

        Never cleans a running run. The immutable usage ledger is untouched and run-state records
        are kept, so status/cost history stays queryable (a cleaned run's worktree just no longer
        exists). `scope="all"` is destructive - it deletes the branch of every terminal run,
        including un-integrated `succeeded` work (commits survive only in git's reflog until gc).
        Scope-mode cleans also reap ORPHANS: worktree dirs under Marshal's own base dir whose run
        record is missing/unreadable (they are invisible to ledger-driven cleanup and would leak
        forever). Returns {removed, orphans_removed, skipped, errors, dry_run}. Use dry_run first
        to preview."""
        return await ws_call(
            workspace,
            lambda svc: svc.clean(
                scope=scope, run_ids=run_ids, older_than_hours=older_than_hours, dry_run=dry_run,
            ),
        )
