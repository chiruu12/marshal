"""run_many with per-job `then`: each review starts when THAT job finishes.

Sibling primaries do not gate each other's follow-ups. A job that finishes first starts its
reviewer immediately; a slower sibling does not hold it back.

then_skipped is set (and `then` left unset) when the follow-up is not run: the primary failed, has
no branch, produced no commits beyond its base, or commit_run could not freeze the work. Reviewing
an empty tree yields a confident "no issues" that means nothing — so Marshal skips rather than
spawn a vacuous reviewer.

Prerequisites (see examples/README.md):
  * uv sync --extra mcp --extra dev
  * a fleet.config.yaml with implementer + reviewer clients (copy fleet.config.example.yaml)
  * those backends installed AND authenticated (run: uv run marshal doctor)

Run from the repo root:  uv run python examples/pipelined_review.py
"""

from pathlib import Path

from marshal_engine.core.config import load_config
from marshal_engine.interfaces.service import MarshalService

IMPLEMENTER = "implementer"
REVIEWER = "reviewer"



def _require_clients(config_path: Path, *names: str) -> None:
    """Fail with guidance, not a stack trace, when the config uses different client names.

    These names come from fleet.config.example.yaml, which the prerequisites tell you to copy -
    but client names are the user's own vocabulary, so a mismatch is expected and should say what
    to do about it.
    """
    declared = load_config(config_path).clients
    missing = [n for n in names if n not in declared]
    if missing:
        have = ", ".join(sorted(declared)) or "(none)"
        raise SystemExit(
            f"this example expects client(s) {', '.join(missing)} in {config_path}; "
            f"declared: {have}. Rename the constants at the top of this file, or copy the "
            f"clients from fleet.config.example.yaml."
        )

def main() -> None:
    _require_clients(Path("fleet.config.yaml"), IMPLEMENTER, REVIEWER)
    service = MarshalService(Path("."), load_config("fleet.config.yaml"))

    results = service.run_many(
        [
            {
                "client": IMPLEMENTER,
                "goal": (
                    "Add a one-line module comment at the top of src/marshal_engine/retry.py "
                    "if it lacks one. Touch only that file."
                ),
                "then": {
                    "client": REVIEWER,
                    "goal": (
                        "Review the implementer's diff for correctness bugs only. "
                        "Write findings; do not edit files."
                    ),
                },
            },
            {
                "client": IMPLEMENTER,
                "goal": (
                    "Add a one-line module comment at the top of src/marshal_engine/logs.py "
                    "if it lacks one. Touch only that file."
                ),
                "then": {
                    "client": REVIEWER,
                    "goal": (
                        "Review the implementer's diff for correctness bugs only. "
                        "Write findings; do not edit files."
                    ),
                },
            },
        ],
        max_concurrency=2,
    )

    # One RunManyJobResult per input job, in input order.
    for i, job in enumerate(results):
        primary = job.primary
        then = job.then
        print(f"job[{i}] primary.run_id={primary.run_id}  primary.status={primary.status}")
        print(f"       then={then.status if then else None}  then_skipped={job.then_skipped!r}")
        if then is not None:
            print(f"       then.run_id={then.run_id}  then.worktree={then.worktree}")


if __name__ == "__main__":
    main()
