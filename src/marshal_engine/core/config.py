"""Fleet configuration - `fleet.config.yaml` declares N named clients.

Each client pins a backend + permission + model. Secrets are referenced (`env:VAR`), never
inlined. An OpenCode client with no model defaults to a Go model (`opencode-go/...`) so runs bill
the Go subscription; naming a `fireworks-ai/...` model is an explicit, warned opt-in that bills
Fireworks credits and reports real per-run USD.

The optional top-level `models:` block is a catalog the driver can read (`list_models` / `marshal
models`) - it describes which model ids the fleet exposes, which backends they run on, and the
`cost`/`quota_type` provenance strings the driver can surface. The catalog is data only; it does
NOT change routing (clients still own backend+model).
"""

from __future__ import annotations

import os
import re
import shlex
from pathlib import Path
from typing import Any

import yaml
from pydantic import BaseModel

from .types import PermissionMode

DEFAULT_OPENCODE_MODEL = "opencode-go/glm-5.2"

# Basenames allowed for ``worktree_setup`` / ``verify`` without ``allow_unsafe_commands: true``.
# Typo / wrong-binary guard only — not a sandbox. Allowlisted tools still run arbitrary
# scripts/code (``python -c``, ``uv run sh -c``, ``make`` recipes, …). Shells (``sh``/``bash``)
# are excluded as non-allowlisted basenames; that does not make allowlisted interpreters safer.
SAFE_SETUP_VERIFY_BINARIES: frozenset[str] = frozenset(
    {
        "uv",
        "npm",
        "pnpm",
        "yarn",
        "bun",
        "make",
        "cargo",
        "go",
        "pytest",
        "python",
        "python3",
        "poetry",
        "pip",
        "pip3",
        "ruff",
        "mypy",
        "tox",
        "nox",
    }
)
_PYTHON_VERSIONED = re.compile(r"^python\d+(\.\d+)?$")

# ``env:`` keys matching any of these substrings (case-insensitive) are refused at load. ``env:`` is
# for provider/config selection (e.g. CODEX_HOME), not literal credentials — that is ``secret_ref``.
_SECRET_ENV_KEY_PARTS: tuple[str, ...] = ("KEY", "TOKEN", "SECRET", "PASSWORD", "CREDENTIAL")

# Per-spawn timeout presets (seconds). The driver can pass a preset name to `run_agent`/`spawn`/
# `run_many`/`marshal run` to override the client's configured `timeout_s` for that one run.
# A raw int (or numeric string) is also accepted; the same value flows to RunRequest.timeout_s.
DURATION_PRESETS: dict[str, int] = {
    "short": 300,    #  5 min
    "medium": 1200,  # 20 min - the typical safe-edit run
    "large": 6000,   # 100 min - heavier multi-file work
    "long": 24000,   # 400 min - benchmark / cross-repo refactors
}

# Must match registry._FACTORIES keys. Kept here so validate() does not import registry
# (config sits below runtime). test_known_backend_names_match_registry asserts equality.
KNOWN_BACKEND_NAMES: frozenset[str] = frozenset(
    {
        "cursor",
        "opencode",
        "codex",
        "command-code",
        "copilot",
        "antigravity",
        "claude-code",
        "goose",
        "zcode",
    }
)


class ConfigError(ValueError):
    """The fleet config is invalid."""


def resolve_duration(value: str | int) -> int:
    """Map a per-spawn `duration` override to a positive integer of seconds.

    Accepts a known preset name (e.g. ``"short"``), a positive int, or a numeric string. Raises
    ``ConfigError`` on an unknown preset, a non-positive value, or a non-numeric string - the
    same error type ``load_config`` raises, so the call site can treat them uniformly.
    """
    if isinstance(value, bool):
        # `bool` is a subclass of `int`; a flag-like True/False has no meaning here.
        raise ConfigError(
            f"duration must be a preset name or positive seconds, got bool: {value!r}"
        )
    if isinstance(value, int):
        seconds = value
    elif isinstance(value, str):
        key = value.strip()
        if key in DURATION_PRESETS:
            return DURATION_PRESETS[key]
        try:
            seconds = int(key)
        except ValueError:
            valid = ", ".join(sorted(DURATION_PRESETS))
            raise ConfigError(
                f"unknown duration {value!r}; valid presets: {valid} (or a positive int of seconds)"
            ) from None
    else:
        raise ConfigError(
            f"duration must be a preset name or positive seconds, got {type(value).__name__}"
        )
    if seconds <= 0:
        raise ConfigError(f"duration must be > 0 seconds, got {seconds}")
    return seconds


