"""Fleet configuration - `fleet.config.yaml` declares N named clients.

Each client pins a backend + permission + model. Secrets are referenced (`env:VAR`), never
inlined. Includes a Fireworks guard: an OpenCode client must use a Go model (`opencode-go/...`),
never a `fireworks-ai/...` model, so runs bill the Go subscription rather than Fireworks credits.
"""

from __future__ import annotations

import os
import shlex
from pathlib import Path
from typing import Any

import yaml
from pydantic import BaseModel

from .types import PermissionMode

DEFAULT_OPENCODE_MODEL = "opencode-go/glm-5.2"


class ConfigError(ValueError):
    """The fleet config is invalid."""


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


class FleetConfig(BaseModel):
    clients: dict[str, ClientConfig] = {}
    # Optional command run once in each fresh worktree before the agent starts (e.g. to provision a
    # venv). None = no setup step. Repo-wide, not per-client - it sets up the checkout, not a run.
    worktree_setup: list[str] | None = None
    # How many times to re-run a run that failed for a TRANSIENT reason (DB lock, rate limit, 5xx,
    # connection error). 0 disables retries. Genuine task failures and timeouts are never retried.
    retries: int = 2


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
    return FleetConfig(
        clients=clients,
        worktree_setup=_parse_setup(raw.get("worktree_setup")),
        retries=_parse_retries(raw.get("retries")),
    )


def _parse_retries(value: Any) -> int:
    """Normalize the optional ``retries`` count (default 2). Must be a non-negative integer."""
    if value is None:
        return 2
    if isinstance(value, bool) or not isinstance(value, int):
        raise ConfigError(f"retries must be a non-negative integer, got {type(value).__name__}")
    if value < 0:
        raise ConfigError(f"retries must be >= 0, got {value}")
    return value


def _parse_setup(value: Any) -> list[str] | None:
    """Normalize the optional post-create worktree command to an argv list (or None).

    Accepts a shell-ish string (``uv sync --extra dev``) or an explicit argv list. The command runs
    once in each fresh worktree before the agent starts - with the driver's VIRTUAL_ENV scrubbed so
    it targets the worktree, not the driver. An empty/blank value is treated as "no setup".
    """
    if value is None:
        return None
    if isinstance(value, str):
        argv = shlex.split(value)
    elif isinstance(value, list):
        argv = [str(x) for x in value]
    else:
        raise ConfigError(
            f"worktree_setup must be a string or list, got {type(value).__name__}"
        )
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
    return warnings
