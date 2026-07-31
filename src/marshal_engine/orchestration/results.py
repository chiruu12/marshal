"""The result and request types the fleet returns and accepts.

Pydantic models rather than dicts: they are the boundary the service, CLI, and MCP layers all
serialize, so validation and JSON shape live in one place.
"""

from __future__ import annotations

from typing import Any

from pydantic import BaseModel

from ..core.types import PermissionMode, TaskSpec
from ..runtime.state import RunRecord

class CollectResult(BaseModel):
    """A run's work surfaced read-only for the driver to review.

    Uncommitted work lives in ``changed_files`` / ``diff`` (working tree vs HEAD). Commits the
    agent made on the run's branch since the merge-base with the collect target are in
    ``committed_changed_files`` / ``committed_diff`` / ``commit_count`` — both sections are
    reported when both exist.

    ``text`` carries the agent's final message when the run changed **no files**. `collect_run` is
    the reflex for "what did this run produce", and for a research or review run the honest answer
    is prose, not a diff — without this the tool returns an empty result for a run that succeeded
    and said something, which reads as "it did nothing". `produced` names which of the two it was,
    so a caller branches on a field instead of inferring from emptiness.

    ``structured`` is the schema-validated JSON object when the run requested ``output_schema`` and
    validation succeeded — a field, not an inference from ``text``.
    """

    run_id: str
    branch: str | None
    worktree: str | None
    changed_files: list[str]
    diff: str
    committed_changed_files: list[str] = []
    committed_diff: str = ""
    commit_count: int = 0
    #: "diff" (files changed) | "text" (no files, but the agent replied) | "nothing" (neither).
    produced: str = "diff"
    #: The agent's final message. Populated ONLY when `produced == "text"` - when there IS a diff,
    #: the diff is the artifact and duplicating the message here would just bloat the reply.
    text: str = ""
    #: Schema-validated object when the run produced one; None otherwise (field, not inference).
    structured: dict[str, Any] | None = None


class IntegrateResult(BaseModel):
    """Outcome of merging a run's worktree branch back into the current branch.

    status is one of: "merged" (changes landed), "conflict" (merge aborted, resolve manually),
    "blocked" (the target checkout is dirty/colliding or detached HEAD - nothing changed, fixable
    then retry), "empty" (the run produced no changes to integrate), or "error" (a git operation
    failed in a way the engine can't classify as cleanly recoverable - commit failure, repo left
    mid-merge, op timeout; surface to a human, see `message`, don't blindly retry).
    """

    run_id: str
    status: str
    branch: str | None = None
    merged_into: str | None = None
    changed_files: list[str] = []
    conflicts: list[str] = []
    commit: str | None = None
    message: str = ""
    base_branch_drift: bool = False  # True when merge target differs from the run's recorded base


class CommitResult(BaseModel):
    """Outcome of freezing a run's work onto its own branch (so a dependent run can chain off it).

    status: "committed" (a new commit was made), "clean" (no *new* commit was needed - the working
    tree was already clean; this is NOT "the branch is empty", e.g. an agent that self-committed),
    "blocked" (the run is still in progress; wait for it to finish), or "error" (a git op failed -
    see `message`). To chain, always use `branch`/`commit` regardless of status - `commit` is the
    branch tip whenever it could be resolved, the concrete ref to base a dependent run on
    (`spawn(..., base_branch=branch)`). Don't gate chaining on `status == "committed"`.
    """

    run_id: str
    status: str
    branch: str | None = None
    commit: str | None = None
    message: str = ""


class CleanResult(BaseModel):
    """Outcome of tearing down finished runs' worktrees + branches (the usage ledger is untouched).

    Reclaims the disk-heavy worktrees; the run-state records are kept so status/history stay
    queryable. A run that is still running is never cleaned (reported under `skipped`).
    """

    removed: list[str] = []
    skipped: list[dict[str, str]] = []  # {run_id, reason}
    errors: list[dict[str, str]] = []   # {run_id, error}
    # DRY RUN ONLY: per-candidate `{run_id, unmerged_commits, merged_into}`. The reported blocker
    # was never the filters - it was not knowing which worktrees held work nobody had landed:
    # "I couldn't tell which held unmerged work that wasn't mine", so 84 worktrees accumulated.
    # Computing it costs a git call per candidate, which is fine for a deliberate preview and is
    # why it is not on the every-row `status` listing.
    unmerged: list[dict[str, Any]] = []
    # Worktree dirs under the manager's base_dir with NO (readable) run record - leaked by a
    # hand-pruned or torn ledger file. Reaped by scope-mode cleans (see Fleet.clean).
    orphans_removed: list[str] = []
    dry_run: bool = False


class StrategyResult(BaseModel):
    """One strategy's measured outcome in a benchmark (the run's recorded facts)."""

    run_id: str
    client: str | None
    backend: str
    model: str | None
    status: str
    cost_usd: float
    source: str | None
    duration_ms: int
    input_tokens: int
    output_tokens: int


class BenchmarkResult(BaseModel):
    """Same task run through N strategies, compared on measured cost/latency/outcome (derived).

    `cheapest`/`fastest` name the winning client among *comparable* strategies only - succeeded,
    and (for cheapest) with a known cost (native/admin-api, never `unavailable`). None when
    no strategy qualifies. The per-strategy rows carry `source` for audit.
    """

    task_id: str
    goal: str
    strategies: list[StrategyResult] = []
    cheapest: str | None = None
    fastest: str | None = None


class RunRequest(BaseModel):
    """One unit of work for a parallel batch (the same parameters Fleet.run takes)."""

    backend_name: str
    task: TaskSpec
    permission: PermissionMode = PermissionMode.SAFE_EDIT
    model: str | None = None
    client: str | None = None
    timeout_s: int = 600
    usage_api: str | None = None  # provider usage-API for real cost (e.g. "eastrouter"); see eastrouter.py
    client_env: dict[str, str] = {}  # per-client vars from fleet.config.yaml


class RunManyJob(BaseModel):
    """One ``run_many`` slot: a primary request and an optional in-worker ``then`` follow-up."""

    request: RunRequest
    then: RunRequest | None = None


class RunManyJobResult(BaseModel):
    """Outcome of one ``run_many`` job. ``then`` is set only when the follow-up actually ran."""

    primary: RunRecord
    then: RunRecord | None = None
    then_skipped: str | None = None  # why ``then`` was not run (failed / no branch / no diff / …)

