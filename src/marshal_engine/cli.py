"""The `marshal` CLI - inspect backends, usage, and fleet state."""

from __future__ import annotations

import argparse
import json
import os
import sys
from pathlib import Path
from typing import Any

from . import __version__
from .config import ConfigError, load_config
from .doctor import FAIL, OK, WARN, run_checks, summarize
from .registry import backend_names, default_backends
from .state import FleetState
from .usage import UsageTracker
from .workflow import load_workflow, validate_workflow, workflow_paths
from .workspaces import (
    WorkspaceRegistry,
    register_workspace,
    remove_workspace,
    scaffold_fleet_config,
    workspaces_file_path,
)


def _cmd_backends(args: argparse.Namespace) -> int:
    backends = default_backends()
    if args.json:
        data = []
        for name in backend_names():
            b = backends[name]
            c = b.capabilities
            data.append(
                {
                    "name": name,
                    "available": b.check_available(),
                    "json_output": c.json_output,
                    "native_usage": c.native_usage,
                    "permission_modes": sorted(m.value for m in c.permission_modes),
                }
            )
        print(json.dumps(data, indent=2))
        return 0
    for name in backend_names():
        b = backends[name]
        c = b.capabilities
        modes = sorted(m.value for m in c.permission_modes)
        print(
            f"{name:13} available={str(b.check_available()):5} "
            f"json={str(c.json_output):5} usage={str(c.native_usage):5} modes={modes}"
        )
    return 0


def _cmd_usage(args: argparse.Namespace) -> int:
    s = UsageTracker(args.dir).summary()
    if args.json:
        print(json.dumps(s.model_dump(mode="json"), indent=2))
        return 0
    t = s.totals
    cps_str = f"${t.cost_per_succeeded:.4f}" if t.cost_per_succeeded is not None else "n/a"
    print(
        f"runs={t.runs}  succeeded={t.succeeded}  cost=${t.cost_usd:.4f} "
        f"(native ${t.cost_native:.4f} / est ${t.cost_estimated:.4f})"
    )
    print(f"  $/run=${t.cost_per_run:.4f}  $/succeeded={cps_str}  in={t.input_tokens} out={t.output_tokens}")
    for backend, v in sorted(s.by_backend.items()):
        print(f"  {backend:13} runs={v.runs:<4} succ={v.succeeded:<4} cost=${v.cost_usd:.4f}")
    return 0


def _cmd_status(args: argparse.Namespace) -> int:
    runs = FleetState(args.state).list()
    if args.json:
        print(json.dumps([r.model_dump(mode="json") for r in runs], indent=2))
        return 0
    if not runs:
        print(f"no runs recorded under {Path(args.state).resolve()}")
        return 0
    for r in runs:
        print(f"{r.run_id:24} {r.backend:12} {r.status:10} ${r.cost_usd:.4f}  {r.worktree or ''}")
    return 0


def _cmd_workflows(args: argparse.Namespace) -> int:
    repo = Path(args.repo or os.environ.get("MARSHAL_REPO", ".")).resolve()
    cfg_path = Path(args.config or os.environ.get("MARSHAL_CONFIG") or repo / "fleet.config.yaml")
    config = None
    if cfg_path.exists():
        try:
            config = load_config(cfg_path)
        except ConfigError:
            config = None  # a broken config is its own `doctor` problem; still list/parse recipes

    wdir = repo / "workflows"
    rows: list[dict[str, Any]] = []
    for p in workflow_paths(wdir):
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
        print(f"no workflows in {wdir} (copy a template from examples/workflows/)")
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
        cfg = f"{r['client_count']} clients" if r["configured"] else "no config"
        print(f"  {r['name']:14}{flag:10} {cfg:12} {r['path']}")
    return 0


_GLYPH = {OK: "✓", WARN: "⚠", FAIL: "✗"}


