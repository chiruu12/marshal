"""The Fleet orchestrator — ties backends + worktrees + usage + state into one run loop.

`Fleet.run(...)` is the cohesive unit: create an isolated worktree, run the chosen backend in it,
record the usage event, persist the run's state, and (by default) keep the worktree so its diff can
be collected/integrated later. Backends are injected (a dict name -> backend) so the Fleet is
testable without real CLIs; the MCP/CLI layer supplies real ones via the registry.
"""

from __future__ import annotations

import sys
from collections.abc import Mapping
from dataclasses import dataclass, field
from datetime import datetime, timezone
from pathlib import Path

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


@dataclass
class CollectResult:
    """A run's uncommitted work, surfaced read-only for the driver to review."""

    run_id: str
    branch: str | None
    worktree: str | None
    changed_files: list[str]
    diff: str


@dataclass
class IntegrateResult:
    """Outcome of merging a run's worktree branch back into the current branch.

    status is "merged" (changes landed), "conflict" (merge aborted, resolve manually), or
    "empty" (the run produced no changes to integrate).
    """

    run_id: str
    status: str
    branch: str | None = None
    merged_into: str | None = None
    changed_files: list[str] = field(default_factory=list)
    conflicts: list[str] = field(default_factory=list)
    commit: str | None = None


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
        self.state = FleetState(base / "fleet.json")
        self.usage = UsageTracker(base / "usage")
        self.backends: dict[str, CodingAgentBackend] = dict(backends)
        self.prices = prices if prices is not None else _load_default_prices()

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
        if backend_name not in self.backends:
            raise ValueError(f"no such backend: {backend_name!r}")
        backend = self.backends[backend_name]
        ts = ts or _now()
        run_id = f"{task.id}.{backend_name}"

        wt = self.worktrees.create(run_id, base_branch=task.base_branch)
        self.state.add(
            RunRecord(
                run_id=run_id,
                task_id=task.id,
                backend=backend_name,
                client=client,
                model=model,
                status=RunStatus.RUNNING.value,
                worktree=str(wt.path),
                branch=wt.branch,
                started_at=ts,
            )
        )

        result = backend.run(
            task, RunOpts(cwd=wt.path, permission=permission, model=model, timeout_s=timeout_s)
        )

        usage = backend.extract_usage(result)        # the seam (default: result.usage)
        self._price_usage(usage, model)              # normalize cost + source (native/estimated/unavailable)
        status = self._authoritative_status(result, wt)

        event = UsageEvent.from_result(
            result, run_id=run_id, backend=backend_name, ts=ts, usage=usage, client=client, model=model
        )
        event.status = status.value                  # report the authoritative outcome (incl. EMPTY)
        self.usage.record(event)

        record = self.state.update(
            run_id,
            status=status.value,
            cost_usd=event.cost_usd,
            input_tokens=event.input_tokens,
            output_tokens=event.output_tokens,
            duration_ms=result.duration_ms,
            source=event.source,
            ended_at=_now(),
            error=result.error,
        )

        if cleanup:
            self.worktrees.remove(wt)
        return record

    def _price_usage(self, usage: UsageRecord | None, model: str | None) -> None:
        """Normalize cost + source in place: keep native cost, else estimate, else unavailable.

        `source` describes how we know the COST. Tokens are kept regardless; a tokened run with no
        price is `unavailable` (cost unknown), never a misleading $0.
        """
        if usage is None:
            return
        if usage.source is UsageSource.NATIVE and usage.cost_usd > 0:
            return  # backend reported real cost (e.g. OpenCode)
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
        the repo's current branch. A conflict is reported and the merge aborted (repo stays
        clean); an empty run is a no-op. On success the run is stamped `merged_into`.
        """
        wt = self._worktree_for(run_id)
        if not wt.branch:
            raise ValueError(f"run {run_id!r} has no branch to integrate")
        changed = self.worktrees.changed_files(wt)
        commit = self.worktrees.commit_all(wt, message or f"marshal: integrate {run_id}")
        if commit is None:
            return IntegrateResult(run_id=run_id, status="empty", branch=wt.branch)

        target = self.worktrees.current_branch()
        merge = self.worktrees.merge(wt.branch)
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
