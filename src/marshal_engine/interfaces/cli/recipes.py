"""The `marshal` CLI - inspect backends, usage, and fleet state."""

from __future__ import annotations

import argparse
import json
import os
import sys
from pathlib import Path
from typing import Any

from ...core.config import ConfigError, FleetConfig, load_config
from ...orchestration.teams import TeamSubject, load_team, team_paths, validate_team
from ...orchestration.workflow import (
    WorkflowRunner,
    load_workflow,
    validate_workflow,
    workflow_paths,
)
from ..service import MarshalService


def _workflow_dirs(repo: Path) -> list[Path]:
    """Workflow search order: repo-local recipes shadow the bundled examples."""
    return [repo / "workflows", repo / "examples" / "workflows"]


def _cmd_workflows(args: argparse.Namespace) -> int:
    repo = Path(args.repo or os.environ.get("MARSHAL_REPO", ".")).resolve()
    cfg_path = Path(args.config or os.environ.get("MARSHAL_CONFIG") or repo / "fleet.config.yaml")
    config = None
    if cfg_path.exists():
        try:
            config = load_config(cfg_path)
        except ConfigError:
            config = None  # a broken config is its own `doctor` problem; still list/parse recipes

    rows: list[dict[str, Any]] = []
    seen: set[str] = set()
    for wdir in _workflow_dirs(repo):
        for p in workflow_paths(wdir):
            if p.stem in seen:
                continue  # shadowed by the same-named recipe earlier in the search order
            seen.add(p.stem)
            row: dict[str, Any] = {"file": p.name, "name": p.stem, "inputs": [], "phases": [], "error": None}
            try:
                spec = load_workflow(p)
                row["name"] = spec.name
                row["inputs"] = spec.inputs
                row["phases"] = [{"name": ph.name, "run": ph.run} for ph in spec.phases]
                if config is not None:
                    validate_workflow(spec, config)  # cross-check client names; fail-fast on a typo
            except ConfigError as exc:
                row["error"] = str(exc)
            rows.append(row)

    if args.json:
        print(json.dumps(rows, indent=2))
        return 1 if any(r["error"] for r in rows) else 0

    if not rows:
        print("no workflows found (copy a template from examples/workflows/)")
        return 0
    for row in rows:
        glyph = "✗" if row["error"] else "✓"
        phases = " → ".join(p["run"] for p in row["phases"]) or "(unparsed)"
        print(f"{glyph} {row['name']:16} [{phases}]  inputs={row['inputs']}")
        if row["error"]:
            print(f"    error: {row['error']}")
    if config is None:
        print(f"\nnote: no readable {cfg_path.name} - client names were not validated")
    return 1 if any(r["error"] for r in rows) else 0


def _cmd_workflow_run(args: argparse.Namespace) -> int:
    """Run a workflow recipe from examples/workflows/ or custom workflows."""
    repo = Path(args.repo or os.environ.get("MARSHAL_REPO", ".")).resolve()
    cfg_path = Path(args.config or os.environ.get("MARSHAL_CONFIG") or repo / "fleet.config.yaml")
    config = load_config(cfg_path) if cfg_path.exists() else FleetConfig()

    # Find the workflow file by name (repo-local recipes shadow examples/workflows/)
    spec = None
    for wdir in _workflow_dirs(repo):
        for p in workflow_paths(wdir):
            if p.stem == args.name:
                spec = load_workflow(p)
                break
        if spec is not None:
            break
    if spec is None:
        print(f"error: workflow {args.name!r} not found in workflows/ or examples/workflows/", file=sys.stderr)
        return 1

    # Parse inputs from --input key=value flags
    inputs: dict[str, str] = {}
    if args.input:
        for item in args.input:
            if "=" not in item:
                print(f"error: input must be in format key=value, got {item!r}", file=sys.stderr)
                return 1
            k, v = item.split("=", 1)
            inputs[k] = v

    # Run the workflow
    try:
        svc = MarshalService(repo, config, config_path=cfg_path)
        runner = WorkflowRunner(svc)
        result = runner.run(spec, inputs, max_concurrency=args.max_concurrency)

        if args.json:
            print(json.dumps(result.model_dump(mode="json"), indent=2))
            return 0

        # Human-readable output
        print(f"workflow {spec.name!r}: {result.status}")
        print(f"  phases: {len(result.phases)}")
        for i, phase in enumerate(result.phases):
            label = phase.name or f"phase-{i}"
            print(f"    {i+1}. {label}: {phase.run} ({len(phase.run_ids)} run(s))")
            for note in phase.notes:
                print(f"       note: {note}")
        if result.next_actions:
            print("  next actions:")
            for action in result.next_actions:
                print(f"    - {action}")
        return 0 if result.status == "completed" else 1
    except ConfigError as exc:
        print(f"error: {exc}", file=sys.stderr)
        return 1


