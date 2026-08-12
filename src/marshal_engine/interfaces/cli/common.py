"""The `marshal` CLI - inspect backends, usage, and fleet state."""

from __future__ import annotations

import argparse
import os
import subprocess
from pathlib import Path

from ...core.config import DURATION_PRESETS
from ..service import MarshalService
from ..workspaces import (
    DEFAULT_WORKSPACE,
    WorkspaceDef,
    build_service_for,
)

def _positive_int(raw: str) -> int:
    """A count that must be >= 1. argparse turns the ValueError into a usage error.

    Unbounded here while the MCP tool bounds the same parameter is not a cosmetic gap: a NEGATIVE
    limit silently returns a *different* slice (`rows[:-5]` drops the last five rather than showing
    five), so the caller gets plausible-looking wrong output instead of an error.
    """
    value = int(raw)
    if value < 1:
        raise ValueError(f"must be 1 or greater, got {value}")
    return value


def _positive_hours(raw: str) -> float:
    """A lookback that must be finite and > 0 - NaN/inf reach timedelta and raise mid-command."""
    value = float(raw)
    if not (value > 0) or value in (float("inf"), float("-inf")):
        raise ValueError(f"must be a finite number greater than 0, got {raw!r}")
    return value


def _resolve_repo(args: argparse.Namespace) -> Path:
    return Path(args.repo or os.environ.get("MARSHAL_REPO", ".")).resolve()


def _require_git_work_tree(repo: Path) -> None:
    """Fail fast when ``--repo`` / ``MARSHAL_REPO`` is not a git work tree.

    Matches ``marshal doctor`` wording so a non-git path does not lead with the missing-config
    advisory (which sends drivers down a config rabbit hole).
    """
    try:
        inside = subprocess.run(
            ["git", "-C", str(repo), "rev-parse", "--is-inside-work-tree"],
            capture_output=True,
            text=True,
            timeout=10,
            check=False,
        )
    except (OSError, subprocess.SubprocessError) as exc:
        raise ValueError(f"{repo}: git not runnable") from exc
    if inside.returncode != 0 or inside.stdout.strip() != "true":
        raise ValueError(
            f"{repo}: not a git work tree (point MARSHAL_REPO / --repo at a git repo)"
        )


def _build_cli_service(args: argparse.Namespace) -> MarshalService:
    """Build a MarshalService for `run`/`spawn` from CLI args (mirrors mcp_server.build_service).

    A repo with no fleet.config.yaml still builds, with zero clients, so `marshal run --backend ...
    --model ...` works ad-hoc without ever editing a config file. Missing config warns on stderr
    (same posture as the MCP entry point) so a wrong ``--repo``/cwd does not look like
    ``known: (none configured)`` with no explanation. Callers that need a git repo (``run`` /
    ``spawn``) must preflight via ``_require_git_work_tree`` first so the advisory never masks a
    non-git path.
    """
    repo = Path(args.repo or os.environ.get("MARSHAL_REPO", ".")).resolve()
    cfg_path = Path(args.config or os.environ.get("MARSHAL_CONFIG") or repo / "fleet.config.yaml")
    return build_service_for(
        WorkspaceDef(name=DEFAULT_WORKSPACE, path=repo, config_path=cfg_path),
        missing_config="legacy",
        config_warnings="plain",
    )


def _add_run_args(parser: argparse.ArgumentParser) -> None:
    parser.add_argument("--client", default=None, help="name of a configured client (from list_clients)")
    parser.add_argument("--backend", default=None, help="ad-hoc backend name (e.g. 'opencode', 'claude-code'); ignored when --client is also set")
    parser.add_argument("--model", default=None, help="model to pass (overrides the client's resolved model)")
    parser.add_argument(
        "--duration", default=None,
        help=f"per-spawn timeout override: a preset ({','.join(DURATION_PRESETS)}) or positive seconds",
    )
    parser.add_argument("--goal", required=True, help="natural-language task for the agent")
    parser.add_argument("--task-id", default=None, help="optional grouping id (default: random)")
    parser.add_argument(
        "--task-kind",
        default=None,
        help="optional free-text tag for the kind of work (e.g. refactor, bugfix); stamped on the usage event",
    )
    parser.add_argument("--repo", default=None, help="target repo root (default: $MARSHAL_REPO or cwd)")
    parser.add_argument("--config", default=None, help="fleet config path (default: <repo>/fleet.config.yaml)")
    parser.add_argument("--json", action="store_true", help="output JSON")
