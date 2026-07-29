"""Tests for the marshal CLI --json output paths."""

from __future__ import annotations

import argparse
import json
import subprocess
from pathlib import Path

import pytest

from marshal_engine import cli
from marshal_engine.budgets import BudgetExceeded
from marshal_engine.usage import Bucket
from marshal_engine.worktree import WorktreeError


def test_backends_json(capsys: pytest.CaptureFixture[str]) -> None:
    ret = cli.main(["backends", "--json"])
    assert ret == 0
    out, _ = capsys.readouterr()
    data = json.loads(out)
    assert isinstance(data, list)
    assert len(data) >= 1
    fidelity_values = {"enforced-denies", "boundary-only"}
    for item in data:
        assert set(item.keys()) == {
            "name",
            "available",
            "json_output",
            "native_usage",
            "permission_modes",
            "permission_fidelity",
        }
        assert isinstance(item["available"], bool)
        assert isinstance(item["json_output"], bool)
        assert isinstance(item["native_usage"], bool)
        assert isinstance(item["permission_modes"], list)
        assert item["permission_fidelity"] in fidelity_values


def test_backends_human(capsys: pytest.CaptureFixture[str]) -> None:
    ret = cli.main(["backends"])
    assert ret == 0
    out, _ = capsys.readouterr()
    assert "available=" in out
    assert "json=" in out
    assert "fidelity=" in out


def test_usage_json(tmp_path: Path, capsys: pytest.CaptureFixture[str]) -> None:
    ret = cli.main(["usage", "--json", "--dir", str(tmp_path / "usage")])
    assert ret == 0
    out, _ = capsys.readouterr()
    data = json.loads(out)
    assert isinstance(data, dict)
    assert "totals" in data
    assert "by_backend" in data
    assert "by_client" in data
    assert "by_model" in data


def test_usage_human_empty(tmp_path: Path, capsys: pytest.CaptureFixture[str]) -> None:
    ret = cli.main(["usage", "--dir", str(tmp_path / "usage")])
    assert ret == 0
    out, _ = capsys.readouterr()
    assert "runs=0" in out


def test_usage_defaults_resolve_against_repo_not_cwd(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch, capsys: pytest.CaptureFixture[str]
) -> None:
    # Usage data lives under the repo's .marshal/usage, but the CLI is invoked from a subdirectory.
    # Without --repo (or MARSHAL_REPO), cwd-relative defaults would miss the ledger.
    from datetime import datetime, timezone

    from marshal_engine.layout import usage_dir
    from marshal_engine.usage import UsageEvent

    repo = tmp_path / "repo"
    subdir = repo / "src" / "pkg"
    subdir.mkdir(parents=True)
    u = usage_dir(repo)
    u.mkdir(parents=True)
    (u / "events.jsonl").write_text(
        UsageEvent(
            ts=datetime.now(timezone.utc).isoformat(),
            run_id="r1", backend="opencode", cost_usd=0.42, status="exited_clean", source="native",
        ).model_dump_json() + "\n"
    )
    monkeypatch.chdir(subdir)
    monkeypatch.delenv("MARSHAL_REPO", raising=False)
    ret = cli.main(["usage", "--json", "--repo", str(repo)])
    assert ret == 0
    data = json.loads(capsys.readouterr()[0])
    assert data["totals"]["runs"] == 1
    assert abs(data["totals"]["cost_usd"] - 0.42) < 1e-9


def test_usage_json_includes_breakdowns_and_window(tmp_path: Path, capsys: pytest.CaptureFixture[str]) -> None:
    # Backward-compat: the four pinned keys (totals/by_backend/by_client/by_model) stay. The new
    # by_backend_model + window metadata is additive (the original test_usage_json only checked
    # the four legacy keys; this one extends, doesn't break).
    from datetime import datetime, timezone

    from marshal_engine.usage import UsageEvent

    u = tmp_path / "usage"
    u.mkdir()
    (u / "events.jsonl").write_text(
        UsageEvent(
            ts=datetime.now(timezone.utc).isoformat(),
            run_id="r1", backend="opencode", model="<provider>/<model-a>",
            cost_usd=0.01, input_tokens=100, output_tokens=10, status="exited_clean", source="native",
        ).model_dump_json() + "\n"
    )
    ret = cli.main(["usage", "--json", "--dir", str(u), "--window", "week"])
    assert ret == 0
    data = json.loads(capsys.readouterr()[0])
    # The four legacy keys still present
    assert {"totals", "by_backend", "by_client", "by_model"} <= set(data)
    # The new additive fields
    assert "by_backend_model" in data
    assert "opencode/<provider>/<model-a>" in data["by_backend_model"]
    assert data["window"] == "week"
    assert data["since"] is not None  # resolved window -> a concrete since


