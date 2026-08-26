"""The MCP tool input surface: parameter descriptions and the shared job models.

These strings ARE the driver-facing documentation - an agent picks a tool from them, so they
live beside the models they annotate rather than scattered through the handlers. `docs/mcp-tools.md` is the
normative reference for the tool list itself.
"""

from __future__ import annotations

from typing import Annotated, Any, TypeVar

from pydantic import BaseModel, Field

_T = TypeVar("_T")

# The MCP driver is an untrusted caller; letting it enlarge the set of repos Marshal may modify is
# an operator decision. `add_workspace` refuses unless the server was started with this env var set
# to exactly "1" (any other value fails closed - no generic truthiness).
_ALLOW_MCP_REGISTRATION_ENV = "MARSHAL_ALLOW_MCP_WORKSPACE_REGISTRATION"

# Shared parameter descriptions so the tool schema the driver sees is self-describing (not just
# title + type). Reused across the tools and the run_many Job model.
_DESC_CLIENT = "Name of a configured client (from list_clients)."
_DESC_MODEL = "Optional model override; when set with a client, replaces the client's resolved model. When set with `backend` (ad-hoc), is the model to run."
_DESC_BACKEND = "Optional bare backend name for an ad-hoc spawn (e.g. 'opencode', 'claude-code'); bypasses fleet.config.yaml. Mutually exclusive with `client` - passing both is refused, not silently resolved."
_DESC_DURATION = (
    "Optional per-spawn timeout override. A preset name (short=300s, medium=1200s, large=6000s, "
    "long=24000s) or a positive integer of seconds. When set, it overrides the resolved timeout."
)
_DESC_GOAL = "Natural-language task for the worker agent."
_DESC_TASK_ID = (
    "Optional grouping id; runs sharing a task_id can be compared head-to-head by report(). "
    "Must be a safe path segment ([A-Za-z0-9._-], no leading '.'/'-'); see SECURITY.md."
)
_DESC_TASK_KIND = (
    "Optional free-text tag for the kind of work (e.g. refactor, bugfix, docs, review). "
    "Caller taxonomy — not a closed enum. Same safe-token rules as task_id; stamped on the "
    "usage event for a future routing layer."
)
_DESC_CONTEXT = "Optional repo-relative paths to point the worker at (injected into its prompt). Each must be TRACKED in git: the worktree holds tracked files only, so a gitignored/untracked path fails the spawn instead of silently reaching the agent as an unopenable path."
_DESC_READ_PATHS = (
    "Optional read-only escape hatch: absolute paths, or paths relative to the driver's repo root, "
    "copied into the worktree under `.marshal-context/` (files 0o444, directories 0o555; git-excluded "
    "from the run diff). Secret-shaped names (.env*, *.pem, id_rsa*, id_ed25519*, or anything "
    "under .ssh) are refused on the path and every descendant; only regular files/directories "
    "are accepted (FIFOs/sockets/devices refused). Missing or refused paths fail the spawn."
)
_DESC_ARTIFACTS_FROM = (
    "Optional run ids whose artifacts this run should be able to read, copied in read-only under "
    "`.marshal-context/artifacts/<run_id>/`. This is how a multi-round loop passes work forward: "
    "round 1 writes its report to `.marshal-artifacts/` in its worktree, Marshal harvests it, and "
    "round 2 names round 1 here instead of you pasting the findings into the next prompt. Tell the "
    "agent in the goal that the file is there. A run with no stored artifacts fails the spawn "
    "rather than silently handing the agent nothing (check `artifacts` on the run record first)."
)
_DESC_BASE_BRANCH = (
    "Optional branch to base the run's worktree on (None = current HEAD). Use after commit_run to "
    "chain dependent work off a prior run's branch."
)
_DESC_OUTPUT_SCHEMA = (
    "Optional JSON Schema (object) for the agent's FINAL MESSAGE. When set, the engine requires "
    "exactly one conforming JSON object, surfaces it as `structured` on the run/collect result, "
    "and fails the run (status failed, error prefixed `structured_output:`) if the reply is "
    "missing or invalid. Omit for today's prose behaviour."
)
_DESC_RUN_ID = "A run id returned by run_agent / spawn / run_many."
_DESC_WORKSPACE = "Target workspace name (from list_workspaces); defaults to the primary workspace."
_DESC_WS_HINT = (
    "Optional workspace hint (from the run's `workspace` field) to skip the ledger scan; the lookup "
    "falls back to scanning all workspaces if it is wrong or omitted."
)


