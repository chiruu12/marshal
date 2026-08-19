"""The `marshal` CLI - inspect backends, usage, and fleet state."""

from __future__ import annotations

import argparse
import json
import os
import sys
from datetime import UTC, datetime, timedelta
from pathlib import Path

from ...accounting.usage import UsageTracker, usage_window_since
from ...core.config import BudgetSpec, ConfigError, load_config
from ...core.layout import logs_dir, runs_dir, usage_dir
from ...core.types import ModelSource
from ...orchestration.fleet import compute_budget_status
from ...orchestration.registry import backend_names, default_backends
from ...runtime.logs import RunLogStore
from ...runtime.state import FleetState, filter_runs, render_run
from ..routing import build_routing
from .common import _build_cli_service, _resolve_repo
from .formatting import (
    _format_bucket_cost,
    _format_bucket_rate,
    _format_cost_display,
    _format_cost_split,
    _print_bucket_table,
    _print_budget_table,
    _print_routing_table,
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
                    "permission_fidelity": c.permission_fidelity.value,
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
            f"json={str(c.json_output):5} usage={str(c.native_usage):5} "
            f"modes={modes} fidelity={c.permission_fidelity.value}"
        )
    return 0


def _cmd_models(args: argparse.Namespace) -> int:
    """Print the `models:` catalog, or - when there is none - what the backends' CLIs report.

    Goes through ``MarshalService.list_models`` rather than reading the config directly, so the
    CLI and the MCP tool answer from the same place. Reading the config here is what let this
    command say "no catalog, add one" on a repo where the MCP tool was returning a full probed
    list - the CLI reporting absence while the same repo had models to show.
    """
    try:
        listing = _build_cli_service(args).list_models()
    except (ConfigError, ValueError) as exc:
        # ValueError too: building a service resolves every configured client's backend, and an
        # unknown backend name raises from the registry. Reading the config directly never
        # touched the registry, so this command could not fail that way before - matches the
        # `run` / `spawn` handling rather than surfacing a traceback.
        print(f"error: {exc}", file=sys.stderr)
        return 1
    if args.json:
        payload = {
            "models": [m.model_dump() for m in listing.models],
            "backend_models": {
                name: cat.model_dump(mode="json") for name, cat in listing.backend_models.items()
            },
            "driver_context": listing.driver_context,
        }
        print(json.dumps(payload, indent=2))
        return 0
    for m in listing.models:
        backends = ",".join(m.backends)
        cost = m.cost or "-"
        quota = m.quota_type or "-"
        print(f"{m.id:40} backends={backends:30} cost={cost:12} quota={quota:14} {m.notes}")
    if not listing.models:
        repo = _resolve_repo(args)
        cfg_path = Path(
            args.config or os.environ.get("MARSHAL_CONFIG") or repo / "fleet.config.yaml"
        )
        print(f"no `models:` catalog in {cfg_path} (add a top-level `models:` list to curate one)")
        # Always print the source. A curated fallback and a live answer look identical once
        # they are both just a list of ids, and only one of them is evidence the account can
        # actually run those models.
        for name in sorted(listing.backend_models):
            catalog = listing.backend_models[name]
            if not catalog.models:
                print(f"\n{name}: nothing to report ({catalog.source.value})")
            else:
                print(f"\n{name} [{catalog.source.value}]: {', '.join(catalog.models)}")
        if any(c.source is ModelSource.STATIC for c in listing.backend_models.values()):
            print("\nstatic = a curated list, not the CLI's answer; it may name unavailable models")
    if listing.driver_context:
        print(f"\ndriver context: {listing.driver_context}")
    return 0


