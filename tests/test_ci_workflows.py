"""Contract tests for the GitHub Actions workflows - lock the CI/release hardening in source.

These parse the workflow YAML and assert *structural* hardening properties (least-privilege
tokens, pinned actions, frozen installs, the tested Python floor) so a future edit that loosens
them trips a test instead of silently shipping. Distinct from ``test_workflow.py``, which covers
Marshal's own declarative *workflow* feature.
"""

from __future__ import annotations

import re
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
    # PyPI Trusted Publishing needs id-token:write; contents stay read-only (no broad write token).
    assert _load(_RELEASE).get("permissions") == {
        "contents": "read",
        "id-token": "write",
    }


def test_release_workflow_is_not_triggered_by_branch_push() -> None:
    # Publishing must be human-gated: published GitHub Release and/or workflow_dispatch only.
    # PyYAML parses the workflow key `on:` as boolean True.
    wf = _load(_RELEASE)
    on = wf.get("on", wf.get(True))
    assert isinstance(on, dict)
    assert "push" not in on
    assert "pull_request" not in on
    assert "release" in on
    assert "workflow_dispatch" in on
    release_types = on["release"].get("types") if isinstance(on["release"], dict) else None
    assert release_types == ["published"]


def test_release_publishes_with_trusted_publishing_not_a_token_secret() -> None:
    # OIDC trusted publishing: use gh-action-pypi-publish and never wire a PyPI API token secret.
    wf = _load(_RELEASE)
    steps = _steps(wf)
    publish_steps = [s for s in steps if "pypi-publish" in str(s.get("uses") or "")]
    assert publish_steps, "release.yml must publish with pypa/gh-action-pypi-publish"
    run_blob = "\n".join(str(s.get("run") or "") for s in steps)
    assert "PYPI_API_TOKEN" not in run_blob
    assert "TWINE_PASSWORD" not in run_blob
    for step in publish_steps:
        with_block = step.get("with") or {}
        for key in ("password", "user", "api-token"):
            assert key not in with_block
    env = wf["jobs"]["release"].get("environment")
    assert env == "pypi" or (isinstance(env, dict) and env.get("name") == "pypi")


def test_all_workflow_actions_are_pinned() -> None:
    # Every third-party action must carry a version/sha ref - no floating refs.
    for wf_path in (_CI, _RELEASE):
        for step in _steps(_load(wf_path)):
            uses = step.get("uses")
            if uses is not None:
                assert "@" in uses, f"{wf_path.name}: action {uses!r} is not pinned"


def test_release_actions_are_pinned_to_commit_shas() -> None:
    # Stricter than the repo-wide pin rule: every step in the release job runs alongside
    # `id-token: write`, so a mutable tag anywhere in it can reach the publishing credential.
    for step in _steps(_load(_RELEASE)):
        uses = step.get("uses")
        if uses is None:
            continue
        ref = str(uses).split("@", 1)[-1]
        assert re.fullmatch(r"[0-9a-f]{40}", ref), (
            f"release.yml: {uses!r} must pin a full commit SHA, not a mutable tag"
        )


def test_release_refuses_a_ref_whose_version_is_not_the_tag() -> None:
    # workflow_dispatch accepts ANY ref, and a publish cannot be undone: the run must verify it is
    # on a tag and that the built version matches it before the publish step.
    steps = _steps(_load(_RELEASE))
    publish_at = next(
        i for i, s in enumerate(steps) if "pypi-publish" in str(s.get("uses") or "")
    )
    guards = [
        i for i, s in enumerate(steps)
        if "github.ref_type" in str(s.get("run") or "")
        and "__version__" in str(s.get("run") or "")
    ]
    assert guards, "release.yml must verify ref_type and the built version before publishing"
    assert min(guards) < publish_at, "the version/tag guard must run BEFORE the publish step"


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
