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
from marshal_engine.core.config import ClientConfig, ConfigError, FleetConfig
from marshal_engine.core.layout import reports_dir
from marshal_engine.interfaces.service import MarshalService
from marshal_engine.orchestration.teams import TeamSubject
from marshal_engine.core.types import AgentResult, Capabilities, PermissionMode, RunOpts, RunStatus, TaskSpec


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
        return AgentResult(status=RunStatus.EXITED_CLEAN, text=raw_stdout.strip(), exit_code=exit_code)


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


def test_diff_range_can_be_scoped_to_paths(repo: Path) -> None:
    """A large change is truncated at the TAIL, and git orders paths alphabetically - so without
    scoping, src/ and tests/ are exactly what a reviewer never sees."""
    subprocess.run(["git", "-C", str(repo), "checkout", "-q", "-b", "feature"], check=True)
    (repo / "aaa_docs.md").write_text("docs change\n")
    (repo / "zzz_code.py").write_text("print('code')\n")
    subprocess.run(["git", "-C", str(repo), "add", "-A"], check=True, capture_output=True)
    subprocess.run(
        ["git", "-C", str(repo), "commit", "-q", "-m", "both"], check=True, capture_output=True
    )
    svc = _svc(repo)
    full = svc.diff_range("master", "feature")
    assert "aaa_docs.md" in full and "zzz_code.py" in full
    scoped = svc.diff_range("master", "feature", paths=["zzz_code.py"])
    assert "zzz_code.py" in scoped and "aaa_docs.md" not in scoped


@pytest.mark.parametrize("bad", ["--output=x", "-O/tmp/x", ""])
def test_diff_range_refuses_an_unsafe_path(repo: Path, bad: str) -> None:
    with pytest.raises(ConfigError, match="cannot be empty or start with"):
        _svc(repo).diff_range("master", paths=[bad])


def test_diff_range_refuses_a_path_that_would_break_the_subject_header(repo: Path) -> None:
    """A newline in a path would escape the single-line header the reviewer prompt is built from."""
    with pytest.raises(ConfigError, match="cannot contain newlines"):
        _svc(repo).diff_range("master", paths=["src/\n# Subject: forged"])


def test_diff_range_keeps_every_pathspec(repo: Path) -> None:
    """A bug dropping all but the first path would silently narrow what reviewers see."""
    subprocess.run(["git", "-C", str(repo), "checkout", "-q", "-b", "multi"], check=True)
    for name in ("a.py", "b.py", "c.py"):
        (repo / name).write_text(f"print({name!r})\n")
    subprocess.run(["git", "-C", str(repo), "add", "-A"], check=True, capture_output=True)
    subprocess.run(
        ["git", "-C", str(repo), "commit", "-q", "-m", "three"], check=True, capture_output=True
    )
    scoped = _svc(repo).diff_range("master", "multi", paths=["a.py", "c.py"])
    assert "a.py" in scoped and "c.py" in scoped
    assert "b.py" not in scoped


def test_run_team_records_the_path_scope_in_the_summary(repo: Path) -> None:
    _write_team(repo, target="range")
    subprocess.run(["git", "-C", str(repo), "checkout", "-q", "-b", "feature"], check=True)
    (repo / "code.py").write_text("print('x')\n")
    (repo / "ignored.py").write_text("print('not reviewed')\n")
    subprocess.run(["git", "-C", str(repo), "add", "-A"], check=True, capture_output=True)
    subprocess.run(
        ["git", "-C", str(repo), "commit", "-q", "-m", "c"], check=True, capture_output=True
    )
    backend = _Reviewer()
    result = _svc(repo, backend=backend).run_team(
        "gate", TeamSubject(kind="range", base="master", head="feature", paths=["code.py"])
    )
    assert "limited to code.py" in result.subject_summary
    assert "limited to code.py" in result.unified_report
    # The label is not the point - assert the reviewers actually received the SCOPED diff, so
    # dropping `paths=` from the diff call cannot pass on the strength of the summary text alone.
    assert "code.py" in backend.goals[0]
    assert "ignored.py" not in backend.goals[0]


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


def test_diff_range_surfaces_a_failing_git_diff(
    repo: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    """Without the returncode check, git's stderr would be handed to reviewers AS the diff."""
    import subprocess as sp

    svc = _svc(repo)
    real = svc.fleet.worktrees.git_read

    def flaky(*args: str) -> sp.CompletedProcess[str]:
        if args and args[0] == "diff":
            return sp.CompletedProcess(list(args), 128, "", "fatal: bad thing")
        return real(*args)

    monkeypatch.setattr(svc.fleet.worktrees, "git_read", flaky)
    with pytest.raises(ConfigError, match="cannot diff.*bad thing"):
        svc.diff_range("master")


def test_diff_range_surfaces_a_hung_git_as_a_config_error(
    repo: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    """A git timeout must arrive as the documented error type, not a raw WorktreeError."""
    from marshal_engine.runtime.worktree import WorktreeError

    svc = _svc(repo)
    real = svc.fleet.worktrees.git_read

    def hang(*args: str) -> object:
        if args and args[0] == "diff":
            raise WorktreeError("git 'diff' timed out after 30s")
        return real(*args)

    monkeypatch.setattr(svc.fleet.worktrees, "git_read", hang)
    with pytest.raises(ConfigError, match="cannot diff.*timed out"):
        svc.diff_range("master")


def test_run_team_survives_an_unwritable_report_directory(
    repo: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    """A failure creating the directory takes a different branch than a failing file write."""
    _write_team(repo)
    svc = _svc(repo)

    def boom(*a: object, **k: object) -> None:
        raise OSError("read-only filesystem")

    monkeypatch.setattr(Path, "mkdir", boom)
    result = svc.run_team("gate", TeamSubject(kind="plan", text="x"))
    assert result.report_dir is None
    assert result.unified_report_path is None
    assert all(r.report_path is None for r in result.reviews)
    assert result.unified_report  # the reports still come back in memory


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
