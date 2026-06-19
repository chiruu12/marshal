"""The Fleet orchestrator — ties backends + worktrees + usage + state into one run loop.

`Fleet.run(...)` is the cohesive unit: create an isolated worktree, run the chosen backend in it,
record the usage event, persist the run's state, and (by default) keep the worktree so its diff can
be collected/integrated later. Backends are injected (a dict name -> backend) so the Fleet is
testable without real CLIs; the MCP/CLI layer supplies real ones via the registry.
"""

from __future__ import annotations

from collections.abc import Mapping
from datetime import datetime, timezone
from pathlib import Path

from .backends.base import CodingAgentBackend
from .state import FleetState, RunRecord
from .types import PermissionMode, RunOpts, RunStatus, TaskSpec
from .usage import UsageEvent, UsageTracker
from .worktree import WorktreeManager


def _now() -> str:
    return datetime.now(timezone.utc).isoformat()


class Fleet:
    def __init__(
        self,
        repo_root: Path | str,
        backends: Mapping[str, CodingAgentBackend],
        *,
        base_dir: Path | str | None = None,
        worktree_base: Path | str | None = None,
    ) -> None:
        self.repo_root = Path(repo_root)
        base = Path(base_dir) if base_dir is not None else self.repo_root / ".marshal"
        self.worktrees = WorktreeManager(self.repo_root, worktree_base or base / "worktrees")
        self.state = FleetState(base / "fleet.json")
        self.usage = UsageTracker(base / "usage")
        self.backends: dict[str, CodingAgentBackend] = dict(backends)

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

        event = UsageEvent.from_result(
            result, run_id=run_id, backend=backend_name, ts=ts, client=client, model=model
        )
        self.usage.record(event)

        record = self.state.update(
            run_id,
            status=result.status.value,
            cost_usd=event.cost_usd,
            input_tokens=event.input_tokens,
            output_tokens=event.output_tokens,
            ended_at=_now(),
            error=result.error,
        )

        if cleanup:
            self.worktrees.remove(wt)
        return record
