"""Fleet configuration - `fleet.config.yaml` declares N named clients.

Each client pins a backend + permission + model. Secrets are referenced (`env:VAR`), never
inlined. Includes a Fireworks guard: an OpenCode client must use a Go model (`opencode-go/...`),
never a `fireworks-ai/...` model, so runs bill the Go subscription rather than Fireworks credits.
"""

from __future__ import annotations

import os
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


class FleetConfig(BaseModel):
    clients: dict[str, ClientConfig] = {}


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
        )
        # Enforce the Fireworks guard at LOAD so an invalid config can't be built via any entry
        # point (CLI / library / MCP), not only the MCP path that happens to call validate().
        _reject_fireworks(client)
        clients[name] = client
    return FleetConfig(clients=clients)


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