class ClientConfig(BaseModel):
    name: str
    backend: str
    model: str | None = None
    permission: PermissionMode = PermissionMode.SAFE_EDIT
    timeout_s: int = 600
    # Per-client literal env vars merged into each agent child (provider routing, e.g. CODEX_HOME).
    # Secrets belong in secret_ref, not here — validated at load.
    env: dict[str, str] = {}
    secret_ref: str | None = None
    # Optional provider usage-API to read REAL cost from after a run (e.g. "eastrouter"). When set,
    # the fleet fetches the actual charge for the run and reports cost as admin-api. Unset means the
    # run keeps whatever the backend reported: native cost if it gave one, else unavailable. Marshal
    # never prices a run itself - there is no local price table, and inventing one would be the fake
    # $0 the ledger exists to refuse. See eastrouter.py.
    usage_api: str | None = None


class ModelSpec(BaseModel):
    """One entry in the optional `models:` catalog the driver can read.

    `id` is a provider+model string (the same one a client would set in its `model:` field).
    `backends` lists the backends that can run it. `cost` / `quota_type` / `notes` are short
    free-form strings the driver surfaces verbatim - cost mirrors the UsageSource values
    (``native`` | ``admin-api`` | ``unavailable``) and quota_type the billing
    shape (``metered`` | ``subscription`` | ``unavailable``). All fields after `id` and
    `backends` are optional so a minimal catalog entry is just ``{id, backends}``.
    """

    id: str
    backends: list[str]
    cost: str = ""
    quota_type: str = ""
    notes: str = ""


class FleetContext(BaseModel):
    """Fleet-wide layered context.

    `worker` is prepended to every worker agent's goal (shared operating assumptions); `driver` is
    surfaced back to the driver (e.g. over MCP) so it knows how the fleet is configured to behave.
    """

    worker: str | None = None
    driver: str | None = None


#: Allowed values for `BudgetSpec.window`. Anything else fails fast at load (the same posture as
#: the other config errors - a typo should never silently disable a budget).
BUDGET_WINDOWS: frozenset[str] = frozenset({"session", "week", "month"})


class BudgetSpec(BaseModel):
    """A cap for a scope (a backend, a client, or the fleet as a whole).

    **Two limits, because one number cannot govern both kinds of client.** `limit_usd` governs
    spend that was actually measured; `limit_runs` governs runs whose cost nobody reported. A fleet
    of subscription/unmeasurable clients under a dollar cap alone is uncapped in practice - it
    reports $0 spent forever - and "within budget" there is a statement about what Marshal can see,
    not about what was consumed. Each limit only ever governs what it can see, and both are
    reported, so neither number pretends to cover the other. At least one must be set.

    By default budgets are **advisory** (`enforce=false`): `Fleet._start` soft-warns on stderr
    when the windowed spend meets the cap, but never blocks the run. Set ``enforce: true`` to
    refuse new matching spawns once spend meets the cap, and to admit at most one in-flight
    matching spawn at a time (concurrency guard against ledger TOCTOU). The check reads the usage
    ledger's `cost_usd`, which is real for meterable backends (source native / admin-api, plus
    legacy ledger lines tagged estimated); subscription / unknown-cost backends report $0, so a $
    budget on them simply never triggers (and shows $0 spent - we do NOT fabricate a fake
    percentage or "remaining").
    Exactly one of `backend` / `client` may be set; neither = a global cap.
    """

    backend: str | None = None
    client: str | None = None
    window: str  # one of BUDGET_WINDOWS - validated by the parser, not pydantic (gives a clean error)
    #: Cap on MEASURED spend (ledger cost from a native / admin-api source). None = no dollar cap.
    limit_usd: float | None = None
    #: Cap on the number of runs in the window whose cost was NOT measured. None = no run cap.
    #: This is the only limit that can govern a subscription backend, whose spend is unknowable here.
    limit_runs: int | None = None
    enforce: bool = False


