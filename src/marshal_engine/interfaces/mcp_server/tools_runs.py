
from __future__ import annotations

from typing import TYPE_CHECKING, Annotated, Any

from pydantic import BaseModel, Field


from .context import ToolContext

if TYPE_CHECKING:  # the mcp SDK is an optional extra; only needed for typing here
    from mcp.server.mcpserver import MCPServer
from .schema import (
    Job,
    _DESC_BACKEND,
    _DESC_BASE_BRANCH,
    _DESC_CLIENT,
    _DESC_CONTEXT,
    _DESC_DURATION,
    _DESC_GOAL,
    _DESC_MODEL,
    _DESC_OUTPUT_SCHEMA,
    _DESC_ARTIFACTS_FROM,
    _DESC_READ_PATHS,
    _DESC_RUN_ID,
    _DESC_TASK_ID,
    _DESC_TASK_KIND,
    _DESC_WORKSPACE,
    _DESC_WS_HINT,
)

def register(app: "MCPServer", ctx: ToolContext) -> None:
    """Register this group's tools on ``app``."""
    registry = ctx.registry
    offload = ctx.offload
    ws_call = ctx.ws_call
    run_call = ctx.run_call
    tag = ctx.tag

    @app.tool()
    async def run_agent(
        goal: Annotated[str, Field(description=_DESC_GOAL)],
        client: Annotated[str | None, Field(description=_DESC_CLIENT + " Omit for an ad-hoc (backend, model) spawn.")] = None,
        task_id: Annotated[str | None, Field(description=_DESC_TASK_ID)] = None,
        task_kind: Annotated[str | None, Field(description=_DESC_TASK_KIND)] = None,
        context_files: Annotated[list[str] | None, Field(description=_DESC_CONTEXT)] = None,
        read_paths: Annotated[list[str] | None, Field(description=_DESC_READ_PATHS)] = None,
        artifacts_from: Annotated[list[str] | None, Field(description=_DESC_ARTIFACTS_FROM)] = None,
        base_branch: Annotated[str | None, Field(description=_DESC_BASE_BRANCH)] = None,
        model: Annotated[str | None, Field(description=_DESC_MODEL)] = None,
        backend: Annotated[str | None, Field(description=_DESC_BACKEND)] = None,
        duration: Annotated[str | int | None, Field(description=_DESC_DURATION)] = None,
        output_schema: Annotated[
            dict[str, Any] | None, Field(description=_DESC_OUTPUT_SCHEMA)
        ] = None,
        workspace: Annotated[str | None, Field(description=_DESC_WORKSPACE)] = None,
    ) -> dict[str, Any]:
        """Delegate a goal to a worker agent in an isolated git worktree (in `workspace`'s repo);
        returns the run record. Product may be a diff or text - both first-class (see collect_run).

        Blocks until finished; for long work prefer spawn (returns at once + cancellable).
        `model` overrides the client's resolved model when `client` is set; for an ad-hoc spawn,
        pass `backend` (+ optional `model`) with no `client`. `duration` overrides the resolved
        timeout (a preset name or positive seconds). `base_branch` bases the worktree on a branch
        other than HEAD (e.g. a prior run's branch after commit_run). `output_schema` requests
        schema-validated structured output on the final message. `task_kind` tags the kind of
        work for the usage ledger (routing facts)."""
        return await ws_call(
            workspace,
            lambda svc: svc.run_agent(
                client, goal, task_id=task_id, context_files=context_files,
                read_paths=read_paths, artifacts_from=artifacts_from, base_branch=base_branch, model=model,
                backend=backend, duration=duration, output_schema=output_schema,
                task_kind=task_kind,
            ),
        )

    @app.tool()
    async def run_many(
        jobs: Annotated[list[Job], Field(description="Jobs to run in parallel, each in its own worktree.")],
        max_concurrency: Annotated[int, Field(description="Max jobs running at once (across all workspaces in the batch).")] = 4,
        workspace: Annotated[
            str | None,
            Field(
                description=(
                    "Default workspace for jobs that omit per-job `workspace` "
                    "(from list_workspaces); defaults to the primary workspace."
                )
            ),
        ] = None,
    ) -> list[dict[str, Any]]:
        """Delegate several goals in parallel, each to its own worktree. Jobs may target different
        registered workspaces via per-job `workspace` (call-level `workspace` is the default for
        jobs that omit it). Each job's product may be a diff or text. Optional per-job `then` runs
        a follow-up in the same worker as soon as that job's primary finishes — it does not wait
        for sibling jobs. Returns one result object per input job (input order), each tagged with
        the workspace it ran in. Ledgers and worktrees stay per-workspace; only the concurrency
        cap is shared."""
        paired = await offload(
            registry.run_many,
            [j.model_dump() for j in jobs],
            max_concurrency=max_concurrency,
            default_workspace=workspace,
        )

        def dump_chain(result: Any) -> dict[str, Any]:
            if isinstance(result, BaseModel):
                return result.model_dump(mode="json")
            assert isinstance(result, dict)
            return result

        out: list[dict[str, Any]] = []
        for ws, chain in paired:
            payload = dump_chain(chain)
            out.append(tag(payload, ws))
        return out

    @app.tool()
    async def spawn(
        goal: Annotated[str, Field(description=_DESC_GOAL)],
        client: Annotated[str | None, Field(description=_DESC_CLIENT + " Omit for an ad-hoc (backend, model) spawn.")] = None,
        task_id: Annotated[str | None, Field(description=_DESC_TASK_ID)] = None,
        task_kind: Annotated[str | None, Field(description=_DESC_TASK_KIND)] = None,
        context_files: Annotated[list[str] | None, Field(description=_DESC_CONTEXT)] = None,
        read_paths: Annotated[list[str] | None, Field(description=_DESC_READ_PATHS)] = None,
        artifacts_from: Annotated[list[str] | None, Field(description=_DESC_ARTIFACTS_FROM)] = None,
        base_branch: Annotated[str | None, Field(description=_DESC_BASE_BRANCH)] = None,
        model: Annotated[str | None, Field(description=_DESC_MODEL)] = None,
        backend: Annotated[str | None, Field(description=_DESC_BACKEND)] = None,
        duration: Annotated[str | int | None, Field(description=_DESC_DURATION)] = None,
        output_schema: Annotated[
            dict[str, Any] | None, Field(description=_DESC_OUTPUT_SCHEMA)
        ] = None,
        workspace: Annotated[str | None, Field(description=_DESC_WORKSPACE)] = None,
    ) -> dict[str, Any]:
        """Start a worker agent in the background in `workspace`'s repo; returns its RUNNING record
        immediately — before worktree provisioning (`setup_cmd` / read_paths) and before the agent
        starts. Same delegation primitive as run_agent (product may be a diff or text).
        Poll get_run/status during setup; cancel_run stops an in-flight setup process group when
        its pid is known (otherwise stamps cancelled and skips the agent). `model`/`backend`/
        `duration`/`base_branch`/`output_schema`/`task_kind` follow the same rules as run_agent
        (override the client's model, ad-hoc spawn by bare backend, per-spawn timeout override,
        chain off a prior run's branch, request schema-validated structured output, or tag the
        kind of work for the usage ledger)."""
        return await ws_call(
            workspace,
            lambda svc: svc.spawn(
                client, goal, task_id=task_id, context_files=context_files,
                read_paths=read_paths, artifacts_from=artifacts_from, base_branch=base_branch, model=model,
                backend=backend, duration=duration, output_schema=output_schema,
                task_kind=task_kind,
            ),
        )

    @app.tool()
    async def cancel_run(
        run_id: Annotated[str, Field(description=_DESC_RUN_ID)],
        workspace: Annotated[str | None, Field(description=_DESC_WS_HINT)] = None,
    ) -> dict[str, Any]:
        """Cancel a running run by id (process-group SIGTERM); returns the updated run record."""
        return await run_call(run_id, workspace, lambda svc: svc.cancel_run(run_id))

    @app.tool()
    async def benchmark(
        goal: Annotated[str, Field(description=_DESC_GOAL)],
        clients: Annotated[list[str], Field(description="Client names to race the same goal through.")],
        task_id: Annotated[str | None, Field(description=_DESC_TASK_ID)] = None,
        max_concurrency: Annotated[int, Field(description="Max clients running at once.")] = 4,
        workspace: Annotated[str | None, Field(description=_DESC_WORKSPACE)] = None,
    ) -> dict[str, Any]:
        """Run one goal through several clients (routing strategies) in one workspace and compare
        cost/latency/outcome."""
        return await ws_call(
            workspace,
            lambda svc: svc.benchmark(goal, clients, task_id=task_id, max_concurrency=max_concurrency),
        )