def test_usage_human_surfaces_by_model_and_tokens(
    tmp_path: Path, capsys: pytest.CaptureFixture[str]
) -> None:
    # The previous human output silently dropped by_client/by_model/cache_read tokens. This locks
    # the fix: the by_model section appears, and an in/out token count is visible in the header.
    from datetime import datetime, timezone

    from marshal_engine.usage import UsageEvent

    u = tmp_path / "usage"
    u.mkdir()
    (u / "events.jsonl").write_text(
        UsageEvent(
            ts=datetime.now(timezone.utc).isoformat(),
            run_id="r1", backend="opencode", model="<provider>/<model-a>",
            client="worker", cost_usd=0.01, input_tokens=100, output_tokens=10,
            cache_read_tokens=5, status="exited_clean", source="native",
        ).model_dump_json() + "\n"
    )
    ret = cli.main(["usage", "--dir", str(u)])
    assert ret == 0
    out, _ = capsys.readouterr()
    assert "by_model" in out  # the previously-hidden section is now printed
    assert "by_client" in out
    assert "by_backend_model" in out
    assert "input_tokens" in out  # the token columns are labeled
    assert "output_tokens" in out
    assert "cache_read_tokens" in out
    # The actual token counts are visible too (the by_model row)
    assert "100" in out and "10" in out


def test_usage_window_flag_is_wired(tmp_path: Path, capsys: pytest.CaptureFixture[str]) -> None:
    # --window goes through to UsageTracker.summary(since=...); verify it produces a valid windowed
    # JSON response with the resolved since populated.
    from datetime import datetime, timezone

    from marshal_engine.usage import UsageEvent

    u = tmp_path / "usage"
    u.mkdir()
    (u / "events.jsonl").write_text(
        UsageEvent(
            ts="2020-01-01T00:00:00Z", run_id="old", backend="opencode", cost_usd=1.00,
        ).model_dump_json() + "\n"
        + UsageEvent(
            ts=datetime.now(timezone.utc).isoformat(),
            run_id="new", backend="opencode", cost_usd=0.01,
        ).model_dump_json() + "\n"
    )
    ret = cli.main(["usage", "--json", "--dir", str(u), "--window", "month"])
    assert ret == 0
    data = json.loads(capsys.readouterr()[0])
    assert data["window"] == "month"
    assert data["since"] is not None
    # The 2020 event is outside any 30d window -> only the recent event is aggregated
    assert data["totals"]["runs"] == 1
    assert abs(data["totals"]["cost_usd"] - 0.01) < 1e-9


def test_usage_session_window_states_cli_caveat(
    tmp_path: Path, capsys: pytest.CaptureFixture[str]
) -> None:
    # CLI has no long-lived Fleet: --window session must succeed and plainly say it means
    # "since this CLI invocation" (typically empty) — not silently return a misleading $0.
    from datetime import datetime, timezone

    from marshal_engine.usage import UsageEvent

    u = tmp_path / "usage"
    u.mkdir()
    (u / "events.jsonl").write_text(
        UsageEvent(
            ts=datetime.now(timezone.utc).isoformat(),
            run_id="r1", backend="opencode", cost_usd=0.50, status="exited_clean", source="native",
        ).model_dump_json() + "\n"
    )
    ret = cli.main(["usage", "--dir", str(u), "--window", "session"])
    assert ret == 0
    out, _ = capsys.readouterr()
    assert "window=session" in out
    assert "since this CLI invocation" in out
    assert "no long-lived Fleet" in out
    # The ledger event is older than "now" session_start → filtered out (honest empty session).
    assert "runs=0" in out


def test_status_json(tmp_path: Path, capsys: pytest.CaptureFixture[str]) -> None:
    ret = cli.main(["status", "--json", "--state", str(tmp_path / "runs")])
    assert ret == 0
    out, _ = capsys.readouterr()
    data = json.loads(out)
    # An envelope, not a bare list: a paged listing has to be able to say how much it left out.
    assert data["runs"] == []
    assert data["matched"] == 0 and data["returned"] == 0
    assert data["truncated"] is False


def test_status_human_no_runs(tmp_path: Path, capsys: pytest.CaptureFixture[str]) -> None:
    ret = cli.main(["status", "--state", str(tmp_path / "runs")])
    assert ret == 0
    out = capsys.readouterr()[0]
    assert "no runs recorded" in out


# --- `marshal logs` subcommand: print the persisted run log -----------------------------------


def test_logs_prints_stored_log(tmp_path: Path, capsys: pytest.CaptureFixture[str]) -> None:
    from marshal_engine.logs import RunLogStore

    log_dir = tmp_path / "logs"
    RunLogStore(log_dir).write("r1", "out-line\n", "err-line\n")
    ret = cli.main(["logs", "r1", "--dir", str(log_dir)])
    assert ret == 0
    out = capsys.readouterr()[0]
    assert "=== run r1 ===" in out
    assert "out-line" in out
    assert "err-line" in out


def test_logs_absent_returns_nonzero(tmp_path: Path, capsys: pytest.CaptureFixture[str]) -> None:
    # No log under the dir -> clear stderr message and non-zero exit so a wrapper / shell can
    # detect the miss without parsing stdout. Mirrors `usage`/`status` posture on absent input.
    ret = cli.main(["logs", "missing", "--dir", str(tmp_path / "logs")])
    assert ret != 0
    err = capsys.readouterr()[1]
    assert "no log for run" in err
    assert "missing" in err


