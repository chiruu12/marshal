"""Canonical on-disk layout for Marshal runtime state under a repo root.

Every path under ``<repo>/.marshal/`` is defined here so the engine, CLI, and MCP
layer agree on one layout. Import from this module instead of hardcoding subpaths.
"""

from __future__ import annotations

from pathlib import Path

_MARSHAL_DIRNAME = ".marshal"


def marshal_dir(repo: Path | str) -> Path:
    """Return ``<repo>/.marshal`` — the root for all Marshal on-disk state."""
    return Path(repo) / _MARSHAL_DIRNAME


def worktrees_dir(repo: Path | str) -> Path:
    """Return ``<repo>/.marshal/worktrees``."""
    return marshal_dir(repo) / "worktrees"


def runs_dir(repo: Path | str) -> Path:
    """Return ``<repo>/.marshal/runs``."""
    return marshal_dir(repo) / "runs"


def usage_dir(repo: Path | str) -> Path:
    """Return ``<repo>/.marshal/usage``."""
    return marshal_dir(repo) / "usage"


def logs_dir(repo: Path | str) -> Path:
    """Return ``<repo>/.marshal/logs``."""
    return marshal_dir(repo) / "logs"


def reports_dir(repo: Path | str) -> Path:
    """Return ``<repo>/.marshal/reports`` — durable markdown twins of team review reports."""
    return marshal_dir(repo) / "reports"


def artifacts_dir(repo: Path | str) -> Path:
    """Return ``<repo>/.marshal/artifacts`` — per-run outputs that outlive their worktree.

    A worktree is discarded on clean, so anything an agent wrote there dies with it. Artifacts are
    harvested out per run so a later round can be handed the previous round's report instead of the
    driver pasting findings into the next prompt by hand.
    """
    return marshal_dir(repo) / "artifacts"


def run_artifacts_dir(repo: Path | str, run_id: str) -> Path:
    """Return ``<repo>/.marshal/artifacts/<run_id>``. Caller must pass a validated run id."""
    return artifacts_dir(repo) / run_id


def budget_gate_path(repo: Path | str) -> Path:
    """Return ``<repo>/.marshal/budget_gate.json`` — cross-process enforce-budget reservations."""
    return marshal_dir(repo) / "budget_gate.json"