class FleetConfig(BaseModel):
    clients: dict[str, ClientConfig] = {}
    # Fleet-wide layered context: `worker` prefixes every worker goal; `driver` is shown to the
    # driver. See FleetContext.
    context: FleetContext = FleetContext()
    # Optional command run once in each fresh worktree before the agent starts (e.g. to provision a
    # venv). None = no setup step. Repo-wide, not per-client - it sets up the checkout, not a run.
    worktree_setup: list[str] | None = None
    # Optional gate command run in the worktree AFTER a run that would otherwise be `succeeded`
    # and changed files (e.g. the repo's full test suite; text-only replies are never gated). A
    # non-zero exit marks the run `verify_failed` instead - the worktree is kept for review.
    # None = trust the agent's own outcome, exactly as before. Repo-wide like worktree_setup;
    # same string-or-argv YAML shape.
    verify: list[str] | None = None
    # When false (default), ``worktree_setup`` / ``verify`` may only use an allowlisted binary
    # basename (see ``SAFE_SETUP_VERIFY_BINARIES``); relative path argv[0] also needs the opt-in.
    # Basename screen only — not a sandbox for args (``python -c``, …). See SECURITY.md.
    allow_unsafe_commands: bool = False
    # When false (default), ``commit_run`` / ``integrate`` pass ``git --no-verify`` so prompting
    # pre-commit/pre-merge hooks cannot deadlock a headless driver. Set true only when hooks are
    # known non-interactive; see SECURITY.md.
    integrate_run_hooks: bool = False
    # When false (default), ``read_paths`` may only name paths inside this workspace's own repo.
    # The secret-name denylist is a guess about which files hold credentials and cannot cover a
    # whole host; scope can. Also what stops `read_paths` reaching another workspace's ledger.
    # True permits any readable path - only where the driver issuing read_paths is trusted.
    allow_external_read_paths: bool = False
    # How many times to re-run a run that failed for a TRANSIENT reason (DB lock, rate limit, 5xx,
    # connection error). 0 disables retries. Genuine task failures and timeouts are never retried.
    retries: int = 2
    # Optional model catalog the driver can read (`list_models` / `marshal models`). Pure data -
    # does NOT influence routing (clients still own backend+model). Absent/empty by default so
    # existing configs load unchanged.
    models: list[ModelSpec] = []
    # Optional advisory $ budgets per scope (backend / client / global) and time window
    # (session / week / month). Absent/empty = no budgets, no behavior change. See BudgetSpec.
    budgets: list[BudgetSpec] = []


