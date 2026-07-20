"""The `marshal` CLI - inspect backends, usage, and fleet state."""

from __future__ import annotations

import argparse
import json
import os
import sys
from datetime import datetime, timedelta, timezone
from pathlib import Path
from typing import Any, Sequence

from . import __version__
from .config import BudgetSpec, ConfigError, DURATION_PRESETS, FleetConfig, load_config
from .doctor import FAIL, OK, WARN, doctor_report, run_checks
from .env import merge_user_path
from .fleet import BudgetStatus, Fleet, compute_budget_status
from .layout import logs_dir, marshal_dir, runs_dir, usage_dir
from .logs import RunLogStore
from .registry import backend_names, default_backends
from .service import MarshalService
from .state import FleetState
from .scaffold import scaffold_fleet_config
from .usage import Bucket, UsageTracker
from .workflow import WorkflowRunner, load_workflow, validate_workflow, workflow_paths
from .workspaces import (
    DEFAULT_WORKSPACE,
    WorkspaceDef,
    WorkspaceRegistry,
    build_service_for,
    register_workspace,
    remove_workspace,
    workspaces_file_path,
)


def _resolve_repo(args: argparse.Namespace) -> Path:
    return Path(args.repo or os.environ.get("MARSHAL_REPO", ".")).resolve()


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


def _cmd_models(args: argparse.Namespace) -> int:
    """Print the optional `models:` catalog from the fleet config (the driver's "sheet")."""
    repo = Path(args.repo or os.environ.get("MARSHAL_REPO", ".")).resolve()
    cfg_path = Path(args.config or os.environ.get("MARSHAL_CONFIG") or repo / "fleet.config.yaml")
    config = FleetConfig()  # empty default - same posture as a repo with no config file
    if cfg_path.exists():
        try:
            config = load_config(cfg_path)
        except ConfigError as exc:
            print(f"error: {exc}", file=sys.stderr)
            return 1
    if args.json:
        payload = {
            "models": [m.model_dump() for m in config.models],
            "driver_context": config.context.driver,
        }
        print(json.dumps(payload, indent=2))
        return 0
    if not config.models:
        print(
            f"no `models:` catalog in {cfg_path} (add a top-level `models:` list to expose one)"
        )
        return 0
    for m in config.models:
        backends = ",".join(m.backends)
        cost = m.cost or "-"
        quota = m.quota_type or "-"
        print(f"{m.id:40} backends={backends:30} cost={cost:12} quota={quota:14} {m.notes}")
    if config.context.driver:
        print(f"\ndriver context: {config.context.driver}")
    return 0


def _align_rows(header: Sequence[str], rows: Sequence[Sequence[Any]]) -> list[str]:
    """Render a header + rows of strings as column-aligned lines (no border).

    Each column is sized to the widest cell; values are stringified and left-aligned. Keeps the
    fixed-width f-string convention used in `_cmd_status` / `_cmd_backends` so the existing
    stdlib-only, no-deps posture holds.
    """
    if not header:
        return []
    widths = [len(str(h)) for h in header]
    for row in rows:
        for i, cell in enumerate(row):
            if i < len(widths):
                widths[i] = max(widths[i], len(str(cell)))
    out = ["  ".join(str(h).ljust(widths[i]) for i, h in enumerate(header))]
    for row in rows:
        # Clamp: a row with more cells than the header renders the extras unpadded rather than
        # raising IndexError (this is a general-purpose helper, so guard against future misuse).
        out.append(
            "  ".join(
                str(c).ljust(widths[i]) if i < len(widths) else str(c)
                for i, c in enumerate(row)
            )
        )
    return out


def _format_cost(b: Bucket) -> str:
    return f"${b.cost_usd:.4f}"


def _format_cost_split(b: Bucket) -> str:
    """Compact native/admin-api/est split; zeros collapsed so the row stays readable."""
    parts: list[str] = []
    for label, val in (
        ("native", b.cost_native),
        ("admin-api", b.cost_admin_api),
        ("est", b.cost_estimated),
    ):
        if val > 0:
            parts.append(f"{label} ${val:.4f}")
    return " ".join(parts) if parts else "-"


