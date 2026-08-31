"""Core data types shared across the Marshal engine.

Value objects are Pydantic models so construction validates inputs and (de)serialization to fleet
state / usage logs / the MCP surface is uniform. Enums stay plain ``str`` enums (Pydantic handles
them natively). The loose, version-variable JSON that backend CLIs emit is deliberately parsed as
plain dicts in the adapters - strict models there would reject on an unexpected upstream field.
"""

from __future__ import annotations

from collections.abc import Callable
from enum import Enum
from pathlib import Path
from typing import Any

from pydantic import BaseModel, ConfigDict, field_validator

from .ids import MAX_TASK_ID_LEN, validate_worktree_id


class PermissionMode(str, Enum):
    """Normalized permission tiers, mapped to each backend's native flags by the adapter.

    Headless agents have no stdin, so a prompting mode would deadlock. SAFE_EDIT is the
    default and must never prompt: it writes inside the worktree without confirmation.
    """

    READ_ONLY = "read-only"   # plan/inspect only; no edits, no shell mutations
    SAFE_EDIT = "safe-edit"   # edit + run within the worktree, no prompts (default)
    YOLO = "yolo"             # fully unrestricted; opt-in only


class PermissionFidelity(str, Enum):
    """How much a permission tier actually enforces beyond worktree isolation.

    ``Capabilities.permission_fidelity`` is the backend's **safe-edit** honesty
    (``marshal backends`` / doctor ``permission:<backend>``). Client listings resolve
    fidelity from the ``(backend, permission)`` pair via ``resolve_permission_fidelity`` —
    so a ``yolo`` client never inherits an ``enforced-denies`` label it does not earn.

    ``enforced-denies`` means the backend or Marshal installs a restriction beyond "runs in
    a worktree" (curated deny overlay, sandbox flag, plan mode, etc.). ``boundary-only``
    means Marshal cannot promise a deny layer — the worktree and explicit integrate remain
    the dependable boundary. ``unrestricted`` means the resolved tier intentionally drops
    the deny/sandbox overlay (``yolo``). Default on Capabilities is the honest fail-closed
    value so unknown/dummy adapters never claim enforcement by accident. Coarse routing
    signal, not a sandbox guarantee or a strength ranking between backends.
    """

    ENFORCED_DENIES = "enforced-denies"
    BOUNDARY_ONLY = "boundary-only"
    UNRESTRICTED = "unrestricted"


def resolve_permission_fidelity(
    backend_fidelity: PermissionFidelity,
    permission: PermissionMode,
) -> PermissionFidelity:
    """Client-facing fidelity for a resolved ``(backend capability, permission)`` pair.

    ``yolo`` is unrestricted by design on every backend (no curated deny / sandbox overlay).
    ``safe-edit`` and ``read-only`` inherit the backend's declared safe-edit fidelity:
    enforcing backends install a real restriction for both tiers; boundary-only backends
    stay honest that Marshal cannot promise a deny layer (cooperative plan/chat modes).
    """
    if permission is PermissionMode.YOLO:
        return PermissionFidelity.UNRESTRICTED
    return backend_fidelity


#: Persisted status spellings that have been renamed, mapped to their current value. Applied when
#: READING any stored status - run records and the usage ledger alike. Nothing on disk is rewritten:
#: a stored status is a fact about what happened, so history is reinterpreted, never edited.
#:
#: The usage ledger matters as much as the run records here. It is append-only, so every event
#: written before the rename says "succeeded"; a reader that only knew the new spelling would
#: silently stop counting those runs and quietly change every historical cost-per-succeeded figure.
STATUS_ALIASES: dict[str, str] = {"succeeded": "exited_clean"}


def canonical_status(value: str) -> str:
    """The current spelling of a stored status string. Unknown values pass through unchanged."""
    return STATUS_ALIASES.get(value, value)


