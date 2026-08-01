
from __future__ import annotations

from datetime import datetime, timedelta, timezone
from typing import TYPE_CHECKING, Annotated, Any

from pydantic import Field

from ...runtime.state import compact_run, filter_runs
from ...accounting.usage import UsageWindow, usage_window_since
from ..workspaces import (
    DEFAULT_WORKSPACE,
)

from .context import ToolContext

if TYPE_CHECKING:  # the mcp SDK is an optional extra; only needed for typing here
    from mcp.server.mcpserver import MCPServer
from .schema import _DESC_RUN_ID, _DESC_WORKSPACE, _DESC_WS_HINT

def register(app: "MCPServer", ctx: ToolContext) -> None:
    """Register this group's tools on ``app``."""
    registry = ctx.registry
    offload = ctx.offload
    ws_call = ctx.ws_call
    run_call = ctx.run_call
    tag = ctx.tag

    @app.tool()
    async def marshal_quickstart() -> dict[str, Any]:
        """START HERE. The canonical four-step loop, and which tool to pick when several look alike.

        Read this before choosing among the run-ish tools (run_agent / spawn / run_many /
        run_workflow) or the status-ish ones (status / get_run / collect_run / get_run_log).
        """
        return {
            "what_marshal_is": (
                "A fleet primitive: one agent spawns and coordinates many sub-agents in parallel, "
                "each in an isolated git worktree, with per-provider cost tracking. A run's "
                "product may be a DIFF or TEXT - both first-class. Marshal runs the agents; you "
                "decide. Use it to implement code, research a question across sources, review a "
                "diff, audit a codebase, or summarise - code delegation is the best-developed "
                "path, not the only one."
            ),
            "non_code_runs": (
                "A run that only reads and reasons DOES return its work: its final message is on "
                "the record as `text`, and the run is `exited_clean`. collect_run reports which "
                "artifact it was via `produced` (`diff` | `text` | `nothing`) and returns the "
                "message itself for a text run; get_run (or status(full=true)) gives you the raw "
                "record if you want it. `empty` is an outcome, not a fault: the process exited 0 "
                "with neither text nor file changes - nothing to integrate. What Marshal lacks "
                "is STRUCTURED output: the result is prose you parse. Where a backend truncates "
                "long final messages (Cursor does), have the agent write its report to a file "
                "instead - that is why the built-in review teams do so."
            ),
            "the_loop": [
                "1. doctor - is this workspace ready? Catches a missing CLI, a broken config, and "
                "a backend that recently failed on billing, BEFORE you spend a run.",
                "2. spawn (or run_agent) - delegate the work to a worker agent.",
                "3. collect_run - read what the run produced (`produced`: diff | text | nothing). "
                "`exited_clean` means the process exited 0, NOT that the work is correct. For a "
                "diff run, review before step 4; for a text run, the value is in `text` - skip "
                "integrate.",
                "4. integrate - merge a diff run's branch into yours. One run at a time. Skip for "
                "text-only work.",
            ],
            "which_run_tool": {
                "run_agent": "Blocks until the run finishes. Use for short work you want inline.",
                "spawn": "Returns immediately with a RUNNING record. Use for anything long - it "
                "does not hold your turn. Poll with get_run, stop with cancel_run.",
                "run_many": "Several jobs in parallel (optional per-job then chains). Returns when all chains finish.",
                "run_workflow": "A declarative recipe (fan-out, gates, integrate) from a YAML file.",
            },
            "which_status_tool": {
                "status": "List runs. Filtered and compact by default - pass `limit`, `status`, "
                "`task_id`, `since_hours`. Check `agent_alive` to tell 'still working' from "
                "'finished, outcome not yet written'.",
                "get_run": "One run's full record, including its final text.",
                "collect_run": "What the run produced (diff and/or text via `produced`). Review "
                "step before integrate; for text runs, read `text`.",
                "get_run_log": "One run's raw stdout/stderr. For diagnosing a failure.",
            },
            "safety": (
                "Runs are isolated in their own worktrees; a bad run costs a worktree, not your "
                "repo. Two things reach your branch: `integrate`, and `run_workflow` when the "
                "recipe declares an integrate phase with `auto: true` - read a workflow before "
                "running it. `exited_clean` means the process exited 0, which is not a claim about "
                "correctness, so review what it produced first."
            ),
            "multi_repo": (
                "Workspace-scoped tools take an optional `workspace` (the global ones - this tool, "
                "list_workspaces, add_workspace - do not). list_workspaces shows what is registered "
                "and whether each is `ready`, with a reason when it is not."
            ),
        }

    @app.tool()
    async def list_clients(
        workspace: Annotated[str | None, Field(description=_DESC_WORKSPACE)] = None,
    ) -> dict[str, Any]:
        """List configured backend clients (name, backend, model, permission, permission_fidelity)
        plus the fleet's driver-facing context, for the chosen workspace. Returns
        {clients, driver_context, workspace}."""
        return await ws_call(workspace, lambda svc: svc.list_clients())

    @app.tool()
    async def list_models(
        workspace: Annotated[str | None, Field(description=_DESC_WORKSPACE)] = None,
    ) -> dict[str, Any]:
        """List the optional `models:` catalog (id, backends, cost, quota_type, notes) plus the
        fleet's driver-facing context, for the chosen workspace. When no catalog is configured,
        `backend_models` carries what each backend's CLI reports right now, keyed by backend -
        `null` there means that CLI exposes no way to ask, NOT that the backend has no models.
        Pure data - does NOT influence routing (clients still own backend+model). Returns
        {models, backend_models, driver_context, workspace}."""
        return await ws_call(workspace, lambda svc: svc.list_models())

    @app.tool()
    async def doctor(
        workspace: Annotated[str | None, Field(description=_DESC_WORKSPACE)] = None,
    ) -> dict[str, Any]:
        """Preflight the SELECTED workspace: toolchain, repo, config, each configured backend's
        CLI availability + auth, and backend safe-edit permission_fidelity (ok for
        enforced-denies, warn for boundary-only — capability of the adapter, not per-client
        yolo/read-only resolution from list_clients). Read-only - run it before spawning to
        catch a missing/unauthenticated backend up front. Returns per-check results + a
        fails/warns roll-up + the workspace."""
        return await ws_call(workspace, lambda svc: svc.doctor())

    @app.tool()
    async def get_run(
        run_id: Annotated[str, Field(description=_DESC_RUN_ID)],
        workspace: Annotated[str | None, Field(description=_DESC_WS_HINT)] = None,
    ) -> dict[str, Any] | None:
        """Get a run record by id, located across all workspaces (or via the `workspace` hint).

        status is one of: exited_clean | empty (exited 0 with neither text nor file changes - an
        outcome, not a fault; nothing to integrate) | failed | timed_out | cancelled |
        verify_failed (had file changes but the workspace's `verify:` gate rejected them - review
        the diff and `verify_output` before deciding). Only `exited_clean` runs with a diff are
        integration candidates; for text-only work read `text` (or collect_run)."""
        resolved = await offload(registry.resolve_run, run_id, workspace)
        if resolved is None:
            return None
        name, svc = resolved
        rec = await offload(svc.get_run, run_id)
        return tag(rec.model_dump(mode="json"), name) if rec else None

    @app.tool()
    async def get_run_log(
        run_id: Annotated[str, Field(description=_DESC_RUN_ID)],
        workspace: Annotated[str | None, Field(description=_DESC_WS_HINT)] = None,
    ) -> dict[str, Any]:
        """Return a run's persisted full stdout/stderr (or null if no log was written).

        Each terminal run (success or failure) gets one file under `<base>/logs/<run_id>.log` with
        a `=== run <id> ===` header, a `--- stdout ---` section, and a `--- stderr ---` section -
        the FULL streams, not the 16KB-truncated `text` on the run record. `log` is null when no
        log exists (a run that pre-dates log storage, or a backend that crashed before producing
        one). The owning workspace is resolved by the same scan as `get_run`, with the same
        `workspace` hint."""
        resolved = await offload(registry.resolve_run, run_id, workspace)
        if resolved is None:
            return tag({"run_id": run_id, "log": None}, workspace or DEFAULT_WORKSPACE)
        name, svc = resolved
        text = await offload(svc.run_log, run_id)
        return tag({"run_id": run_id, "log": text}, name)

    @app.tool()
    async def collect_run(
        run_id: Annotated[str, Field(description=_DESC_RUN_ID)],
        workspace: Annotated[str | None, Field(description=_DESC_WS_HINT)] = None,
    ) -> dict[str, Any]:
        """Collect what a run produced: diff/changed files and/or final text via `produced`
        (read-only; nothing is merged). Branch on `produced` (`diff` | `text` | `nothing`)."""
        return await run_call(run_id, workspace, lambda svc: svc.collect_run(run_id))

    @app.tool()
    async def read_run_file(
        run_id: Annotated[str, Field(description=_DESC_RUN_ID)],
        path: Annotated[str, Field(description=(
            "Path RELATIVE to that run's worktree root. Absolute paths and '..' are refused."
        ))],
        workspace: Annotated[str | None, Field(description=_DESC_WS_HINT)] = None,
    ) -> dict[str, Any]:
        """Read one file out of a run's worktree - how one agent's output reaches the next.

        For handing over an ARTIFACT (a report, a findings file, a generated spec): read it here,
        then put the content in the next run's `goal`. That keeps the handover faithful - the next
        agent reads what the first actually wrote, instead of the driver's paraphrase of it.

        For building ON a run's code rather than reading its conclusions, use `commit_run` then
        `spawn(base_branch=<that run's branch>)` instead - the next worktree is cut from the work.

        Returns `{run_id, path, content, truncated, size_bytes}`. Check `truncated`: large files are
        clipped, and acting on a prefix while believing it is whole is the mistake worth avoiding.
        """
        return await run_call(
            run_id, workspace, lambda svc: svc.read_run_file(run_id, path),
        )

    @app.tool()
    async def status(
        workspace: Annotated[str | None, Field(description=_DESC_WORKSPACE + " Omit to list ALL workspaces.")] = None,
        limit: Annotated[int, Field(ge=1, le=500, description=(
            "Max runs to return, newest first. The reply always reports `matched` alongside "
            "`returned`, so a truncated list is never mistaken for the whole ledger."
        ))] = 50,
        status: Annotated[str | None, Field(description=(
            "Only runs with this exact status (e.g. 'running', 'exited_clean', 'empty', "
            "'verify_failed'). `empty` is an outcome (exited 0, neither text nor file changes), "
            "not a failure."
        ))] = None,
        task_id: Annotated[str | None, Field(description="Only runs with this task_id.")] = None,
        since_hours: Annotated[float | None, Field(gt=0, description=(
            "Only runs started within this many hours. A run with an unreadable start time is "
            "KEPT - a missing timestamp is not evidence it falls outside the window."
        ))] = None,
        full: Annotated[bool, Field(description=(
            "Include the agent's final message and verify output. Off by default: these are "
            "unbounded and dominate a listing's size. Use `get_run` for one run's full text."
        ))] = False,
    ) -> dict[str, Any]:
        """List fleet runs, newest first, with status and cost. Omit `workspace` to aggregate
        across every workspace (each run tagged with its workspace); pass one to scope to it.

        Compact by default: `text` and `verify_output` are replaced by `has_text` /
        `has_verify_output` flags, so an omitted field is never misread as an empty one. Pass
        `full=true` for the whole record. Filter with `status` / `task_id` / `since_hours` and page
        with `limit` rather than pulling the entire ledger.
        """
        rows = await offload(registry.ledger_runs, workspace)
        since = (
            datetime.now(timezone.utc) - timedelta(hours=since_hours)
            if since_hours is not None else None
        )
        by_run = {id(rec): ws for ws, rec in rows}
        matched = filter_runs(
            [rec for _, rec in rows], status=status, task_id=task_id, since=since
        )
        page = matched[:limit]
        return {
            "runs": [
                tag(rec.model_dump(mode="json") if full else compact_run(rec), by_run[id(rec)])
                for rec in page
            ],
            "returned": len(page),
            "matched": len(matched),
            "truncated": len(matched) > len(page),
            "compact": not full,
        }

    @app.tool()
    async def usage(
        window: Annotated[
            UsageWindow,
            Field(description=(
                "Time window: 'session' (since the MCP server started - the Fleet's "
                "session_start), 'day' (last 24h), 'week' (last 7d), 'month' (last 30d), "
                "'all' (the full ledger, default). Same set as `marshal usage --window`. The "
                "resolved window and `since` are echoed back in the response."
            )),
        ] = "all",
        workspace: Annotated[str | None, Field(description=_DESC_WORKSPACE)] = None,
    ) -> dict[str, Any]:
        """Per-provider usage summary (totals + by backend/client/model, plus a per-backend/model
        breakdown and token totals) for one workspace. Time-windowed via `window`; default is the
        full ledger. `by_backend_model` is keyed like 'opencode/<model-a>'. When the target
        workspace's fleet config declares `budgets:`, a `budgets` list is included with per-budget
        scope / window / windowed spend / limit / remaining / enforce / spent_known (soft-warn by
        default; `enforce: true` may refuse subsequent matching spawns on that workspace).
        `spent_known: false` means spend is unknown — do not treat spent/remaining as measured."""
        svc = await offload(registry.get, workspace)
        now = datetime.now(timezone.utc)
        since = usage_window_since(window, session_start=svc.session_start, now=now)
        summary = await offload(svc.usage, since, None)
        budgets = await offload(svc.budget_status, now)
        payload = {
            "window": window,
            "since": since.isoformat() if since is not None else None,
            **summary.model_dump(mode="json"),
        }
        if budgets:
            payload["budgets"] = [b.model_dump(mode="json") for b in budgets]
        return tag(payload, workspace or DEFAULT_WORKSPACE)

    @app.tool()
    async def report(
        task_id: Annotated[str, Field(description="The benchmark task_id whose runs to compare.")],
        workspace: Annotated[str | None, Field(description=_DESC_WORKSPACE)] = None,
    ) -> dict[str, Any]:
        """Derive the strategy comparison for a past benchmark task_id from the workspace's ledger
        (read-only). task_ids are per-workspace, so pass the workspace the benchmark ran in."""
        return await ws_call(workspace, lambda svc: svc.report(task_id))
