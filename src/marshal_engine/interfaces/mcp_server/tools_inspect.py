
from __future__ import annotations

from datetime import UTC, datetime, timedelta
from typing import TYPE_CHECKING, Annotated, Any, Literal

from pydantic import Field

from ...accounting.usage import UsageWindow, usage_window_since
from ...runtime.state import RunView, filter_runs, render_run
from ..workspaces import (
    DEFAULT_WORKSPACE,
)
from .context import ToolContext

if TYPE_CHECKING:  # the mcp SDK is an optional extra; only needed for typing here
    from mcp.server.mcpserver import MCPServer
from .schema import _DESC_RUN_ID, _DESC_WORKSPACE, _DESC_WS_HINT


def register(app: MCPServer, ctx: ToolContext) -> None:
    """Register this group's tools on ``app``."""
    registry = ctx.registry
    offload = ctx.offload
    ws_call = ctx.ws_call
    run_call = ctx.run_call
    tag = ctx.tag

    @app.tool()
    async def marshal_quickstart() -> dict[str, Any]:
        """START HERE. The canonical loop, and which tool to pick when several look alike.

        Read this before choosing among the run-ish tools (run_agent / spawn / run_many /
        run_workflow) or the read-ish ones (status / get_run / collect_run / get_run_log /
        read_run_file) - `which_read_tool` says what each one returns.
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
                "message itself for a text run; get_run (or status(view='full')) gives you the raw "
                "record if you want it. `empty` is an outcome, not a fault: the process exited 0 "
                "with neither text nor file changes - nothing to integrate. What Marshal lacks "
                "is STRUCTURED output: the result is prose you parse. Where a backend truncates "
                "long final messages (Cursor does), have the agent write its report to a file "
                "instead - that is why the built-in review teams do so."
            ),
            "the_loop": [
                ("1. doctor - is this workspace ready? Catches a missing CLI, a broken config, and "
                 "a backend that recently failed on billing, BEFORE you spend a run."),
                "2. spawn (or run_agent) - delegate the work to a worker agent.",
                ("2b. wait_for_runs - after spawning, block on the run ids instead of polling "
                 "`status` yourself. One tool call covers the whole wait; hand-rolled polling spends "
                 "a turn per tick to be told 'not yet'. Expiry returns what settled so far, so "
                 "re-call for whatever is still `pending`."),
                ("3. collect_run - read what the run produced (`produced`: diff | text | nothing). "
                 "`exited_clean` means the process exited 0, NOT that the work is correct. For a "
                 "diff run, review before step 4; for a text run, the value is in `text` - skip "
                 "integrate."),
                ("4. integrate - merge a diff run's branch into yours. One run at a time. Skip for "
                 "text-only work."),
                ("5. set_outcome - record the runs you DIDN'T merge (`rejected` / `abandoned`). "
                 "integrate records `integrated` for you; nothing records the rest, so a run you "
                 "reviewed and threw away looks exactly like one nobody read. `routing` rates are "
                 "computed over judged runs only, so without this they read 100% for everyone."),
            ],
            "which_run_tool": {
                "run_agent": "Blocks until the run finishes. Use for short work you want inline.",
                "spawn": "Returns immediately with a RUNNING record. Use for anything long - it "
                "does not hold your turn. Then wait_for_runs; stop with cancel_run.",
                "wait_for_runs": "Blocks until spawned runs finish (or your timeout). Not a way to "
                "start work - the close of the spawn loop. `settled` means finished, NOT succeeded: "
                "branch on each record's `status`.",
                "run_many": "Several jobs in parallel (optional per-job then chains). Returns when all chains finish.",
                "run_workflow": "A declarative recipe (fan-out, gates, integrate) from a YAML file.",
            },
            # `get`, `collect`, and `read` are three verbs for one operation, so a tool's name does
            # not say what it returns. Until they are renamed this map is how a driver picks, and
            # every run-reading tool has to appear in it (test-enforced; `docs/mcp-tools.md` is the
            # normative list of what exists).
            "which_read_tool": {
                "status": "Reads MANY runs, filtered - poll-shaped by default; pass `limit`, "
                "`status`, `task_id`, `since_hours`, and `view` to widen. Check `agent_alive` to "
                "tell 'still working' from 'finished, outcome not yet written'.",
                "get_run": "Reads ONE run's whole record, including its final text.",
                "collect_run": "Reads what one run PRODUCED (diff and/or text via `produced`). The "
                "review step before integrate; for text runs, the value is in `text`.",
                "get_run_log": "Reads one run's raw stdout/stderr, untruncated. For diagnosing a "
                "failure the record's `text` does not explain.",
                "read_run_file": "Reads ONE FILE out of a run's worktree, by path. How an artifact "
                "reaches the next agent: read the file the run wrote, put it in the next `goal`. "
                "To build on a run's CODE instead, use commit_run + spawn(base_branch=...).",
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
        `backend_models` maps each backend to {models, source}: source `probed` means its CLI
        answered just now, `static` means a curated list that may name models the account cannot
        actually run, `unavailable` means it could not be asked at all. Do NOT treat a `static`
        list as evidence a model is runnable. Pure data - does NOT influence routing (clients
        still own backend+model). Returns {models, backend_models, driver_context, workspace}."""
        return await ws_call(workspace, lambda svc: svc.list_models())

    @app.tool()
    async def routing(
        task_kind: Annotated[str | None, Field(description=(
            "Only report this kind of work (the free-text tag passed at spawn, e.g. 'refactor'). "
            "Omit for every kind."
        ))] = None,
        window: Annotated[Literal["session", "day", "week", "month", "all"], Field(
            description="Time window over the usage ledger. Default 'all' - routing wants history."
        )] = "all",
        workspace: Annotated[str | None, Field(description=_DESC_WORKSPACE)] = None,
    ) -> dict[str, Any]:
        """Which client's work actually got KEPT, per kind of task - your measured history, ranked.

        Read this before a fan-out instead of guessing from model names. Derived on read from the
        usage ledger joined to recorded outcomes; nothing is stored.

        Read the numbers honestly, because the ledger will not flatter you:
        - `integration_rate` is over JUDGED runs only, and is `null` when nothing has been judged -
          that is "unknown", not 0%. `n_judged` sits beside it; a rate without its n is not evidence.
        - If you record integrations but never rejections (`set_outcome`), every rate reads 100%
          and this tool is decorative. `caveat` says so when nothing has been judged at all.
        - `mean_cost_per_integrated` is `null`, never 0, when no integrated run reported real cost.
          A client with unmeasured cost is never ranked *on* cost - it neither wins nor loses that
          tiebreak. Compare it against `measured_cost_all_usd`, which includes the money spent on
          runs you rejected.
        - `recommended` is `null` rather than a guess when nothing can be ranked.

        Returns {cells, recommended, recommended_task_kind, total_runs, total_judged,
        events_without_record, task_kind_filter, caveat, window, workspace}."""
        result = await ws_call(
            workspace, lambda svc: svc.routing(task_kind=task_kind, window=window)
        )
        result["window"] = window
        return result

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
        """Reads ONE RUN'S WHOLE RECORD by id, across all workspaces (or via the `workspace` hint).

        status is one of: queued | running (NOT terminal - the run is still in flight, which is the
        common state during a fan-out; wait_for_runs rather than judging it) | exited_clean |
        empty (exited 0 with neither text nor file changes - an outcome, not a fault; nothing to
        integrate) | failed | timed_out | cancelled | verify_failed (had file changes but the
        workspace's `verify:` gate rejected them - review the diff and `verify_output` before
        deciding). Only `exited_clean` runs with a diff are integration candidates; for text-only
        work read `text` (or collect_run) - and check `text_truncated` before treating `text` as a
        finished product."""
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
        """Reads ONE RUN'S RAW STDOUT/STDERR, untruncated (null if no log was written).

        Each terminal run (success or failure) gets one file under `<base>/logs/<run_id>.log` with
        a `=== run <id> ===` header, a `--- stdout ---` section, and a `--- stderr ---` section -
        the FULL streams, not the 16KB-truncated `text` on the run record - including EVERY attempt of
        a retried run, each under `--- attempt N/M ---`. `log` is null when the run
        exists but wrote no log (a run that pre-dates log storage, or a backend that crashed before
        producing one) - never as an answer to "no such run", which raises, naming the registered
        workspaces. The owning workspace is resolved by the same scan as `get_run`, with the same
        `workspace` hint."""
        # `require_run`, not `resolve_run`: an unknown id is an error, not a null log. Returning
        # `{log: null}` here made "no such run" byte-identical to "this run wrote no log" apart
        # from the id echoed back - so a driver read a typo'd or already-cleaned id as a run that
        # had crashed before producing diagnostics, and re-spawned it. It also stamped `workspace`
        # with whatever the caller passed, naming a workspace nothing had resolved and that may
        # not be registered at all. Every sibling raises here; `service.run_log` does too.
        name, svc = await offload(registry.require_run, run_id, workspace)
        text = await offload(svc.run_log, run_id)
        return tag({"run_id": run_id, "log": text}, name)

    @app.tool()
    async def collect_run(
        run_id: Annotated[str, Field(description=_DESC_RUN_ID)],
        workspace: Annotated[str | None, Field(description=_DESC_WS_HINT)] = None,
    ) -> dict[str, Any]:
        """Reads WHAT ONE RUN PRODUCED: diff/changed files and/or final text, tagged by `produced`
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
        """Reads ONE FILE out of a run's worktree - how one agent's output reaches the next.

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
        view: Annotated[RunView, Field(description=(
            "How much of each run to return. 'poll' (default) is the polling shape: whether it "
            "finished and whether it was any good. 'compact' adds the rest of the record "
            "(worktree, branch, base_commit, tokens, read_paths, artifacts...) minus the unbounded "
            "text. 'full' adds the agent's final message and verify output - unbounded, and it "
            "dominates a listing's size, so prefer `get_run` for one run's text."
        ))] = "poll",
    ) -> dict[str, Any]:
        """List fleet runs, newest first, with status and cost. Omit `workspace` to aggregate
        across every workspace (each run tagged with its workspace); pass one to scope to it.

        Returns the polling shape by default - `run_id`, `task_id`, `backend`, `client`, `status`,
        `agent_alive`, `cost_usd`, `source`, `duration_ms`, `outcome`, `ended_at`. Widen with
        `view` when you need a run's worktree or token counts, or call `get_run` for one run.
        Every view replaces `text` / `verify_output` with `has_text` / `has_verify_output` flags
        until you ask for 'full', so an omitted field is never misread as an empty one. Filter with
        `status` / `task_id` / `since_hours` and page with `limit` rather than pulling the ledger.
        """
        rows = await offload(registry.ledger_runs, workspace)
        since = (
            datetime.now(UTC) - timedelta(hours=since_hours)
            if since_hours is not None else None
        )
        by_run = {id(rec): ws for ws, rec in rows}
        matched = filter_runs(
            [rec for _, rec in rows], status=status, task_id=task_id, since=since
        )
        page = matched[:limit]
        return {
            "runs": [
                tag(render_run(rec, view), by_run[id(rec)]) for rec in page
            ],
            "returned": len(page),
            "matched": len(matched),
            "truncated": len(matched) > len(page),
            "view": view,
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
        now = datetime.now(UTC)
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
