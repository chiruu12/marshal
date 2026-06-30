"""Contract tests for the GitHub Actions workflows - lock the CI/release hardening in source.

These parse the workflow YAML and assert *structural* hardening properties (least-privilege
tokens, pinned actions, frozen installs, the tested Python floor) so a future edit that loosens
them trips a test instead of silently shipping. Distinct from ``test_workflow.py``, which covers
Marshal's own declarative *workflow* feature.
"""

from __future__ import annotations

from pathlib import Path
from typing import Any

import yaml

import marshal_engine

_WF_DIR = Path(marshal_engine.__file__).resolve().parents[2] / ".github" / "workflows"
_CI = _WF_DIR / "ci.yml"
_RELEASE = _WF_DIR / "release.yml"


def _load(path: Path) -> dict[str, Any]:
    data = yaml.safe_load(path.read_text(encoding="utf-8"))
    assert isinstance(data, dict)
    return data


def _steps(wf: dict[str, Any]) -> list[dict[str, Any]]:
    steps: list[dict[str, Any]] = []
    for job in wf.get("jobs", {}).values():
        steps.extend(job.get("steps", []))
    return steps


def test_ci_workflow_is_least_privilege() -> None:
    # Hardening: CI only reads the repo; it must never carry a default-broad write token.
    assert _load(_CI).get("permissions") == {"contents": "read"}


def test_release_workflow_permissions_are_minimal() -> None:
    # Release creates a GitHub Release (needs contents:write) and nothing more.
    assert _load(_RELEASE).get("permissions") == {"contents": "write"}


def test_all_workflow_actions_are_pinned() -> None:
    # Every third-party action must carry a version/sha ref - no floating refs.
    for wf_path in (_CI, _RELEASE):
        for step in _steps(_load(wf_path)):
            uses = step.get("uses")
            if uses is not None:
                assert "@" in uses, f"{wf_path.name}: action {uses!r} is not pinned"


def test_dependency_sync_is_frozen() -> None:
    # Reproducible installs: every `uv sync` pins the lockfile with --frozen.
    for wf_path in (_CI, _RELEASE):
        for step in _steps(_load(wf_path)):
            run = step.get("run") or ""
            if "uv sync" in run:
                assert "--frozen" in run, f"{wf_path.name}: a `uv sync` is missing --frozen"


def test_ci_matrix_tests_the_minimum_supported_python() -> None:
    # CLAUDE.md / pyproject pin Python >= 3.11; the floor must actually be exercised in CI.
    matrix = _load(_CI)["jobs"]["gate"]["strategy"]["matrix"]["python-version"]
    assert "3.11" in matrix


def test_ci_matrix_exercises_macos() -> None:
    # The engine's process-group logic (killpg/start_new_session/worktrees) is POSIX-specific;
    # macOS (the dev platform) must be exercised, not only Linux.
    matrix = _load(_CI)["jobs"]["gate"]["strategy"]["matrix"]
    oses = list(matrix.get("os", [])) + [inc.get("os") for inc in matrix.get("include", [])]
    assert any("macos" in (o or "") for o in oses), oses


def test_ci_enforces_a_coverage_floor() -> None:
    # A coverage gate must run in CI so an untested regression fails the build, not slips through.
    runs = " ".join(step.get("run") or "" for step in _steps(_load(_CI)))
    assert "--cov-fail-under" in runs


def test_release_enforces_a_coverage_floor() -> None:
    # The release gate matches CI: never cut a release under the coverage floor.
    runs = " ".join(step.get("run") or "" for step in _steps(_load(_RELEASE)))
    assert "--cov-fail-under" in runs
