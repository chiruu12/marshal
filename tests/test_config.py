"""Tests for fleet config loading, the Go-model default, and the Fireworks guard."""

from __future__ import annotations

from pathlib import Path

import pytest

from marshal_engine.config import (
    DEFAULT_OPENCODE_MODEL,
    ClientConfig,
    ConfigError,
    FleetConfig,
    load_config,
    resolve_model,
    validate,
)
from marshal_engine.types import PermissionMode

_YAML = """
defaults:
  permission: safe-edit
  timeout_s: 300
clients:
  implementer:
    backend: opencode
    model: opencode-go/glm-5.2
    secret_ref: env:OPENCODE_API_KEY
  reviewer:
    backend: cursor
    permission: read-only
"""


def test_load_merges_defaults(tmp_path: Path) -> None:
    p = tmp_path / "fleet.config.yaml"
    p.write_text(_YAML)
    cfg = load_config(p)
    assert set(cfg.clients) == {"implementer", "reviewer"}
    impl = cfg.clients["implementer"]
    assert impl.backend == "opencode"
    assert impl.model == "opencode-go/glm-5.2"
    assert impl.permission is PermissionMode.SAFE_EDIT
    assert impl.timeout_s == 300  # from defaults
    rev = cfg.clients["reviewer"]
    assert rev.permission is PermissionMode.READ_ONLY


def test_resolve_model_defaults_opencode_to_go() -> None:
    c = ClientConfig(name="x", backend="opencode")  # no model
    assert resolve_model(c) == DEFAULT_OPENCODE_MODEL
    c2 = ClientConfig(name="y", backend="cursor")
    assert resolve_model(c2) is None


def test_fireworks_model_is_rejected() -> None:
    cfg = FleetConfig(
        clients={
            "bad": ClientConfig(
                name="bad",
                backend="opencode",
                model="fireworks-ai/accounts/fireworks/models/glm-5p2",
            )
        }
    )
    with pytest.raises(ConfigError, match="Fireworks"):
        validate(cfg)


def test_load_config_rejects_fireworks_at_load(tmp_path: Path) -> None:
    # The guard must fire at LOAD, not only when validate() is called (the MCP path).
    p = tmp_path / "fleet.config.yaml"
    p.write_text(
        "clients:\n"
        "  bad:\n"
        "    backend: opencode\n"
        "    model: fireworks-ai/accounts/fireworks/models/glm-5p2\n"
    )
    with pytest.raises(ConfigError, match="Fireworks"):
        load_config(p)


def test_missing_config_file_raises_friendly_error(tmp_path: Path) -> None:
    with pytest.raises(ConfigError, match="no fleet config"):
        load_config(tmp_path / "does-not-exist.yaml")


def test_worktree_setup_string_is_split(tmp_path: Path) -> None:
    p = tmp_path / "fleet.config.yaml"
    p.write_text("worktree_setup: uv sync --extra dev --extra mcp\n" + _YAML)
    cfg = load_config(p)
    assert cfg.worktree_setup == ["uv", "sync", "--extra", "dev", "--extra", "mcp"]


def test_worktree_setup_list_passes_through(tmp_path: Path) -> None:
    p = tmp_path / "fleet.config.yaml"
    p.write_text("worktree_setup:\n  - uv\n  - sync\n  - --extra\n  - dev\n" + _YAML)
    cfg = load_config(p)
    assert cfg.worktree_setup == ["uv", "sync", "--extra", "dev"]


def test_worktree_setup_absent_or_blank_is_none(tmp_path: Path) -> None:
    p = tmp_path / "fleet.config.yaml"
    p.write_text(_YAML)
    assert load_config(p).worktree_setup is None  # absent -> no setup step
    p.write_text('worktree_setup: "   "\n' + _YAML)
    assert load_config(p).worktree_setup is None  # blank string splits to [] -> None


def test_worktree_setup_wrong_type_raises(tmp_path: Path) -> None:
    p = tmp_path / "fleet.config.yaml"
    p.write_text("worktree_setup: 42\n" + _YAML)
    with pytest.raises(ConfigError, match="worktree_setup"):
        load_config(p)


def test_missing_secret_warns(monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.delenv("OPENCODE_API_KEY", raising=False)
    cfg = FleetConfig(
        clients={
            "impl": ClientConfig(
                name="impl",
                backend="opencode",
                model="opencode-go/glm-5.2",
                secret_ref="env:OPENCODE_API_KEY",
            )
        }
    )
    warnings = validate(cfg)
    assert any("not set" in w for w in warnings)