def load_config(path: Path | str) -> FleetConfig:
    p = Path(path)
    if not p.exists():
        raise ConfigError(
            f"no fleet config at {p}; scaffold one with `marshal init`, then edit it"
        )
    raw_any: Any = yaml.safe_load(p.read_text(encoding="utf-8"))
    raw: dict[str, Any] = raw_any or {}
    defaults: dict[str, Any] = raw.get("defaults") or {}
    clients: dict[str, ClientConfig] = {}
    for name, spec in (raw.get("clients") or {}).items():
        merged: dict[str, Any] = {**defaults, **(spec or {})}
        if "backend" not in merged:
            raise ConfigError(f"client {name!r}: missing required 'backend'")
        client = ClientConfig(
            name=name,
            backend=str(merged["backend"]),
            model=str(merged["model"]) if merged.get("model") else None,
            permission=PermissionMode(str(merged.get("permission", "safe-edit"))),
            timeout_s=int(merged.get("timeout_s", 600)),
            env=_parse_client_env(merged.get("env"), client=name),
            secret_ref=str(merged["secret_ref"]) if merged.get("secret_ref") else None,
            usage_api=str(merged["usage_api"]) if merged.get("usage_api") else None,
        )
        clients[name] = client
    # Fleet-wide layered context (tolerate it being absent or not a mapping). `worker` prefixes
    # every worker goal; `driver` is surfaced to the driver.
    ctx_raw = raw.get("context")
    ctx_raw = ctx_raw if isinstance(ctx_raw, dict) else {}
    context = FleetContext(
        worker=str(ctx_raw["worker"]) if ctx_raw.get("worker") else None,
        driver=str(ctx_raw["driver"]) if ctx_raw.get("driver") else None,
    )
    worktree_setup = _parse_setup(raw.get("worktree_setup"))
    verify = _parse_setup(raw.get("verify"), field="verify")
    allow_unsafe_commands = _parse_allow_unsafe_commands(raw.get("allow_unsafe_commands"))
    # Fail closed on static allowlist refusal before any caller builds a Fleet / worktree.
    # Runtime setup()/verify() keep the same check as a backstop.
    reject_disallowed_setup_commands(
        worktree_setup=worktree_setup,
        verify=verify,
        allow_unsafe_commands=allow_unsafe_commands,
    )
    return FleetConfig(
        clients=clients,
        context=context,
        worktree_setup=worktree_setup,
        verify=verify,
        allow_unsafe_commands=allow_unsafe_commands,
        integrate_run_hooks=_parse_integrate_run_hooks(raw.get("integrate_run_hooks")),
        allow_external_read_paths=_parse_bool_flag(
            raw.get("allow_external_read_paths"), "allow_external_read_paths"
        ),
        retries=_parse_retries(raw.get("retries")),
        models=_parse_models(raw.get("models")),
        budgets=_parse_budgets(raw.get("budgets")),
    )


def setup_command_basename(argv0: str) -> str:
    """Basename of argv[0] for allowlist checks (strips a Windows ``.exe`` suffix)."""
    name = Path(argv0).name
    if name.lower().endswith(".exe"):
        name = name[:-4]
    return name


def is_relative_setup_argv0(argv0: str) -> bool:
    """True when ``argv0`` is a relative path (resolves against worktree cwd), not a bare name."""
    path = Path(argv0)
    if path == Path(path.name):
        return False
    return not path.is_absolute()


def is_safe_setup_binary(argv0: str) -> bool:
    """True when ``argv0``'s basename is on the setup/verify allowlist (incl. ``python3.N``)."""
    lower = setup_command_basename(argv0).lower()
    return lower in SAFE_SETUP_VERIFY_BINARIES or bool(_PYTHON_VERSIONED.fullmatch(lower))


def setup_command_refusal(argv: list[str], *, allow_unsafe: bool) -> str | None:
    """Return a refusal reason if ``argv`` must not run, else ``None``.

    Screens argv[0] only: allowlisted basenames pass without opt-in even when later args execute
    arbitrary code (``python -c``, ``uv run sh -c``, ``make -f``, …). Relative path argv[0]
    (e.g. ``.venv/bin/python``) and non-allowlisted basenames (including ``sh``/``bash``) require
    ``allow_unsafe=True``. Absolute path argv[0] is checked by basename. Empty argv is treated as
    unset by callers; this helper assumes a non-empty command.
    """
    if allow_unsafe:
        return None
    binary = argv[0] if argv else ""
    if not binary:
        return "empty command"
    # Relative paths resolve against the worktree cwd and may point at agent-rewritten binaries.
    if is_relative_setup_argv0(binary):
        return (
            f"relative path argv[0] {binary!r} is refused without allow_unsafe_commands: true "
            "(resolves inside the worktree; use a bare basename or an absolute path)"
        )
    if is_safe_setup_binary(binary):
        return None
    name = setup_command_basename(binary)
    return (
        f"binary {name!r} is not on the worktree_setup/verify allowlist; "
        "set allow_unsafe_commands: true to run it "
        "(allowlist checks basename only — not a sandbox for args)"
    )


