"""Canonical on-disk layout for Marshal runtime state under a repo root.

Every path under ``<repo>/.marshal/`` is defined here so the engine, CLI, and MCP
layer agree on one layout. Import from this module instead of hardcoding subpaths.
"""

from __future__ import annotations

import hashlib
import os
import re
from pathlib import Path

_MARSHAL_DIRNAME = ".marshal"


def marshal_dir(repo: Path | str) -> Path:
    """Return ``<repo>/.marshal`` — the root for all Marshal on-disk state."""
    return Path(repo) / _MARSHAL_DIRNAME


def marshal_home() -> Path:
    """Marshal's per-user directory: ``MARSHAL_HOME`` if set, else ``~/.marshal``.

    Same home the workspace registry already lives in. The env var exists so a run tree can be put
    on another disk - and so the test suite never writes to the developer's real home.
    """
    raw = os.environ.get("MARSHAL_HOME")
    return Path(raw).expanduser() if raw else Path.home() / ".marshal"


def runs_root(repo: Path | str) -> Path:
    """Where this repo's run directories live - ``<marshal home>/worktrees/<name>-<digest>``.

    **Outside the repo, deliberately.** A run's working tree is the one directory an agent is meant
    to write, and while it sat at ``<repo>/.marshal/worktrees/<id>`` a plain ``../../..`` reached
    the operator's live checkout, their `.git`, and Marshal's own ledger - no exploit needed, just a
    relative path (#175). Moving it out does not make the agent unable to write elsewhere on the
    host; it removes the case where wandering upward lands somewhere costly by accident.

    Keyed by a digest of the resolved repo path, not by name alone: two checkouts of the same
    project (a worktree per feature, say) are different repos and must not share a run tree. The
    readable name is a prefix so the directory is identifiable by eye.
    """
    resolved = Path(repo).expanduser().resolve()
    digest = hashlib.sha256(str(resolved).encode("utf-8")).hexdigest()[:12]
    name = re.sub(r"[^A-Za-z0-9._-]", "-", resolved.name) or "repo"
    return marshal_home() / "worktrees" / f"{name}-{digest}"


def legacy_worktrees_dir(repo: Path | str) -> Path:
    """Return ``<repo>/.marshal/worktrees`` - where run directories lived before ``runs_root``.

    Kept so runs created by an older Marshal stay cleanable: their records hold absolute paths
    under here, and cleanup refuses any path outside a known base. Nothing new is created here.
    """
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