def _print_bucket_table(title: str, buckets: dict[str, Bucket]) -> None:
    if not buckets:
        return
    print(f"\n{title}")
    header = (
        "name", "runs", "succeeded", "cost_usd", "cost split",
        "input_tokens", "output_tokens", "cache_read_tokens",
    )
    rows = [
        (
            name,
            str(v.runs),
            str(v.succeeded),
            _format_cost(v),
            _format_cost_split(v),
            str(v.input_tokens),
            str(v.output_tokens),
            str(v.cache_read_tokens),
        )
        for name, v in sorted(buckets.items())
    ]
    for line in _align_rows(header, rows):
        print(f"  {line}")


def _cmd_usage(args: argparse.Namespace) -> int:
    since = _usage_window_since(args.window)
    repo = _resolve_repo(args)
    usage_path = Path(args.dir) if args.dir is not None else usage_dir(repo)
    tracker = UsageTracker(usage_path)
    s = tracker.summary(since=since)
    # Optional: load the fleet config to surface any advisory `budgets:` alongside the ledger.
    # Absent / unreadable / empty -> `[]` (the "no behavior change" contract for users who don't opt in).
    budgets: list[BudgetSpec] = []
    if args.config:
        try:
            budgets = load_config(args.config).budgets
        except ConfigError as exc:
            print(f"error: {exc}", file=sys.stderr)
            return 1
    # The CLI has no long-lived Fleet, so a `session` budget window reads as $0 (the process
    # just started). Pass `now` as both ends - honest "since this CLI invocation".
    now = datetime.now(timezone.utc)
    budget_rows = compute_budget_status(tracker, now, budgets, now)
    if args.json:
        payload = {
            "totals": s.totals.model_dump(mode="json"),
            "by_backend": {k: v.model_dump(mode="json") for k, v in s.by_backend.items()},
            "by_client": {k: v.model_dump(mode="json") for k, v in s.by_client.items()},
            "by_model": {k: v.model_dump(mode="json") for k, v in s.by_model.items()},
            "by_backend_model": {
                k: v.model_dump(mode="json") for k, v in s.by_backend_model.items()
            },
            "window": args.window,
            "since": since.isoformat() if since is not None else None,
        }
        if budget_rows:
            payload["budgets"] = [b.model_dump(mode="json") for b in budget_rows]
        print(json.dumps(payload, indent=2))
        return 0
    t = s.totals
    cps_str = f"${t.cost_per_succeeded:.4f}" if t.cost_per_succeeded is not None else "n/a"
    window_label = f"  window={args.window}" if args.window != "all" else ""
    print(
        f"runs={t.runs}  succeeded={t.succeeded}  cost=${t.cost_usd:.4f} "
        f"(native ${t.cost_native:.4f} / admin-api ${t.cost_admin_api:.4f} / est ${t.cost_estimated:.4f})"
        f"{window_label}"
    )
    print(
        f"  $/run=${t.cost_per_run:.4f}  $/succeeded={cps_str}  "
        f"in={t.input_tokens}  out={t.output_tokens}  cache_read={t.cache_read_tokens}"
    )
    _print_bucket_table("by_backend", s.by_backend)
    _print_bucket_table("by_client", s.by_client)
    _print_bucket_table("by_model", s.by_model)
    _print_bucket_table("by_backend_model", s.by_backend_model)
    _print_budget_table(budget_rows)
    return 0


def _print_budget_table(rows: Sequence[BudgetStatus]) -> None:
    """Print the configured budgets as an aligned table. No-op when no budgets.

    A subscription / unknown-cost backend reports $0, so a $ budget on it shows `$0.0000` spent
    (not a fake percentage); the table just makes that explicit.
    """
    if not rows:
        return
    print("\nbudgets")
    header = ("scope", "window", "spent", "limit", "remaining", "mode")
    table_rows = [
        (
            r.scope,
            r.window,
            f"${r.spent_usd:.4f}",
            f"${r.limit_usd:.4f}",
            f"${r.remaining_usd:.4f}",
            "enforce" if r.enforce else "soft-warn",
        )
        for r in rows
    ]
    for line in _align_rows(header, table_rows):
        print(f"  {line}")


def _usage_window_since(window: str) -> datetime | None:
    """Map the CLI `--window` name to a UTC `since` (None for "all"). The CLI has no server
    reference, so it uses rolling windows; "day" (last 24h) is the session-equivalent."""
    if window == "all":
        return None
    now = datetime.now(timezone.utc)
    if window == "day":
        return now - timedelta(hours=24)
    if window == "week":
        return now - timedelta(days=7)
    if window == "month":
        return now - timedelta(days=30)
    raise ValueError(f"unknown usage window: {window!r} (use day|week|month|all)")


