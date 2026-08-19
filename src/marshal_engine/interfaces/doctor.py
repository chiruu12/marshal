"""``marshal doctor`` - a preflight that tells you whether a fresh setup will actually run.

A new user's first failures are environmental, not Marshal's: a backend CLI that isn't installed,
a config that doesn't parse, an env var that isn't set, the wrong Python. This module runs each
check and returns a structured verdict; the CLI renders it. Checks are kept side-effect-light and
the backend probes are injectable so the whole thing is unit-testable without spawning processes.

Severity:
  * ``fail`` - Marshal will not work until this is fixed (missing git, unparseable config, a
    configured backend's CLI absent, a Fireworks model).
  * ``warn`` - likely fine but worth knowing (uv missing, the ``mcp`` extra absent, a
    ``secret_ref`` env var unset - which is OK if you authenticated the CLI via its own login).
  * ``ok`` - verified good.
"""

from __future__ import annotations

import importlib.util
import os
import shutil
import subprocess
import sys
from collections.abc import Mapping
from datetime import UTC, datetime, timedelta
from pathlib import Path

from pydantic import BaseModel

from ..backends.base import CodingAgentBackend
from ..core.config import (
    ConfigError,
    FleetConfig,
    load_config,
    resolve_secret,
    setup_command_refusal,
)
from ..core.layout import runs_dir
from ..core.types import PermissionFidelity
from ..orchestration.registry import default_backends, make_backend
from ..orchestration.teams import discover_teams, validate_team
from ..runtime.env import DETACHED_STDIO, is_base_env_var
from ..runtime.state import FleetState, RunRecord

OK = "ok"
WARN = "warn"
FAIL = "fail"

#: How to install + authenticate each backend's CLI (Marshal does not install them).
BACKEND_HINTS: dict[str, str] = {
    "opencode": "npm i -g opencode-ai  &&  opencode auth login",
    "cursor": "install the Cursor CLI (cursor-agent), then `cursor-agent login` (or set CURSOR_API_KEY)",
    "codex": "install the Codex CLI, then `codex login` (ChatGPT) or set OPENAI_API_KEY",
    "command-code": "npm i -g command-code, then `command-code login`",
    "antigravity": (
        "install the Antigravity CLI (agy) ≥ 1.1.12 (required for --mode and "
        "--output-format json), "
        "then complete its OAuth login"
    ),
    "claude-code": "install Claude Code (claude), then authenticate via its login or set ANTHROPIC_API_KEY",
    "goose": (
        "install Goose CLI (https://block.github.io/goose), then `goose configure`. "
        "For Cursor-backed runs use model `cursor-agent/<model>` and `cursor-agent login`. "
        "Headless needs GOOSE_MODE=auto (Marshal sets this)."
    ),
}

MIN_PYTHON = (3, 11)


#: Substrings that mark a run failure as "the provider refused because the account is out of money
#: or allowance", as opposed to the task being wrong. Matched case-insensitively against a run
#: record's `error`. Deliberately conservative: a false positive tells an operator their billing is
#: broken when it is not, which is its own wasted detour.
#:
#: Rate limiting is deliberately NOT here. "429" and "rate limit" mean *slow down*, not *pay* - the
#: retry policy already backs off and retries them, and pointing an operator at "top up or switch
#: providers" for throttling sends them to the wrong remedy entirely. This check is about money.
_QUOTA_MARKERS = (
    "insufficient balance",
    "insufficient credit",
    "insufficient_quota",
    "quota exceeded",
    "exceeded your current quota",
    "out of credits",
    "payment required",
    "billing",
    "subscription",
)

#: How far back the ledger is read for the quota check. Long enough to catch "I hit this earlier
#: today", short enough that last week's since-topped-up balance is not still shouting.
_QUOTA_WINDOW_HOURS = 24.0


def recent_quota_failures(
    repo: Path, *, now: datetime | None = None, hours: float = _QUOTA_WINDOW_HOURS
) -> dict[str, tuple[int, str]]:
    """Backends whose recent runs failed for billing/quota reasons -> (count, latest error).

    Derived entirely from the run ledger we already keep, so it needs no provider API and cannot
    itself be wrong about the provider's state: it reports what actually happened, not a prediction.
    A backend absent from the result has no such failures recorded - which is **not** the same as
    "its quota is healthy", and the check must never say otherwise.
    """
    cutoff = (now or datetime.now(UTC)) - timedelta(hours=hours)
    # backend -> (count, when of the newest, that run's error)
    seen: dict[str, tuple[int, datetime, str]] = {}
    for rec in FleetState(runs_dir(repo)).list():
        if not rec.error or not _looks_like_quota(rec.error):
            continue
        when = _ended_at(rec)
        if when is None or when < cutoff:
            continue
        count, newest_when, newest_error = seen.get(rec.backend, (0, cutoff, ""))
        if when >= newest_when or not newest_error:
            newest_when, newest_error = when, rec.error
        seen[rec.backend] = (count + 1, newest_when, newest_error)
    return {backend: (count, error) for backend, (count, _, error) in seen.items()}


