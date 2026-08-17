"""Tests for `marshal doctor` preflight checks (fake backends, real tmp git repos, no network)."""

from __future__ import annotations

import subprocess
from datetime import datetime, timedelta, timezone
from pathlib import Path

import pytest

from marshal_engine.backends.base import CodingAgentBackend
from marshal_engine.core.layout import runs_dir
from marshal_engine.core.types import (
    AgentResult,
    Capabilities,
    PermissionFidelity,
    PermissionMode,
    RunOpts,
    RunStatus,
    TaskSpec,
)
from marshal_engine.interfaces.doctor import FAIL, OK, WARN, run_checks, summarize
from marshal_engine.runtime.state import FleetState, RunRecord


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
        credential_env_vars: tuple[str, ...] = (),
        unavailable_detail: str = "CLI not on PATH / not runnable",
    ) -> None:
        self.name = name
        self.binary = name
        self.capabilities = Capabilities(permission_fidelity=permission_fidelity)
        self.credential_env_vars = credential_env_vars
        self._available = available
        self._account = account
        self._verifies_auth = verifies_auth
        self._unavailable_detail = (
            "binary not found in PATH" if name == "goose" else unavailable_detail
        )

    def check_available(self) -> bool:
        return self._available

    def unavailable_detail(self) -> str:
        return self._unavailable_detail

    def account_info(self) -> dict[str, str] | None:
        return self._account

    def verifies_auth(self) -> bool:
        return self._verifies_auth

    def build_invocation(self, task: TaskSpec, opts: RunOpts) -> list[str]:
        return [self.binary]

    def map_permission(self, mode: PermissionMode) -> list[str]:
        return []

    def parse_output(self, raw_stdout: str, raw_stderr: str, exit_code: int) -> AgentResult:
        return AgentResult(status=RunStatus.EXITED_CLEAN, exit_code=exit_code)


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
    hooks = _by_name(checks, "integrate-hooks")
    assert hooks.status == WARN
    assert "integrate_run_hooks" in (hooks.fix or "")
    assert "--no-verify" in hooks.detail
    assert "agent-modified" in (hooks.fix or "")
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
    assert "agent-modified" in hooks.detail
    assert "non-interactive" in (hooks.fix or "")
    assert "deadlock" in hooks.detail or "prompting" in hooks.detail


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
    assert "after the agent" in unsafe.detail
    assert "agent-modified" in unsafe.detail
    assert _by_name(checks, "budgets").status == WARN
    assert "advisory" in _by_name(checks, "budgets").detail
    fails, _ = summarize(checks)
    assert fails == 0


def test_doctor_setup_only_does_not_claim_post_agent_verify(tmp_path: Path) -> None:
    """worktree_setup is pre-agent; doctor must not attribute verify's timing hazard to it."""
    repo = _git_repo(tmp_path / "repo")
    body = """
clients:
  impl:
    backend: opencode
    model: opencode-go/glm-5.2
worktree_setup: uv sync
"""
    cfg = _write_config(tmp_path / "fleet.config.yaml", body)
    checks = run_checks(repo, cfg, backends={"opencode": _FakeBackend("opencode", available=True)})
    unsafe = _by_name(checks, "unsafe-commands")
    assert unsafe.status == WARN
    assert "worktree_setup" in unsafe.detail
    assert "verify" not in unsafe.detail
    assert "after the agent" not in unsafe.detail
    assert "agent-modified" not in unsafe.detail
    assert "agent-modified" not in (unsafe.fix or "")


def test_doctor_fails_when_setup_needs_opt_in(tmp_path: Path) -> None:
    """Non-allowlisted worktree_setup without opt-in is a config FAIL (load-time refusal)."""
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
    config = _by_name(checks, "config")
    assert config.status == FAIL
    assert "allowlist" in config.detail or "allow_unsafe_commands" in config.detail
    # Config never loaded — hygiene advisories and backend checks are skipped.
    assert "unsafe-commands" not in _names(checks)
    assert not any(n.startswith("backend:") for n in _names(checks))


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
    assert "review collect_run" not in (unsafe.fix or "")