class RunStatus(str, Enum):
    """Run outcomes. NOTE the deliberate weakness of `EXITED_CLEAN`.

    It was `succeeded`, and every field review said the same thing about that word: it claims more
    than it checks. The run's process exited 0; whether the work is *correct* is a separate question
    that only a diff review (or the `verify:` gate) answers. The integrate docstring and CLAUDE.md
    both had to shout that caveat, and - as one reviewer put it - when a description has to shout a
    caveat, the shape is wrong. `exited_clean` says exactly what was observed, so the warning is no
    longer load-bearing.

    Records written before the rename carry `"succeeded"`; `RunRecord` migrates them on read (see
    `state.py`). Nothing rewrites history on disk.
    """

    QUEUED = "queued"
    RUNNING = "running"
    EXITED_CLEAN = "exited_clean"
    EMPTY = "empty"           # exited 0 with neither text nor file changes; counts in $/run, not $/succeeded
    FAILED = "failed"
    TIMED_OUT = "timed_out"
    CANCELLED = "cancelled"
    # The agent finished and produced work, but the workspace's `verify:` command rejected it.
    # Distinct from FAILED so a driver can tell "the run broke" from "reviewable work exists but
    # the repo's gate said no" - the worktree is kept for review either way.
    VERIFY_FAILED = "verify_failed"


#: The statuses that mean a run has NOT finished. Everything else is terminal.
#:
#: Defined as the complement rather than by listing the terminal states, so a status added later is
#: terminal until someone says otherwise. That is the safe default for the two readers: `routing`
#: would rather skip an unjudgeable run than count it, and a waiter would rather return early than
#: block on a state it does not recognise until its timeout expires.
NON_TERMINAL_STATUSES = frozenset({RunStatus.QUEUED.value, RunStatus.RUNNING.value})


def is_terminal(status: str | None) -> bool:
    """Has this run finished? The one definition - `routing` and `wait_for_runs` share it.

    `None` (a record with no status yet) is not terminal. Note this asks about the RECORD, not the
    process: a supervisor killed mid-run leaves a record reading `running` forever, which is why
    every waiter is timeout-bounded rather than trusting this to eventually flip.
    """
    return status is not None and status not in NON_TERMINAL_STATUSES


class RunOutcome(str, Enum):
    """A driver's judgment about a run's WORK, distinct from `RunStatus`'s process truth.

    `exited_clean` says the process exited 0; it says nothing about whether the diff was any good.
    That verdict arrives later, from whoever reviewed it, and is stamped on the run record.

    `INTEGRATED` is a mechanical fact - a merge commit exists - so it is never overwritten. The
    other three are opinions and may be revised. Absence of an outcome means "not judged yet",
    which is emphatically not the same as `REJECTED`: every routing number is a ratio over
    *judged* runs, and conflating the two would silently count work nobody has looked at as work
    someone refused.

    `ADVISORY` exists because the other three all assume the work was a diff. A read-only
    reviewer, an audit, or a plan-consensus panel produces findings that get *used* while merging
    nothing, and the only verdict that previously fit was `ABANDONED` - which reads as "gave up"
    and, being a judged non-integration, drove the client's integration rate toward zero for work
    it was never eligible to win. `ADVISORY` says the work was used and there was nothing to
    merge; the routing ledger counts it as judged but keeps it out of the integration-rate
    denominator. It is a verdict about the work, so it is not for a run that merely produced no
    diff - that is `empty`, a status, or `REJECTED` if the findings were wrong.
    """

    INTEGRATED = "integrated"
    REJECTED = "rejected"
    ABANDONED = "abandoned"
    ADVISORY = "advisory"


class UsageSource(str, Enum):
    """Provenance of a usage record - never present an estimate as ground truth."""

    NATIVE = "native"            # backend reported tokens+cost in its output
    ADMIN_API = "admin-api"      # fetched from a provider account/admin API
    UNAVAILABLE = "unavailable"  # backend exposes no usage data