def reject_disallowed_setup_commands(
    *,
    worktree_setup: list[str] | None,
    verify: list[str] | None,
    allow_unsafe_commands: bool,
) -> None:
    """Raise ``ConfigError`` when a configured setup/verify command fails the allowlist.

    Called from ``load_config`` so CLI / doctor / MCP / hot-reload never accept a static
    misconfiguration that would otherwise create-then-teardown worktrees. Does not change
    allowlist membership or ``allow_unsafe_commands`` semantics.
    """
    for field, cmd in (("worktree_setup", worktree_setup), ("verify", verify)):
        if not cmd:
            continue
        reason = setup_command_refusal(cmd, allow_unsafe=allow_unsafe_commands)
        if reason:
            raise ConfigError(f"{field}: {reason}")


def _parse_bool_flag(value: Any, key: str) -> bool:
    """Parse an optional boolean config flag; absent means False (the closed setting)."""
    if value is None:
        return False
    if isinstance(value, bool):
        return value
    raise ConfigError(f"{key} must be a boolean, got {type(value).__name__}")


def _parse_allow_unsafe_commands(value: Any) -> bool:
    if value is None:
        return False
    if isinstance(value, bool):
        return value
    raise ConfigError(
        f"allow_unsafe_commands must be a boolean, got {type(value).__name__}"
    )


def _parse_integrate_run_hooks(value: Any) -> bool:
    if value is None:
        return False
    if isinstance(value, bool):
        return value
    raise ConfigError(
        f"integrate_run_hooks must be a boolean, got {type(value).__name__}"
    )


def _parse_models(value: Any) -> list[ModelSpec]:
    """Normalize the optional top-level ``models:`` catalog. Absent/empty -> ``[]``.

    Each entry must have a non-empty ``id`` and a ``backends`` list of strings; the other fields
    default to empty strings. A malformed entry raises ``ConfigError`` so a typo fails fast at
    load (same as the other config errors), instead of silently dropping a catalog row.
    """
    if value is None:
        return []
    if not isinstance(value, list):
        raise ConfigError(f"models must be a list, got {type(value).__name__}")
    out: list[ModelSpec] = []
    for i, entry in enumerate(value):
        if not isinstance(entry, dict):
            raise ConfigError(f"models[{i}] must be a mapping, got {type(entry).__name__}")
        if not entry.get("id"):
            raise ConfigError(f"models[{i}]: missing required 'id'")
        backends_raw = entry.get("backends", [])
        if (
            not isinstance(backends_raw, list)
            or not backends_raw
            or not all(isinstance(b, str) for b in backends_raw)
        ):
            raise ConfigError(
                f"models[{i}].backends must be a non-empty list of strings, got {backends_raw!r}"
            )
        out.append(
            ModelSpec(
                id=str(entry["id"]),
                backends=list(backends_raw),
                cost=str(entry.get("cost", "") or ""),
                quota_type=str(entry.get("quota_type", "") or ""),
                notes=str(entry.get("notes", "") or ""),
            )
        )
    return out


def _parse_retries(value: Any) -> int:
    """Normalize the optional ``retries`` count (default 2). Must be a non-negative integer."""
    if value is None:
        return 2
    if isinstance(value, bool) or not isinstance(value, int):
        raise ConfigError(f"retries must be a non-negative integer, got {type(value).__name__}")
    if value < 0:
        raise ConfigError(f"retries must be >= 0, got {value}")
    return value