def _cmd_status(args: argparse.Namespace) -> int:
    repo = _resolve_repo(args)
    state_dir = Path(args.state) if args.state is not None else runs_dir(repo)
    runs = FleetState(state_dir).list()
    if args.json:
        print(json.dumps([r.model_dump(mode="json") for r in runs], indent=2))
        return 0
    if not runs:
        print(f"no runs recorded under {state_dir.resolve()}")
        return 0
    for r in runs:
        print(f"{r.run_id:24} {r.backend:12} {r.status:10} ${r.cost_usd:.4f}  {r.worktree or ''}")
    return 0


def _cmd_logs(args: argparse.Namespace) -> int:
    """Print the persisted stdout/stderr for one run. Non-zero when no log exists for the id."""
    repo = _resolve_repo(args)
    log_dir = Path(args.dir) if args.dir is not None else logs_dir(repo)
    try:
        text = RunLogStore(log_dir).read(args.run_id)
    except ValueError as exc:  # unsafe run_id (path separators) - fail clean, not a traceback
        print(f"error: {exc}", file=sys.stderr)
        return 1
    if text is None:
        print(f"no log for run {args.run_id!r} under {log_dir.resolve()}", file=sys.stderr)
        return 1
    sys.stdout.write(text)
    if not text.endswith("\n"):
        sys.stdout.write("\n")
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


def _cmd_workflow_run(args: argparse.Namespace) -> int:
    """Run a workflow recipe from examples/workflows/ or custom workflows."""
    repo = Path(args.repo or os.environ.get("MARSHAL_REPO", ".")).resolve()
    cfg_path = Path(args.config or os.environ.get("MARSHAL_CONFIG") or repo / "fleet.config.yaml")
    config = load_config(cfg_path) if cfg_path.exists() else FleetConfig()

    # Find the workflow file by name (search examples/workflows/ and workflows/)
    wdir = repo / "workflows"
    spec = None
    for p in workflow_paths(wdir):
        if p.stem == args.name:
            spec = load_workflow(p)
            break
    if spec is None:
        print(f"error: workflow {args.name!r} not found in {wdir}", file=sys.stderr)
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


def _cmd_memory(args: argparse.Namespace) -> int:
    svc = _build_cli_service(args)
    cfg = svc.config.memory

    if args.mem_cmd == "query":
        if not cfg.enabled or not cfg.recall_enabled:
            print("memory is disabled; set memory.enabled (and recall_enabled) in fleet.config.yaml")
            return 0
        result = svc.memory_query(args.text)
        print(result if result else "(no relevant memory)")
        return 0

    if args.mem_cmd == "add":
        tags = [t.strip() for t in args.tags.split(",") if t.strip()] if args.tags else None
        print(svc.memory_remember(args.text, tags))
        return 0

    if args.mem_cmd == "stats":
        stats = svc.memory_stats()
        if args.json:
            print(json.dumps(stats, indent=2))
            return 0
        print(f"enabled={stats['enabled']}")
        print(f"recall_enabled={stats['recall_enabled']}")
        print(f"remember_enabled={stats['remember_enabled']}")
        print(f"data_dir={stats['data_dir']}")
        print(f"repo_key={stats['repo_key']}")
        print(f"recall_top_k={stats['recall_top_k']}")
        print(f"recall_max_chars={stats['recall_max_chars']}")
        print(f"cognee_installed={stats['cognee_installed']}")
        return 0

    if args.mem_cmd == "improve":
        if not cfg.enabled:
            print("memory is disabled; set memory.enabled in fleet.config.yaml")
            return 0
        svc.memory_improve()
        print(f"improved memory dataset {svc._repo_key!r}")
        return 0

    if args.mem_cmd == "forget":
        if not cfg.enabled:
            print("memory is disabled; set memory.enabled in fleet.config.yaml")
            return 0
        if args.all:
            svc.memory_forget(all=True)
            print("forgot all memory datasets")
        else:
            svc.memory_forget()
            print(f"forgot memory dataset {svc._repo_key!r}")
        return 0

    return 1


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