def _one_line(text: str, limit: int = 120) -> str:
    """First line of an error, clipped - a doctor row is a summary, not a log."""
    first = _first_line(text)
    return first if len(first) <= limit else first[: limit - 1] + "\u2026"


def _looks_like_quota(error: str) -> bool:
    lowered = error.lower()
    return any(marker in lowered for marker in _QUOTA_MARKERS)


def _ended_at(rec: RunRecord) -> datetime | None:
    for stamp in (rec.ended_at, rec.started_at):
        if not stamp:
            continue
        try:
            parsed = datetime.fromisoformat(stamp)
        except ValueError:
            continue
        return parsed if parsed.tzinfo else parsed.replace(tzinfo=UTC)
    return None


class Check(BaseModel):
    """One preflight result. ``fix`` is shown only when ``status`` is not ``ok``."""

    name: str
    status: str
    detail: str
    fix: str = ""

    def __init__(self, name: str, status: str, detail: str, fix: str = "", /) -> None:
        super().__init__(name=name, status=status, detail=detail, fix=fix)


class DoctorReport(BaseModel):
    """Preflight verdict: per-check results plus a roll-up. ``ok`` is true when nothing failed."""

    checks: list[Check]
    fails: int
    warns: int
    ok: bool


def _first_line(text: str) -> str:
    text = text.strip()
    return text.splitlines()[0] if text else ""


def _format_plan(info: dict[str, str]) -> str:
    """Render a backend's account_info() into a one-line plan summary."""
    plan = info.get("plan", "?")
    model = info.get("model")
    return f"{plan} (model {model})" if model else plan


def _binary_version(binary: str, arg: str = "--version", timeout: float = 10.0) -> str | None:
    """Return the binary's version line, or None if it's missing / not runnable."""
    if shutil.which(binary) is None:
        return None
    try:
        proc = subprocess.run([binary, arg], capture_output=True, text=True, timeout=timeout, check=False, **DETACHED_STDIO)
    except (OSError, subprocess.SubprocessError):
        return None
    if proc.returncode != 0:
        return None
    return _first_line(proc.stdout or proc.stderr) or binary


def _git_repo_check(repo: Path) -> Check:
    """The target repo must be a git work tree with a branch checked out (integrate needs both)."""
    try:
        inside = subprocess.run(
            ["git", "-C", str(repo), "rev-parse", "--is-inside-work-tree"],
            capture_output=True,
            text=True,
            timeout=10,
            check=False,
            **DETACHED_STDIO,
        )
    except (OSError, subprocess.SubprocessError):
        return Check("repo", FAIL, f"{repo}: git not runnable", "install git")
    if inside.returncode != 0 or inside.stdout.strip() != "true":
        return Check(
            "repo", FAIL, f"{repo}: not a git work tree", "point MARSHAL_REPO at a git repo"
        )
    head = subprocess.run(
        ["git", "-C", str(repo), "symbolic-ref", "-q", "HEAD"],
        capture_output=True,
        text=True,
        timeout=10,
        check=False,
        **DETACHED_STDIO,
    )
    if head.returncode != 0:
        return Check(
            "repo", FAIL, f"{repo}: detached HEAD", "git checkout a branch (integrate refuses detached HEAD)"
        )
    branch = head.stdout.strip().rsplit("/", 1)[-1]
    return Check("repo", OK, f"{repo} (branch {branch})")