class ModelSource(str, Enum):
    """Provenance of a model listing - never present a curated list as a live answer.

    Same rule as ``UsageSource``, applied to a second kind of fact. A driver choosing what to
    route at needs to know whether the CLI said this *just now* or whether Marshal is reciting
    what a doc said months ago: a static list can name a model the account cannot actually run,
    and a run that fails on an unknown model id is a wasted worktree and a confusing error.

    ``STATIC`` and ``PROBE_FAILED`` both carry a curated list that may be stale; they differ in
    whether a live probe was attempted. ``list_models`` treats ``static`` as "not live evidence"
    either way; drift needs the distinction so an installed CLI whose probe just failed is not
    mistaken for an adapter that never had a probe.
    """

    PROBED = "probed"            # this backend's CLI reported these ids just now
    STATIC = "static"            # curated list; no live probe was attempted (or CLI absent)
    PROBE_FAILED = "probe-failed"  # live probe ran and failed; models are the curated fallback
    UNAVAILABLE = "unavailable"  # nothing to report at all


class ModelCatalog(BaseModel):
    """What a backend can say about the models it runs, with where the answer came from.

    Carrying the provenance inline is what lets ``UNAVAILABLE`` mean "could not ask" without
    overloading an empty list or a null to mean it - the ambiguity that let a static fallback
    read as a live probe. ``PROBE_FAILED`` is the third state: the adapter tried and the CLI did
    not answer, so the curated ids are present but unverified.
    """

    model_config = ConfigDict(frozen=True)

    models: list[str] = []
    source: ModelSource = ModelSource.UNAVAILABLE


class Capabilities(BaseModel):
    """Feature flags so the orchestrator degrades gracefully per backend."""

    model_config = ConfigDict(frozen=True)

    json_output: bool = False
    native_usage: bool = False    # emits tokens/cost in its own output
    permission_modes: frozenset[PermissionMode] = frozenset()
    # Default boundary-only: unknown/third-party adapters fail honest rather than claiming
    # a deny layer they did not declare. Built-in backends set this explicitly.
    permission_fidelity: PermissionFidelity = PermissionFidelity.BOUNDARY_ONLY


class TaskSpec(BaseModel):
    """A single unit of work handed to one agent."""

    id: str
    goal: str                              # natural-language task for the agent
    # Caller-supplied free-text tag for the kind of work (`refactor`, `bugfix`, `docs`, …).
    # Taxonomy is the user's — not a closed enum. Validated as a short safe token (same rules as id).
    task_kind: str | None = None
    context_files: list[str] = []          # minimal files the worker should see
    # Declared read-only escape hatch: absolute paths, or paths relative to the driver's repo root,
    # copied into the worktree under `.marshal-context/` (see Fleet provisioning). Unlike
    # context_files these are deliberately outside the worktree checkout.
    read_paths: list[str] = []
    # Run ids whose harvested artifacts this run should be able to read. Copied in read-only under
    # `.marshal-context/artifacts/<run_id>/`. This is how round N's report reaches round N+1
    # without the driver pasting findings into the next prompt by hand.
    artifacts_from: list[str] = []
    base_branch: str | None = None         # branch to base the worktree on (None = current HEAD)
    # Optional JSON Schema for the agent's FINAL MESSAGE. When set (including {}), the fleet
    # injects a prompt instruction and validates the reply as one JSON object; see
    # fleet._apply_structured_output. None (default) leaves behaviour identical to an unstructured
    # run. Empty {} means "any JSON object" (extraction still requires an object).
    output_schema: dict[str, Any] | None = None

    @field_validator("id")
    @classmethod
    def _id_must_be_safe_path_segment(cls, v: str) -> str:
        return validate_worktree_id(v, max_len=MAX_TASK_ID_LEN)

    @field_validator("task_kind")
    @classmethod
    def _task_kind_must_be_safe_token(cls, v: str | None) -> str | None:
        # Same fail-closed token rules as task id. Sourced from `ids` (not `worktree`) so this
        # stays a leaf import — `types -> worktree` is a forbidden edge (see test_import_layers).
        if v is None:
            return None
        return validate_worktree_id(v, max_len=MAX_TASK_ID_LEN)

    @field_validator("output_schema")
    @classmethod
    def _output_schema_must_be_valid_json_schema(
        cls, v: dict[str, Any] | None
    ) -> dict[str, Any] | None:
        if v is None:
            return None
        # Lazy import: keep the core types importable without pulling jsonschema until needed.
        from jsonschema.exceptions import SchemaError
        from jsonschema.validators import validator_for

        try:
            validator_for(v).check_schema(v)
        except SchemaError as exc:
            raise ValueError(f"invalid output_schema: {exc.message}") from exc
        return v