def _cmd_teams(args: argparse.Namespace) -> int:
    """List review teams and validate them against the fleet config (incl. the read-only check)."""
    repo = Path(args.repo or os.environ.get("MARSHAL_REPO", ".")).resolve()
    cfg_path = Path(args.config or os.environ.get("MARSHAL_CONFIG") or repo / "fleet.config.yaml")
    config = None
    if cfg_path.exists():
        try:
            config = load_config(cfg_path)
        except ConfigError:
            config = None  # a broken config is its own `doctor` problem; still list/parse teams

    tdir = repo / "teams"
    rows: list[dict[str, Any]] = []
    for p in team_paths(tdir):
        row: dict[str, Any] = {"file": p.name, "name": p.stem, "target": None, "roles": [], "error": None}
        try:
            spec = load_team(p)
            row["name"] = spec.name
            row["target"] = spec.target
            row["roles"] = [{"name": r.name, "client": r.client} for r in spec.roles]
            if config is not None:
                # Cross-checks client names AND the fail-closed read-only rule, so a team that
                # could never run is caught here rather than at review time.
                validate_team(spec, config)
        except ConfigError as exc:
            row["error"] = str(exc)
        rows.append(row)

    if args.json:
        print(json.dumps(rows, indent=2))
        return 1 if any(r["error"] for r in rows) else 0

    if not rows:
        print(f"no teams in {tdir} (copy a template from examples/teams/)")
        return 0
    for row in rows:
        glyph = "✗" if row["error"] else "✓"
        roles = ", ".join(f"{r['name']}→{r['client']}" for r in row["roles"]) or "(unparsed)"
        print(f"{glyph} {row['name']:16} [{row['target'] or '?'}]  {roles}")
        if row["error"]:
            print(f"    error: {row['error']}")
    if config is None:
        print(f"\nnote: no readable {cfg_path.name} - clients and the read-only rule were not validated")
    return 1 if any(r["error"] for r in rows) else 0


def _cmd_team_run(args: argparse.Namespace) -> int:
    """Run a review team against a subject and print its report."""
    repo = Path(args.repo or os.environ.get("MARSHAL_REPO", ".")).resolve()
    cfg_path = Path(args.config or os.environ.get("MARSHAL_CONFIG") or repo / "fleet.config.yaml")
    config = load_config(cfg_path) if cfg_path.exists() else FleetConfig()

    text = args.text
    if args.plan_file:
        try:
            text = Path(args.plan_file).read_text(encoding="utf-8")
        except OSError as exc:
            print(f"error: cannot read {args.plan_file}: {exc}", file=sys.stderr)
            return 1

    base, head = args.base, args.head
    pr_ref = None
    if args.pr is not None and (base or head):
        print(
            "error: pass either --pr or --base/--head, not both: --pr resolves the range itself, "
            "and honouring both would review something neither argument describes.",
            file=sys.stderr,
        )
        return 1

    try:
        svc = MarshalService(repo, config, config_path=cfg_path)
        if args.pr is not None:
            pr_ref = svc.resolve_pr(args.pr)
            base, head = pr_ref.base, pr_ref.head
        subject = TeamSubject(
            kind=args.target, run_id=args.run_id, base=base, head=head, text=text,
            paths=args.path or [],
        )
        result = svc.run_team(args.name, subject, max_concurrency=args.max_concurrency)
    except ConfigError as exc:
        print(f"error: {exc}", file=sys.stderr)
        return 1

    if args.json:
        payload = result.model_dump(mode="json")
        if pr_ref is not None:
            payload["pull_request"] = {
                "number": pr_ref.number, "title": pr_ref.title, "url": pr_ref.url,
                "state": pr_ref.state, "stale": pr_ref.stale,
                "base": pr_ref.base, "head": pr_ref.head,
            }
        print(json.dumps(payload, indent=2))
    else:
        if pr_ref is not None:
            # Printed BEFORE the reviews: a panel reporting on the wrong PR reads exactly like one
            # reporting on the right PR, so name the subject before anyone reads a word of it.
            stale = "  [CLOSED/MERGED]" if pr_ref.stale else ""
            print(f"PR #{pr_ref.number}: {pr_ref.title}{stale}")
            print(f"{pr_ref.url}  ({pr_ref.base}...{pr_ref.head[:12]})\n")
        print(result.unified_report)
        if result.report_dir:
            print(f"reports written: {result.report_dir}")
    # Exit status reports whether the PANEL ran, not what it concluded - there is no verdict to
    # branch on, and a shell gate must not mistake "everyone reviewed" for "everyone approved".
    return 1 if result.incomplete_roles else 0
