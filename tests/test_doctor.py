"""Tests for `marshal doctor` preflight checks (fake backends, real tmp git repos, no network)."""

from __future__ import annotations

import subprocess
from pathlib import Path

from marshal_engine.backends.base import CodingAgentBackend
from marshal_engine.doctor import FAIL, OK, WARN, run_checks, summarize
from marshal_engine.types import (
    AgentResult,
    Capabilities,
    PermissionFidelity,
    PermissionMode,
    RunOpts,
    RunStatus,
    TaskSpec,
)


class _FakeBackend(CodingAgentBackend):
    """Backend whose availability is fixed; only `check_available` matters to the doctor."""

    def __init__(
        self,
        name: str,
        *,
        available: bool,
        account: dict[str, str] | None = None,
        verifies_auth: bool = False,
        permission_fidelity: PermissionFidelity = PermissionFidelity.BOUNDARY_ONLY,
    ) -> None:
        self.name = name
        self.binary = name
        self.capabilities = Capabilities(permission_fidelity=permission_fidelity)
        self._available = available
        self._account = account
        self._verifies_auth = verifies_auth

    def check_available(self) -> bool:
        return self._available

    def account_info(self) -> dict[str, str] | None:
        return self._account

    def verifies_auth(self) -> bool:
        return self._verifies_auth

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
    # Hygiene advisories are warnings, never failures.
    assert _by_name(checks, "integrate-hooks").status == WARN
    assert "integrate_run_hooks" in (_by_name(checks, "integrate-hooks").fix or "")
    fails, _ = summarize(checks)
    assert fails == 0


def test_doctor_warns_when_integrate_run_hooks_opted_in(tmp_path: Path) -> None:
    repo = _git_repo(tmp_path / "repo")
    body = """
clients:
  impl:
    backend: opencode
    model: opencode-go/glm-5.2
integrate_run_hooks: true
"""
    cfg = _write_config(tmp_path / "fleet.config.yaml", body)
    checks = run_checks(repo, cfg, backends={"opencode": _FakeBackend("opencode", available=True)})
    hooks = _by_name(checks, "integrate-hooks")
    assert hooks.status == WARN
    assert "integrate_run_hooks: true" in hooks.detail
    assert "non-interactive" in (hooks.fix or "")


def test_doctor_warns_on_unsafe_commands_and_advisory_budgets(tmp_path: Path) -> None:
    repo = _git_repo(tmp_path / "repo")
    body = """
clients:
  impl:
    backend: opencode
    model: opencode-go/glm-5.2
worktree_setup: uv sync
verify: uv run pytest -q
budgets:
  - window: week
    limit_usd: 5.0
"""
    cfg = _write_config(tmp_path / "fleet.config.yaml", body)
    checks = run_checks(repo, cfg, backends={"opencode": _FakeBackend("opencode", available=True)})
    unsafe = _by_name(checks, "unsafe-commands")
    assert unsafe.status == WARN
    assert "worktree_setup" in unsafe.detail
    assert "allowlisted" in unsafe.detail
    assert _by_name(checks, "budgets").status == WARN
    assert "advisory" in _by_name(checks, "budgets").detail
    fails, _ = summarize(checks)
    assert fails == 0


def test_doctor_warns_when_setup_needs_opt_in(tmp_path: Path) -> None:
    repo = _git_repo(tmp_path / "repo")
    body = """
clients:
  impl:
    backend: opencode
    model: opencode-go/glm-5.2
worktree_setup: sh -c "uv sync"
"""
    cfg = _write_config(tmp_path / "fleet.config.yaml", body)
    checks = run_checks(repo, cfg, backends={"opencode": _FakeBackend("opencode", available=True)})
    unsafe = _by_name(checks, "unsafe-commands")
    assert unsafe.status == WARN
    assert "non-allowlisted" in unsafe.detail
    assert "allow_unsafe_commands" in unsafe.detail


def test_doctor_warns_when_allow_unsafe_opted_in(tmp_path: Path) -> None:
    repo = _git_repo(tmp_path / "repo")
    body = """
clients:
  impl:
    backend: opencode
    model: opencode-go/glm-5.2
worktree_setup: sh -c "uv sync"
allow_unsafe_commands: true
"""
    cfg = _write_config(tmp_path / "fleet.config.yaml", body)
    checks = run_checks(repo, cfg, backends={"opencode": _FakeBackend("opencode", available=True)})
    unsafe = _by_name(checks, "unsafe-commands")
    assert unsafe.status == WARN
    assert "allow_unsafe_commands: true" in unsafe.detail
    assert "arbitrary argv" in unsafe.detail


def test_missing_backend_cli_fails(tmp_path: Path) -> None:
    repo = _git_repo(tmp_path / "repo")
    cfg = _write_config(tmp_path / "fleet.config.yaml", _CONFIG)
    checks = run_checks(repo, cfg, backends={"opencode": _FakeBackend("opencode", available=False)})

    backend = _by_name(checks, "backend:opencode")
    assert backend.status == FAIL
    assert "opencode auth login" in backend.fix
    fails, _ = summarize(checks)
    assert fails >= 1


def test_probe_missing_from_snapshot_constructs_fresh_backend(tmp_path: Path, monkeypatch) -> None:
    # A service built before this backend was configured hands doctor a snapshot without it.
    # Doctor must probe a freshly constructed backend (the same path a spawn takes), not FAIL on
    # the stale snapshot.
    import marshal_engine.doctor as doctor_mod

    repo = _git_repo(tmp_path / "repo")
    cfg = _write_config(tmp_path / "fleet.config.yaml", _CONFIG)
    monkeypatch.setattr(
        doctor_mod, "make_backend", lambda name: _FakeBackend(name, available=True)
    )
    checks = run_checks(repo, cfg, backends={})  # empty snapshot: the config's backend is absent

    assert _by_name(checks, "backend:opencode").status == OK