def _cmd_doctor(args: argparse.Namespace) -> int:
    repo = Path(args.repo or os.environ.get("MARSHAL_REPO", ".")).resolve()
    cfg_path = Path(args.config or os.environ.get("MARSHAL_CONFIG") or repo / "fleet.config.yaml")
    checks = run_checks(repo, cfg_path)
    fails, warns = summarize(checks)
    if args.json:
        payload = {
            "checks": [
                {"name": c.name, "status": c.status, "detail": c.detail, "fix": c.fix} for c in checks
            ],
            "fails": fails,
            "warns": warns,
        }
        print(json.dumps(payload, indent=2))
        return 1 if fails else 0
    for c in checks:
        print(f"{_GLYPH[c.status]} {c.name}: {c.detail}")
        if c.fix and c.status != OK:
            print(f"    fix: {c.fix}")
    print(f"\n{fails} issue(s), {warns} warning(s)")
    return 1 if fails else 0


def main(argv: list[str] | None = None) -> int:
    p = argparse.ArgumentParser(
        prog="marshal", description="Marshal - control plane for headless coding agents"
    )
    p.add_argument("-v", "--version", action="store_true")
    sub = p.add_subparsers(dest="cmd")
    pb = sub.add_parser("backends", help="list backends and availability")
    pb.add_argument("--json", action="store_true", help="output JSON")
    pu = sub.add_parser("usage", help="show usage summary")
    pu.add_argument("--dir", default=".marshal/usage")
    pu.add_argument("--json", action="store_true", help="output JSON")
    ps = sub.add_parser("status", help="list fleet runs")
    ps.add_argument("--state", default=".marshal/runs", help="per-run state directory")
    ps.add_argument("--json", action="store_true", help="output JSON")
    pd = sub.add_parser("doctor", help="preflight: check the setup is ready to run agents")
    pd.add_argument("--repo", default=None, help="target repo root (default: $MARSHAL_REPO or cwd)")
    pd.add_argument("--config", default=None, help="fleet config path (default: <repo>/fleet.config.yaml)")
    pd.add_argument("--json", action="store_true", help="output JSON")
    pw = sub.add_parser("workflows", help="list and validate workflow recipes")
    pw.add_argument("--repo", default=None, help="target repo root (default: $MARSHAL_REPO or cwd)")
    pw.add_argument("--config", default=None, help="fleet config path (default: <repo>/fleet.config.yaml)")
    pw.add_argument("--json", action="store_true", help="output JSON")
    pws = sub.add_parser("workspace", help="manage the workspace registry (~/.marshal/workspaces.yaml)")
    wsub = pws.add_subparsers(dest="ws_cmd")
    wadd = wsub.add_parser("add", help="register a repo as a workspace (path defaults to cwd)")
    wadd.add_argument("name", help="short name to register the repo under")
    wadd.add_argument("path", nargs="?", default=None, help="repo path (default: current directory)")
    wadd.add_argument("--no-scaffold", action="store_true", help="don't create a starter fleet.config.yaml")
    wadd.add_argument("--json", action="store_true", help="output JSON")
    wls = wsub.add_parser("list", help="list registered workspaces")
    wls.add_argument("--json", action="store_true", help="output JSON")
    wrm = wsub.add_parser("remove", help="remove a workspace from the registry")
    wrm.add_argument("name", help="workspace name to remove")
    sub.add_parser("mcp", help="run the MCP server over stdio")
    args = p.parse_args(argv)

    if args.version:
        print(f"marshal {__version__}")
        return 0
    if args.cmd == "backends":
        return _cmd_backends(args)
    if args.cmd == "usage":
        return _cmd_usage(args)
    if args.cmd == "status":
        return _cmd_status(args)
    if args.cmd == "doctor":
        return _cmd_doctor(args)
    if args.cmd == "workflows":
        return _cmd_workflows(args)
    if args.cmd == "workspace":
        return _cmd_workspace(args)
    if args.cmd == "mcp":
        try:
            from .mcp_server import main as serve

            serve()
        except ImportError:
            print(
                "marshal mcp needs the optional 'mcp' extra; install it with: uv sync --extra mcp",
                file=sys.stderr,
            )
            return 1
        return 0
    print(f"marshal {__version__}")
    p.print_help()
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