def test_doctor_allow_unsafe_plus_verify_hint_matches_allowlisted_verify(tmp_path: Path) -> None:
    """Arbitrary argv + post-agent verify must carry at least the allowlisted+verify integrate hint."""
    repo = _git_repo(tmp_path / "repo")
    body = """
clients:
  impl:
    backend: opencode
    model: opencode-go/glm-5.2
worktree_setup: sh -c "uv sync"
verify: sh -c "uv run pytest -q"
allow_unsafe_commands: true
"""
    cfg = _write_config(tmp_path / "fleet.config.yaml", body)
    checks = run_checks(repo, cfg, backends={"opencode": _FakeBackend("opencode", available=True)})
    unsafe = _by_name(checks, "unsafe-commands")
    assert unsafe.status == WARN
    assert "after the agent" in unsafe.detail
    assert "agent-modified" in unsafe.detail
    assert "review collect_run" in (unsafe.fix or "")
    assert "CI before integrate" in (unsafe.fix or "")


def test_missing_backend_cli_fails(tmp_path: Path) -> None:
    repo = _git_repo(tmp_path / "repo")
    cfg = _write_config(tmp_path / "fleet.config.yaml", _CONFIG)
    checks = run_checks(repo, cfg, backends={"opencode": _FakeBackend("opencode", available=False)})

    backend = _by_name(checks, "backend:opencode")
    assert backend.status == FAIL
    assert "opencode auth login" in backend.fix
    fails, _ = summarize(checks)
    assert fails >= 1


