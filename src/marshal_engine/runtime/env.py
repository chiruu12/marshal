"""Environment hygiene for subprocesses Marshal spawns (agents + worktree setup).

Children receive an **allowlist**, not a copy of the driver's ``os.environ``. The base set is
operational (PATH, locale, certs, XDG, …). Each backend may also declare its own credential vars
(``CodingAgentBackend.credential_env_vars``); only that backend's run sees them. Everything else
is dropped — including ``LEAKY_VENV_VARS``, every ``MARSHAL_*`` session var, and unrelated secrets
(``AWS_*``, ``GH_TOKEN``, another backend's API key, …).

Per-client ``env:`` (fleet config) still layers non-secret literals after the allowlist — that is
the escape hatch for an operational var the base set omits. There is no "inherit everything" flag.

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
from collections.abc import Iterable, Mapping, Sequence
from pathlib import Path
from types import MappingProxyType
from typing import Any, Final

# Spawn kwargs every child in Marshal must be started with. Splat as ``**DETACHED_STDIO``.
#
# Marshal usually runs as a *stdio* MCP server: it is a child of the host (Claude Code, Cursor),
# it shares the host's process group and controlling terminal, and its own stdin is the JSON-RPC
# pipe. Both of those are inherited by default, and both are hazards:
#
#   stdin=DEVNULL         a child that reads stdin would consume JSON-RPC bytes addressed to us,
#                         corrupting the protocol stream - or block forever waiting on a pipe
#                         that only ever carries someone else's traffic.
#   start_new_session     a child sharing the controlling terminal can do job control on it
#                         (an interactive shell does this unconditionally). tcsetpgrp/tcsetattr
#                         from a process group that is not the terminal's foreground group raises
#                         SIGTTOU, and SIGTTIN/SIGTTOU are delivered to the *whole process group* -
#                         which includes the MCP host. The host is then suspended (ps STAT ``T``)
#                         with no crash and no stderr. setsid gives the child no controlling
#                         terminal at all, so the failure is structurally unreachable.
#
# Enforced for every spawn site under ``src/`` by ``tests/test_invariants.py``.
DETACHED_STDIO: Final[Mapping[str, Any]] = MappingProxyType(
    {"stdin": subprocess.DEVNULL, "start_new_session": True}
)

# Vars that pin a child to the driver's Python install; never inherited from the parent (``extra``
# may still set them deliberately). PATH is intentionally allowlisted - uv/git/the backend CLIs
# need it (and merge_user_path, called once at engine entry, sets it up before that).
LEAKY_VENV_VARS = ("VIRTUAL_ENV", "PYTHONHOME")

# Marshal session vars set on the driver/MCP process (MARSHAL_CONFIG, MARSHAL_REPO, …). Cleared so
# a worker's tests and `marshal` CLI resolve the worktree, not the driver's repo/config.
_MARSHAL_PREFIX = "MARSHAL_"

# Minimum length for a credential *value* to be redacted in logs / run text. Shorter values
# (e.g. "1", "true") would over-redact ordinary output.
_REDACT_MIN_VALUE_LEN = 8

# Union of every built-in backend's ``credential_env_vars``. Kept here so redaction does not
# import registry (env sits below runtime). test_known_credential_env_vars_match_backends
# asserts equality with the live ClassVars.
KNOWN_CREDENTIAL_ENV_VARS: frozenset[str] = frozenset(
    {
        "CURSOR_API_KEY",
        "OPENCODE_API_KEY",
        "OPENAI_API_KEY",
        "CODEX_API_KEY",
        "COMMAND_CODE_API_KEY",
        "ANTIGRAVITY_API_KEY",
        "GEMINI_API_KEY",
        "ANTHROPIC_API_KEY",
        "GOOSE_PROVIDER",
        "GOOSE_MODEL",
        "ZCODE_API_KEY",
        "COPILOT_GITHUB_TOKEN",
        "GH_TOKEN",
        "GITHUB_TOKEN",
    }
)

#: Names carried in a backend's ``credential_env_vars`` whose VALUES are not secrets. They select a
#: provider and a model, so the backend genuinely needs them in its child env - but they hold
#: ordinary words, and redaction rewrites the text it scans. With ``GOOSE_PROVIDER=anthropic`` and
#: ``GOOSE_MODEL=claude-sonnet-4-5`` exported, every run's final message, structured output and log
#: came back with those words replaced by ``[redacted:GOOSE_PROVIDER]`` - and redaction runs before
#: every write, so no unredacted copy is kept anywhere. `runtime/state.py` says the final message
#: IS the product for a review or research run.
#:
#: One set was doing two jobs. The allowlist wants these names; redaction must not.
_NON_SECRET_CREDENTIAL_VARS: frozenset[str] = frozenset({"GOOSE_PROVIDER", "GOOSE_MODEL"})

# Exact names every child genuinely needs. Err toward operational usefulness; exclude anything
# credential-shaped or loader-hijack (LD_PRELOAD, PYTHONPATH, NODE_OPTIONS, GIT_SSH_COMMAND, …).
_BASE_ENV_EXACT: frozenset[str] = frozenset(
    {
        # Process / user identity
        "PATH",
        "HOME",
        "USER",
        "LOGNAME",
        "USERNAME",  # Windows
        "SHELL",
        "TERM",
        "TERMINFO",
        "COLORTERM",
        "NO_COLOR",
        "FORCE_COLOR",
        "COLUMNS",
        "LINES",
        "TZ",
        "DISPLAY",
        "WAYLAND_DISPLAY",
        # Temp dirs (CLIs and subprocess tools)
        "TMPDIR",
        "TMP",
        "TEMP",
        # Locale (exact; LC_* also matched by prefix below)
        "LANG",
        "LANGUAGE",
        "LC_ALL",
        # TLS / CA bundles (confusing failures without these)
        "SSL_CERT_FILE",
        "SSL_CERT_DIR",
        "REQUESTS_CA_BUNDLE",
        "CURL_CA_BUNDLE",
        "CERT_PATH",
        # HTTP(S) proxies (case variants — some tools only read one)
        "HTTP_PROXY",
        "HTTPS_PROXY",
        "ALL_PROXY",
        "NO_PROXY",
        "http_proxy",
        "https_proxy",
        "all_proxy",
        "no_proxy",
        # Windows path roots (CLI config under USERPROFILE / APPDATA)
        "USERPROFILE",
        "HOMEDRIVE",
        "HOMEPATH",
        "APPDATA",
        "LOCALAPPDATA",
        "PROGRAMDATA",
        "SYSTEMROOT",
        "WINDIR",
        "COMSPEC",
        "PATHEXT",
    }
)

# Prefixes for operational families. ``__CF`` / ``__PYVENV`` are macOS runtime glue (text encoding,
# venv launcher); without them some Apple Python / GUI-adjacent lookups fail oddly.
_BASE_ENV_PREFIXES: tuple[str, ...] = (
    "LC_",
    "XDG_",
    "SSL_CERT_",
    "__CF",
    "__PYVENV",
)


def _is_marshal_env_var(name: str) -> bool:
    """True when ``name`` is a Marshal session variable (``MARSHAL_*``), not a similar prefix."""
    return name.startswith(_MARSHAL_PREFIX)


def is_base_env_var(name: str) -> bool:
    """True when ``name`` is on the operational allowlist (exact or prefix)."""
    if name in _BASE_ENV_EXACT:
        return True
    return any(name.startswith(p) for p in _BASE_ENV_PREFIXES)


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
# PROMPT_COMMAND, etc.) cannot hang the engine. A cold zsh with compinit routinely needs >2s, and
# the cost is paid once per process (cached) - so 3s buys real hits without risking a hang; a shell
# that's still silent after that falls through to the static fallback dirs below.
_USER_PATH_TIMEOUT_S = 3.0

# Where user-installed CLIs live when the login-shell probe can't tell us (shell missing, timed
# out, or rcfile broken). Only the dirs that exist on this machine are used. This turns a silent
# permanent PATH miss - which made doctor call a working ~/.local/bin/cursor-agent "not on PATH" -
# into a useful default.
_FALLBACK_USER_DIRS: tuple[str, ...] = (
    "~/.local/bin",
    "~/.cargo/bin",
    "/opt/homebrew/bin",
    "/usr/local/bin",
    "~/bin",
    "~/.bun/bin",
    "~/.deno/bin",
    "~/.npm-global/bin",
)

# Module-level cache for user_path(): None = not tried, "" = tried and got nothing,
# any other str = the result. The answer cannot change within a single process (the shell would
# have to be re-sourced mid-run), so cache the first successful result and the first miss.
_USER_PATH_CACHE: str | None = None


def child_env(
    extra: dict[str, str] | None = None,
    *,
    client: dict[str, str] | None = None,
    credentials: Sequence[str] = (),
) -> dict[str, str]:
    """Allowlisted child environment, with per-client ``client`` then ``extra`` layered on top.

    Starts from the operational base set (``is_base_env_var``) plus ``credentials`` (the running
    backend's ``credential_env_vars``). Drops ``LEAKY_VENV_VARS``, every ``MARSHAL_*`` session
    variable, and everything else in the parent — so a cursor agent never sees
    ``ANTHROPIC_API_KEY`` / ``AWS_*`` / ``GH_TOKEN``.

    Per-client ``client`` env is applied after the allowlist; keys that would undo hygiene (venv
    pins, ``MARSHAL_*``, ``PATH``) are ignored. ``extra`` wins last, so backend ``prepare()`` stamps
    (``GOOSE_MODE``, ``OPENCODE_CONFIG_CONTENT``, …) and deliberate ``extra_env`` overrides still
    reach the child even when not on the allowlist.
    """
    cred_set = frozenset(credentials)
    env: dict[str, str] = {}
    for k, v in os.environ.items():
        if k in LEAKY_VENV_VARS or _is_marshal_env_var(k):
            continue
        if is_base_env_var(k) or k in cred_set:
            env[k] = v
    if client:
        for k, v in client.items():
            if k in LEAKY_VENV_VARS or _is_marshal_env_var(k) or k == "PATH":
                continue
            env[k] = v
    if extra:
        env.update(extra)
    return env


def all_credential_env_vars(
    backends: Mapping[str, object] | None = None,
) -> frozenset[str]:
    """Union of every backend's ``credential_env_vars`` (for log redaction / doctor).

    With no ``backends`` argument, returns ``KNOWN_CREDENTIAL_ENV_VARS`` so redaction covers
    credentials even when the run used a different adapter than the one that leaked the value —
    without importing the registry (which would cycle: env ← backends.base ← registry).
    """
    if backends is None:
        return KNOWN_CREDENTIAL_ENV_VARS
    names: set[str] = set()
    for backend in backends.values():
        for name in getattr(backend, "credential_env_vars", ()) or ():
            names.add(str(name))
    return frozenset(names)


def credential_redactions(
    environ: Mapping[str, str] | None = None,
    *,
    credential_names: Iterable[str] | None = None,
    min_len: int = _REDACT_MIN_VALUE_LEN,
) -> list[tuple[str, str]]:
    """``(env_var_name, value)`` pairs to scrub from logs, longest values first.

    Only values of length ``>= min_len`` are included so short tokens do not mangle ordinary text,
    and names in ``_NON_SECRET_CREDENTIAL_VARS`` are excluded entirely - they are allowlisted into a
    child's env because a backend needs them, not because their values are secret.
    """
    env = os.environ if environ is None else environ
    names = (
        frozenset(credential_names)
        if credential_names is not None
        else all_credential_env_vars()
    )
    names = frozenset(names)
    pairs: list[tuple[str, str]] = []
    for name in names - _NON_SECRET_CREDENTIAL_VARS:
        value = env.get(name)
        if value is not None and len(value) >= min_len:
            pairs.append((name, value))
    # Longest first so a value that is a prefix of another is not partially replaced.
    pairs.sort(key=lambda item: len(item[1]), reverse=True)
    return pairs


def redact_secrets(
    text: str,
    environ: Mapping[str, str] | None = None,
    *,
    credential_names: Iterable[str] | None = None,
    min_len: int = _REDACT_MIN_VALUE_LEN,
) -> str:
    """Replace known credential *values* in ``text`` with ``[redacted:VAR]`` markers."""
    if not text:
        return text
    out = text
    for name, value in credential_redactions(
        environ, credential_names=credential_names, min_len=min_len
    ):
        if value in out:
            out = out.replace(value, f"[redacted:{name}]")
    return out


def _existing_fallback_path(dirs: tuple[str, ...]) -> str:
    """Join the given dirs (``~`` expanded) that exist on this machine into a PATH string."""
    existing = [str(p) for d in dirs if (p := Path(d).expanduser()).is_dir()]
    return os.pathsep.join(existing)


def user_path(
    *,
    shells: tuple[str, ...] | None = None,
    timeout: float = _USER_PATH_TIMEOUT_S,
    fallback_dirs: tuple[str, ...] | None = None,
) -> str | None:
    """Best-effort: derive the user's interactive-shell PATH.

    Returns the PATH string a fresh terminal would show. When every shell probe fails (no shell,
    timeout, broken rcfile), falls back to the well-known user bin dirs that exist on disk
    (``_FALLBACK_USER_DIRS``) rather than remembering a bare miss - a silent permanent miss is how
    doctor came to call working user-installed CLIs "not on PATH". Returns None only when the
    fallback is empty too. Used to recover backend-CLI visibility when Marshal is launched in a
    context that didn't source the user's rc files (an MCP host, a launchd job, a non-interactive
    SSH session). The result - probe hit, fallback, or genuine miss - is cached at module level:
    the answer cannot change within a process. ``shells``/``timeout``/``fallback_dirs`` are
    injectable for tests; the tuple params default to their module constants (resolved at call time
    so a monkeypatched module attribute takes effect - a default-arg binding would freeze the value
    at import time and silently ignore the patch).
    """
    global _USER_PATH_CACHE
    if _USER_PATH_CACHE is not None:
        return _USER_PATH_CACHE or None
    candidates = shells if shells is not None else _SHELL_CANDIDATES
    for shell in candidates:
        if not shell or shutil.which(shell) is None:
            continue
        try:
            # ``-i`` is what makes this probe worth running - most users export PATH from
            # .zshrc, which a non-interactive shell never sources - and also what makes it
            # dangerous: an interactive shell takes control of its terminal. DETACHED_STDIO
            # leaves it no terminal to take. Do not drop those kwargs from this call.
            proc = subprocess.run(
                [shell, "-ilc", "echo $PATH"],
                capture_output=True,
                text=True,
                timeout=timeout,
                check=False,
                **DETACHED_STDIO,
            )
        except (OSError, subprocess.SubprocessError):
            continue
        if proc.returncode != 0:
            continue
        path = (proc.stdout or "").strip()
        if path:
            _USER_PATH_CACHE = path
            return path
    dirs = fallback_dirs if fallback_dirs is not None else _FALLBACK_USER_DIRS
    # Cache whatever the fallback yields ("" = genuine miss) so subsequent calls don't re-spawn
    # shells; clear the module-level _USER_PATH_CACHE in tests to force a re-probe.
    _USER_PATH_CACHE = _existing_fallback_path(dirs)
    return _USER_PATH_CACHE or None


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
