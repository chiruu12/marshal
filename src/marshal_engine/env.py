"""Environment hygiene for subprocesses Marshal spawns (agents + worktree setup).

The driver typically runs inside its own activated virtualenv, so ``os.environ`` carries
``VIRTUAL_ENV`` (and sometimes ``PYTHONHOME``) pointing at the DRIVER's interpreter. Every agent
runs in an isolated worktree with its own ``.venv``; if a child inherits the driver's
``VIRTUAL_ENV``, tools like ``uv run`` / ``python`` resolve to the driver's environment instead of
the worktree's - so an agent's ``uv run pytest`` silently tests the driver's installed code, not the
worktree's edits. Stripping those vars makes each child resolve its own worktree environment.

The driver can also launch Marshal with a stripped PATH (an MCP host spawned by a windowserver
process inherits a tiny default PATH, not the user's zshrc PATH). User-installed CLIs
(``opencode``, ``cursor-agent``, Homebrew binaries, ``~/.local/bin``) then look missing to
``shutil.which`` and ``marshal doctor`` falsely reports them as not installed.
``merge_user_path`` derives the user's interactive PATH from their login shell and unions it with
the current one, so backend lookups work the same as in a fresh terminal. Opt out with
``MARSHAL_NO_PATH_FIX=1`` (e.g. in a hermetic CI container where the user PATH is wrong or already
complete - or anywhere the cost of sourcing a login shell is not worth the diagnostic benefit).
"""

from __future__ import annotations

import os
import shutil
import subprocess

# Vars that pin a child to the driver's Python install; cleared so the worktree's own environment
# (its `.venv`) wins. PATH is intentionally NOT touched by child_env - uv/git/the backend CLIs
# need it (and merge_user_path, called once at engine entry, sets it up before that).
LEAKY_VENV_VARS = ("VIRTUAL_ENV", "PYTHONHOME")

# Shell candidates (in order) used to derive the user's interactive PATH. $SHELL first so the
# answer matches what the user would see in a fresh terminal of THEIR shell, then common
# fallbacks for environments where $SHELL is unset or the binary is missing. Each must support
# ``-ilc`` (login + interactive + one command) - the -i is the load-bearing flag, since most
# distros put PATH exports in .zshrc / .bashrc, not .zshenv / .bash_profile.
_SHELL_CANDIDATES: tuple[str, ...] = (
    os.environ.get("SHELL", "") or "",
    "/bin/zsh",
    "/bin/bash",
    "/usr/bin/bash",
    "/bin/sh",
)

# Bound the shell call so a misbehaving rcfile (compinit, slow prompt init, network mounts in
# PROMPT_COMMAND, etc.) cannot hang the engine. 2s is generous for a healthy shell, fatal for one
# that's stuck - the merge is a recovery path, not a critical-path dependency.
_USER_PATH_TIMEOUT_S = 2.0

# Module-level cache for user_path(): None = not tried, "" = tried and got nothing,
# any other str = the result. The answer cannot change within a single process (the shell would
# have to be re-sourced mid-run), so cache the first successful result and the first miss.
_USER_PATH_CACHE: str | None = None


def child_env(extra: dict[str, str] | None = None) -> dict[str, str]:
    """``os.environ`` minus the driver's venv pins, with ``extra`` layered on top.

    ``extra`` wins, so a caller that genuinely wants to set ``VIRTUAL_ENV`` for a child still can.
    """
    env = {k: v for k, v in os.environ.items() if k not in LEAKY_VENV_VARS}
    if extra:
        env.update(extra)
    return env


def user_path(
    *,
    shells: tuple[str, ...] | None = None,
    timeout: float = _USER_PATH_TIMEOUT_S,
) -> str | None:
    """Best-effort: derive the user's interactive-shell PATH.

    Returns the PATH string a fresh terminal would show, or None on any failure. Used to recover
    backend-CLI visibility when Marshal is launched in a context that didn't source the user's rc
    files (an MCP host, a launchd job, a non-interactive SSH session). Result is cached at module
    level: the first successful call wins, and a miss (no shell available) is remembered so we
    don't keep retrying. ``shells`` and ``timeout`` are injectable for tests; ``shells`` defaults to
    ``_SHELL_CANDIDATES`` (resolved at call time so a monkeypatched module attribute takes effect
    - a default-arg binding would freeze the value at import time and silently ignore the patch).
    """
    global _USER_PATH_CACHE
    if _USER_PATH_CACHE is not None:
        return _USER_PATH_CACHE or None
    candidates = shells if shells is not None else _SHELL_CANDIDATES
    for shell in candidates:
        if not shell or shutil.which(shell) is None:
            continue
        try:
            proc = subprocess.run(
                [shell, "-ilc", "echo $PATH"],
                capture_output=True,
                text=True,
                timeout=timeout,
            )
        except (OSError, subprocess.SubprocessError):
            continue
        if proc.returncode != 0:
            continue
        path = (proc.stdout or "").strip()
        if path:
            _USER_PATH_CACHE = path
            return path
    # Remember the miss so subsequent calls don't re-spawn shells; clear via the module-level
    # _USER_PATH_CACHE in tests if they need to force a re-probe.
    _USER_PATH_CACHE = ""
    return None


def merge_user_path() -> bool:
    """Union the user's login-shell PATH into ``os.environ['PATH']`` (in place).

    Adds only directories the current PATH does not already contain (preserves order; the current
    PATH wins for ties, so any pre-existing entry - including the system default - stays put).
    Idempotent: calling twice adds nothing new (the cache + dedup both gate it). Returns True iff
    at least one directory was appended. Opt out with ``MARSHAL_NO_PATH_FIX=1`` (e.g. in a hermetic
    CI container where the user PATH is wrong or already complete).
    """
    if os.environ.get("MARSHAL_NO_PATH_FIX"):
        return False
    path = user_path()
    if not path:
        return False
    current = os.environ.get("PATH", "")
    have = {p for p in current.split(os.pathsep) if p}
    appended: list[str] = []
    for entry in path.split(os.pathsep):
        if entry and entry not in have:
            appended.append(entry)
            have.add(entry)
    if not appended:
        return False
    os.environ["PATH"] = os.pathsep.join([*current.split(os.pathsep), *appended])
    return True