def _build_cli_service(args: argparse.Namespace) -> MarshalService:
    """Build a MarshalService for `run`/`spawn` from CLI args (mirrors mcp_server.build_service).

    A repo with no fleet.config.yaml still builds, with zero clients, so `marshal run --backend ...
    --model ...` works ad-hoc without ever editing a config file.
    """
    repo = Path(args.repo or os.environ.get("MARSHAL_REPO", ".")).resolve()
    cfg_path = Path(args.config or os.environ.get("MARSHAL_CONFIG") or repo / "fleet.config.yaml")
    return build_service_for(
        WorkspaceDef(name=DEFAULT_WORKSPACE, path=repo, config_path=cfg_path),
        missing_config="silent",
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
    parser.add_argument("--repo", default=None, help="target repo root (default: $MARSHAL_REPO or cwd)")
    parser.add_argument("--config", default=None, help="fleet config path (default: <repo>/fleet.config.yaml)")
    parser.add_argument("--json", action="store_true", help="output JSON")


def _cmd_run_like(args: argparse.Namespace, *, spawn: bool) -> int:
    """Shared body for `run` (blocking) and `spawn` (background)."""
    try:
        svc = _build_cli_service(args)
    except ConfigError as exc:
        print(f"error: {exc}", file=sys.stderr)
        return 1
    run_kwargs = {
        "task_id": args.task_id,
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
    except (ValueError, ConfigError) as exc:
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


def main(argv: list[str] | None = None) -> int:
    # Recover the user's interactive PATH if the CLI was launched from a context that didn't
    # source their rc files (a non-interactive SSH session, a launchd job). For the normal case of
    # `marshal ...` from a terminal this is a no-op: PATH is already complete and the cache keeps
    # `user_path()` from re-spawning a shell. No-op entirely when MARSHAL_NO_PATH_FIX=1.
    merge_user_path()
    p = argparse.ArgumentParser(
        prog="marshal", description="Marshal - control plane for headless coding agents"
    )
    p.add_argument("-v", "--version", action="store_true")
    sub = p.add_subparsers(dest="cmd")
    pb = sub.add_parser("backends", help="list backends and availability")
    pb.add_argument("--json", action="store_true", help="output JSON")
    pm = sub.add_parser("models", help="list the optional `models:` catalog from fleet.config.yaml")
    pm.add_argument("--repo", default=None, help="target repo root (default: $MARSHAL_REPO or cwd)")
    pm.add_argument("--config", default=None, help="fleet config path (default: <repo>/fleet.config.yaml)")
    pm.add_argument("--json", action="store_true", help="output JSON")
    pu = sub.add_parser("usage", help="show usage summary")
    pu.add_argument("--repo", default=None, help="target repo root (default: $MARSHAL_REPO or cwd)")
    pu.add_argument("--dir", default=None, help="usage ledger directory (default: <repo>/.marshal/usage)")
    pu.add_argument(
        "--window",
        default="all",
        choices=["day", "week", "month", "all"],
        help="rolling time window: day=last 24h, week=7d, month=30d, all=everything (default)",
    )
    pu.add_argument(
        "--config", default=None,
        help="optional fleet config path; when set, also surfaces any configured advisory `budgets:`",
    )
    pu.add_argument("--json", action="store_true", help="output JSON")
    ps = sub.add_parser("status", help="list fleet runs")
    ps.add_argument("--repo", default=None, help="target repo root (default: $MARSHAL_REPO or cwd)")
    ps.add_argument("--state", default=None, help="per-run state directory (default: <repo>/.marshal/runs)")
    ps.add_argument("--json", action="store_true", help="output JSON")
    pl = sub.add_parser("logs", help="print the persisted stdout/stderr for one run")
    pl.add_argument("run_id", help="the run id to fetch the log for")
    pl.add_argument("--repo", default=None, help="target repo root (default: $MARSHAL_REPO or cwd)")
    pl.add_argument("--dir", default=None, help="per-run logs directory (default: <repo>/.marshal/logs)")
    pc = sub.add_parser("clean", help="tear down finished runs' worktrees + branches")
    pc.add_argument("run_ids", nargs="*", help="specific run ids to clean (default: by --scope)")
    pc.add_argument("--repo", default=None, help="target repo root (default: $MARSHAL_REPO or cwd)")
    pc.add_argument(
        "--scope",
        default="finished",
        choices=["merged", "finished", "all"],
        help="merged (integrated only) | finished (default; keeps un-integrated succeeded) | all",
    )
    pc.add_argument("--older-than", type=float, default=None, metavar="HOURS",
                    help="only clean runs that ended at least this many hours ago")
    pc.add_argument("--dry-run", action="store_true", help="preview without removing anything")
    pc.add_argument("--json", action="store_true", help="output JSON")
    pd = sub.add_parser("doctor", help="preflight: check the setup is ready to run agents")
    pd.add_argument("--repo", default=None, help="target repo root (default: $MARSHAL_REPO or cwd)")
    pd.add_argument("--config", default=None, help="fleet config path (default: <repo>/fleet.config.yaml)")
    pd.add_argument("--json", action="store_true", help="output JSON")
    pw = sub.add_parser("workflows", help="list and validate workflow recipes")
    pw.add_argument("--repo", default=None, help="target repo root (default: $MARSHAL_REPO or cwd)")
    pw.add_argument("--config", default=None, help="fleet config path (default: <repo>/fleet.config.yaml)")
    pw.add_argument("--json", action="store_true", help="output JSON")
    pwr = sub.add_parser("workflow", help="run a workflow recipe")
    wrub = pwr.add_subparsers(dest="wf_cmd", required=True)
    wrun = wrub.add_parser("run", help="execute a workflow by name")
    wrun.add_argument("name", help="workflow name (stem of .yaml file)")
    wrun.add_argument("--input", action="append", default=None, help="workflow input: key=value (repeatable)")
    wrun.add_argument("--repo", default=None, help="target repo root (default: $MARSHAL_REPO or cwd)")
    wrun.add_argument("--config", default=None, help="fleet config path (default: <repo>/fleet.config.yaml)")
    wrun.add_argument("--max-concurrency", type=int, default=4, help="max concurrent agents (default: 4)")
    wrun.add_argument("--json", action="store_true", help="output JSON")
    prun = sub.add_parser("run", help="run a task on a configured client (or ad-hoc by backend+model)")
    _add_run_args(prun)
    pspwn = sub.add_parser("spawn", help="start a task in the background and return its RUNNING record at once")
    _add_run_args(pspwn)
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
    pm = sub.add_parser("memory", help="Marshal Recall memory layer")
    mem_common = argparse.ArgumentParser(add_help=False)
    mem_common.add_argument("--repo", default=None, help="target repo root (default: $MARSHAL_REPO or cwd)")
    mem_common.add_argument("--config", default=None, help="fleet config path (default: <repo>/fleet.config.yaml)")
    msub = pm.add_subparsers(dest="mem_cmd", required=True)
    pmq = msub.add_parser("query", parents=[mem_common], help="recall memory for a query")
    pmq.add_argument("text", help="natural-language query")
    pma = msub.add_parser("add", parents=[mem_common], help="store a freeform note in memory")
    pma.add_argument("text", help="note text to remember")
    pma.add_argument("--tags", default=None, help="comma-separated tags to attach to the note")
    msub.add_parser("improve", parents=[mem_common], help="run memify on this repo's memory dataset")
    pmf = msub.add_parser("forget", parents=[mem_common], help="forget memory for this repo (or --all)")
    pmf.add_argument("--all", action="store_true", help="wipe all memory datasets")
    pms = msub.add_parser("stats", parents=[mem_common], help="show memory configuration and data paths")
    pms.add_argument("--json", action="store_true", help="output JSON")
    sub.add_parser("mcp", help="run the MCP server over stdio")
    args = p.parse_args(argv)

    if args.version:
        print(f"marshal {__version__}")
        return 0
    if args.cmd == "backends":
        return _cmd_backends(args)
    if args.cmd == "models":
        return _cmd_models(args)
    if args.cmd == "usage":
        return _cmd_usage(args)
    if args.cmd == "status":
        return _cmd_status(args)
    if args.cmd == "logs":
        return _cmd_logs(args)
    if args.cmd == "clean":
        return _cmd_clean(args)
    if args.cmd == "doctor":
        return _cmd_doctor(args)
    if args.cmd == "workflows":
        return _cmd_workflows(args)
    if args.cmd == "workflow" and args.wf_cmd == "run":
        return _cmd_workflow_run(args)
    if args.cmd == "run":
        return _cmd_run(args)
    if args.cmd == "spawn":
        return _cmd_spawn(args)
    if args.cmd == "workspace":
        return _cmd_workspace(args)
    if args.cmd == "memory":
        return _cmd_memory(args)
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