def test_unknown_backend_name_fails_distinctly(tmp_path: Path) -> None:
    repo = _git_repo(tmp_path / "repo")
    cfg = _write_config(
        tmp_path / "fleet.config.yaml", "clients:\n  x:\n    backend: no-such-backend\n"
    )
    checks = run_checks(repo, cfg, backends={})

    backend = _by_name(checks, "backend:no-such-backend")
    assert backend.status == FAIL
    assert backend.detail == "unknown backend name"  # not the misleading "CLI not on PATH"


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


def test_present_but_unauthenticated_backend_fails(tmp_path: Path) -> None:
    # A backend whose account_info() is an authenticated-only probe (verifies_auth=True): CLI is
    # present but returns no account info -> not logged in. Doctor must FAIL it, not green-light it.
    repo = _git_repo(tmp_path / "repo")
    cfg = _write_config(tmp_path / "fleet.config.yaml", _CONFIG)
    backend = _FakeBackend("opencode", available=True, account=None, verifies_auth=True)
    checks = run_checks(repo, cfg, backends={"opencode": backend})
    b = _by_name(checks, "backend:opencode")
    assert b.status == FAIL
    assert "not authenticated" in b.detail
    assert "opencode auth login" in b.fix
    assert "plan:opencode" not in _names(checks)


def test_authenticated_probe_backend_is_ok_with_plan(tmp_path: Path) -> None:
    # verifies_auth=True AND account info present -> authenticated: OK + a plan line.
    repo = _git_repo(tmp_path / "repo")
    cfg = _write_config(tmp_path / "fleet.config.yaml", _CONFIG)
    backend = _FakeBackend(
        "opencode", available=True, account={"plan": "Ultra", "model": "x"}, verifies_auth=True
    )
    checks = run_checks(repo, cfg, backends={"opencode": backend})
    assert _by_name(checks, "backend:opencode").status == OK
    assert _by_name(checks, "plan:opencode").detail == "Ultra (model x)"


def test_no_auth_probe_backend_stays_available_without_account(tmp_path: Path) -> None:
    # verifies_auth=False (the default for most backends): a None account_info is "no plan info",
    # not "unauthenticated" - the CLI is still reported available (auth simply not verified).
    repo = _git_repo(tmp_path / "repo")
    cfg = _write_config(tmp_path / "fleet.config.yaml", _CONFIG)
    checks = run_checks(repo, cfg, backends={"opencode": _FakeBackend("opencode", available=True)})
    assert _by_name(checks, "backend:opencode").status == OK


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
    assert "permission:opencode" in _names(checks)
    assert "permission:cursor" not in _names(checks)


def test_permission_fidelity_enforced_denies_is_ok(tmp_path: Path) -> None:
    repo = _git_repo(tmp_path / "repo")
    cfg = _write_config(tmp_path / "fleet.config.yaml", _CONFIG)
    checks = run_checks(
        repo,
        cfg,
        backends={
            "opencode": _FakeBackend(
                "opencode",
                available=True,
                permission_fidelity=PermissionFidelity.ENFORCED_DENIES,
            )
        },
    )
    perm = _by_name(checks, "permission:opencode")
    assert perm.status == OK
    assert "enforced-denies" in perm.detail
    assert "worktree" in perm.detail


def test_permission_fidelity_boundary_only_warns(tmp_path: Path) -> None:
    repo = _git_repo(tmp_path / "repo")
    cfg = _write_config(tmp_path / "fleet.config.yaml", _CONFIG)
    checks = run_checks(
        repo,
        cfg,
        backends={
            "opencode": _FakeBackend(
                "opencode",
                available=True,
                permission_fidelity=PermissionFidelity.BOUNDARY_ONLY,
            )
        },
    )
    perm = _by_name(checks, "permission:opencode")
    assert perm.status == WARN
    assert "boundary-only" in perm.detail
    assert "worktree" in perm.detail
    assert perm.fix  # actionable guidance
    fails, _ = summarize(checks)
    assert fails == 0  # warning, never a failure


def test_permission_fidelity_present_when_cli_unavailable(tmp_path: Path) -> None:
    repo = _git_repo(tmp_path / "repo")
    cfg = _write_config(tmp_path / "fleet.config.yaml", _CONFIG)
    checks = run_checks(
        repo,
        cfg,
        backends={
            "opencode": _FakeBackend(
                "opencode",
                available=False,
                permission_fidelity=PermissionFidelity.ENFORCED_DENIES,
            )
        },
    )
    assert _by_name(checks, "backend:opencode").status == FAIL
    perm = _by_name(checks, "permission:opencode")
    assert perm.status == OK
    assert "enforced-denies" in perm.detail


def test_unknown_backend_has_no_fidelity_check(tmp_path: Path) -> None:
    repo = _git_repo(tmp_path / "repo")
    cfg = _write_config(
        tmp_path / "fleet.config.yaml", "clients:\n  x:\n    backend: no-such-backend\n"
    )
    checks = run_checks(repo, cfg, backends={})
    assert _by_name(checks, "backend:no-such-backend").detail == "unknown backend name"
    assert "permission:no-such-backend" not in _names(checks)