def test_doctor_json(tmp_path: Path, capsys: pytest.CaptureFixture[str]) -> None:
    ret = cli.main(["doctor", "--json", "--repo", str(tmp_path), "--config", str(tmp_path / "none.yaml")])
    assert ret in (0, 1)  # exit code tracks hard failures; structure is what we assert here
    data = json.loads(capsys.readouterr()[0])
    assert set(data) == {"checks", "fails", "warns", "ok"}
    assert isinstance(data["checks"], list) and data["checks"]
    for c in data["checks"]:
        assert set(c.keys()) == {"name", "status", "detail", "fix"}
    assert "python" in {c["name"] for c in data["checks"]}


def test_clean_no_runs_json(tmp_path: Path, capsys: pytest.CaptureFixture[str]) -> None:
    ret = cli.main(["clean", "--json", "--repo", str(tmp_path)])
    assert ret == 0
    data = json.loads(capsys.readouterr()[0])
    assert data["removed"] == [] and data["skipped"] == [] and data["errors"] == []
    assert data["orphans_removed"] == []  # the sweep result is part of the JSON shape


def test_clean_human_no_runs(tmp_path: Path, capsys: pytest.CaptureFixture[str]) -> None:
    ret = cli.main(["clean", "--repo", str(tmp_path), "--dry-run"])
    assert ret == 0
    assert "would remove 0 run(s)" in capsys.readouterr()[0]


_FLEET = "clients:\n  a:\n    backend: cursor\n  b:\n    backend: cursor\n"


def _repo_with_workflow(tmp_path: Path, body: str) -> Path:
    repo = tmp_path / "repo"
    (repo / "workflows").mkdir(parents=True)
    (repo / "fleet.config.yaml").write_text(_FLEET)
    (repo / "workflows" / "review.yaml").write_text(body)
    return repo


_VALID = "name: review\ninputs: [target]\nphases:\n  - run: fan_out\n    clients: [a, b]\n    goal: 'check {target}'\n  - run: collect\n"
_BAD_CLIENT = "name: review\nphases:\n  - run: fan_out\n    clients: [ghost]\n    goal: g\n"


def test_workflows_json_valid(tmp_path: Path, capsys: pytest.CaptureFixture[str]) -> None:
    repo = _repo_with_workflow(tmp_path, _VALID)
    ret = cli.main(["workflows", "--repo", str(repo), "--json"])
    assert ret == 0
    data = json.loads(capsys.readouterr()[0])
    assert data[0]["name"] == "review"
    assert data[0]["error"] is None
    assert [p["run"] for p in data[0]["phases"]] == ["fan_out", "collect"]


def test_workflows_validates_client_names(tmp_path: Path, capsys: pytest.CaptureFixture[str]) -> None:
    repo = _repo_with_workflow(tmp_path, _BAD_CLIENT)
    ret = cli.main(["workflows", "--repo", str(repo)])
    assert ret == 1  # an invalid recipe makes the command fail (fail-fast for CI)
    out, _ = capsys.readouterr()
    assert "unknown client" in out


def test_workflows_none_present(tmp_path: Path, capsys: pytest.CaptureFixture[str]) -> None:
    repo = tmp_path / "repo"
    repo.mkdir()
    ret = cli.main(["workflows", "--repo", str(repo)])
    assert ret == 0
    assert "no workflows" in capsys.readouterr()[0]


# --- teams subcommand -------------------------------------------------------------------------


_RO_FLEET = (
    "clients:\n"
    "  ro-a:\n    backend: cursor\n    permission: read-only\n"
    "  ro-b:\n    backend: cursor\n    permission: read-only\n"
    "  rw:\n    backend: cursor\n    permission: safe-edit\n"
)
_TEAM = (
    "target: plan\nroles:\n"
    "  - name: architect\n    client: {a}\n    rubric: rules\n"
    "  - name: tests\n    client: ro-b\n    rubric: tests\n"
)


def _repo_with_team(tmp_path: Path, body: str) -> Path:
    repo = tmp_path / "repo"
    (repo / "teams").mkdir(parents=True)
    (repo / "fleet.config.yaml").write_text(_RO_FLEET)
    (repo / "teams" / "gate.yaml").write_text(body)
    return repo


def test_teams_json_valid(tmp_path: Path, capsys: pytest.CaptureFixture[str]) -> None:
    repo = _repo_with_team(tmp_path, _TEAM.format(a="ro-a"))
    ret = cli.main(["teams", "--repo", str(repo), "--json"])
    assert ret == 0
    data = json.loads(capsys.readouterr()[0])
    assert data[0]["name"] == "gate"
    assert data[0]["target"] == "plan"
    assert data[0]["error"] is None
    assert [r["name"] for r in data[0]["roles"]] == ["architect", "tests"]


def test_teams_flags_a_reviewer_that_can_write(
    tmp_path: Path, capsys: pytest.CaptureFixture[str]
) -> None:
    """`marshal teams` must surface the read-only rule, not just unknown client names."""
    repo = _repo_with_team(tmp_path, _TEAM.format(a="rw"))
    ret = cli.main(["teams", "--repo", str(repo)])
    assert ret == 1
    assert "must be read-only" in capsys.readouterr()[0]


