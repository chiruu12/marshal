"""The Fleet orchestrator — ties backends + worktrees + usage + state into one run loop.

`Fleet.run(...)` is the cohesive unit: create an isolated worktree, run the chosen backend in it,
record the usage event, persist the run's state, and (by default) keep the worktree so its diff can
be collected/integrated later. Backends are injected (a dict name -> backend) so the Fleet is
testable without real CLIs; the MCP/CLI layer supplies real ones via the registry.
"""

from __future__ import annotations

import sys
import threading
import time
import uuid
from collections.abc import Mapping
from concurrent.futures import ThreadPoolExecutor
from datetime import datetime, timezone
from pathlib import Path

from pydantic import BaseModel

from .backends.base import CodingAgentBackend
from .pricing import PriceTable, PricingError
from .state import FleetState, RunRecord
from .types import AgentResult, PermissionMode, RunOpts, RunStatus, TaskSpec, UsageRecord, UsageSource
from .usage import UsageEvent, UsageTracker
from .worktree import Worktree, WorktreeError, WorktreeManager


def _now() -> str:
    return datetime.now(timezone.utc).isoformat()


def _load_default_prices() -> PriceTable:
    """Load the shipped price table; on any problem fall back to empty (everything unpriced)."""
    try:
        return PriceTable.load()
    except PricingError as exc:
        print(f"[marshal] price table unavailable: {exc}; costs will be unpriced", file=sys.stderr)
        return PriceTable({})


class CollectResult(BaseModel):
    """A run's uncommitted work, surfaced read-only for the driver to review."""

    run_id: str
    branch: str | None
    worktree: str | None
    changed_files: list[str]
    diff: str