class ProgressTimeout(BaseModel):
    """Opt-in policy for ending a run on evidence of progress rather than on the clock alone.

    A single wall-clock number treats two opposite situations identically: a run that stalled at
    30s burns its whole cap before anyone notices, and a run still working at the cap is killed
    with its tokens already spent. Elapsed time is not evidence about whether work is happening.

    The hard ceiling is NEVER removed - a silent, hung process must always die, which is the
    invariant this sits underneath. This policy only decides EARLIER kills (a stalled run) and
    BOUNDED extensions (a productive one), always below `hard_ceiling_s`.

    Progress is measured as the newest mtime under the run's worktree: backend-independent, and
    it needs no per-backend calibration. The known blind spot is deliberate: an agent that
    reasons for a long time without writing anything looks idle, which is why `stall_s` must sit
    well above however long a backend legitimately stays quiet, and why this is opt-in.
    """

    enabled: bool = False
    #: Kill once nothing under the worktree has changed for this long.
    stall_s: int = 300
    #: First deadline. A run still making progress here is extended, not killed. None = timeout_s.
    soft_deadline_s: int | None = None
    #: The backstop. Never exceeded, whatever progress says. None = timeout_s (no extension).
    hard_ceiling_s: int | None = None
    #: How often to re-measure progress.
    poll_interval_s: int = 15


class RunOpts(BaseModel):
    """How to run a TaskSpec. Backend-agnostic; adapters translate these to native flags."""

    cwd: Path                              # where the agent runs (typically a worktree)
    permission: PermissionMode = PermissionMode.SAFE_EDIT
    model: str | None = None
    session_id: str | None = None         # resume a prior session if the backend supports it
    timeout_s: int = 600                  # external timeout + kill - never run without one
    client_env: dict[str, str] = {}       # per-client vars from fleet.config.yaml (after scrub)
    extra_env: dict[str, str] = {}
    on_pid: Callable[[int], None] | None = None  # called by base.run() with the child pid
    # Called by base.run() once the child has been REAPED. Until then the OS cannot reuse its pid,
    # so this is what tells a canceller that signalling that pid is no longer safe.
    on_exit: Callable[[], None] | None = None
    #: Opt-in progress policy. None (the default) keeps the plain `timeout_s` wall clock.
    progress: ProgressTimeout | None = None


class UsageRecord(BaseModel):
    """Normalized usage/cost for one run."""

    backend: str
    model: str | None = None
    input_tokens: int = 0
    output_tokens: int = 0
    cache_read_tokens: int = 0
    cache_write_tokens: int = 0
    cost_usd: float = 0.0
    source: UsageSource = UsageSource.UNAVAILABLE


class AgentResult(BaseModel):
    """Normalized result of one agent run, regardless of backend."""

    status: RunStatus
    text: str = ""                         # final assistant message
    session_id: str | None = None
    usage: UsageRecord | None = None
    exit_code: int | None = None
    duration_ms: int = 0                    # wall-clock around the run, stamped by base.run()
    error: str | None = None
    raw_stdout: str = ""
    raw_stderr: str = ""
    # True only when `base.run()` timed a run out, signalled its process group, and then OBSERVED
    # the agent still alive. A fact about what the kill achieved, not a prediction: the default is
    # False because "we did not see it survive" is what every other path knows. Callers use it to
    # tell a timed-out run whose agent is gone from one that is still writing to the worktree.
    agent_survived_kill: bool = False
    # Schema-validated JSON object from the final message when TaskSpec.output_schema was set and
    # the reply conformed. None when no schema was requested, or when validation failed (see error).
    structured: dict[str, Any] | None = None