def test_too_old_agy_fails_with_version_floor(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    """Found-but-too-old agy must FAIL with the ≥ 1.1.8 floor named — not OK / not 'not on PATH'."""
    import marshal_engine.backends.antigravity as agy_mod
    from marshal_engine.backends.antigravity import AntigravityBackend

    repo = _git_repo(tmp_path / "repo")
    cfg = _write_config(
        tmp_path / "fleet.config.yaml",
        "clients:\n  agy:\n    backend: antigravity\n",
    )
    monkeypatch.setattr(agy_mod.shutil, "which", lambda _b: "/usr/bin/agy")

    class _Proc:
        returncode = 0
        stdout = "1.1.7"
        stderr = ""

    monkeypatch.setattr(agy_mod.subprocess, "run", lambda *_a, **_k: _Proc())
    checks = run_checks(repo, cfg, backends={"antigravity": AntigravityBackend()})
    backend = _by_name(checks, "backend:antigravity")
    assert backend.status == FAIL
    assert "1.1.7" in backend.detail
    assert "1.1.8" in backend.detail
    assert "too old" in backend.detail
    assert "not on PATH" not in backend.detail
    assert "1.1.8" in (backend.fix or "")


def test_probe_missing_from_snapshot_constructs_fresh_backend(tmp_path: Path, monkeypatch) -> None:
    # A service built before this backend was configured hands doctor a snapshot without it.
    # Doctor must probe a freshly constructed backend (the same path a spawn takes), not FAIL on
    # the stale snapshot.
    import marshal_engine.interfaces.doctor as doctor_mod

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
    # Match real OpenCode: OPENCODE_API_KEY is on the credential allowlist and is forwarded.
    checks = run_checks(
        repo,
        cfg,
        backends={
            "opencode": _FakeBackend(
                "opencode", available=True, credential_env_vars=("OPENCODE_API_KEY",)
            )
        },
    )
    assert _by_name(checks, "secret:impl").status == OK
    assert _by_name(checks, "child-env:opencode").status == OK
    assert "OPENCODE_API_KEY=set→forwarded" in _by_name(checks, "child-env:opencode").detail
    assert "child-env-secret:impl" not in _names(checks)


def test_doctor_warns_when_secret_ref_var_is_not_forwarded(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    monkeypatch.setenv("OPENCODE_API_KEY", "sk-test-long")
    repo = _git_repo(tmp_path / "repo")
    cfg = _write_config(tmp_path / "fleet.config.yaml", _CONFIG)
    # Fake with empty credential allowlist: secret_ref var is set but will not reach the child.
    checks = run_checks(repo, cfg, backends={"opencode": _FakeBackend("opencode", available=True)})
    warn = _by_name(checks, "child-env-secret:impl")
    assert warn.status == WARN
    assert "NOT on the opencode credential allowlist" in warn.detail


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


_GOOSE_CONFIG = """
clients:
  impl:
    backend: goose
    model: cursor-agent/auto
"""


def test_doctor_goose_unavailable_fails_with_binary_not_found(tmp_path: Path) -> None:
    repo = _git_repo(tmp_path / "repo")
    cfg = _write_config(tmp_path / "fleet.config.yaml", _GOOSE_CONFIG)
    backend = _FakeBackend("goose", available=False)
    checks = run_checks(repo, cfg, backends={"goose": backend})
    b = _by_name(checks, "backend:goose")
    assert b.status == FAIL
    assert b.detail == "binary not found in PATH"


def test_doctor_goose_available_unauthenticated_warns(tmp_path: Path) -> None:
    repo = _git_repo(tmp_path / "repo")
    cfg = _write_config(tmp_path / "fleet.config.yaml", _GOOSE_CONFIG)
    backend = _FakeBackend("goose", available=True, verifies_auth=False)
    checks = run_checks(repo, cfg, backends={"goose": backend})
    b = _by_name(checks, "backend:goose")
    assert b.status == WARN
    assert "available (authentication missing / run 'goose configure')" in b.detail


def test_doctor_goose_available_and_authenticated_is_ok(tmp_path: Path) -> None:
    repo = _git_repo(tmp_path / "repo")
    cfg = _write_config(tmp_path / "fleet.config.yaml", _GOOSE_CONFIG)
    backend = _FakeBackend(
        "goose", available=True, account={"plan": "anthropic", "model": "claude-sonnet"}, verifies_auth=True
    )
    checks = run_checks(repo, cfg, backends={"goose": backend})
    b = _by_name(checks, "backend:goose")
    assert b.status == OK
    assert b.detail == "available & authenticated"
    assert _by_name(checks, "plan:goose").detail == "anthropic (model claude-sonnet)"



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
    assert "backend safe-edit" in perm.detail  # backend capability, not per-client
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
    assert "backend safe-edit" in perm.detail  # backend capability, not per-client
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


def test_doctor_flags_a_team_whose_reviewer_can_write(tmp_path: Path) -> None:
    """A team that can never run should surface at preflight, not when you reach for the panel."""
    repo = tmp_path / "repo"
    (repo / "teams").mkdir(parents=True)
    (repo / "fleet.config.yaml").write_text(
        "clients:\n  rw:\n    backend: cursor\n    permission: safe-edit\n"
    )
    (repo / "teams" / "gate.yaml").write_text(
        "target: plan\nroles:\n"
        "  - name: a\n    client: rw\n    rubric: x\n"
        "  - name: b\n    client: rw\n    rubric: y\n"
    )
    checks = run_checks(repo, repo / "fleet.config.yaml", backends={})
    teams = [c for c in checks if c.name == "teams"]
    assert teams and teams[0].status == WARN
    assert "must be read-only" in teams[0].detail
    # Optional feature: a broken team never fails the whole preflight.
    assert not any(c.status == FAIL and c.name == "teams" for c in checks)


def test_doctor_reports_valid_teams_as_ok(tmp_path: Path) -> None:
    repo = tmp_path / "repo"
    (repo / "teams").mkdir(parents=True)
    (repo / "fleet.config.yaml").write_text(
        "clients:\n"
        "  ro-a:\n    backend: cursor\n    permission: read-only\n"
        "  ro-b:\n    backend: cursor\n    permission: read-only\n"
    )
    (repo / "teams" / "gate.yaml").write_text(
        "target: plan\nroles:\n"
        "  - name: a\n    client: ro-a\n    rubric: x\n"
        "  - name: b\n    client: ro-b\n    rubric: y\n"
    )
    checks = run_checks(repo, repo / "fleet.config.yaml", backends={})
    teams = [c for c in checks if c.name == "teams"]
    assert teams and teams[0].status == OK


def test_doctor_surfaces_an_unparseable_team_file(tmp_path: Path) -> None:
    """A team file that doesn't even parse must name itself here, not only via list_teams."""
    repo = tmp_path / "repo"
    (repo / "teams").mkdir(parents=True)
    (repo / "fleet.config.yaml").write_text("clients: {}\n")
    (repo / "teams" / "broken.yaml").write_text("roles: not-a-list\n")
    checks = run_checks(repo, repo / "fleet.config.yaml", backends={})
    teams = [c for c in checks if c.name == "teams"]
    assert teams and teams[0].status == WARN
    assert "broken.yaml" in teams[0].detail


def test_doctor_omits_the_teams_check_when_no_teams_exist(tmp_path: Path) -> None:
    """No teams/ dir is the common case; don't add a row for a feature nobody opted into."""
    repo = tmp_path / "repo"
    repo.mkdir()
    (repo / "fleet.config.yaml").write_text("clients: {}\n")
    checks = run_checks(repo, repo / "fleet.config.yaml", backends={})
    assert not [c for c in checks if c.name == "teams"]


def _quota_repo(tmp_path: Path) -> Path:
    repo = tmp_path / "repo"
    repo.mkdir()
    (repo / "fleet.config.yaml").write_text("clients: {}\n")
    return repo


def _record(**kw: object) -> RunRecord:
    base = {"task_id": "t", "backend": "cursor", "status": "failed"}
    return RunRecord(**{**base, **kw})  # type: ignore[arg-type]


def test_doctor_warns_when_recent_runs_failed_on_billing(tmp_path: Path) -> None:
    """`doctor` green used to mean "CLI present and authed", which two field reports independently
    mistook for "ready to run" - both found an exhausted balance only by spending a run. The
    ledger already knows; surface it."""
    repo = _quota_repo(tmp_path)
    runs = FleetState(runs_dir(repo))
    now = datetime.now(timezone.utc)
    runs.add(_record(
        run_id="a.cursor.1",
        error="cursor-agent: Insufficient balance. Please top up.",
        ended_at=(now - timedelta(hours=2)).isoformat(),
    ))
    runs.add(_record(
        run_id="b.cursor.2",
        error="quota exceeded for premium models",
        ended_at=(now - timedelta(hours=1)).isoformat(),
    ))

    checks = run_checks(repo, repo / "fleet.config.yaml", backends={})
    quota = [c for c in checks if c.name == "quota:cursor"]
    assert quota and quota[0].status == WARN
    assert "2 run(s)" in quota[0].detail
    assert "quota exceeded" in quota[0].detail, "should quote the most recent failure"


def test_doctor_stays_silent_when_no_billing_failures_were_recorded(tmp_path: Path) -> None:
    """Silence means "nothing was seen", and must never be dressed up as "quota verified healthy" -
    doctor cannot read provider balances, and claiming otherwise is the overclaim being fixed."""
    repo = _quota_repo(tmp_path)
    runs = FleetState(runs_dir(repo))
    runs.add(_record(
        run_id="a.cursor.1",
        error="AssertionError: expected 2 got 3",  # a real task failure, not billing
        ended_at=datetime.now(timezone.utc).isoformat(),
    ))
    checks = run_checks(repo, repo / "fleet.config.yaml", backends={})
    assert not [c for c in checks if c.name.startswith("quota:")]


def test_doctor_ignores_billing_failures_outside_the_window(tmp_path: Path) -> None:
    """A balance topped up last week should not still be shouting."""
    repo = _quota_repo(tmp_path)
    runs = FleetState(runs_dir(repo))
    runs.add(_record(
        run_id="old.cursor.1",
        error="Insufficient balance",
        ended_at=(datetime.now(timezone.utc) - timedelta(days=7)).isoformat(),
    ))
    checks = run_checks(repo, repo / "fleet.config.yaml", backends={})
    assert not [c for c in checks if c.name.startswith("quota:")]


def test_doctor_does_not_treat_rate_limiting_as_a_billing_problem(tmp_path: Path) -> None:
    """A 429 means *slow down*, not *pay*. The retry policy already backs off and retries it, and
    telling an operator to top up or switch providers over throttling sends them to the wrong
    remedy - a false positive here costs the same detour the check exists to prevent."""
    repo = _quota_repo(tmp_path)
    runs = FleetState(runs_dir(repo))
    now = datetime.now(timezone.utc)
    runs.add(_record(
        run_id="rl.cursor.1",
        error="HTTP 429: rate limit exceeded, retry after 20s",
        ended_at=now.isoformat(),
    ))
    checks = run_checks(repo, repo / "fleet.config.yaml", backends={})
    assert not [c for c in checks if c.name.startswith("quota:")]
