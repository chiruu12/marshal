"""The `marshal` CLI - inspect backends, usage, and fleet state."""

from __future__ import annotations

import argparse
import sys

from ...core._version import __version__
from ...runtime.env import merge_user_path
from ...accounting.usage import USAGE_WINDOWS
from .admin import _cmd_clean, _cmd_doctor, _cmd_init, _cmd_workspace
from .common import _add_run_args, _positive_hours, _positive_int
from .inspect import (
    _cmd_backends,
    _cmd_logs,
    _cmd_models,
    _cmd_routing,
    _cmd_status,
    _cmd_usage,
)
from .recipes import _cmd_team_run, _cmd_teams, _cmd_workflow_run, _cmd_workflows
from ...core.types import RunOutcome
from .runs import _cmd_outcome, _cmd_run, _cmd_spawn

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
        choices=list(USAGE_WINDOWS),
        help=(
            "time window: session=since this CLI invocation (no long-lived Fleet; typically "
            "empty — use day/week/month for rolling spend), day=last 24h, week=7d, month=30d, "
            "all=everything (default). Same set as the MCP usage tool."
        ),
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
    ps.add_argument("--limit", type=_positive_int, default=50,
                    help="max runs, newest first (default 50)")
    ps.add_argument("--status", default=None, help="only runs with this status")
    ps.add_argument("--task-id", default=None, help="only runs with this task_id")
    ps.add_argument("--since-hours", type=_positive_hours, default=None,
                    help="only runs started within N hours")
    ps.add_argument(
        "--full", action="store_true",
        help="include the agent's final message and verify output (omitted by default: unbounded)",
    )
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
    pi = sub.add_parser("init", help="scaffold a starter fleet.config.yaml in the repo")
    pi.add_argument("--repo", default=None, help="target repo root (default: $MARSHAL_REPO or cwd)")
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
    pt = sub.add_parser("teams", help="list and validate adversarial review teams")
    pt.add_argument("--repo", default=None, help="target repo root (default: $MARSHAL_REPO or cwd)")
    pt.add_argument("--config", default=None, help="fleet config path (default: <repo>/fleet.config.yaml)")
    pt.add_argument("--json", action="store_true", help="output JSON")
    ptr = sub.add_parser("team", help="run an adversarial review team")
    trub = ptr.add_subparsers(dest="team_cmd", required=True)
    trun = trub.add_parser("run", help="run a review team against a subject")
    trun.add_argument("name", help="team name (stem of .yaml file)")
    trun.add_argument(
        "--target", required=True, choices=("run", "plan", "range", "audit"),
        help="what to review; must match the team's declared target",
    )
    trun.add_argument("--run-id", default=None, help="run whose diff to review (--target run)")
    trun.add_argument("--base", default=None, help="base ref (--target range)")
    trun.add_argument("--head", default=None, help="head ref (--target range; default HEAD)")
    trun.add_argument(
        "--path", action="append", default=None,
        help="limit a range diff to this path (repeatable); without it a large diff is truncated at the tail",
    )
    trun.add_argument("--text", default=None, help="the plan to review (--target plan)")
    trun.add_argument("--plan-file", default=None, help="read the plan from a file (--target plan)")
    trun.add_argument("--repo", default=None, help="target repo root (default: $MARSHAL_REPO or cwd)")
    trun.add_argument("--config", default=None, help="fleet config path (default: <repo>/fleet.config.yaml)")
    trun.add_argument("--max-concurrency", type=int, default=4, help="max concurrent reviewers (default: 4)")
    trun.add_argument("--json", action="store_true", help="output JSON")
    prun = sub.add_parser("run", help="run a task on a configured client (or ad-hoc by backend+model)")
    _add_run_args(prun)
    pspwn = sub.add_parser("spawn", help="start a task in the background and return its RUNNING record at once")
    _add_run_args(pspwn)
    prout = sub.add_parser("routing", help="which client's work actually got kept, per task kind")
    prout.add_argument("--task-kind", default=None, help="only this kind of work (e.g. refactor)")
    prout.add_argument(
        # `session` is deliberately absent: a one-shot CLI process has no session, so the window
        # would start at report time and could only ever return nothing. `marshal usage` still
        # accepts it (spend "so far" is at least a coherent question); a routing report built from
        # zero runs is not, so the flag refuses the question rather than answering it emptily.
        "--window", default="all", choices=[w for w in USAGE_WINDOWS if w != "session"],
        help="time window over the usage ledger (default: all - routing wants history)",
    )
    prout.add_argument("--repo", default=None, help="target repo root (default: $MARSHAL_REPO or cwd)")
    prout.add_argument("--json", action="store_true", help="output JSON")
    pout = sub.add_parser("outcome", help="record whether a finished run's work was any good")
    pout.add_argument("run_id", help="the run to judge")
    pout.add_argument(
        "outcome",
        choices=[o.value for o in RunOutcome],
        help="integrated (usually written by integrate) | rejected | abandoned",
    )
    pout.add_argument("--note", default=None, help="short reason, kept with the record")
    pout.add_argument("--repo", default=None, help="target repo root (default: $MARSHAL_REPO or cwd)")
    pout.add_argument("--state", default=None, help="run-records dir (default: <repo>/.marshal/runs)")
    pout.add_argument("--json", action="store_true", help="output JSON")
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
    if args.cmd == "init":
        return _cmd_init(args)
    if args.cmd == "doctor":
        return _cmd_doctor(args)
    if args.cmd == "workflows":
        return _cmd_workflows(args)
    if args.cmd == "workflow" and args.wf_cmd == "run":
        return _cmd_workflow_run(args)
    if args.cmd == "teams":
        return _cmd_teams(args)
    if args.cmd == "team" and args.team_cmd == "run":
        return _cmd_team_run(args)
    if args.cmd == "run":
        return _cmd_run(args)
    if args.cmd == "spawn":
        return _cmd_spawn(args)
    if args.cmd == "routing":
        return _cmd_routing(args)
    if args.cmd == "outcome":
        return _cmd_outcome(args)
    if args.cmd == "workspace":
        return _cmd_workspace(args)
    if args.cmd == "mcp":
        try:
            from ..mcp_server import main as serve

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
