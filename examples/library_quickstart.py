"""Minimal no-driver Marshal run: run one task, review the diff, integrate it.

Prerequisites (see examples/README.md):
  * uv sync --extra mcp --extra dev
  * a fleet.config.yaml with at least one client (copy fleet.config.example.yaml)
  * that client's backend CLI installed AND authenticated (run: uv run marshal doctor)

Run from the repo root:  uv run python examples/library_quickstart.py
"""

from pathlib import Path

from marshal_engine.config import load_config
from marshal_engine.service import MarshalService

# `implementer` is the OpenCode client in fleet.config.example.yaml. Change it to a client
# name from your own fleet.config.yaml.
CLIENT = "implementer"


def main() -> None:
    service = MarshalService(Path("."), load_config("fleet.config.yaml"))

    # Runs the agent in its own isolated git worktree under .marshal/worktrees/.
    # Your main branch is untouched.
    record = service.run_agent(CLIENT, "Add a one-line docstring to a function that lacks one")
    print(f"status={record.status}  cost_usd={record.cost_usd}  source={record.source}")
    print(f"worktree={record.worktree}")

    # Read-only: inspect the diff before merging anything.
    collected = service.collect_run(record.run_id)
    print("changed files:", collected.changed_files)

    # Explicit merge into your current branch, then clean up the worktree.
    result = service.integrate(record.run_id, cleanup=True)
    print("integrate:", result.status)


if __name__ == "__main__":
    main()
