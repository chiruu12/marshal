"""End-to-end team review through MarshalService: real repo, real runs, stub reviewer backends.

Covers the wiring the pure tests in test_teams.py cannot: subject resolution against real git,
report persistence under `.marshal/reports/`, and the fail-closed read-only check firing through
the public `run_team` entry point.
"""

from __future__ import annotations

import subprocess
import sys
from pathlib import Path

import pytest

from marshal_engine.backends.base import CodingAgentBackend
from marshal_engine.config import ClientConfig, ConfigError, FleetConfig
from marshal_engine.layout import reports_dir
from marshal_engine.service import MarshalService
from marshal_engine.teams import TeamSubject
from marshal_engine.types import AgentResult, Capabilities, PermissionMode, RunOpts, RunStatus, TaskSpec


class _Reviewer(CodingAgentBackend):
    """A backend that prints a canned verdict, so a panel can be driven without a real agent."""

    name = "reviewer"
    binary = "python"
    capabilities = Capabilities()

    def __init__(self, verdict: str = "## Bottom line\nlooks fine") -> None:
        self.verdict = verdict
        self.goals: list[str] = []

    def check_available(self) -> bool:
        return True

    def build_invocation(self, task: TaskSpec, opts: RunOpts) -> list[str]:
        self.goals.append(task.goal)
        return [sys.executable, "-c", f"print({self.verdict!r})"]

    def map_permission(self, mode: PermissionMode) -> list[str]:
        return []

    def parse_output(self, raw_stdout: str, raw_stderr: str, exit_code: int) -> AgentResult:
        return AgentResult(status=RunStatus.SUCCEEDED, text=raw_stdout.strip(), exit_code=exit_code)


def _init_repo(root: Path) -> None:
    def git(*a: str) -> None:
        subprocess.run(["git", "-C", str(root), *a], check=True, capture_output=True, text=True)

    git("init", "-q")
    git("config", "user.email", "t@t")
    git("config", "user.name", "t")
    (root / "README.md").write_text("hi")
    git("add", "-A")
    git("commit", "-q", "-m", "init")


@pytest.fixture
def repo(tmp_path: Path) -> Path:
    r = tmp_path / "repo"
    r.mkdir()
    _init_repo(r)
    return r


_TEAM_YAML = """
description: two-lens gate
target: {target}
roles:
  - name: architect
    client: ro-a
    rubric: rules and scope
  - name: tests
    client: ro-b
    rubric: real behaviour tests
"""


def _svc(
    repo: Path,
    *,
    permission: PermissionMode = PermissionMode.READ_ONLY,
    backend: _Reviewer | None = None,
) -> MarshalService:
    cfg = FleetConfig(
        clients={
            n: ClientConfig(name=n, backend="reviewer", permission=permission)
            for n in ("ro-a", "ro-b")
        }
    )
    return MarshalService(repo, cfg, backends={"reviewer": backend or _Reviewer()})


def _write_team(repo: Path, *, target: str = "plan", name: str = "gate") -> None:
    d = repo / "teams"
    d.mkdir(exist_ok=True)
    (d / f"{name}.yaml").write_text(_TEAM_YAML.format(target=target), encoding="utf-8")


def test_run_team_writes_one_report_per_reviewer_plus_a_unified_one(repo: Path) -> None:
    _write_team(repo)
    svc = _svc(repo)
    result = svc.run_team("gate", TeamSubject(kind="plan", text="ship a widget"))

    assert [r.role for r in result.reviews] == ["architect", "tests"]
    assert all(r.completed for r in result.reviews)

    assert result.report_dir is not None
    directory = Path(result.report_dir)
    assert directory.parent == reports_dir(repo)
    assert sorted(p.name for p in directory.glob("*.md")) == [
        "README.md", "architect.md", "tests.md",
    ]
    for review in result.reviews:
        assert review.report_path is not None
        assert Path(review.report_path).read_text(encoding="utf-8").startswith(
            f"# {review.role} review"
        )

    unified = Path(result.unified_report_path or "").read_text(encoding="utf-8")
    assert unified == result.unified_report
    assert "# Team review: gate" in unified
    assert "contains no decision" in unified


def test_run_team_unified_report_points_at_the_per_role_files(repo: Path) -> None:
    _write_team(repo)
    result = _svc(repo).run_team("gate", TeamSubject(kind="plan", text="x"))
    assert "architect.md" in result.unified_report
    assert "tests.md" in result.unified_report


def test_run_team_groups_the_panel_under_one_task_id(repo: Path) -> None:
    """A review is one unit of spend, so usage()/report() can price it as a whole."""
    _write_team(repo)
    svc = _svc(repo)
    result = svc.run_team("gate", TeamSubject(kind="plan", text="x"))
    task_ids = {svc.get_run(r.run_id).task_id for r in result.reviews if r.run_id}  # type: ignore[union-attr]
    assert len(task_ids) == 1
    assert task_ids.pop().startswith("team.gate.")


def test_run_team_refuses_a_writable_reviewer_before_spawning(repo: Path) -> None:
    _write_team(repo)
    backend = _Reviewer()
    svc = _svc(repo, permission=PermissionMode.SAFE_EDIT, backend=backend)
    with pytest.raises(ConfigError, match="must be read-only"):
        svc.run_team("gate", TeamSubject(kind="plan", text="x"))
    assert backend.goals == []


