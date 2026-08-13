
from __future__ import annotations

from typing import TYPE_CHECKING, Annotated, Any, Literal

from pydantic import Field

from ...orchestration.teams import TeamSubject
from ..workspaces import (
    DEFAULT_WORKSPACE,
)
from .context import ToolContext

if TYPE_CHECKING:  # the mcp SDK is an optional extra; only needed for typing here
    from mcp.server.mcpserver import MCPServer
from .schema import _DESC_WORKSPACE


def register(app: "MCPServer", ctx: ToolContext) -> None:
    """Register this group's tools on ``app``."""
    registry = ctx.registry
    offload = ctx.offload
    ws_call = ctx.ws_call
    tag = ctx.tag

    @app.tool()
    async def list_workflows(
        workspace: Annotated[str | None, Field(description=_DESC_WORKSPACE)] = None,
    ) -> dict[str, Any]:
        """List declared workflow recipes (name, description, inputs, phase summary) for a workspace.

        Malformed recipe files are returned in ``errors`` (filename -> message) so a driver can tell
        a broken recipe from a missing one."""
        svc = await offload(registry.get, workspace)
        listing = await offload(svc.list_workflows)
        return tag(
            {
                "workflows": [
                    {
                        "name": w.name,
                        "description": w.description,
                        "inputs": w.inputs,
                        "phases": [{"name": p.name, "run": p.run} for p in w.phases],
                    }
                    for w in listing.workflows
                ],
                "errors": listing.errors,
            },
            workspace or DEFAULT_WORKSPACE,
        )

    @app.tool()
    async def run_workflow(
        name: Annotated[str, Field(description="Workflow recipe name (from list_workflows).")],
        inputs: Annotated[dict[str, Any] | None, Field(description="Inputs the recipe declares.")] = None,
        workspace: Annotated[str | None, Field(description=_DESC_WORKSPACE)] = None,
    ) -> dict[str, Any]:
        """Run a workflow recipe by name in `workspace`'s repo. Integration is gated off by default -
        the result's `next_actions` lists the runs to review and integrate. Validates before any spawn."""
        return await ws_call(workspace, lambda svc: svc.run_workflow(name, inputs))

    @app.tool()
    async def list_teams(
        workspace: Annotated[str | None, Field(description=_DESC_WORKSPACE)] = None,
    ) -> dict[str, Any]:
        """List declared adversarial review teams (name, description, target, roles) for a workspace.

        A team is a panel of independent read-only reviewers, each pinned to the client best at its
        lens. Malformed team files are returned in ``errors`` (filename -> message) so a driver can
        tell a broken team from a missing one."""
        svc = await offload(registry.get, workspace)
        listing = await offload(svc.list_teams)
        return tag(
            {
                "teams": [
                    {
                        "name": t.name,
                        "description": t.description,
                        "target": t.target,
                        "roles": [{"name": r.name, "client": r.client} for r in t.roles],
                    }
                    for t in listing.teams
                ],
                "errors": listing.errors,
            },
            workspace or DEFAULT_WORKSPACE,
        )

    @app.tool()
    async def run_team(
        name: Annotated[str, Field(description="Review team name (from list_teams).")],
        target: Annotated[
            Literal["run", "plan", "range", "audit"],
            Field(description=(
                "What is being reviewed; must match the team's declared target. 'run' judges a "
                "run's diff (give `run_id`), 'range' a commit range (`base`, optional `head`), "
                "'plan' free text (`text`), 'audit' the repo as it stands."
            )),
        ],
        run_id: Annotated[str | None, Field(description="Run whose diff to review (target 'run').")] = None,
        base: Annotated[str | None, Field(description="Base ref (target 'range').")] = None,
        head: Annotated[str | None, Field(description="Head ref (target 'range'; default HEAD).")] = None,
        pr: Annotated[int | None, Field(description=(
            "GitHub PR number to review (target 'range'). Fills in `base`/`head` for you, so pass "
            "it INSTEAD of them, not alongside. Needs `gh` on PATH."
        ))] = None,
        text: Annotated[str | None, Field(description="The plan to review (target 'plan').")] = None,
        paths: Annotated[
            list[str] | None,
            Field(description=(
                "Limit a 'range' diff to these paths. Use it on a large change: the subject is "
                "truncated at the tail, and git orders paths alphabetically, so src/ and tests/ "
                "are exactly what gets cut."
            )),
        ] = None,
        workspace: Annotated[str | None, Field(description=_DESC_WORKSPACE)] = None,
    ) -> dict[str, Any]:
        """Run a panel of independent, read-only reviewers and get back their reports.

        Each role reviews ONE lens on the SAME subject in parallel isolation and writes a report.
        You get `unified_report` (read this FIRST - it shows the whole panel and every review
        inline) plus per-role entries with the full text and a `report_path`; all of it is
        persisted under `.marshal/reports/<stamp>-<team>-<id>/`.

        **This tool computes no verdict.** There is no pass/fail and no tally: collecting the
        objections, weighing them against each other, and deciding what happens next is YOUR job -
        that judgment is not safely derivable from reviewer prose. Reviewers listed under
        `incomplete_roles` did not report at all; that is a missing lens, not approval. Nothing is
        ever integrated by this tool.

        **Reviewing a PR:** pass `pr=<number>` with `target='range'`. A pull request IS a commit
        range, so there is no separate 'pr' target and every `target: range` team reviews one
        unchanged; `pr` only resolves the endpoints (via `gh`) and diffs from the merge base, so the
        panel sees what the PR changed and not what the base branch gained since. The reply echoes
        `pull_request` with the title and URL - check it names the PR you meant before you read a
        word of the reviews, and note `stale: true` for an already-closed or merged PR."""
        pr_ref = None
        if pr is not None:
            if base or head:
                raise ValueError(
                    "pass either `pr` or `base`/`head`, not both: `pr` resolves the range itself, "
                    "and honouring both would review something neither argument describes."
                )
            # Same workspace `ws_call` will use below, so the PR is resolved against the repo whose
            # remote actually owns it - not the default one. `registry.get` is inside the offload,
            # not an argument to it: resolving a workspace can re-read the registry file and build a
            # service, and evaluating that on the event loop would stall every other in-flight tool
            # call (a poll, a status, a cancel) behind this one's disk work.
            pr_ref = await offload(lambda: registry.get(workspace).resolve_pr(pr))
            base, head = pr_ref.base, pr_ref.head

        subject = TeamSubject(
            kind=target, run_id=run_id, base=base, head=head, text=text, paths=paths or []
        )
        result = await ws_call(workspace, lambda svc: svc.run_team(name, subject))
        if pr_ref is not None:
            # Echoed so the driver can confirm WHAT was reviewed. A panel reporting on the wrong
            # PR reads exactly like one reporting on the right PR.
            result["pull_request"] = {
                "number": pr_ref.number, "title": pr_ref.title, "url": pr_ref.url,
                "state": pr_ref.state, "stale": pr_ref.stale,
                "base": pr_ref.base, "head": pr_ref.head,
            }
        return result
