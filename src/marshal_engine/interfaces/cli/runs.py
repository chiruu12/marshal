"""The `marshal` CLI - inspect backends, usage, and fleet state."""

from __future__ import annotations

import argparse
import json
import os
import sys
from pathlib import Path

from ...accounting.budgets import BudgetExceeded
from ...core.config import ConfigError
from ...runtime.worktree import WorktreeError
from .common import _build_cli_service, _require_git_work_tree

def _cmd_run_like(args: argparse.Namespace, *, spawn: bool) -> int:
    """Shared body for `run` (blocking) and `spawn` (background)."""
    repo = Path(args.repo or os.environ.get("MARSHAL_REPO", ".")).resolve()
    try:
        # Before missing-config warnings: non-git --repo is the primary error (see issue #19).
        _require_git_work_tree(repo)
        svc = _build_cli_service(args)
    except (ConfigError, ValueError) as exc:
        print(f"error: {exc}", file=sys.stderr)
        return 1
    run_kwargs = {
        "task_id": args.task_id,
        "task_kind": args.task_kind,
        "model": args.model,
        "backend": args.backend,
        "duration": args.duration,
    }
    try:
        rec = (
            svc.spawn(args.client, args.goal, **run_kwargs)
            if spawn
            else svc.run_agent(args.client, args.goal, **run_kwargs)
        )
    except (ValueError, ConfigError, BudgetExceeded, WorktreeError) as exc:
        # WorktreeError: worktree create/remove failures after a valid git repo was confirmed.
        # Prefer a one-line stderr over a traceback.
        print(f"error: {exc}", file=sys.stderr)
        return 1
    if args.json:
        print(json.dumps(rec.model_dump(mode="json"), indent=2))
        return 0
    line = f"{rec.run_id}  {rec.backend}/{rec.model or '-'}  {rec.status}"
    if spawn:
        line += "  (poll: marshal status)"
    print(line)
    return 0


def _cmd_run(args: argparse.Namespace) -> int:
    """Run a task synchronously on a configured client (or ad-hoc by bare backend + model)."""
    return _cmd_run_like(args, spawn=False)


def _cmd_spawn(args: argparse.Namespace) -> int:
    """Start a run in the background; returns its RUNNING record at once."""
    return _cmd_run_like(args, spawn=True)
