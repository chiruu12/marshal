"""The `marshal` CLI — inspect backends, usage, and fleet state."""

from __future__ import annotations

import argparse
import os
import sys
from pathlib import Path

from . import __version__
from .doctor import FAIL, OK, WARN, run_checks, summarize
from .registry import backend_names, default_backends
from .state import FleetState
from .usage import UsageTracker


def _cmd_backends(args: argparse.Namespace) -> int:
    backends = default_backends()
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
    if not runs:
        print(f"no runs recorded under {Path(args.state).resolve()}")
        return 0
    for r in runs:
        print(f"{r.run_id:24} {r.backend:12} {r.status:10} ${r.cost_usd:.4f}  {r.worktree or ''}")
    return 0


_GLYPH = {OK: "✓", WARN: "⚠", FAIL: "✗"}


def _cmd_doctor(args: argparse.Namespace) -> int:
    repo = Path(args.repo or os.environ.get("MARSHAL_REPO", ".")).resolve()
    cfg_path = Path(args.config or os.environ.get("MARSHAL_CONFIG") or repo / "fleet.config.yaml")
    checks = run_checks(repo, cfg_path)
    for c in checks:
        print(f"{_GLYPH[c.status]} {c.name}: {c.detail}")
        if c.fix and c.status != OK:
            print(f"    fix: {c.fix}")
    fails, warns = summarize(checks)
    print(f"\n{fails} issue(s), {warns} warning(s)")
    return 1 if fails else 0


def main(argv: list[str] | None = None) -> int:
    p = argparse.ArgumentParser(
        prog="marshal", description="Marshal — control plane for headless coding agents"
    )
    p.add_argument("-v", "--version", action="store_true")
    sub = p.add_subparsers(dest="cmd")
    sub.add_parser("backends", help="list backends and availability")
    pu = sub.add_parser("usage", help="show usage summary")
    pu.add_argument("--dir", default=".marshal/usage")
    ps = sub.add_parser("status", help="list fleet runs")
    ps.add_argument("--state", default=".marshal/runs", help="per-run state directory")
    pd = sub.add_parser("doctor", help="preflight: check the setup is ready to run agents")
    pd.add_argument("--repo", default=None, help="target repo root (default: $MARSHAL_REPO or cwd)")
    pd.add_argument("--config", default=None, help="fleet config path (default: <repo>/fleet.config.yaml)")
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