class IntegrateResult(BaseModel):
    """Outcome of merging a run's worktree branch back into the current branch.

    status is one of: "merged" (changes landed), "conflict" (merge aborted, resolve manually),
    "blocked" (the target checkout is dirty/colliding or detached HEAD — nothing changed, fixable
    then retry), "empty" (the run produced no changes to integrate), or "error" (a git operation
    failed in a way the engine can't classify as cleanly recoverable — commit failure, repo left
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

    `cheapest`/`fastest` name the winning client among *comparable* strategies only — succeeded,
    and (for cheapest) with a known cost (native/estimated, never `unavailable`). None when no
    strategy qualifies. The per-strategy rows carry `source` so an estimate is never read as truth.
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


class Fleet:
    def __init__(
        self,
        repo_root: Path | str,
        backends: Mapping[str, CodingAgentBackend],
        *,
        base_dir: Path | str | None = None,
        worktree_base: Path | str | None = None,
        prices: PriceTable | None = None,
    ) -> None:
        self.repo_root = Path(repo_root)
        base = Path(base_dir) if base_dir is not None else self.repo_root / ".marshal"
        self.worktrees = WorktreeManager(self.repo_root, worktree_base or base / "worktrees")
        self.state = FleetState(base / "runs")
        self.usage = UsageTracker(base / "usage")
        self.backends: dict[str, CodingAgentBackend] = dict(backends)
        self.prices = prices if prices is not None else _load_default_prices()
        # `git worktree add` is the one step that races across threads; serialize just that (it's
        # milliseconds — the long-running agent runs still proceed fully in parallel).
        self._create_lock = threading.Lock()
        # Persistent pool for non-blocking spawn(); lives as long as this Fleet (i.e. the long-lived
        # MCP server) so background runs outlive the driver turn that started them.
        self._bg: ThreadPoolExecutor | None = None
        self._bg_max = 4

    def run(
        self,
        backend_name: str,
        task: TaskSpec,
        *,
        permission: PermissionMode = PermissionMode.SAFE_EDIT,
        model: str | None = None,
        client: str | None = None,
        timeout_s: int = 600,
        ts: str | None = None,
        cleanup: bool = False,
    ) -> RunRecord:
        """Run one task synchronously: worktree -> backend -> usage -> persist. Blocks until done."""
        req = RunRequest(
            backend_name=backend_name,
            task=task,
            permission=permission,
            model=model,
            client=client,
            timeout_s=timeout_s,
        )
        run_id, wt, started = self._start(req, ts)
        return self._execute(req, run_id, wt, started, cleanup=cleanup)

    def spawn(self, request: RunRequest, *, ts: str | None = None) -> str:
        """Start a run in the background and return its run_id immediately (does NOT wait).

        The run is recorded RUNNING synchronously (so `status()`/`get_run()` see it at once), then
        the agent executes on a persistent pool that outlives this call — so background runs survive
        the driver turn that started them. The driver polls for the terminal status.
        """
        run_id, wt, started = self._start(request, ts)
        self._executor().submit(self._execute_bg, request, run_id, wt, started)
        return run_id

    def spawn_many(self, requests: list[RunRequest], *, ts: str | None = None) -> list[str]:
        """Spawn several runs in the background; return their run_ids in order."""
        return [self.spawn(req, ts=ts) for req in requests]

    def shutdown(self, *, wait: bool = True) -> None:
        """Shut the background spawn pool (drains in-flight runs). A no-op if none were spawned."""
        if self._bg is not None:
            self._bg.shutdown(wait=wait)
            self._bg = None

    def _executor(self) -> ThreadPoolExecutor:
        if self._bg is None:
            self._bg = ThreadPoolExecutor(max_workers=self._bg_max, thread_name_prefix="marshal-spawn")
        return self._bg

    def _start(self, req: RunRequest, ts: str | None) -> tuple[str, Worktree, str]:
        """Synchronous prefix: validate, create the worktree, record RUNNING -> (run_id, wt, ts)."""
        if req.backend_name not in self.backends:
            raise ValueError(f"no such backend: {req.backend_name!r}")
        started = ts or _now()
        # Globally unique: a retry or same-task fan-out must not collide on the branch, the worktree
        # dir, or the state record. task_id stays the grouping key on RunRecord.
        run_id = f"{req.task.id}.{req.backend_name}.{uuid.uuid4().hex[:8]}"
        with self._create_lock:
            wt = self.worktrees.create(run_id, base_branch=req.task.base_branch)
        self.state.add(
            RunRecord(
                run_id=run_id,
                task_id=req.task.id,
                backend=req.backend_name,
                client=req.client,
                model=req.model,
                status=RunStatus.RUNNING.value,
                worktree=str(wt.path),
                branch=wt.branch,
                started_at=started,
            )
        )
        return run_id, wt, started

    def _execute(
        self, req: RunRequest, run_id: str, wt: Worktree, ts: str, *, cleanup: bool = False
    ) -> RunRecord:
        """Execute suffix: run the backend, price + classify, persist the terminal record."""
        backend = self.backends[req.backend_name]
        try:
            result = backend.run(
                req.task,
                RunOpts(
                    cwd=wt.path, permission=req.permission, model=req.model, timeout_s=req.timeout_s
                ),
            )
            usage = backend.extract_usage(result)    # the seam (default: result.usage)
            self._price_usage(usage, req.model)      # normalize cost + source
            status = self._authoritative_status(result, wt)
            event = UsageEvent.from_result(
                result, run_id=run_id, backend=req.backend_name, ts=ts, usage=usage,
                client=req.client, model=req.model,
            )
            event.status = status.value              # report the authoritative outcome (incl. EMPTY)
            self.usage.record(event)
            record = self.state.update(
                run_id,
                status=status.value,
                cost_usd=event.cost_usd,
                input_tokens=event.input_tokens,
                output_tokens=event.output_tokens,
                duration_ms=result.duration_ms,
                source=event.source,
                text=result.text[:16000],  # the agent's final message, so reply/analysis tasks are reviewable
                ended_at=_now(),
                error=result.error,
            )
        except Exception as exc:  # noqa: BLE001 — never leave a run stranded as RUNNING
            # Terminal-stamp the record before re-raising, so one failure can't leave a zombie.
            self.state.update(run_id, status=RunStatus.FAILED.value, ended_at=_now(), error=f"fleet: {exc}")
            raise

        if cleanup:
            self.worktrees.remove(wt)
        return record

    def _execute_bg(self, req: RunRequest, run_id: str, wt: Worktree, ts: str) -> None:
        """Background variant: the outcome (incl. failure) is already persisted; never propagate."""
        try:
            self._execute(req, run_id, wt, ts)
        except Exception:  # noqa: BLE001 — _execute already terminal-stamped; the driver polls status()
            pass

    def run_many(
        self,
        requests: list[RunRequest],
        *,
        max_concurrency: int = 4,
        stagger_s: float = 0.1,
    ) -> list[RunRecord]:
        """Run a batch of requests concurrently in isolated worktrees; block until all finish.

        Concurrency is capped at `max_concurrency` (each agent CLI is 150-400 MB, so an uncapped
        fan-out OOMs the host). Submissions are spaced by `stagger_s` to ease the Cursor
        concurrent-launch file-lock race. A single request's failure is captured as a FAILED record
        and never aborts the batch. Records are returned in the same order as `requests`.
        """
        results: list[RunRecord | None] = [None] * len(requests)
        with ThreadPoolExecutor(max_workers=max(1, max_concurrency)) as pool:
            futures = {}
            for i, req in enumerate(requests):
                if stagger_s and i:
                    time.sleep(stagger_s)
                futures[pool.submit(self._run_request, req)] = i
            for fut in futures:
                results[futures[fut]] = fut.result()  # _run_request never raises
        return [r for r in results if r is not None]

    def _run_request(self, req: RunRequest) -> RunRecord:
        """run() one request, capturing any failure as a FAILED record so a batch survives it."""
        try:
            return self.run(
                req.backend_name,
                req.task,
                permission=req.permission,
                model=req.model,
                client=req.client,
                timeout_s=req.timeout_s,
            )
        except Exception as exc:  # noqa: BLE001 — one job's failure must not abort the batch
            return RunRecord(
                run_id=f"{req.task.id}.{req.backend_name}",
                task_id=req.task.id,
                backend=req.backend_name,
                client=req.client,
                model=req.model,
                status=RunStatus.FAILED.value,
                ended_at=_now(),
                error=f"run_many: {exc}",
            )

    def _price_usage(self, usage: UsageRecord | None, model: str | None) -> None:
        """Normalize cost + source in place: keep native cost, else estimate, else unavailable.

        `source` describes how we know the COST. Tokens are kept regardless; a tokened run with no
        price is `unavailable` (cost unknown), never a misleading $0.
        """
        if usage is None:
            return
        if usage.source is UsageSource.NATIVE:
            return  # backend authoritatively reported the cost (a real $0 included) — never override
        if usage.input_tokens + usage.output_tokens <= 0:
            usage.cost_usd = 0.0
            usage.source = UsageSource.UNAVAILABLE
            return
        est = self.prices.estimate(
            model or usage.model,
            input_tokens=usage.input_tokens,
            output_tokens=usage.output_tokens,
            cache_read_tokens=usage.cache_read_tokens,
        )
        if est is None:
            usage.cost_usd = 0.0
            usage.source = UsageSource.UNAVAILABLE  # unpriced -> cost unavailable (tokens kept)
        else:
            usage.cost_usd = est
            usage.source = UsageSource.ESTIMATED

    def _authoritative_status(self, result: AgentResult, wt: Worktree) -> RunStatus:
        """A clean exit that produced no work (no text, no file changes) is EMPTY, not success."""
        if result.status is not RunStatus.SUCCEEDED:
            return result.status
        if result.text.strip():
            return RunStatus.SUCCEEDED
        try:
            changed = self.worktrees.changed_files(wt)
        except WorktreeError:
            return RunStatus.SUCCEEDED  # can't tell -> don't mislabel a success as empty
        return RunStatus.SUCCEEDED if changed else RunStatus.EMPTY

    def collect_run(self, run_id: str) -> CollectResult:
        """Surface a run's uncommitted diff + changed files. Read-only — nothing is merged."""
        wt = self._worktree_for(run_id)
        return CollectResult(
            run_id=run_id,
            branch=wt.branch or None,
            worktree=str(wt.path),
            changed_files=self.worktrees.changed_files(wt),
            diff=self.worktrees.diff(wt),
        )

    def integrate(
        self, run_id: str, *, message: str | None = None, cleanup: bool = False
    ) -> IntegrateResult:
        """Merge a run's worktree branch back into the current branch, handling conflicts.

        Commits the worktree's uncommitted work onto its branch, then merges that branch into
        the repo's current branch. Outcomes: "merged" (stamps `merged_into`), "conflict" (aborted,
        repo left clean), "blocked" (target dirty/colliding or detached HEAD — fix it and retry),
        or "empty" (nothing to integrate). The blocked/conflict commit stays on the branch, so a
        retry after fixing the target re-merges it instead of reporting "empty".
        """
        wt = self._worktree_for(run_id)
        if not wt.branch:
            raise ValueError(f"run {run_id!r} has no branch to integrate")
        try:
            target = self.worktrees.current_branch()  # refuses detached HEAD before committing
        except WorktreeError as exc:
            return IntegrateResult(run_id=run_id, status="blocked", branch=wt.branch, message=str(exc))

        try:
            changed = self.worktrees.changed_files(wt)
            commit = self.worktrees.commit_all(wt, message or f"marshal: integrate {run_id}")
            # "empty" only when the worktree is clean AND the branch has no commits past target.
            # (A prior blocked/conflict already committed the work, so a retry still has work to merge.)
            if commit is None and not self.worktrees.has_unmerged_commits(wt.branch, target):
                return IntegrateResult(run_id=run_id, status="empty", branch=wt.branch)
            if commit is None:
                # retry: a prior blocked/conflict attempt already committed the work, so the
                # worktree is clean now. Report what the branch actually lands, not the empty
                # worktree state. (Compute before the merge — afterwards target..branch is empty.)
                commit = self.worktrees.branch_tip(wt.branch)
                changed = self.worktrees.merged_diff_files(wt.branch, target)
            merge = self.worktrees.merge(wt.branch)
        except WorktreeError as exc:
            # a git op failed in a way we can't classify as cleanly recoverable (commit failure,
            # repo left mid-merge, timeout). Surface a distinct "error" status (not the recoverable
            # "blocked") so a driver doesn't blindly retry — the cause needs a human.
            return IntegrateResult(
                run_id=run_id, status="error", branch=wt.branch, merged_into=target, message=str(exc)
            )
        if merge.blocked:
            return IntegrateResult(
                run_id=run_id,
                status="blocked",
                branch=wt.branch,
                merged_into=target,
                commit=commit,
                message=merge.message,
            )
        if not merge.ok:
            return IntegrateResult(
                run_id=run_id,
                status="conflict",
                branch=wt.branch,
                merged_into=target,
                conflicts=merge.conflicts,
                commit=commit,
            )

        self.state.update(run_id, merged_into=target)
        if cleanup:
            self.worktrees.remove(wt)
        return IntegrateResult(
            run_id=run_id,
            status="merged",
            branch=wt.branch,
            merged_into=target,
            changed_files=changed,
            commit=commit,
        )

    def _worktree_for(self, run_id: str) -> Worktree:
        """Reconstruct the live Worktree for a recorded run, or raise if it is gone."""
        rec = self.state.get(run_id)
        if rec is None:
            raise ValueError(f"no such run: {run_id!r}")
        if not rec.worktree:
            raise ValueError(f"run {run_id!r} has no worktree")
        path = Path(rec.worktree)
        if not path.exists():
            raise ValueError(f"worktree for run {run_id!r} no longer exists: {path}")
        return Worktree(task_id=rec.task_id, path=path, branch=rec.branch or "")
