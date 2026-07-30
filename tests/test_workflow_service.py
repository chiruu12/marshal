"""End-to-end workflow loading through MarshalService: containment before any agent spawns.

Mirrors the team-file containment tests in test_teams_service.py. A workflow can spawn the fleet
and carry ``integrate`` with ``auto: true``, so path resolution must stay inside ``workflows/``.
"""

from __future__ import annotations

import subprocess
import sys
from pathlib import Path

import pytest

from marshal_engine.backends.base import CodingAgentBackend
from marshal_engine.config import ClientConfig, ConfigError, FleetConfig
from marshal_engine.service import MarshalService
from marshal_engine.types import AgentResult, Capabilities, PermissionMode, RunOpts, RunStatus, TaskSpec


class _Worker(CodingAgentBackend):
    """Backend that records invocations so containment tests can assert nothing spawned."""

    name = "worker"
    binary = "python"
    capabilities = Capabilities()

    def __init__(self) -> None:
        self.invocations = 0

    def check_available(self) -> bool:
        return True

    def build_invocation(self, task: TaskSpec, opts: RunOpts) -> list[str]:
        self.invocations += 1
        return [sys.executable, "-c", "print('ok')"]

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


_WORKFLOW_YAML = """
name: review
inputs: [target]
phases:
  - run: fan_out
    clients: [a]
    goal: 'check {target}'
  - run: collect
"""


def _svc(repo: Path, backend: _Worker | None = None) -> tuple[MarshalService, _Worker]:
    worker = backend or _Worker()
    cfg = FleetConfig(clients={"a": ClientConfig(name="a", backend="worker")})
    return MarshalService(repo, cfg, backends={"worker": worker}), worker


def _write_workflow(repo: Path, *, name: str = "review") -> Path:
    d = repo / "workflows"
    d.mkdir(exist_ok=True)
    path = d / f"{name}.yaml"
    path.write_text(_WORKFLOW_YAML, encoding="utf-8")
    return path


def test_run_workflow_refuses_an_absolute_path_outside_the_workspace(
    repo: Path, tmp_path: Path
) -> None:
    """A workflow is a recipe for the fleet; it must come from this repo's workflows/ dir."""
    _write_workflow(repo)
    outside = tmp_path / "evil.yaml"
    outside.write_text(_WORKFLOW_YAML, encoding="utf-8")
    svc, worker = _svc(repo)
    with pytest.raises(ConfigError, match="outside"):
        svc.run_workflow(str(outside), {"target": "x"})
    assert worker.invocations == 0


def test_run_workflow_refuses_a_traversal_path(repo: Path) -> None:
    _write_workflow(repo)
    svc, worker = _svc(repo)
    with pytest.raises(ConfigError, match="outside"):
        svc.run_workflow("../../evil.yaml", {"target": "x"})
    assert worker.invocations == 0


def test_run_workflow_accepts_a_recipe_inside_workflows(repo: Path) -> None:
    """REGRESSION: containment must not reject a legitimate in-repo workflow."""
    _write_workflow(repo)
    svc, worker = _svc(repo)
    result = svc.run_workflow("review", {"target": "src/x.py"})
    assert result.status in ("completed", "awaiting_review", "failed", "partial")
    assert worker.invocations >= 1