def _permission_fidelity_check(name: str, backend: CodingAgentBackend) -> Check:
    """Backend safe-edit capability — not the per-client resolved fidelity from ``list_clients``.

    ``permission:<backend>`` reports what this adapter's ``safe-edit`` installs. A configured
    client's ``yolo`` / ``read-only`` fidelity is resolved separately on ``list_clients`` (see
    ``resolve_permission_fidelity``). Independent of CLI availability/auth — fidelity is
    declared on the adapter, so the check still appears when the binary is missing.
    ``boundary-only`` warns; it never fails a doctor run (worktree isolation remains the
    dependable boundary either way).
    """
    fidelity = backend.capabilities.permission_fidelity
    if fidelity is PermissionFidelity.ENFORCED_DENIES:
        return Check(
            f"permission:{name}",
            OK,
            (
                f"backend safe-edit fidelity={fidelity.value}: backend/Marshal installs a "
                "restriction beyond the worktree; the worktree remains the isolation boundary"
            ),
        )
    return Check(
        f"permission:{name}",
        WARN,
        (
            f"backend safe-edit fidelity={fidelity.value}: no Marshal-enforced deny layer; "
            "the worktree and explicit integrate are the dependable boundary"
        ),
        (
            "prefer an enforced-denies backend (cursor/opencode/codex) for sensitive work, "
            "or treat this backend as worktree-isolated only"
        ),
    )


