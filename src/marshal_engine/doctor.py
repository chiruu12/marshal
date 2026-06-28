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
import shutil
import subprocess
import sys
from collections.abc import Mapping
from dataclasses import dataclass
from pathlib import Path

from .backends.base import CodingAgentBackend
from .config import ConfigError, FleetConfig, load_config, resolve_secret
from .registry import default_backends

OK = "ok"
WARN = "warn"
FAIL = "fail"

#: How to install + authenticate each backend's CLI (Marshal does not install them).
BACKEND_HINTS: dict[str, str] = {
    "opencode": "npm i -g opencode-ai  &&  opencode auth login",
    "cursor": "install the Cursor CLI (cursor-agent), then `cursor-agent login` (or set CURSOR_API_KEY)",
    "codex": "install the Codex CLI, then `codex login` (ChatGPT) or set OPENAI_API_KEY",
    "antigravity": "install the Antigravity CLI (agy), then complete its OAuth login",
    "claude-code": "install Claude Code (claude), then authenticate via its login or set ANTHROPIC_API_KEY",
}

MIN_PYTHON = (3, 11)


@dataclass
class Check:
    """One preflight result. ``fix`` is shown only when ``status`` is not ``ok``.

    Deliberately a dataclass, not a Pydantic model: it's a trivial CLI-only display struct,
    constructed positionally a dozen times below, and never serialized or returned over MCP - so a
    model would add keyword-arg verbosity with zero validation/serialization benefit.
    """

    name: str
    status: str
    detail: str
    fix: str = ""


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
        proc = subprocess.run([binary, arg], capture_output=True, text=True, timeout=timeout)
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
    )
    if head.returncode != 0:
        return Check(
            "repo", FAIL, f"{repo}: detached HEAD", "git checkout a branch (integrate refuses detached HEAD)"
        )
    branch = head.stdout.strip().rsplit("/", 1)[-1]
    return Check("repo", OK, f"{repo} (branch {branch})")


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
        checks.append(Check("config", OK, f"{config_path} ({len(config.clients)} clients)"))
    except ConfigError as exc:
        checks.append(Check("config", FAIL, str(exc), "fix the config, then re-run `marshal doctor`"))

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
        available = backend.check_available() if backend is not None else False
        checks.append(
            Check(
                f"backend:{name}",
                OK if available else FAIL,
                "available" if available else "CLI not on PATH / not authenticated",
                "" if available else BACKEND_HINTS.get(name, f"install the {name} CLI"),
            )
        )
        # Surface plan/account context for backends that expose it (e.g. Cursor's plan tier).
        if available and backend is not None:
            info = backend.account_info()
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

    return checks


def summarize(checks: list[Check]) -> tuple[int, int]:
    """Return ``(fails, warns)``."""
    fails = sum(1 for c in checks if c.status == FAIL)
    warns = sum(1 for c in checks if c.status == WARN)
    return fails, warns