def _parse_budgets(value: Any) -> list[BudgetSpec]:
    """Normalize the optional top-level ``budgets:`` advisory caps. Absent -> ``[]``.

    Each entry: optional ``backend`` OR optional ``client`` (not both, not neither), a ``window``
    in {session, week, month}, and at least one of a positive ``limit_usd`` (measured spend) or a
    positive ``limit_runs`` (runs whose cost was not measured). A malformed entry raises
    ``ConfigError`` so a typo fails fast at load (same posture as the other config errors),
    instead of silently dropping a budget.
    """
    if value is None:
        return []
    if not isinstance(value, list):
        raise ConfigError(f"budgets must be a list, got {type(value).__name__}")
    out: list[BudgetSpec] = []
    for i, entry in enumerate(value):
        if not isinstance(entry, dict):
            raise ConfigError(f"budgets[{i}] must be a mapping, got {type(entry).__name__}")
        backend_raw = entry.get("backend")
        client_raw = entry.get("client")
        backend = str(backend_raw) if backend_raw else None
        client = str(client_raw) if client_raw else None
        if backend is not None and client is not None:
            raise ConfigError(
                f"budgets[{i}]: set at most one of 'backend' or 'client' (got both); "
                "a budget is scoped to a backend, a client, or the whole fleet - never two"
            )
        window_raw = entry.get("window")
        if not isinstance(window_raw, str) or window_raw not in BUDGET_WINDOWS:
            valid = ", ".join(sorted(BUDGET_WINDOWS))
            raise ConfigError(
                f"budgets[{i}].window must be one of {valid}, got {window_raw!r}"
            )
        limit_raw = entry.get("limit_usd")
        if limit_raw is not None:
            if isinstance(limit_raw, bool) or not isinstance(limit_raw, (int, float)):
                raise ConfigError(
                    f"budgets[{i}].limit_usd must be a positive number, got "
                    f"{type(limit_raw).__name__}"
                )
            if limit_raw <= 0:
                raise ConfigError(f"budgets[{i}].limit_usd must be > 0, got {limit_raw}")
        runs_raw = entry.get("limit_runs")
        if runs_raw is not None:
            if isinstance(runs_raw, bool) or not isinstance(runs_raw, int):
                raise ConfigError(
                    f"budgets[{i}].limit_runs must be a positive integer, got "
                    f"{type(runs_raw).__name__}"
                )
            if runs_raw <= 0:
                raise ConfigError(f"budgets[{i}].limit_runs must be > 0, got {runs_raw}")
        if limit_raw is None and runs_raw is None:
            # A budget that caps nothing is almost certainly a typo, and it would sit in `usage`
            # looking like a control that is in force. Refuse at load rather than display a lie.
            raise ConfigError(
                f"budgets[{i}] must set limit_usd, limit_runs, or both - a budget with neither "
                f"caps nothing. Use limit_usd for measured spend and limit_runs for clients whose "
                f"cost cannot be measured."
            )
        enforce_raw = entry.get("enforce", False)
        if not isinstance(enforce_raw, bool):
            raise ConfigError(
                f"budgets[{i}].enforce must be a boolean, got {type(enforce_raw).__name__}"
            )
        out.append(
            BudgetSpec(
                backend=backend,
                client=client,
                window=window_raw,
                limit_usd=float(limit_raw) if limit_raw is not None else None,
                limit_runs=runs_raw,
                enforce=enforce_raw,
            )
        )
    return out


def _is_secret_shaped_env_key(key: str) -> bool:
    upper = key.upper()
    return any(part in upper for part in _SECRET_ENV_KEY_PARTS)


def _parse_client_env(value: Any, *, client: str) -> dict[str, str]:
    """Normalize a client's ``env:`` block. Refuses secret-shaped keys, empty keys, and PATH."""
    if value is None:
        return {}
    if not isinstance(value, dict):
        raise ConfigError(
            f"client {client!r}: env must be a mapping, got {type(value).__name__}"
        )
    out: dict[str, str] = {}
    for raw_key, raw_val in value.items():
        key = str(raw_key)
        if not key:
            raise ConfigError(f"client {client!r}: env has an empty key")
        if key == "PATH":
            raise ConfigError(
                f"client {client!r}: env must not set PATH; Marshal merges the user's interactive "
                "PATH at engine entry — overriding PATH here would break that recovery"
            )
        if _is_secret_shaped_env_key(key):
            raise ConfigError(
                f"client {client!r}: env key {key!r} looks like a secret; use secret_ref "
                f"(env:VAR) instead. env: is for provider/config selection (e.g. CODEX_HOME), "
                "not literal credentials"
            )
        if not isinstance(raw_val, str):
            raise ConfigError(
                f"client {client!r}: env[{key!r}] must be a string, got {type(raw_val).__name__}"
            )
        val = raw_val
        if val.startswith("~"):
            val = str(Path(val).expanduser())
        out[key] = val
    return out