def run_checks(
    repo: Path,
    config_path: Path,
    *,
    backends: Mapping[str, CodingAgentBackend] | None = None,
) -> list[Check]:
    """Run every preflight check and return the results in display order."""
    checks: list[Check] = []

    # --- toolchain ---------------------------------------------------------------------------
    v = sys.version_info
    py_ok = (v.major, v.minor) >= MIN_PYTHON
    checks.append(
        Check(
            "python",
            OK if py_ok else FAIL,
            f"{v.major}.{v.minor}.{v.micro}",
            "" if py_ok else f"install Python >={MIN_PYTHON[0]}.{MIN_PYTHON[1]}",
        )
    )
    uv = _binary_version("uv")
    checks.append(
        Check("uv", OK if uv else WARN, uv or "not found", "" if uv else "install uv: https://docs.astral.sh/uv/")
    )
    git = _binary_version("git")
    checks.append(Check("git", OK if git else FAIL, git or "not found", "" if git else "install git"))

    checks.append(_git_repo_check(repo))

    # --- config ------------------------------------------------------------------------------
    config: FleetConfig | None = None
    try:
        config = load_config(config_path)
        n = len(config.clients)
        checks.append(
            Check("config", OK, f"{config_path} ({n} client{'' if n == 1 else 's'})")
        )
    except ConfigError as exc:
        checks.append(Check("config", FAIL, str(exc), "fix the config, then re-run `marshal doctor`"))

    # --- review teams ------------------------------------------------------------------------
    # A team that can never run (unknown client, a reviewer that isn't read-only) otherwise only
    # surfaces when you reach for the panel - which is exactly when you don't want to debug config.
    # WARN, never FAIL: teams are optional, and a broken one blocks no other Marshal work.
    if config is not None:
        listing = discover_teams(repo / "teams")
        problems: list[str] = [f"{name}: {msg}" for name, msg in sorted(listing.errors.items())]
        for spec in listing.teams:
            try:
                validate_team(spec, config)
            except ConfigError as exc:
                problems.append(str(exc))
        if listing.teams or listing.errors:
            checks.append(
                Check(
                    "teams",
                    WARN if problems else OK,
                    "; ".join(problems)
                    if problems
                    else f"{len(listing.teams)} team(s) in {repo / 'teams'}, all valid",
                    "fix the team file(s); reviewers must name clients with permission: read-only"
                    if problems
                    else "",
                )
            )

    # --- provider quota / billing (from the ledger, not a provider API) -----------------------
    # `doctor` green used to mean "CLI present and authed", which two field reports independently
    # mistook for "ready to run" - both discovered an exhausted balance by spending a run. A
    # backend can be installed, logged in, and out of credit. Only the failures actually recorded
    # are reported: silence here means nothing was seen, never that quota was checked and is fine.
    for backend_name, (count, last_error) in sorted(recent_quota_failures(repo).items()):
        checks.append(
            Check(
                f"quota:{backend_name}",
                WARN,
                f"{count} run(s) failed on billing/quota in the last "
                f"{int(_QUOTA_WINDOW_HOURS)}h - latest: {_one_line(last_error)}",
                "top up or switch this backend's provider, or route to another client; "
                "doctor cannot see provider balances, only what past runs reported",
            )
        )

    # --- mcp extra ---------------------------------------------------------------------------
    has_mcp = importlib.util.find_spec("mcp") is not None
    checks.append(
        Check(
            "mcp extra",
            OK if has_mcp else WARN,
            "installed" if has_mcp else "not installed",
            "" if has_mcp else "uv sync --extra mcp  (needed to run the MCP server)",
        )
    )

    if config is None:
        return checks

    # --- per-backend CLI availability (only the backends the config actually references) ------
    probes = dict(backends) if backends is not None else default_backends()
    for name in sorted({c.backend for c in config.clients.values()}):
        backend = probes.get(name)
        hint = BACKEND_HINTS.get(name, f"install the {name} CLI")
        if backend is None:
            # The caller's snapshot (e.g. a service built before this backend was configured)
            # doesn't know the name. Probe a freshly constructed backend - the same construction
            # path a spawn's _ensure_backend uses - so doctor's verdict matches what a run would
            # actually do instead of failing on a stale snapshot.
            try:
                backend = make_backend(name)
            except ValueError:
                checks.append(Check(f"backend:{name}", FAIL, "unknown backend name", hint))
                continue
        # Fidelity is static adapter metadata — emit even when the CLI is missing/unauthed.
        checks.append(_permission_fidelity_check(name, backend))
        if not backend.check_available():
            checks.append(Check(f"backend:{name}", FAIL, backend.unavailable_detail(), hint))
            continue
        # The CLI is present. If the backend exposes an authenticated-only probe (account_info),
        # use it to verify credentials too: a logged-out CLI still passes `--version` but dies on
        # the first real run, so doctor must not green-light it as merely "available".
        info = backend.account_info()
        if info is None and backend.verifies_auth():
            # account_info() is an authed-only probe that returned nothing: almost always "logged
            # out", but a transient probe failure (timeout/blip) looks the same - so name both. FAIL
            # (not WARN) is deliberate: a false FAIL costs a re-run; a false OK costs a wasted fan-out.
            checks.append(
                Check(f"backend:{name}", FAIL, "CLI present but not authenticated (or auth probe failed)", hint)
            )
            continue
        checks.append(Check(f"backend:{name}", OK, "available"))
        # Surface plan/account context for backends that expose it (e.g. Cursor's plan tier).
        if info:
            checks.append(Check(f"plan:{name}", OK, _format_plan(info)))

    # --- secrets (advisory: secret_ref is NOT injected; CLI login is the real auth path) ------
    for c in config.clients.values():
        if not (c.secret_ref and c.secret_ref.startswith("env:")):
            continue
        var = c.secret_ref[len("env:") :]
        is_set = resolve_secret(c.secret_ref) is not None
        checks.append(
            Check(
                f"secret:{c.name}",
                OK if is_set else WARN,
                f"{c.secret_ref} {'set' if is_set else 'unset'}",
                ""
                if is_set
                else f"export {var}, or ignore this if you authenticated the {c.backend} CLI via its own login",
            )
        )

    # --- child env allowlist (what agent subprocesses actually inherit) ----------------------
    # Agents no longer inherit the driver's full environment. Surface the policy so a sudden
    # "backend cannot authenticate" is diagnosable without reading source.
    checks.extend(_child_env_checks(config, probes))

    # --- trust-boundary / hygiene advisories (config present; never FAIL these) --------------
    if config.worktree_setup or config.verify:
        fields: list[str] = []
        if config.worktree_setup:
            fields.append("worktree_setup")
        if config.verify:
            fields.append("verify")
        joined = " + ".join(fields)
        cmds = [c for c in (config.worktree_setup, config.verify) if c]
        blocked = [
            setup_command_refusal(cmd, allow_unsafe=False)
            for cmd in cmds
            if setup_command_refusal(cmd, allow_unsafe=False)
        ]
        if config.allow_unsafe_commands:
            detail = f"{joined} run as your user (allow_unsafe_commands: true — arbitrary argv)"
            if config.verify:
                detail += (
                    "; verify runs after the agent and may execute agent-modified content"
                )
                hint = (
                    "treat fleet.config.yaml like code you execute; only point it at trusted "
                    "repos; review collect_run / CI before integrate"
                )
            else:
                hint = (
                    "treat fleet.config.yaml like code you execute; only point it at trusted repos"
                )
        elif blocked:
            detail = (
                f"{joined} use non-allowlisted binary; refused until "
                "allow_unsafe_commands: true (or switch to an allowlisted basename)"
            )
            hint = (
                "allowlist checks argv[0] basename only (uv/npm/pnpm/make/cargo/go/pytest/"
                "python/…); non-allowlisted basenames and relative path argv[0] need the "
                "opt-in — see docs/config.md"
            )
            if config.verify:
                hint += (
                    "; verify runs after the agent and may execute agent-modified project files"
                )
        else:
            if config.verify:
                detail = (
                    f"{joined} run allowlisted binaries as your user "
                    "(allowlist is not a sandbox — verify runs after the agent and may "
                    "execute agent-modified project files)"
                )
                hint = (
                    "treat fleet.config.yaml like code you execute; only point it at trusted "
                    "repos; review collect_run / CI before integrate"
                )
            else:
                detail = (
                    f"{joined} run allowlisted binaries as your user "
                    "(allowlist is not a sandbox — review still)"
                )
                hint = (
                    "treat fleet.config.yaml like code you execute; only point it at trusted repos"
                )
        checks.append(Check("unsafe-commands", WARN, detail, hint))
    if config.budgets:
        enforced = sum(1 for b in config.budgets if b.enforce)
        advisory = len(config.budgets) - enforced
        detail = (
            f"{enforced} enforced, {advisory} advisory (soft-warn only)"
            if enforced
            else f"{advisory} advisory (soft-warn only; set enforce: true to refuse over-cap spawns)"
        )
        checks.append(Check("budgets", OK if enforced == len(config.budgets) else WARN, detail, ""))
    if config.integrate_run_hooks:
        checks.append(
            Check(
                "integrate-hooks",
                WARN,
                "integrate_run_hooks: true — commit_run / integrate run git hooks "
                "(prompting hooks can deadlock headless merges; hooks may be "
                "agent-modified / repo-controlled scripts)",
                "use only non-interactive hooks with trusted provenance; "
                "keep verify: / CI as a backup gate",
            )
        )
    else:
        checks.append(
            Check(
                "integrate-hooks",
                WARN,
                "commit_run / integrate use git --no-verify (hooks skipped for headless reliability)",
                "set integrate_run_hooks: true only for non-interactive hooks; "
                "default --no-verify also avoids executing possibly agent-modified hook scripts; "
                "review diffs and rely on verify: / CI",
            )
        )

    return checks


