"""Run an adversarial review team over a candidate run's diff.

The engine returns reports only — it computes no verdict, score, or pass/fail. Collecting the
objections and deciding whether to integrate is the caller's job.

Prerequisites (see examples/README.md):
  * uv sync --extra mcp --extra dev
  * a fleet.config.yaml with implementer + a permission: read-only reviewer
    (copy fleet.config.example.yaml; names below match that file)
  * those backends installed AND authenticated (run: uv run marshal doctor)

Run from the repo root:  uv run python examples/adversarial_review.py
"""

from pathlib import Path

from marshal_engine.config import load_config
from marshal_engine.service import MarshalService
from marshal_engine.teams import TeamSubject

IMPLEMENTER = "implementer"
REVIEWER = "reviewer"
TEAM = "example-run-review"

# One-lens panel so the example runs against fleet.config.example.yaml's `reviewer`.
# For multi-lens gates see examples/teams/hard-gate.yaml (copy into <repo>/teams/).
_TEAM_YAML = f"""\
description: One-lens review of a run diff; you read the report and decide.
target: run
roles:
  - name: correctness
    client: {REVIEWER}
    rubric: |
      Hunt for defects in the changed code only.
      Describe each defect as a concrete failure case (inputs and wrong result).
      Do not edit files. Do not approve or reject — report findings only.
"""



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

def _ensure_team() -> None:
    path = Path("teams") / f"{TEAM}.yaml"
    path.parent.mkdir(parents=True, exist_ok=True)
    if not path.exists():
        path.write_text(_TEAM_YAML, encoding="utf-8")
        print(f"wrote {path}")


def main() -> None:
    _ensure_team()
    _require_clients(Path("fleet.config.yaml"), IMPLEMENTER, REVIEWER)
    service = MarshalService(Path("."), load_config("fleet.config.yaml"))

    # The task must produce a diff UNCONDITIONALLY. A conditional edit ("add X if missing") can
    # legitimately do nothing, and a review panel handed an empty subject is refused - correctly,
    # since a reviewer that sees nothing reports no issues, which is worse than no review at all.
    record = service.run_agent(
        IMPLEMENTER,
        "Create a new file examples/_review_candidate.py containing a single function "
        "`halve(n: int) -> float` that returns n / 2, with a one-line docstring. "
        "Create only that file.",
    )
    print(f"candidate status={record.status}  run_id={record.run_id}")

    if record.status != "exited_clean":
        print(f"candidate run did not succeed ({record.status}); nothing to review")
        return

    collected = service.collect_run(record.run_id)
    if not collected.changed_files:
        # The task above creates a file unconditionally, so an empty diff means the agent did not
        # do it. Stop and say so: this team targets `run`, and reviewing some unrelated commit
        # range instead would demonstrate the panel on material nobody asked about.
        print("candidate produced no diff - the agent did not create the file; nothing to review")
        return

    result = service.run_team(TEAM, TeamSubject(kind="run", run_id=record.run_id))

    print("--- unified_report (read this first) ---")
    print(result.unified_report)
    print("---")
    print(f"report_dir={result.report_dir}")
    print(f"incomplete_roles={result.incomplete_roles}")
    print(f"next_actions={result.next_actions}")
    # No verdict field exists. Integrating (or not) is your call after reading the reports.
    print("engine verdict: (none — decide from the reports above)")


if __name__ == "__main__":
    main()