class Job(BaseModel):
    """One parallel job for run_many: which client runs what, optionally scoped.

    A job may set ``workspace`` to target a registered repo; jobs without it use the call-level
    ``workspace`` (default workspace when omitted). Mixed-workspace batches share one concurrency
    cap; each workspace keeps its own config, worktrees, and ledger. A job may also be specified
    ad-hoc (omit ``client``, set ``backend`` + optional ``model``) for harness-first routing without
    a configured fleet.config.yaml client. Optional ``then`` runs a follow-up in the same worker as
    soon as this job's primary finishes (see ``run_many``).
    """

    client: Annotated[str | None, Field(description=_DESC_CLIENT + " Omit to spawn ad-hoc by `backend`.")] = None
    goal: Annotated[str, Field(description=_DESC_GOAL)]
    task_id: Annotated[str | None, Field(description=_DESC_TASK_ID)] = None
    task_kind: Annotated[str | None, Field(description=_DESC_TASK_KIND)] = None
    context_files: Annotated[list[str] | None, Field(description=_DESC_CONTEXT)] = None
    read_paths: Annotated[list[str] | None, Field(description=_DESC_READ_PATHS)] = None
    artifacts_from: Annotated[
        list[str] | None, Field(description=_DESC_ARTIFACTS_FROM)
    ] = None
    model: Annotated[str | None, Field(description=_DESC_MODEL)] = None
    backend: Annotated[str | None, Field(description=_DESC_BACKEND)] = None
    duration: Annotated[str | int | None, Field(description=_DESC_DURATION)] = None
    output_schema: Annotated[
        dict[str, Any] | None, Field(description=_DESC_OUTPUT_SCHEMA)
    ] = None
    then: Annotated[
        ThenJob | None,
        Field(
            description=(
                "Optional follow-up run in the same worker after this job's primary finishes. "
                "Same fields as a job (no nested `then` or `workspace`). Skipped when the primary "
                "failed or has no branch."
            )
        ),
    ] = None
    workspace: Annotated[
        str | None,
        Field(
            description=(
                "Optional per-job workspace (from list_workspaces). Overrides the call-level "
                "`workspace` for this job only."
            )
        ),
    ] = None


class ThenJob(BaseModel):
    """Follow-up stage for a run_many job (same field set as Job, minus workspace/then).

    Keep this in step with ``Job``. A field declared there and omitted here is dropped silently:
    Pydantic ignores unknown keys, and the engine's ``job_request`` reads the same names off both
    bodies - so the follow-up spawns without the input the driver asked for and reports success.
    ``read_paths`` and ``artifacts_from`` were missing for exactly that reason, which inverted two
    documented fail-closed guarantees ("missing or refused paths fail the spawn", "a run with no
    stored artifacts fails the spawn rather than silently handing the agent nothing") into no-ops.
    """

    client: Annotated[str | None, Field(description=_DESC_CLIENT + " Omit to spawn ad-hoc by `backend`.")] = None
    goal: Annotated[str, Field(description=_DESC_GOAL)]
    task_id: Annotated[str | None, Field(description=_DESC_TASK_ID)] = None
    task_kind: Annotated[str | None, Field(description=_DESC_TASK_KIND)] = None
    context_files: Annotated[list[str] | None, Field(description=_DESC_CONTEXT)] = None
    read_paths: Annotated[list[str] | None, Field(description=_DESC_READ_PATHS)] = None
    artifacts_from: Annotated[
        list[str] | None, Field(description=_DESC_ARTIFACTS_FROM)
    ] = None
    model: Annotated[str | None, Field(description=_DESC_MODEL)] = None
    backend: Annotated[str | None, Field(description=_DESC_BACKEND)] = None
    duration: Annotated[str | int | None, Field(description=_DESC_DURATION)] = None
    output_schema: Annotated[
        dict[str, Any] | None, Field(description=_DESC_OUTPUT_SCHEMA)
    ] = None


Job.model_rebuild()


