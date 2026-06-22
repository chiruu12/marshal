"""Tests for `marshal doctor` preflight checks (fake backends, real tmp git repos, no network)."""

from __future__ import annotations

import subprocess
from pathlib import Path

from marshal_engine.backends.base import CodingAgentBackend
from marshal_engine.doctor import FAIL, OK, WARN, run_checks, summarize
from marshal_engine.types import AgentResult, Capabilities, PermissionMode, RunOpts, RunStatus, TaskSpec


class _FakeBackend(CodingAgentBackend):
    """Backend whose availability is fixed; only `check_available` matters to the doctor."""

    capabilities = Capabilities()

    def __init__(
        self, name: str, *, available: bool, account: dict[str, str] | None = None
    ) -> None:
        self.name = name
        self.binary = name
        self._available = available
        self._account = account

    def check_available(self) -> bool:
        return self._available

    def account_info(self) -> dict[str, str] | None:
        return self._account

    def build_invocation(self, task: TaskSpec, opts: RunOpts) -> list[str]:
        return [self.binary]

    def map_permission(self, mode: PermissionMode) -> list[str]:
        return []

    def parse_output(self, raw_stdout: str, raw_stderr: str, exit_code: int) -> AgentResult:
        return AgentResult(status=RunStatus.SUCCEEDED, exit_code=exit_code)


def _git_repo(path: Path) -> Path:
    path.mkdir(parents=True, exist_ok=True)
    subprocess.run(["git", "init", "-q", str(path)], check=True)
    return path


def _by_name(checks: list, name: str):
    return next(c for c in checks if c.name == name)


def _names(checks: list) -> set[str]:
    return {c.name for c in checks}


def _write_config(path: Path, body: str) -> Path:
    path.write_text(body, encoding="utf-8")
    return path


_CONFIG = """
clients:
  impl:
    backend: opencode
    model: opencode-go/glm-5.2
    secret_ref: env:OPENCODE_API_KEY
"""


def test_happy_path_has_no_failures(tmp_path: Path, monkeypatch) -> None:
    monkeypatch.delenv("OPENCODE_API_KEY", raising=False)
    repo = _git_repo(tmp_path / "repo")
    cfg = _write_config(tmp_path / "fleet.config.yaml", _CONFIG)
    checks = run_checks(repo, cfg, backends={"opencode": _FakeBackend("opencode", available=True)})

    assert _by_name(checks, "python").status == OK
    assert _by_name(checks, "git").status == OK
    assert _by_name(checks, "repo").status == OK
    assert _by_name(checks, "config").status == OK
    assert _by_name(checks, "backend:opencode").status == OK
    # secret_ref is never injected, so an unset env var is a warning, not a failure.
    assert _by_name(checks, "secret:impl").status == WARN
    fails, _ = summarize(checks)
    assert fails == 0


def test_missing_backend_cli_fails(tmp_path: Path) -> None:
    repo = _git_repo(tmp_path / "repo")
    cfg = _write_config(tmp_path / "fleet.config.yaml", _CONFIG)
    checks = run_checks(repo, cfg, backends={"opencode": _FakeBackend("opencode", available=False)})

    backend = _by_name(checks, "backend:opencode")
    assert backend.status == FAIL
    assert "opencode auth login" in backend.fix
    fails, _ = summarize(checks)
    assert fails >= 1


def test_set_secret_is_ok(tmp_path: Path, monkeypatch) -> None:
    monkeypatch.setenv("OPENCODE_API_KEY", "sk-test")
    repo = _git_repo(tmp_path / "repo")
    cfg = _write_config(tmp_path / "fleet.config.yaml", _CONFIG)
    checks = run_checks(repo, cfg, backends={"opencode": _FakeBackend("opencode", available=True)})
    assert _by_name(checks, "secret:impl").status == OK


def test_bad_config_fails_and_skips_backend_checks(tmp_path: Path) -> None:
    repo = _git_repo(tmp_path / "repo")
    missing = tmp_path / "nope.yaml"
    checks = run_checks(repo, missing)

    assert _by_name(checks, "config").status == FAIL
    # No config means we can't know which backends matter - those checks are skipped.
    assert not any(n.startswith("backend:") for n in _names(checks))
    assert not any(n.startswith("secret:") for n in _names(checks))


def test_non_git_repo_fails(tmp_path: Path) -> None:
    plain = tmp_path / "plain"
    plain.mkdir()
    cfg = _write_config(tmp_path / "fleet.config.yaml", _CONFIG)
    checks = run_checks(plain, cfg, backends={"opencode": _FakeBackend("opencode", available=True)})
    assert _by_name(checks, "repo").status == FAIL


def test_account_info_surfaced_as_plan_check(tmp_path: Path) -> None:
    repo = _git_repo(tmp_path / "repo")
    cfg = _write_config(tmp_path / "fleet.config.yaml", _CONFIG)
    backend = _FakeBackend("opencode", available=True, account={"plan": "Pro", "model": "glm-5.2"})
    checks = run_checks(repo, cfg, backends={"opencode": backend})
    plan = _by_name(checks, "plan:opencode")
    assert plan.status == OK
    assert plan.detail == "Pro (model glm-5.2)"


def test_no_plan_check_when_account_info_absent_or_unavailable(tmp_path: Path) -> None:
    repo = _git_repo(tmp_path / "repo")
    cfg = _write_config(tmp_path / "fleet.config.yaml", _CONFIG)
    # available but no account_info -> no plan check
    none_acct = run_checks(repo, cfg, backends={"opencode": _FakeBackend("opencode", available=True)})
    assert "plan:opencode" not in _names(none_acct)
    # account_info present but backend unavailable -> not probed, no plan check
    unavail = run_checks(
        repo,
        cfg,
        backends={"opencode": _FakeBackend("opencode", available=False, account={"plan": "Pro"})},
    )
    assert "plan:opencode" not in _names(unavail)


def test_only_referenced_backends_are_probed(tmp_path: Path) -> None:
    repo = _git_repo(tmp_path / "repo")
    cfg = _write_config(tmp_path / "fleet.config.yaml", _CONFIG)
    # Provide a cursor backend too; config references only opencode, so cursor is not probed.
    checks = run_checks(
        repo,
        cfg,
        backends={
            "opencode": _FakeBackend("opencode", available=True),
            "cursor": _FakeBackend("cursor", available=False),
        },
    )
    assert "backend:opencode" in _names(checks)
    assert "backend:cursor" not in _names(checks)