def test_run_team_rejects_a_subject_that_does_not_match_the_target(repo: Path) -> None:
    _write_team(repo, target="run")
    with pytest.raises(ConfigError, match="reviews target 'run'"):
        _svc(repo).run_team("gate", TeamSubject(kind="plan", text="x"))


def test_run_team_surfaces_an_objection_without_interpreting_it(repo: Path) -> None:
    """The engine carries the objection through verbatim; judging it is the caller's job."""
    _write_team(repo)
    svc = _svc(repo, backend=_Reviewer("## Blocking\na.py:1 has no coverage"))
    result = svc.run_team("gate", TeamSubject(kind="plan", text="x"))
    assert all(r.completed for r in result.reviews)
    assert "a.py:1 has no coverage" in result.reviews[0].review
    assert "a.py:1 has no coverage" in result.unified_report
    assert result.incomplete_roles == []


def test_run_team_on_a_range_diffs_real_git(repo: Path) -> None:
    _write_team(repo, target="range")
    subprocess.run(["git", "-C", str(repo), "checkout", "-q", "-b", "feature"], check=True)
    (repo / "new.py").write_text("print('x')\n")
    subprocess.run(["git", "-C", str(repo), "add", "-A"], check=True, capture_output=True)
    subprocess.run(
        ["git", "-C", str(repo), "commit", "-q", "-m", "add"], check=True, capture_output=True
    )

    backend = _Reviewer()
    svc = _svc(repo, backend=backend)
    result = svc.run_team("gate", TeamSubject(kind="range", base="master", head="feature"))
    assert all(r.completed for r in result.reviews)
    assert "new.py" in backend.goals[0]


def test_diff_range_raises_on_an_unknown_ref(repo: Path) -> None:
    with pytest.raises(ConfigError, match="unknown base ref"):
        _svc(repo).diff_range("no-such-ref")
    with pytest.raises(ConfigError, match="unknown head ref"):
        _svc(repo).diff_range("master", "no-such-ref")


@pytest.mark.parametrize(
    "ref",
    ["--output=pwned.txt", "-O/tmp/x", "--ext-diff", ""],
)
def test_diff_range_refuses_a_ref_that_git_would_read_as_an_option(repo: Path, ref: str) -> None:
    """REGRESSION: `base='--output=<path>'` made a "read-only" diff write an arbitrary file.

    It also emptied stdout, so the panel would then review nothing and could return `pass`.
    """
    with pytest.raises(ConfigError, match="invalid base ref|unknown base ref"):
        _svc(repo).diff_range(ref)
    assert not (repo / "pwned.txt").exists()
    assert not list(repo.glob("pwned.txt*"))


def test_run_team_refuses_a_team_file_outside_the_workspace(repo: Path, tmp_path: Path) -> None:
    """A team file is prompt text for the fleet; it must come from this repo's teams/ dir."""
    _write_team(repo)
    outside = tmp_path / "evil.yaml"
    outside.write_text(_TEAM_YAML.format(target="plan"), encoding="utf-8")
    svc = _svc(repo)
    with pytest.raises(ConfigError, match="outside"):
        svc.run_team(str(outside), TeamSubject(kind="plan", text="x"))
    with pytest.raises(ConfigError, match="outside"):
        svc.run_team("../../evil.yaml", TeamSubject(kind="plan", text="x"))


def test_run_team_refuses_an_empty_range(repo: Path) -> None:
    """base == head is an empty diff; a panel with nothing to object to would pass it."""
    _write_team(repo, target="range")
    with pytest.raises(ConfigError, match="nothing to review"):
        _svc(repo).run_team("gate", TeamSubject(kind="range", base="master", head="master"))


def test_run_team_by_path_and_missing_name(repo: Path) -> None:
    _write_team(repo)
    svc = _svc(repo)
    result = svc.run_team(str(repo / "teams" / "gate.yaml"), TeamSubject(kind="plan", text="x"))
    assert all(r.completed for r in result.reviews)
    with pytest.raises(ConfigError, match="no team 'nope'"):
        svc.run_team("nope", TeamSubject(kind="plan", text="x"))
    with pytest.raises(ConfigError, match="no team file at"):
        svc.run_team(str(repo / "teams" / "gone.yaml"), TeamSubject(kind="plan", text="x"))


def test_list_teams_surfaces_specs_and_broken_files(repo: Path) -> None:
    _write_team(repo)
    (repo / "teams" / "broken.yaml").write_text("roles: 3\n", encoding="utf-8")
    listing = _svc(repo).list_teams()
    assert [t.name for t in listing.teams] == ["gate"]
    assert "broken.yaml" in listing.errors


def test_report_write_failure_does_not_lose_the_report(repo: Path, monkeypatch: pytest.MonkeyPatch) -> None:
    _write_team(repo)
    svc = _svc(repo)

    def boom(*a: object, **k: object) -> None:
        raise OSError("disk full")

    monkeypatch.setattr(Path, "write_text", boom)
    result = svc.run_team("gate", TeamSubject(kind="plan", text="x"))
    assert [r.role for r in result.reviews] == ["architect", "tests"]
    assert result.unified_report_path is None
    assert result.unified_report  # the in-memory report survives a disk failure
