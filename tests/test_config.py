"""Tests for fleet config loading, the Go-model default, and the Fireworks guard."""

from __future__ import annotations

from pathlib import Path

import pytest

from marshal_engine.config import (
    DEFAULT_OPENCODE_MODEL,
    DURATION_PRESETS,
    BudgetSpec,
    ClientConfig,
    ConfigError,
    FleetConfig,
    FleetContext,
    ModelSpec,
    load_config,
    resolve_duration,
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


def test_verify_string_is_split(tmp_path: Path) -> None:
    p = tmp_path / "fleet.config.yaml"
    p.write_text("verify: uv run pytest -q\n" + _YAML)
    assert load_config(p).verify == ["uv", "run", "pytest", "-q"]


def test_verify_list_passes_through(tmp_path: Path) -> None:
    p = tmp_path / "fleet.config.yaml"
    p.write_text("verify:\n  - make\n  - check\n" + _YAML)
    assert load_config(p).verify == ["make", "check"]


def test_verify_absent_or_blank_is_none(tmp_path: Path) -> None:
    p = tmp_path / "fleet.config.yaml"
    p.write_text(_YAML)
    assert load_config(p).verify is None  # absent -> no gate, exactly the old behavior
    p.write_text('verify: "   "\n' + _YAML)
    assert load_config(p).verify is None


def test_verify_wrong_type_raises(tmp_path: Path) -> None:
    p = tmp_path / "fleet.config.yaml"
    p.write_text("verify: 42\n" + _YAML)
    with pytest.raises(ConfigError, match="verify"):
        load_config(p)


def test_retries_defaults_to_two(tmp_path: Path) -> None:
    p = tmp_path / "fleet.config.yaml"
    p.write_text(_YAML)
    assert load_config(p).retries == 2


def test_retries_explicit_value(tmp_path: Path) -> None:
    p = tmp_path / "fleet.config.yaml"
    p.write_text("retries: 0\n" + _YAML)
    assert load_config(p).retries == 0  # 0 disables retries


def test_retries_negative_or_wrong_type_raises(tmp_path: Path) -> None:
    p = tmp_path / "fleet.config.yaml"
    p.write_text("retries: -1\n" + _YAML)
    with pytest.raises(ConfigError, match="retries"):
        load_config(p)
    p.write_text('retries: "two"\n' + _YAML)
    with pytest.raises(ConfigError, match="retries"):
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


def test_validate_warns_on_budget_scope_that_never_fires(tmp_path: Path) -> None:
    # A budget scoped to a client/backend nothing runs under would silently never fire - validate()
    # surfaces it as a soft warning (the same posture as the unset-secret warning).
    cfg = FleetConfig(
        clients={"impl": ClientConfig(name="impl", backend="opencode", model="opencode-go/glm-5.2")},
        budgets=[
            BudgetSpec(client="ghost", window="week", limit_usd=5.0),
            BudgetSpec(backend="nonexistent", window="month", limit_usd=5.0),
        ],
    )
    warnings = validate(cfg)
    assert any("ghost" in w and "never fires" in w for w in warnings)
    assert any("nonexistent" in w and "never fires" in w for w in warnings)


def test_context_block_parses_into_fleet_context(tmp_path: Path) -> None:
    p = tmp_path / "fleet.config.yaml"
    p.write_text(
        _YAML
        + "\ncontext:\n"
        "  worker: Prefer small commits.\n"
        "  driver: Fleet runs review + impl.\n"
    )
    cfg = load_config(p)
    assert cfg.context == FleetContext(
        worker="Prefer small commits.", driver="Fleet runs review + impl."
    )


def test_context_absent_yields_none_fields(tmp_path: Path) -> None:
    p = tmp_path / "fleet.config.yaml"
    p.write_text(_YAML)  # no context block
    cfg = load_config(p)
    assert cfg.context.worker is None
    assert cfg.context.driver is None
    # and the default-constructed FleetConfig is also empty
    assert FleetConfig().context == FleetContext(worker=None, driver=None)


# --- models: catalog parse (present / absent / malformed) ------------------------------------


def test_models_block_absent_yields_empty_catalog(tmp_path: Path) -> None:
    p = tmp_path / "fleet.config.yaml"
    p.write_text(_YAML)  # no `models:` key
    cfg = load_config(p)
    assert cfg.models == []  # absent -> empty list, no error


def test_default_fleetconfig_has_empty_models() -> None:
    # The default-constructed FleetConfig also has an empty catalog, so library code can rely on
    # the field always being a list (not None) regardless of how the config was built.
    assert FleetConfig().models == []


def test_models_block_parses_into_modelspec_list(tmp_path: Path) -> None:
    p = tmp_path / "fleet.config.yaml"
    p.write_text(
        _YAML
        + "\nmodels:\n"
        "  - id: <provider>/<model-a>\n"
        "    backends: [opencode, claude-code]\n"
        "    cost: native\n"
        "    quota_type: subscription\n"
        "    notes: placeholder\n"
        "  - id: <provider>/<model-b>\n"
        "    backends: [cursor]\n"
    )
    cfg = load_config(p)
    assert cfg.models == [
        ModelSpec(
            id="<provider>/<model-a>",
            backends=["opencode", "claude-code"],
            cost="native",
            quota_type="subscription",
            notes="placeholder",
        ),
        ModelSpec(
            id="<provider>/<model-b>",
            backends=["cursor"],
            cost="",
            quota_type="",
            notes="",
        ),
    ]


def test_models_block_wrong_type_raises(tmp_path: Path) -> None:
    p = tmp_path / "fleet.config.yaml"
    p.write_text("models: 42\n" + _YAML)
    with pytest.raises(ConfigError, match="models must be a list"):
        load_config(p)


def test_models_block_entry_not_a_mapping_raises(tmp_path: Path) -> None:
    p = tmp_path / "fleet.config.yaml"
    p.write_text("models:\n  - not-a-mapping\n" + _YAML)
    with pytest.raises(ConfigError, match="must be a mapping"):
        load_config(p)


def test_models_block_missing_id_raises(tmp_path: Path) -> None:
    p = tmp_path / "fleet.config.yaml"
    p.write_text("models:\n  - backends: [opencode]\n" + _YAML)
    with pytest.raises(ConfigError, match="missing required 'id'"):
        load_config(p)


def test_models_block_backends_wrong_type_raises(tmp_path: Path) -> None:
    p = tmp_path / "fleet.config.yaml"
    p.write_text("models:\n  - id: <provider>/<model>\n    backends: opencode\n" + _YAML)
    with pytest.raises(ConfigError, match="backends must be a non-empty list of strings"):
        load_config(p)


def test_models_block_empty_backends_raises(tmp_path: Path) -> None:
    # A catalog row that names no backend can't run anything, so it's as malformed as a wrong type.
    p = tmp_path / "fleet.config.yaml"
    p.write_text("models:\n  - id: <provider>/<model>\n    backends: []\n" + _YAML)
    with pytest.raises(ConfigError, match="backends must be a non-empty list"):
        load_config(p)


# --- resolve_duration: preset / int / numeric string / errors --------------------------------


def test_resolve_duration_each_preset() -> None:
    for name, seconds in DURATION_PRESETS.items():
        assert resolve_duration(name) == seconds


def test_resolve_duration_raw_int_passes_through() -> None:
    assert resolve_duration(600) == 600
    assert resolve_duration(1) == 1  # smallest valid positive value


def test_resolve_duration_numeric_string() -> None:
    assert resolve_duration("600") == 600
    assert resolve_duration("  300  ") == 300  # surrounding whitespace is stripped


def test_resolve_duration_unknown_preset_lists_valid_names() -> None:
    with pytest.raises(ConfigError) as exc:
        resolve_duration("xl")
    msg = str(exc.value)
    assert "xl" in msg
    # every valid preset appears in the error so the driver can fix the typo without consulting docs
    for name in DURATION_PRESETS:
        assert name in msg


def test_resolve_duration_non_numeric_string_raises() -> None:
    with pytest.raises(ConfigError, match="unknown duration"):
        resolve_duration("five-minutes")


def test_resolve_duration_non_positive_int_raises() -> None:
    with pytest.raises(ConfigError, match="must be > 0"):
        resolve_duration(0)
    with pytest.raises(ConfigError, match="must be > 0"):
        resolve_duration(-10)


def test_resolve_duration_non_positive_numeric_string_raises() -> None:
    with pytest.raises(ConfigError, match="must be > 0"):
        resolve_duration("0")
    with pytest.raises(ConfigError, match="must be > 0"):
        resolve_duration("-5")


def test_resolve_duration_wrong_type_raises() -> None:
    with pytest.raises(ConfigError, match="got float"):
        resolve_duration(1.5)  # type: ignore[arg-type]
    with pytest.raises(ConfigError, match="got bool"):
        resolve_duration(True)  # type: ignore[arg-type]


# --- budgets: advisory $ caps per scope (backend / client / global) per window -----------------


def test_budgets_block_absent_yields_empty_list(tmp_path: Path) -> None:
    p = tmp_path / "fleet.config.yaml"
    p.write_text(_YAML)  # no `budgets:` key
    cfg = load_config(p)
    assert cfg.budgets == []  # absent -> empty list, no error
    assert FleetConfig().budgets == []  # default-constructed too


def test_budgets_block_parses_each_scope(tmp_path: Path) -> None:
    p = tmp_path / "fleet.config.yaml"
    p.write_text(
        _YAML
        + "\nbudgets:\n"
        "  - client: implementer\n"
        "    window: week\n"
        "    limit_usd: 5.00\n"
        "  - backend: cursor\n"
        "    window: session\n"
        "    limit_usd: 1.00\n"
        "  - window: month\n"  # global: neither backend nor client
        "    limit_usd: 25.00\n"
    )
    cfg = load_config(p)
    assert cfg.budgets == [
        BudgetSpec(client="implementer", window="week", limit_usd=5.00),
        BudgetSpec(backend="cursor", window="session", limit_usd=1.00),
        BudgetSpec(window="month", limit_usd=25.00),  # backend + client both None
    ]


def test_budgets_block_wrong_type_raises(tmp_path: Path) -> None:
    p = tmp_path / "fleet.config.yaml"
    p.write_text("budgets: 42\n" + _YAML)
    with pytest.raises(ConfigError, match="budgets must be a list"):
        load_config(p)


def test_budgets_block_entry_not_a_mapping_raises(tmp_path: Path) -> None:
    p = tmp_path / "fleet.config.yaml"
    p.write_text("budgets:\n  - not-a-mapping\n" + _YAML)
    with pytest.raises(ConfigError, match="must be a mapping"):
        load_config(p)


def test_budgets_block_invalid_window_raises(tmp_path: Path) -> None:
    p = tmp_path / "fleet.config.yaml"
    p.write_text("budgets:\n  - window: year\n    limit_usd: 1.0\n" + _YAML)
    with pytest.raises(ConfigError, match=r"window must be one of"):
        load_config(p)


def test_budgets_block_both_backend_and_client_set_raises(tmp_path: Path) -> None:
    # A budget is scoped to ONE of backend / client / global - never two of them.
    p = tmp_path / "fleet.config.yaml"
    p.write_text(
        "budgets:\n"
        "  - backend: cursor\n"
        "    client: implementer\n"
        "    window: week\n"
        "    limit_usd: 1.0\n" + _YAML
    )
    with pytest.raises(ConfigError, match="at most one of 'backend' or 'client'"):
        load_config(p)


def test_budgets_block_non_positive_limit_raises(tmp_path: Path) -> None:
    p = tmp_path / "fleet.config.yaml"
    p.write_text("budgets:\n  - window: week\n    limit_usd: 0\n" + _YAML)
    with pytest.raises(ConfigError, match="limit_usd must be > 0"):
        load_config(p)
    p.write_text("budgets:\n  - window: week\n    limit_usd: -1.0\n" + _YAML)
    with pytest.raises(ConfigError, match="limit_usd must be > 0"):
        load_config(p)


def test_budgets_block_missing_window_raises(tmp_path: Path) -> None:
    p = tmp_path / "fleet.config.yaml"
    p.write_text("budgets:\n  - limit_usd: 1.0\n" + _YAML)
    with pytest.raises(ConfigError, match="window must be one of"):
        load_config(p)


def test_budgets_block_non_numeric_limit_raises(tmp_path: Path) -> None:
    p = tmp_path / "fleet.config.yaml"
    p.write_text("budgets:\n  - window: week\n    limit_usd: many\n" + _YAML)
    with pytest.raises(ConfigError, match="limit_usd must be a positive number"):
        load_config(p)