def _parse_setup(value: Any, field: str = "worktree_setup") -> list[str] | None:
    """Normalize an optional worktree command (``worktree_setup`` / ``verify``) to argv (or None).

    Accepts a shell-ish string (``uv sync --extra dev``) or an explicit argv list. Both commands
    run in a worktree with the driver's VIRTUAL_ENV scrubbed so they target the worktree, not the
    driver - setup before the agent starts, verify after it would succeed. An empty/blank value is
    treated as "none".
    """
    if value is None:
        return None
    if isinstance(value, str):
        argv = shlex.split(value)
    elif isinstance(value, list):
        argv = [str(x) for x in value]
    else:
        raise ConfigError(f"{field} must be a string or list, got {type(value).__name__}")
    return argv or None


def resolve_model(client: ClientConfig) -> str | None:
    """The model to actually pass - defaults OpenCode to a Go model so it never hits Fireworks."""
    if client.backend == "opencode" and not client.model:
        return DEFAULT_OPENCODE_MODEL
    return client.model


def resolve_secret(ref: str | None) -> str | None:
    if ref and ref.startswith("env:"):
        return os.environ.get(ref[4:])
    return None


def metered_provider_warning(client: ClientConfig) -> str | None:
    """Advisory when a client is pointed at a separately-metered provider, else None.

    This used to be a hard rejection, on the theory that a ``fireworks-ai/*`` model billing
    Fireworks credits instead of the OpenCode Go subscription was always a mistake. It is not -
    those models report real per-run USD, which is the only cost provenance some fleets have, so
    refusing them denied users a provider *and* the measured-cost story.

    The accident it actually guarded against is still guarded: ``resolve_model`` defaults an
    OpenCode client with no model to the Go subscription, so nothing reaches a metered provider
    unless someone typed its id. That makes this a notice, not a veto - and it no longer takes
    the whole config down with it (a single client's billing choice used to make every other
    client in the file unloadable).
    """
    if client.backend == "opencode" and client.model and client.model.startswith("fireworks-ai/"):
        return (
            f"client {client.name!r}: OpenCode model {client.model!r} bills Fireworks credits, "
            "not the Go subscription (use an 'opencode-go/...' model for the sub)"
        )
    return None


def validate(cfg: FleetConfig) -> list[str]:
    """Raise ConfigError on hard problems; return a list of soft warnings."""
    warnings: list[str] = []
    for c in cfg.clients.values():
        metered = metered_provider_warning(c)
        if metered:
            warnings.append(metered)
        if c.backend == "opencode" and not c.model:
            warnings.append(
                f"client {c.name!r}: no model set; defaulting to {DEFAULT_OPENCODE_MODEL} (Go sub)"
            )
        if c.secret_ref and c.secret_ref.startswith("env:") and resolve_secret(c.secret_ref) is None:
            warnings.append(f"client {c.name!r}: secret {c.secret_ref!r} is not set in the environment")
    # An advisory budget scoped to a name nothing runs under would silently never fire - warn (the
    # same "a typo should never silently disable a budget" posture the parser takes for window/limit).
    for b in cfg.budgets:
        if b.client is not None and b.client not in cfg.clients:
            warnings.append(
                f"budget scope client {b.client!r} is not a configured client; this cap never fires"
            )
        if b.backend is not None and b.backend not in KNOWN_BACKEND_NAMES:
            warnings.append(
                f"budget scope backend {b.backend!r} is not a known backend; this cap never fires"
            )
    return warnings