def test_teams_none_present(tmp_path: Path, capsys: pytest.CaptureFixture[str]) -> None:
    repo = tmp_path / "repo"
    repo.mkdir()
    ret = cli.main(["teams", "--repo", str(repo)])
    assert ret == 0
    assert "no teams" in capsys.readouterr()[0]


def test_teams_json_has_no_decision_field(tmp_path: Path, capsys: pytest.CaptureFixture[str]) -> None:
    """The engine computes no verdict, so no surface may advertise one."""
    repo = _repo_with_team(tmp_path, _TEAM.format(a="ro-a"))
    cli.main(["teams", "--repo", str(repo), "--json"])
    data = json.loads(capsys.readouterr()[0])
    assert "decision" not in data[0]


def _stub_team_result(incomplete: list[str]) -> object:
    from marshal_engine.teams import RoleReview, TeamReview, TeamSubject

    return TeamReview(
        name="gate",
        team_run_id="t1",
        subject=TeamSubject(kind="plan", text="x"),
        reviews=[RoleReview(role="architect", client="ro-a", completed=not incomplete)],
        unified_report="# Team review: gate\n",
        incomplete_roles=incomplete,
    )


@pytest.mark.parametrize(
    ("incomplete", "expected"), [([], 0), (["tests"], 1)]
)
def test_team_run_exit_code_reports_whether_the_panel_ran(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch, capsys: pytest.CaptureFixture[str],
    incomplete: list[str], expected: int,
) -> None:
    """The shell-gate contract: non-zero means a reviewer did NOT report, never "they objected".

    Without this, `return 1 if result.incomplete_roles else 0` could be reverted to `return 0` with
    a green suite, and `marshal team run ... && deploy` would proceed on a partial panel.
    """
    from marshal_engine.service import MarshalService

    repo = _repo_with_team(tmp_path, _TEAM.format(a="ro-a"))
    monkeypatch.setattr(
        MarshalService, "run_team", lambda self, name, subject, **kw: _stub_team_result(incomplete)
    )
    ret = cli.main(["team", "run", "gate", "--target", "plan", "--text", "x", "--repo", str(repo)])
    assert ret == expected
    assert "# Team review: gate" in capsys.readouterr()[0]


