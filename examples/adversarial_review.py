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


def _ensure_team() -> None:
    path = Path("teams") / f"{TEAM}.yaml"
    path.parent.mkdir(parents=True, exist_ok=True)
    if not path.exists():
        path.write_text(_TEAM_YAML, encoding="utf-8")
        print(f"wrote {path}")


def main() -> None:
    _ensure_team()
    service = MarshalService(Path("."), load_config("fleet.config.yaml"))

    record = service.run_agent(
        IMPLEMENTER,
        "Add a one-line module comment at the top of src/marshal_engine/env.py if it lacks one.",
    )
    print(f"candidate status={record.status}  run_id={record.run_id}")

    result = service.run_team(
        TEAM,
        TeamSubject(kind="run", run_id=record.run_id),
    )

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
