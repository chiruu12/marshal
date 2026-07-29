"""One registry, several repos: list_workspaces then mixed-workspace run_many.

Each workspace keeps its own fleet.config.yaml, worktrees, and usage ledger. Only the concurrency
cap (max_concurrency on this call, plus the process-wide run_gate when set) is shared.

Production / MCP: WorkspaceRegistry.from_env() reads ~/.marshal/workspaces.yaml — the same
payload list_workspaces returns. This script also builds two throwaway repos so the isolation
property is visible without a second real checkout.

Prerequisites (see examples/README.md):
  * uv sync --extra mcp --extra dev
  * a fleet.config.yaml with at least one client (copy fleet.config.example.yaml)
  * that client's backend CLI installed AND authenticated (run: uv run marshal doctor)

Run from the repo root:  uv run python examples/multi_workspace.py
"""

import shutil
import subprocess
import tempfile
from pathlib import Path

from marshal_engine.config import load_config
from marshal_engine.state import FleetState
from marshal_engine.workspaces import WorkspaceDef, WorkspaceRegistry

CLIENT = "implementer"


def _mini_repo(root: Path, config_src: Path) -> Path:
    root.mkdir(parents=True)
    subprocess.run(["git", "init", "-q", "-b", "main"], cwd=root, check=True)
    subprocess.run(["git", "config", "user.email", "example@marshal.local"], cwd=root, check=True)
    subprocess.run(["git", "config", "user.name", "marshal-example"], cwd=root, check=True)
    (root / "README.md").write_text(f"# {root.name}\n", encoding="utf-8")
    shutil.copy2(config_src, root / "fleet.config.yaml")
    subprocess.run(["git", "add", "-A"], cwd=root, check=True)
    subprocess.run(["git", "commit", "-q", "-m", "init"], cwd=root, check=True)
    return root.resolve()


def main() -> None:
    config_src = Path("fleet.config.yaml")
    if not config_src.exists():
        raise SystemExit("fleet.config.yaml missing; copy fleet.config.example.yaml first")

    # Same entry point the MCP server uses for list_workspaces.
    live = WorkspaceRegistry.from_env()
    print("from_env workspaces:")
    for row in live.describe():
        print(
            f"  {row['name']}: path={row['path']}  ready={row['ready']}  "
            f"clients={row['client_count']}  reason={row.get('ready_reason')}"
        )

    with tempfile.TemporaryDirectory(prefix="marshal-multi-ws-") as tmp:
        base = Path(tmp)
        default_repo = _mini_repo(base / "default", config_src)
        other_repo = _mini_repo(base / "other", config_src)
        # Confirm the config still loads (catches a typo before any agent starts).
        load_config(default_repo / "fleet.config.yaml")

        reg = WorkspaceRegistry(
            [
                WorkspaceDef("default", default_repo, default_repo / "fleet.config.yaml"),
                WorkspaceDef("other", other_repo, other_repo / "fleet.config.yaml"),
            ]
        )
        print("demo registry:")
        for row in reg.describe():
            print(f"  {row['name']}: path={row['path']}  ready={row['ready']}")

        paired = reg.run_many(
            [
                {
                    "client": CLIENT,
                    "workspace": "default",
                    "goal": "Add a one-line comment to README.md saying workspace=default.",
                },
                {
                    "client": CLIENT,
                    "workspace": "other",
                    "goal": "Add a one-line comment to README.md saying workspace=other.",
                },
            ],
            max_concurrency=2,
        )

        for ws, result in paired:
            primary = result.primary
            print(
                f"{ws}: run_id={primary.run_id}  status={primary.status}  "
                f"worktree={primary.worktree}"
            )

        # Ledgers stay per-workspace — no shared run state across repos.
        default_ids = {r.run_id for r in FleetState(default_repo / ".marshal" / "runs").list()}
        other_ids = {r.run_id for r in FleetState(other_repo / ".marshal" / "runs").list()}
        print("default ledger:", sorted(default_ids))
        print("other ledger:", sorted(other_ids))
        print("shared concurrency cap: max_concurrency=2 on this run_many call")


if __name__ == "__main__":
    main()