def test_team_run_reads_a_plan_from_a_file(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    from marshal_engine.service import MarshalService

    repo = _repo_with_team(tmp_path, _TEAM.format(a="ro-a"))
    plan = tmp_path / "plan.md"
    plan.write_text("ship the widget", encoding="utf-8")
    seen: dict[str, object] = {}

    def fake(self: object, name: str, subject: object, **kw: object) -> object:
        seen["text"] = subject.text  # type: ignore[attr-defined]
        return _stub_team_result([])

    monkeypatch.setattr(MarshalService, "run_team", fake)
    ret = cli.main(
        ["team", "run", "gate", "--target", "plan", "--plan-file", str(plan), "--repo", str(repo)]
    )
    assert ret == 0
    assert seen["text"] == "ship the widget"


def test_team_run_reports_an_unreadable_plan_file(
    tmp_path: Path, capsys: pytest.CaptureFixture[str]
) -> None:
    repo = _repo_with_team(tmp_path, _TEAM.format(a="ro-a"))
    ret = cli.main(
        ["team", "run", "gate", "--target", "plan", "--plan-file", str(tmp_path / "gone.md"),
         "--repo", str(repo)]
    )
    assert ret == 1
    assert "cannot read" in capsys.readouterr()[1]


def test_teams_says_when_it_could_not_validate_clients(
    tmp_path: Path, capsys: pytest.CaptureFixture[str]
) -> None:
    """With no readable config the read-only rule is unchecked - say so rather than implying a pass."""
    repo = tmp_path / "repo"
    (repo / "teams").mkdir(parents=True)
    (repo / "teams" / "gate.yaml").write_text(_TEAM.format(a="ro-a"))
    ret = cli.main(["teams", "--repo", str(repo)])
    assert ret == 0
    assert "were not validated" in capsys.readouterr()[0]


def test_team_run_reports_a_config_error(tmp_path: Path, capsys: pytest.CaptureFixture[str]) -> None:
    repo = _repo_with_team(tmp_path, _TEAM.format(a="ro-a"))
    ret = cli.main(
        ["team", "run", "gate", "--target", "run", "--run-id", "x", "--repo", str(repo)]
    )
    assert ret == 1  # target mismatch: the team reviews plans
    assert "reviews target 'plan'" in capsys.readouterr()[1]


# --- workspace registry subcommand ----------------------------------------------------------


def test_workspace_add_list_remove(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch, capsys: pytest.CaptureFixture[str]
) -> None:
    repo = tmp_path / "r"
    repo.mkdir()
    monkeypatch.setenv("MARSHAL_REPO", str(tmp_path))
    monkeypatch.setenv("MARSHAL_WORKSPACES_FILE", str(tmp_path / "w.yaml"))
    # The MCP-only registration gate must not affect the human-run CLI: prove the whole
    # add/list/remove flow works with the opt-in absent.
    monkeypatch.delenv("MARSHAL_ALLOW_MCP_WORKSPACE_REGISTRATION", raising=False)

    assert cli.main(["workspace", "add", "alpha", str(repo)]) == 0
    assert (repo / "fleet.config.yaml").exists()  # scaffolded by default
    assert "registered workspace 'alpha'" in capsys.readouterr()[0]

    assert cli.main(["workspace", "list"]) == 0
    out = capsys.readouterr()[0]
    assert "alpha" in out and "default" in out

    assert cli.main(["workspace", "remove", "alpha"]) == 0
    assert "removed workspace 'alpha'" in capsys.readouterr()[0]
    assert cli.main(["workspace", "remove", "alpha"]) == 1  # already gone -> nonzero
    assert "no workspace" in capsys.readouterr()[0]


def test_workspace_add_json_and_no_scaffold(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch, capsys: pytest.CaptureFixture[str]
) -> None:
    repo = tmp_path / "r"
    repo.mkdir()
    monkeypatch.setenv("MARSHAL_REPO", str(tmp_path))
    monkeypatch.setenv("MARSHAL_WORKSPACES_FILE", str(tmp_path / "w.yaml"))

    assert cli.main(["workspace", "add", "alpha", str(repo), "--no-scaffold", "--json"]) == 0
    data = json.loads(capsys.readouterr()[0])
    assert data["name"] == "alpha" and data["scaffolded"] is False
    assert not (repo / "fleet.config.yaml").exists()  # --no-scaffold honored


def test_workspace_bare_lists(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch, capsys: pytest.CaptureFixture[str]
) -> None:
    monkeypatch.setenv("MARSHAL_REPO", str(tmp_path))
    monkeypatch.setenv("MARSHAL_WORKSPACES_FILE", str(tmp_path / "w.yaml"))
    assert cli.main(["workspace"]) == 0  # no subcommand -> lists, must not crash
    assert "default" in capsys.readouterr()[0]


# --- cost display honesty ---------------------------------------------------------------


def test_format_cost_display_unavailable() -> None:
    assert cli._format_cost_display(0.0, "unavailable") == "unavailable"
    assert "$0.0000" not in cli._format_cost_display(0.0, "unavailable")


def test_format_cost_display_native_zero() -> None:
    assert cli._format_cost_display(0.0, "native") == "$0.0000"


def test_format_cost_display_native_amount() -> None:
    assert cli._format_cost_display(0.4695, "native") == "$0.4695"


def test_format_cost_display_missing_source() -> None:
    assert cli._format_cost_display(0.0, None) == "unavailable"


def test_status_human_unavailable_cost(
    tmp_path: Path, capsys: pytest.CaptureFixture[str]
) -> None:
    from marshal_engine.state import FleetState, RunRecord

    runs = tmp_path / "runs"
    state = FleetState(runs)
    state.add(RunRecord(
        run_id="cursor-run.cursor.abc12345", task_id="t", backend="cursor",
        status="exited_clean", cost_usd=0.0, source="unavailable",
    ))
    ret = cli.main(["status", "--state", str(runs)])
    assert ret == 0
    out = capsys.readouterr()[0]
    assert "unavailable" in out
    assert "$0.0000" not in out


def test_status_human_native_zero_cost(
    tmp_path: Path, capsys: pytest.CaptureFixture[str]
) -> None:
    from marshal_engine.state import FleetState, RunRecord

    runs = tmp_path / "runs"
    state = FleetState(runs)
    state.add(RunRecord(
        run_id="free-run.claude-code.abc123", task_id="t", backend="claude-code",
        status="exited_clean", cost_usd=0.0, source="native",
    ))
    ret = cli.main(["status", "--state", str(runs)])
    assert ret == 0
    out = capsys.readouterr()[0]
    line = [ln for ln in out.splitlines() if "claude-code" in ln][0]
    assert "$0.0000" in line
    assert "unavailable" not in line


def test_status_human_native_real_cost(
    tmp_path: Path, capsys: pytest.CaptureFixture[str]
) -> None:
    from marshal_engine.state import FleetState, RunRecord

    runs = tmp_path / "runs"
    state = FleetState(runs)
    state.add(RunRecord(
        run_id="paid-run.claude-code.abc123", task_id="t", backend="claude-code",
        status="exited_clean", cost_usd=0.4695, source="native",
    ))
    ret = cli.main(["status", "--state", str(runs)])
    assert ret == 0
    assert "$0.4695" in capsys.readouterr()[0]


def test_usage_human_unavailable_backend_bucket(
    tmp_path: Path, capsys: pytest.CaptureFixture[str]
) -> None:
    from datetime import datetime, timezone

    from marshal_engine.usage import UsageEvent

    u = tmp_path / "usage"
    u.mkdir()
    (u / "events.jsonl").write_text(
        UsageEvent(
            ts=datetime.now(timezone.utc).isoformat(),
            run_id="r1", backend="cursor", cost_usd=0.0, status="exited_clean",
            source="unavailable",
        ).model_dump_json() + "\n"
    )
    ret = cli.main(["usage", "--dir", str(u)])
    assert ret == 0
    out = capsys.readouterr()[0]
    assert "cost=unavailable" in out
    assert "cost=$0.0000" not in out
    assert "unavailable" in out.split("by_backend")[1]


def test_usage_human_native_zero_totals(
    tmp_path: Path, capsys: pytest.CaptureFixture[str]
) -> None:
    from datetime import datetime, timezone

    from marshal_engine.usage import UsageEvent

    u = tmp_path / "usage"
    u.mkdir()
    (u / "events.jsonl").write_text(
        UsageEvent(
            ts=datetime.now(timezone.utc).isoformat(),
            run_id="r1", backend="claude-code", cost_usd=0.0, status="exited_clean",
            source="native",
        ).model_dump_json() + "\n"
    )
    ret = cli.main(["usage", "--dir", str(u)])
    assert ret == 0
    out = capsys.readouterr()[0]
    assert "cost=$0.0000" in out
    assert "$/run=$0.0000" in out


def test_usage_human_budget_unavailable_spent(
    tmp_path: Path, capsys: pytest.CaptureFixture[str]
) -> None:
    from datetime import datetime, timezone

    from marshal_engine.usage import UsageEvent

    u = tmp_path / "usage"
    u.mkdir()
    (u / "events.jsonl").write_text(
        UsageEvent(
            ts=datetime.now(timezone.utc).isoformat(),
            run_id="r1", backend="cursor", client="composer", cost_usd=0.0,
            status="exited_clean", source="unavailable",
        ).model_dump_json() + "\n"
    )
    cfg = tmp_path / "fleet.config.yaml"
    cfg.write_text(
        "clients:\n  composer:\n    backend: cursor\n"
        "budgets:\n"
        "  - backend: cursor\n    window: month\n    limit_usd: 5.0\n"
    )
    ret = cli.main(["usage", "--dir", str(u), "--config", str(cfg), "--window", "month"])
    assert ret == 0
    out = capsys.readouterr()[0]
    assert "\nbudgets" in out
    # Spent and remaining are unknown when runs exist but cost is unavailable.
    budget_section = out.split("\nbudgets", 1)[1]
    assert budget_section.count("unavailable") >= 2
    assert "$0.0000" not in budget_section.split("limit")[0]


# --- `marshal usage` budgets: optional `--config` surfaces the configured `budgets:` -----


def test_usage_json_includes_budgets_when_configured(
    tmp_path: Path, capsys: pytest.CaptureFixture[str]
) -> None:
    # When --config points at a fleet.config.yaml that declares `budgets:`, the JSON usage
    # response includes a `budgets` list with scope / window / spent / limit / remaining. This
    # is additive: tests that don't set --config still see the legacy four-key shape.
    from datetime import datetime, timezone

    from marshal_engine.usage import UsageEvent

    u = tmp_path / "usage"
    u.mkdir()
    (u / "events.jsonl").write_text(
        UsageEvent(
            ts=datetime.now(timezone.utc).isoformat(),
            run_id="r1", backend="opencode", client="worker", model="<provider>/<model-a>",
            cost_usd=0.40, status="exited_clean", source="native",
        ).model_dump_json() + "\n"
    )
    cfg = tmp_path / "fleet.config.yaml"
    cfg.write_text(
        "clients:\n  worker:\n    backend: opencode\n"
        "budgets:\n"
        "  - client: worker\n    window: week\n    limit_usd: 1.0\n"
        "  - backend: opencode\n    window: session\n    limit_usd: 0.50\n"
    )
    ret = cli.main(["usage", "--json", "--dir", str(u), "--config", str(cfg), "--window", "week"])
    assert ret == 0
    data = json.loads(capsys.readouterr()[0])
    assert "budgets" in data
    assert {b["scope"] for b in data["budgets"]} == {"client:worker", "backend:opencode"}
    # The worker event booked $0.40 against the $1.0 cap -> $0.60 remaining.
    worker = next(b for b in data["budgets"] if b["scope"] == "client:worker")
    assert abs(worker["spent_usd"] - 0.40) < 1e-6
    assert worker["limit_usd"] == 1.0
    assert abs(worker["remaining_usd"] - 0.60) < 1e-6
    # The backend budget under session has no spend (CLI session_start == now) -> $0.50 remaining.
    be = next(b for b in data["budgets"] if b["scope"] == "backend:opencode")
    assert be["spent_usd"] == 0.0
    assert be["limit_usd"] == 0.5
    assert be["remaining_usd"] == 0.5


def test_usage_json_omits_budgets_when_no_config(
    tmp_path: Path, capsys: pytest.CaptureFixture[str]
) -> None:
    # The "no behavior change" contract: without --config, the JSON response has NO `budgets`
    # key (preserves the existing tests' assertions on the four legacy keys).
    ret = cli.main(["usage", "--json", "--dir", str(tmp_path / "usage")])
    assert ret == 0
    data = json.loads(capsys.readouterr()[0])
    assert "budgets" not in data


def test_usage_human_includes_budgets_section(
    tmp_path: Path, capsys: pytest.CaptureFixture[str]
) -> None:
    # The human output gets a "budgets" table when budgets are configured, aligned via _align_rows.
    cfg = tmp_path / "fleet.config.yaml"
    cfg.write_text(
        "clients:\n  worker:\n    backend: opencode\n"
        "budgets:\n"
        "  - window: month\n    limit_usd: 5.0\n"
    )
    ret = cli.main(["usage", "--dir", str(tmp_path / "usage"), "--config", str(cfg)])
    assert ret == 0
    out = capsys.readouterr()[0]
    assert "\nbudgets" in out
    assert "global" in out  # scope label
    assert "month" in out
    assert "$5.0000" in out  # the limit column
    assert "$0.0000" in out  # spent column (empty ledger, session collapse)


# --- `marshal models` subcommand -------------------------------------------------------------


_CATALOG = (
    "models:\n"
    "  - id: <provider>/<model-a>\n"
    "    backends: [opencode, claude-code]\n"
    "    cost: native\n"
    "    quota_type: subscription\n"
    "    notes: placeholder\n"
    "  - id: <provider>/<model-b>\n"
    "    backends: [cursor]\n"
    "    cost: estimated\n"
)


def test_models_json_with_catalog(tmp_path: Path, capsys: pytest.CaptureFixture[str]) -> None:
    cfg = tmp_path / "fleet.config.yaml"
    cfg.write_text(_CATALOG)
    ret = cli.main(["models", "--repo", str(tmp_path), "--config", str(cfg), "--json"])
    assert ret == 0
    data = json.loads(capsys.readouterr()[0])
    assert set(data) == {"models", "driver_context"}
    assert data["models"] == [
        {"id": "<provider>/<model-a>", "backends": ["opencode", "claude-code"],
         "cost": "native", "quota_type": "subscription", "notes": "placeholder"},
        {"id": "<provider>/<model-b>", "backends": ["cursor"],
         "cost": "estimated", "quota_type": "", "notes": ""},
    ]
    assert data["driver_context"] is None


def test_models_human_prints_each_row(tmp_path: Path, capsys: pytest.CaptureFixture[str]) -> None:
    cfg = tmp_path / "fleet.config.yaml"
    cfg.write_text(_CATALOG)
    ret = cli.main(["models", "--repo", str(tmp_path), "--config", str(cfg)])
    assert ret == 0
    out = capsys.readouterr()[0]
    assert "<provider>/<model-a>" in out
    assert "opencode,claude-code" in out
    assert "native" in out
    assert "<provider>/<model-b>" in out
    assert "cursor" in out


def test_models_no_catalog_prints_friendly_message(tmp_path: Path, capsys: pytest.CaptureFixture[str]) -> None:
    # Repo with no config file: `marshal models` is a no-op-ish view that explains the absence.
    ret = cli.main(["models", "--repo", str(tmp_path), "--config", str(tmp_path / "none.yaml")])
    assert ret == 0
    out = capsys.readouterr()[0]
    assert "no `models:` catalog" in out


def test_models_malformed_config_returns_error(
    tmp_path: Path, capsys: pytest.CaptureFixture[str]
) -> None:
    cfg = tmp_path / "fleet.config.yaml"
    cfg.write_text("models: 42\nclients: {}\n")  # malformed catalog -> ConfigError
    ret = cli.main(["models", "--repo", str(tmp_path), "--config", str(cfg)])
    assert ret == 1
    err = capsys.readouterr()[1]
    assert "models must be a list" in err


def test_run_missing_config_warns_and_names_path(
    tmp_path: Path, capsys: pytest.CaptureFixture[str]
) -> None:
    # Valid git repo with no fleet.config.yaml must warn on stderr and name the path in the
    # resolution error - not a bare `known: (none configured)`.
    repo = tmp_path / "repo"
    repo.mkdir()
    subprocess.run(["git", "init", "-q", str(repo)], check=True)
    ret = cli.main(
        [
            "run",
            "--repo",
            str(repo),
            "--client",
            "goose-cursor",
            "--goal",
            "pong",
        ]
    )
    assert ret == 1
    err = capsys.readouterr()[1]
    assert "no fleet config" in err
    assert "goose-cursor" in err
    assert str(repo / "fleet.config.yaml") in err


def test_run_nongit_repo_fails_without_missing_config_advisory(
    tmp_path: Path, capsys: pytest.CaptureFixture[str]
) -> None:
    """Non-git --repo must fail with a git-repo error, not lead with the missing-config hint."""
    ret = cli.main(
        [
            "run",
            "--repo",
            str(tmp_path),
            "--backend",
            "goose",
            "--goal",
            "x",
        ]
    )
    assert ret == 1
    err = capsys.readouterr().err
    assert "not a git work tree" in err
    assert "point MARSHAL_REPO" in err or "--repo" in err
    assert "no fleet config" not in err
    assert "Copy fleet.config.example.yaml" not in err


def test_run_and_spawn_catch_budget_exceeded(
    monkeypatch: pytest.MonkeyPatch, capsys: pytest.CaptureFixture[str]
) -> None:
    """enforce refusals must exit 1 with stderr for every CLI run/spawn path (all backends)."""

    class _FakeSvc:
        def run_agent(self, *_a: object, **_k: object) -> object:
            raise BudgetExceeded("refusing new spawn (enforce=true)")

        def spawn(self, *_a: object, **_k: object) -> object:
            raise BudgetExceeded("refusing new spawn (enforce=true)")

    monkeypatch.setattr(cli, "_require_git_work_tree", lambda _repo: None)
    monkeypatch.setattr(cli, "_build_cli_service", lambda _args: _FakeSvc())
    args = argparse.Namespace(
        client=None,
        goal="x",
        task_id=None,
        model=None,
        backend="goose",
        duration=None,
        repo=None,
        config=None,
        json=False,
    )
    assert cli._cmd_run_like(args, spawn=False) == 1
    assert "enforce=true" in capsys.readouterr().err
    assert cli._cmd_run_like(args, spawn=True) == 1
    assert "enforce=true" in capsys.readouterr().err


def test_run_and_spawn_catch_worktree_error(
    monkeypatch: pytest.MonkeyPatch, capsys: pytest.CaptureFixture[str]
) -> None:
    """Worktree failures after a valid git repo must exit 1 with stderr, not a traceback."""

    class _FakeSvc:
        def run_agent(self, *_a: object, **_k: object) -> object:
            raise WorktreeError("worktree add failed: fatal: not a git repository")

        def spawn(self, *_a: object, **_k: object) -> object:
            raise WorktreeError("worktree add failed: fatal: not a git repository")

    monkeypatch.setattr(cli, "_require_git_work_tree", lambda _repo: None)
    monkeypatch.setattr(cli, "_build_cli_service", lambda _args: _FakeSvc())
    args = argparse.Namespace(
        client=None,
        goal="x",
        task_id=None,
        model=None,
        backend="goose",
        duration=None,
        repo=None,
        config=None,
        json=False,
    )
    assert cli._cmd_run_like(args, spawn=False) == 1
    assert "not a git repository" in capsys.readouterr().err
    assert cli._cmd_run_like(args, spawn=True) == 1
    assert "not a git repository" in capsys.readouterr().err


def test_status_since_hours_filters(tmp_path: Path, capsys: pytest.CaptureFixture[str]) -> None:
    """`--since-hours` is its own branch and shipped briefly with a NameError in it - the suite
    stayed green because no test exercised the filter. An unexercised branch is an untested one
    however many tests pass around it."""
    from marshal_engine.state import FleetState, RunRecord

    runs = tmp_path / "runs"
    state = FleetState(runs)
    state.add(RunRecord(run_id="old.e.1", task_id="t", backend="echo", status="exited_clean",
                        started_at="2020-01-01T00:00:00+00:00"))

    ret = cli.main(["status", "--json", "--state", str(runs), "--since-hours", "1"])
    assert ret == 0
    assert json.loads(capsys.readouterr()[0])["matched"] == 0

    ret = cli.main(["status", "--json", "--state", str(runs), "--since-hours", "999999"])
    assert ret == 0
    assert json.loads(capsys.readouterr()[0])["matched"] == 1


def test_status_rejects_a_nonpositive_limit(tmp_path: Path) -> None:
    """A negative limit does not error in Python - `rows[:-5]` silently returns a DIFFERENT slice
    (all but the last five), so the caller gets plausible-looking wrong output. The MCP tool bounds
    this parameter; the CLI must not be the lax door to the same logic."""
    with pytest.raises(SystemExit):
        cli.main(["status", "--state", str(tmp_path / "runs"), "--limit", "-5"])
    with pytest.raises(SystemExit):
        cli.main(["status", "--state", str(tmp_path / "runs"), "--limit", "0"])


def test_status_rejects_a_nonfinite_lookback(tmp_path: Path) -> None:
    """NaN and inf survive float() and only blow up later inside timedelta, mid-command."""
    for bad in ("nan", "inf", "0", "-3"):
        with pytest.raises(SystemExit):
            cli.main(["status", "--state", str(tmp_path / "runs"), "--since-hours", bad])


def test_bucket_rate_annotates_a_partially_priced_bucket() -> None:
    """A rate over a mixed bucket divides known spend by ALL runs, so say what it covers."""
    b = Bucket(runs=10, priced_runs=1, cost_usd=0.5, cost_per_run=0.05)
    out = cli._format_bucket_rate(b.cost_per_run, b)
    assert out == "$0.0500 (1/10 priced)"


def test_bucket_rate_unannotated_when_every_run_is_priced() -> None:
    b = Bucket(runs=4, priced_runs=4, cost_usd=0.4, cost_per_run=0.1)
    assert cli._format_bucket_rate(b.cost_per_run, b) == "$0.1000"


def test_bucket_rate_unavailable_when_nothing_is_priced() -> None:
    b = Bucket(runs=3, priced_runs=0, cost_usd=0.0, cost_per_run=0.0)
    assert cli._format_bucket_rate(b.cost_per_run, b) == "unavailable"
