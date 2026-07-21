"""Fleet configuration - `fleet.config.yaml` declares N named clients.

Each client pins a backend + permission + model. Secrets are referenced (`env:VAR`), never
inlined. Includes a Fireworks guard: an OpenCode client must use a Go model (`opencode-go/...`),
never a `fireworks-ai/...` model, so runs bill the Go subscription rather than Fireworks credits.

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
# Not a sandbox: allowlisted tools can still run arbitrary scripts/code (e.g. ``python -c``,
# ``make`` recipes). Shells (``sh``/``bash``/…) are intentionally excluded — they need the opt-in.
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

# Per-spawn timeout presets (seconds). The driver can pass a preset name to `run_agent`/`spawn`/
# `run_many`/`marshal run` to override the client's configured `timeout_s` for that one run.
# A raw int (or numeric string) is also accepted; the same value flows to RunRequest.timeout_s.
DURATION_PRESETS: dict[str, int] = {
    "short": 300,    #  5 min
    "medium": 1200,  # 20 min - the typical safe-edit run
    "large": 6000,   # 100 min - heavier multi-file work
    "long": 24000,   # 400 min - benchmark / cross-repo refactors
}


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
    secret_ref: str | None = None
    # Optional provider usage-API to read REAL cost from after a run (e.g. "eastrouter"). When set,
    # the fleet fetches the actual charge for the run and reports cost as admin-api instead of an
    # estimate. Unset = price from the local table (or unavailable). See eastrouter.py.
    usage_api: str | None = None


class ModelSpec(BaseModel):
    """One entry in the optional `models:` catalog the driver can read.

    `id` is a provider+model string (the same one a client would set in its `model:` field).
    `backends` lists the backends that can run it. `cost` / `quota_type` / `notes` are short
    free-form strings the driver surfaces verbatim - cost mirrors the UsageSource values
    (``native`` | ``admin-api`` | ``estimated`` | ``scraped`` | ``unavailable``) and quota_type the billing
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
    """A $ cap for a scope (a backend, a client, or the fleet as a whole).

    By default budgets are **advisory** (`enforce=false`): `Fleet._start` soft-warns on stderr
    when the windowed spend meets the cap, but never blocks the run. Set ``enforce: true`` to
    refuse new matching spawns once spend meets the cap, and to admit at most one in-flight
    matching spawn at a time (concurrency guard against ledger TOCTOU). The check reads the usage
    ledger's `cost_usd`, which is real for meterable backends (source native / admin-api /
    estimated); subscription / unknown-cost backends report $0, so a $ budget on them simply
    never triggers (and shows $0 spent - we do NOT fabricate a fake percentage or "remaining").
    Exactly one of `backend` / `client` may be set; neither = a global cap.
    """

    backend: str | None = None
    client: str | None = None
    window: str  # one of BUDGET_WINDOWS - validated by the parser, not pydantic (gives a clean error)
    limit_usd: float
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
    # basename (see ``SAFE_SETUP_VERIFY_BINARIES``). Shells and anything else need
    # ``allow_unsafe_commands: true``. Not a sandbox — see SECURITY.md.
    allow_unsafe_commands: bool = False
    # When false (default), ``commit_run`` / ``integrate`` pass ``git --no-verify`` so prompting
    # pre-commit/pre-merge hooks cannot deadlock a headless driver. Set true only when hooks are
    # known non-interactive; see SECURITY.md.
    integrate_run_hooks: bool = False
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
            f"no fleet config at {p}; copy the example and edit it: "
            "cp fleet.config.example.yaml fleet.config.yaml"
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
            secret_ref=str(merged["secret_ref"]) if merged.get("secret_ref") else None,
            usage_api=str(merged["usage_api"]) if merged.get("usage_api") else None,
        )
        # Enforce the Fireworks guard at LOAD so an invalid config can't be built via any entry
        # point (CLI / library / MCP), not only the MCP path that happens to call validate().
        _reject_fireworks(client)
        clients[name] = client
    # Fleet-wide layered context (tolerate it being absent or not a mapping). `worker` prefixes
    # every worker goal; `driver` is surfaced to the driver.
    ctx_raw = raw.get("context")
    ctx_raw = ctx_raw if isinstance(ctx_raw, dict) else {}
    context = FleetContext(
        worker=str(ctx_raw["worker"]) if ctx_raw.get("worker") else None,
        driver=str(ctx_raw["driver"]) if ctx_raw.get("driver") else None,
    )
    return FleetConfig(
        clients=clients,
        context=context,
        worktree_setup=_parse_setup(raw.get("worktree_setup")),
        verify=_parse_setup(raw.get("verify"), field="verify"),
        allow_unsafe_commands=_parse_allow_unsafe_commands(raw.get("allow_unsafe_commands")),
        integrate_run_hooks=_parse_integrate_run_hooks(raw.get("integrate_run_hooks")),
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


def is_safe_setup_binary(argv0: str) -> bool:
    """True when ``argv0``'s basename is on the setup/verify allowlist (incl. ``python3.N``)."""
    lower = setup_command_basename(argv0).lower()
    return lower in SAFE_SETUP_VERIFY_BINARIES or bool(_PYTHON_VERSIONED.fullmatch(lower))


def setup_command_refusal(argv: list[str], *, allow_unsafe: bool) -> str | None:
    """Return a refusal reason if ``argv`` must not run, else ``None``.

    Allowlisted basenames pass without opt-in. Anything else (including ``sh``/``bash``) requires
    ``allow_unsafe=True``. Empty argv is treated as unset by callers; this helper assumes a
    non-empty command.
    """
    if allow_unsafe:
        return None
    binary = argv[0] if argv else ""
    if not binary:
        return "empty command"
    if is_safe_setup_binary(binary):
        return None
    name = setup_command_basename(binary)
    return (
        f"binary {name!r} is not on the worktree_setup/verify allowlist; "
        "set allow_unsafe_commands: true to run it (shells and arbitrary argv always need this)"
    )


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
    in {session, week, month}, and a positive ``limit_usd``. A malformed entry raises
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
        if isinstance(limit_raw, bool) or not isinstance(limit_raw, (int, float)):
            raise ConfigError(
                f"budgets[{i}].limit_usd must be a positive number, got {type(limit_raw).__name__}"
            )
        if limit_raw <= 0:
            raise ConfigError(f"budgets[{i}].limit_usd must be > 0, got {limit_raw}")
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
                limit_usd=float(limit_raw),
                enforce=enforce_raw,
            )
        )
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


