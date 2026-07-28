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


def _is_version_guard(step: dict[str, Any]) -> bool:
    """The step that refuses to publish a ref whose version is not the tag.

    Identified by what it reads, not by its name: the ref type (via `env`, since a tag name is
    untrusted input) plus the built artifact under `dist/`.
    """
    run = str(step.get("run") or "")
    env = str(step.get("env") or "")
    return ("REF_TYPE" in run or "github.ref_type" in env) and "dist/" in run


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
    # The environment must be SET (it is what carries the required reviewer and the v*-tag-only
    # deployment policy). Which name it must be is asserted once, in the test below.
    env = wf["jobs"]["release"].get("environment")
    assert env, "the release job must run in a protected GitHub Environment"


def test_release_environment_name_matches_the_configured_one() -> None:
    """The environment name is case-sensitive and a mismatch fails SILENTLY: the run resolves to a
    different (non-existent) environment, so its required reviewers and `v*`-tag-only deployment
    policy do not apply and its secrets are not in scope. Everything that makes publishing safe
    hangs off this string matching, and nothing in CI would otherwise notice.

    The configured environment on this repo is `PYPI` (verified via the API). Change both together.
    """
    env = _load(_RELEASE)["jobs"]["release"].get("environment")
    name = env.get("name") if isinstance(env, dict) else env
    assert name == "PYPI", (
        f"release.yml targets environment {name!r}; the repo's configured environment is 'PYPI' "
        "and the names must match exactly"
    )


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


def test_no_release_step_interpolates_untrusted_ref_values_into_a_script() -> None:
    """A git tag may contain shell metacharacters. Substituting `github.ref_name` (or a
    `workflow_dispatch` input) into a `run:` body lets a crafted tag execute commands in a job that
    holds `id-token: write` - i.e. with reach to the publishing credential. Pass them via `env:`
    and read them as quoted variables instead."""
    untrusted = ("github.ref_name", "github.event.inputs", "inputs.", "github.head_ref")
    for step in _steps(_load(_RELEASE)):
        run = str(step.get("run") or "")
        for expr in untrusted:
            assert expr not in run, (
                f"release.yml step {step.get('name')!r} interpolates {expr!r} into a script; "
                "pass it through `env:` and reference it as a quoted variable"
            )


def test_release_verifies_the_version_of_the_built_artifact() -> None:
    """The guard must read the version from the built wheel, not the source tree. hatchling builds
    from `[project].version`; checking `marshal_engine.__version__` verifies a value the artifact
    does not necessarily carry, so a drift between them passes the check and publishes a version
    that does not match the tag - and a PyPI upload cannot be taken back."""
    guards = [s for s in _steps(_load(_RELEASE)) if _is_version_guard(s)]
    assert guards, "release.yml has no tag/version guard step"
    # Comments are stripped: the script may *explain* why it avoids the source version.
    body = "\n".join(
        line for s in guards for line in str(s.get("run") or "").splitlines()
        if not line.lstrip().startswith("#")
    )
    assert "dist/" in body and ".whl" in body, "the guard does not read the built wheel"
    assert "__version__" not in body, "the guard reads the source version, not the artifact's"


def test_release_refuses_a_ref_whose_version_is_not_the_tag() -> None:
    # workflow_dispatch accepts ANY ref, and a publish cannot be undone: the run must verify it is
    # on a tag and that the built version matches it before the publish step.
    steps = _steps(_load(_RELEASE))
    publish_at = next(
        i for i, s in enumerate(steps) if "pypi-publish" in str(s.get("uses") or "")
    )
    guards = [i for i, s in enumerate(steps) if _is_version_guard(s)]
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
