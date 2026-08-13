"""The `marshal` CLI - inspect backends, usage, and fleet state."""

from __future__ import annotations

import argparse
import json
import os
import sys
from pathlib import Path

from ...core.layout import marshal_dir
from ...orchestration.fleet import Fleet
from ..doctor import FAIL, OK, WARN, doctor_report, run_checks
from ..scaffold import scaffold_fleet_config
from ..workspaces import (
    WorkspaceRegistry,
    register_workspace,
    remove_workspace,
    workspaces_file_path,
)
from .common import _resolve_repo


def _cmd_workspace(args: argparse.Namespace) -> int:
    """Manage the central workspace registry (~/.marshal/workspaces.yaml)."""
    as_json = getattr(args, "json", False)
    if args.ws_cmd == "add":
        path = Path(args.path or os.getcwd())
        # Register first - it validates the name + that the path is an existing dir - so a bad path
        # errors cleanly instead of scaffolding a stray fleet.config.yaml into nowhere.
        try:
            wdef = register_workspace(args.name, path)
        except ValueError as exc:
            print(f"error: {exc}", file=sys.stderr)
            return 1
        scaffolded = scaffold_fleet_config(wdef.path) if not args.no_scaffold else False
        if as_json:
            print(json.dumps({"name": wdef.name, "path": str(wdef.path), "scaffolded": scaffolded}, indent=2))
            return 0
        print(f"registered workspace {wdef.name!r} -> {wdef.path}")
        if scaffolded:
            print(f"  scaffolded a starter {wdef.config_path.name} (edit it, then `marshal doctor`)")
        elif not wdef.config_path.exists():
            print(f"  note: no {wdef.config_path.name} yet (zero clients) - add one or re-run with scaffolding")
        print(f"  registry: {workspaces_file_path()}")
        return 0

    if args.ws_cmd == "remove":
        removed = remove_workspace(args.name)
        print(f"removed workspace {args.name!r}" if removed else f"no workspace {args.name!r} in the registry")
        return 0 if removed else 1

    # default: list
    rows = WorkspaceRegistry.from_env().describe()
    if as_json:
        print(json.dumps(rows, indent=2))
        return 0
    print(f"registry: {workspaces_file_path()}")
    for r in rows:
        flag = " (default)" if r["default"] else ""
        # Show why a workspace is unusable, not just that a config file happens to exist: "0
        # clients" and "config does not load" look identical otherwise, and both look like "ready".
        cfg = f"{r['client_count']} clients" if r["ready"] else f"NOT READY: {r['ready_reason']}"
        # Recency first among the trailing columns: with a dozen registered repos, "which was I
        # just in" is the question the list actually gets asked.
        last = (r["last_activity_at"] or "")[:16].replace("T", " ") or "no runs"
        print(f"  {r['name']:14}{flag:10} {cfg:12} {last:17} {r['path']}")
    return 0


def _cmd_clean(args: argparse.Namespace) -> int:
    """Tear down finished runs' worktrees + branches (the usage ledger is never touched)."""
    repo = _resolve_repo(args)
    # clean needs no backends - a bare Fleet just reuses its state + worktree managers.
    fleet = Fleet(repo, {}, base_dir=marshal_dir(repo))
    result = fleet.clean(
        scope=args.scope,
        run_ids=args.run_ids or None,
        older_than_hours=args.older_than,
        dry_run=args.dry_run,
    )
    if args.json:
        print(json.dumps(result.model_dump(mode="json"), indent=2))
        return 1 if result.errors else 0
    verb = "would remove" if result.dry_run else "removed"
    print(
        f"{verb} {len(result.removed)} run(s); orphans {len(result.orphans_removed)}; "
        f"skipped {len(result.skipped)}; errors {len(result.errors)}"
    )
    for rid in result.removed:
        print(f"  {verb}: {rid}")
    for rid in result.orphans_removed:
        print(f"  {verb} orphan: {rid} (worktree with no run record)")
    for s in result.skipped:
        print(f"  skipped: {s['run_id']} ({s['reason']})")
    for e in result.errors:
        print(f"  error: {e['run_id']} ({e['error']})")
    return 1 if result.errors else 0


def _cmd_init(args: argparse.Namespace) -> int:
    """Scaffold a starter ``fleet.config.yaml`` into the repo. Does not touch the workspace registry."""
    repo = _resolve_repo(args)
    cfg_path = repo / "fleet.config.yaml"
    if not scaffold_fleet_config(repo):
        print(f"error: {cfg_path} already exists; not overwriting", file=sys.stderr)
        return 1
    print(f"wrote {cfg_path}")
    print("Uncomment at least one client, then run `marshal doctor`.")
    return 0


def _cmd_doctor(args: argparse.Namespace) -> int:
    repo = Path(args.repo or os.environ.get("MARSHAL_REPO", ".")).resolve()
    cfg_path = Path(args.config or os.environ.get("MARSHAL_CONFIG") or repo / "fleet.config.yaml")
    report = doctor_report(run_checks(repo, cfg_path))
    if args.json:
        print(json.dumps(report.model_dump(mode="json"), indent=2))
        return 1 if report.fails else 0
    for c in report.checks:
        print(f"{_GLYPH[c.status]} {c.name}: {c.detail}")
        if c.fix and c.status != OK:
            print(f"    fix: {c.fix}")
    print(f"\n{report.fails} issue(s), {report.warns} warning(s)")
    return 1 if report.fails else 0

_GLYPH = {OK: "✓", WARN: "⚠", FAIL: "✗"}