def _child_env_checks(
    config: FleetConfig,
    probes: Mapping[str, CodingAgentBackend],
) -> list[Check]:
    """Describe the child-env allowlist and which backend credentials will be forwarded."""
    checks: list[Check] = []
    # Count how many parent vars currently match the operational base (for a concrete signal).
    base_present = sorted(k for k in os.environ if is_base_env_var(k))
    checks.append(
        Check(
            "child-env",
            OK,
            (
                f"agents inherit an allowlist only "
                f"({len(base_present)} operational var(s) present in parent; "
                f"unrelated secrets like AWS_*/GH_TOKEN are dropped)"
            ),
            "to pass an extra non-secret var, set it under the client's env: in fleet.config.yaml "
            "(secret-shaped keys and PATH are refused); see docs/config.md",
        )
    )
    for backend_name in sorted({c.backend for c in config.clients.values()}):
        backend = probes.get(backend_name)
        if backend is None:
            try:
                backend = make_backend(backend_name)
            except ValueError:
                continue
        creds = tuple(backend.credential_env_vars)
        if not creds:
            checks.append(
                Check(
                    f"child-env:{backend_name}",
                    OK,
                    "no credential env vars forwarded (CLI login / config files only)",
                )
            )
            continue
        parts: list[str] = []
        for var in creds:
            parts.append(f"{var}={'set→forwarded' if var in os.environ else 'unset'}")
        checks.append(
            Check(
                f"child-env:{backend_name}",
                OK,
                f"forwards {', '.join(parts)}",
            )
        )

    # secret_ref naming a var that this backend will NOT forward — common upgrade footgun.
    for c in config.clients.values():
        if not (c.secret_ref and c.secret_ref.startswith("env:")):
            continue
        var = c.secret_ref[len("env:") :]
        if var not in os.environ:
            continue
        backend = probes.get(c.backend)
        if backend is None:
            try:
                backend = make_backend(c.backend)
            except ValueError:
                continue
        if var in backend.credential_env_vars:
            continue
        checks.append(
            Check(
                f"child-env-secret:{c.name}",
                WARN,
                (
                    f"{var} is set in the parent environment but is NOT on the "
                    f"{c.backend} credential allowlist — the agent child will not receive it"
                ),
                (
                    f"authenticate via the {c.backend} CLI login, or use a credential var that "
                    f"backend forwards (see child-env:{c.backend}); "
                    f"secret_ref is advisory only and never injects"
                ),
            )
        )
    return checks


def summarize(checks: list[Check]) -> tuple[int, int]:
    """Return ``(fails, warns)``."""
    fails = sum(1 for c in checks if c.status == FAIL)
    warns = sum(1 for c in checks if c.status == WARN)
    return fails, warns


def doctor_report(checks: list[Check]) -> DoctorReport:
    """Build the unified doctor payload consumed by the CLI and MCP surfaces."""
    fails, warns = summarize(checks)
    return DoctorReport(checks=checks, fails=fails, warns=warns, ok=fails == 0)