def _reject_fireworks(client: ClientConfig) -> None:
    """Hard guard: an OpenCode client must never use a ``fireworks-ai/*`` model.

    Such a model bills Fireworks credits instead of the Go subscription. This is the single
    source of truth for the rule; both ``load_config`` and ``validate`` call it.
    """
    if client.backend == "opencode" and client.model and client.model.startswith("fireworks-ai/"):
        raise ConfigError(
            f"client {client.name!r}: OpenCode model {client.model!r} bills Fireworks credits; "
            "use an 'opencode-go/...' model"
        )


def validate(cfg: FleetConfig) -> list[str]:
    """Raise ConfigError on hard problems; return a list of soft warnings."""
    warnings: list[str] = []
    for c in cfg.clients.values():
        _reject_fireworks(c)
        if c.backend == "opencode" and not c.model:
            warnings.append(
                f"client {c.name!r}: no model set; defaulting to {DEFAULT_OPENCODE_MODEL} (Go sub)"
            )
        if c.secret_ref and c.secret_ref.startswith("env:") and resolve_secret(c.secret_ref) is None:
            warnings.append(f"client {c.name!r}: secret {c.secret_ref!r} is not set in the environment")
    # An advisory budget scoped to a name nothing runs under would silently never fire - warn (the
    # same "a typo should never silently disable a budget" posture the parser takes for window/limit).
    known_backends: set[str] | None = None
    for b in cfg.budgets:
        if b.client is not None and b.client not in cfg.clients:
            warnings.append(
                f"budget scope client {b.client!r} is not a configured client; this cap never fires"
            )
        if b.backend is not None:
            if known_backends is None:
                from .registry import backend_names  # lazy: avoid a module-level import cycle

                known_backends = set(backend_names())
            if b.backend not in known_backends:
                warnings.append(
                    f"budget scope backend {b.backend!r} is not a known backend; this cap never fires"
                )
    return warnings