def _cmd_usage(args: argparse.Namespace) -> int:
    # The CLI has no long-lived Fleet, so `session` = since this invocation (typically empty).
    # Pass `now` as session_start — honest, not a silent fake of MCP's process-lifetime clock.
    now = datetime.now(UTC)
    since = usage_window_since(args.window, session_start=now, now=now)
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
    # Same honesty for budget status: a `session` budget window reads as $0 (process just started).
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
    cps_str = _format_bucket_rate(
        t.cost_per_succeeded or 0.0, t, no_successes=t.cost_per_succeeded is None
    )
    if args.window == "session":
        # CLI has no long-lived Fleet — say so plainly rather than silently returning ~$0.
        window_label = (
            "  window=session (since this CLI invocation — typically empty; "
            "no long-lived Fleet; use day/week/month for rolling spend)"
        )
    elif args.window != "all":
        window_label = f"  window={args.window}"
    else:
        window_label = ""
    print(
        f"runs={t.runs}  succeeded={t.succeeded}  cost={_format_bucket_cost(t)} "
        f"({_format_cost_split(t)})"
        f"{window_label}"
    )
    print(
        f"  $/run={_format_bucket_rate(t.cost_per_run, t)}  $/succeeded={cps_str}  "
        f"in={t.input_tokens}  out={t.output_tokens}  "
        f"cache_read={t.cache_read_tokens}  cache_write={t.cache_write_tokens}"
    )
    _print_bucket_table("by_backend", s.by_backend)
    _print_bucket_table("by_client", s.by_client)
    _print_bucket_table("by_model", s.by_model)
    _print_bucket_table("by_backend_model", s.by_backend_model)
    _print_budget_table(budget_rows)
    return 0


def _cmd_status(args: argparse.Namespace) -> int:
    repo = _resolve_repo(args)
    state_dir = Path(args.state) if args.state is not None else runs_dir(repo)
    since = (
        datetime.now(UTC) - timedelta(hours=args.since_hours)
        if args.since_hours is not None else None
    )
    matched = filter_runs(
        FleetState(state_dir).list(), status=args.status, task_id=args.task_id, since=since
    )
    runs = matched if args.limit is None else matched[: args.limit]
    if args.json:
        # Same views and same default as the MCP tool, and the same refusal to cap silently.
        print(json.dumps({
            "runs": [render_run(r, args.view) for r in runs],
            "returned": len(runs),
            "matched": len(matched),
            "truncated": len(matched) > len(runs),
            "view": args.view,
        }, indent=2))
        return 0
    if len(matched) > len(runs):
        print(f"showing {len(runs)} of {len(matched)} runs (raise --limit to see more)")
    if not runs:
        print(f"no runs recorded under {state_dir.resolve()}")
        return 0
    cost_w = max((len(_format_cost_display(r.cost_usd, r.source)) for r in runs), default=11)
    for r in runs:
        cost = _format_cost_display(r.cost_usd, r.source)
        print(f"{r.run_id:24} {r.backend:12} {r.status:10} {cost:<{cost_w}}  {r.worktree or ''}")
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


def _cmd_routing(args: argparse.Namespace) -> int:
    """Which client's work actually got kept, per kind of task.

    Config-free like `marshal status`: this reads history, and history does not depend on which
    clients happen to be configured today.
    """
    repo = _resolve_repo(args)
    ledger = build_routing(
        UsageTracker(usage_dir(repo)),
        FleetState(runs_dir(repo)),
        window=args.window,
        task_kind=args.task_kind,
    )
    if args.json:
        payload = ledger.model_dump(mode="json")
        payload["window"] = args.window
        print(json.dumps(payload, indent=2))
        return 0
    if not ledger.cells:
        # `session` is not an accepted choice here (see parser.py), so an empty ledger means an
        # empty ledger - it cannot be an artefact of a window that starts at report time.
        print("no runs recorded yet")
        return 0
    _print_routing_table(ledger)
    if ledger.caveat:
        # Not an error: there IS history, nobody has judged it. Saying "no data" would be wrong.
        print(f"\nnote: {ledger.caveat}")
    elif ledger.recommended:
        print(f"\nbest for {ledger.recommended_task_kind!r}: {ledger.recommended}")
    elif ledger.recommended_by_task_kind:
        # Several kinds in view: one line each. There is no single winner across kinds.
        print()
        for kind, client in ledger.recommended_by_task_kind.items():
            print(f"best for {kind!r}: {client}")
    return 0
