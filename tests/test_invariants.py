"""Architectural-invariant tests - lock the engine's core invariants (CLAUDE.md) in source.

These assert *structure*, not behaviour: they trip if a future change silently breaks an
invariant that is otherwise only documented in prose. They are anchored on stable symbols
(enum members, argv tokens, decorator/function names parsed from source) rather than on
comments or prose, so they guard the rule without churning on wording.

Each block names the invariant it guards. New invariants belong here, with a one-line
"Invariant:" note, so the contract stays self-documenting.
"""

from __future__ import annotations

import ast
import inspect
from pathlib import Path

import pytest

import marshal_engine
from marshal_engine.backends.base import CodingAgentBackend
from marshal_engine.core.config import ClientConfig
from marshal_engine.orchestration.registry import backend_names, default_backends
from marshal_engine.core.types import (
    Capabilities,
    PermissionFidelity,
    PermissionMode,
    RunOpts,
    UsageRecord,
    UsageSource,
    resolve_permission_fidelity,
)

_PKG = Path(marshal_engine.__file__).resolve().parent
_REPO_ROOT = _PKG.parents[1]  # .../src/marshal_engine -> .../src -> repo root
_BACKENDS = default_backends()


# --- AST helpers: read public-surface names from source without importing optional deps -------


def _decorated_tool_names(path: Path) -> list[str]:
    """Names of functions registered as MCP tools (decorated with ``@app.tool()``)."""
    tree = ast.parse(path.read_text(encoding="utf-8"))
    names: list[str] = []
    for node in ast.walk(tree):
        if not isinstance(node, (ast.FunctionDef, ast.AsyncFunctionDef)):  # tools are async defs
            continue
        for dec in node.decorator_list:
            target = dec.func if isinstance(dec, ast.Call) else dec
            if isinstance(target, ast.Attribute) and target.attr == "tool":
                names.append(node.name)
    return names


def _subcommand_names(path: Path) -> list[str]:
    """Literal names passed to ``add_parser("...")`` in the CLI."""
    tree = ast.parse(path.read_text(encoding="utf-8"))
    names: list[str] = []
    for node in ast.walk(tree):
        if (
            isinstance(node, ast.Call)
            and isinstance(node.func, ast.Attribute)
            and node.func.attr == "add_parser"
            and node.args
            and isinstance(node.args[0], ast.Constant)
            and isinstance(node.args[0].value, str)
        ):
            names.append(node.args[0].value)
    return names


# --- defaults: safe, non-prompting, always-timed ---------------------------------------------


def test_runopts_default_permission_is_the_safe_nonprompting_tier() -> None:
    # Invariant: headless = no stdin, so the default must be a non-prompting tier (safe-edit),
    # never a prompting mode and never read-only by accident.
    assert RunOpts(cwd=Path(".")).permission is PermissionMode.SAFE_EDIT


def test_every_run_carries_an_external_timeout_by_default() -> None:
    # Invariant: "every agent run gets an external timeout + kill" - the default is never 0/None.
    assert RunOpts(cwd=Path(".")).timeout_s > 0


def test_client_config_default_permission_is_safe_edit() -> None:
    # Invariant: the config default must agree with the run default (safe-edit).
    assert ClientConfig(name="x", backend="cursor").permission is PermissionMode.SAFE_EDIT


# --- capabilities <-> map_permission agreement -----------------------------------------------


@pytest.mark.parametrize("name", sorted(_BACKENDS))
def test_capabilities_agree_with_map_permission(name: str) -> None:
    # Invariant: a backend advertises exactly the modes its map_permission accepts, and every
    # backend supports the default safe-edit tier (else run() with defaults would raise).
    backend = _BACKENDS[name]
    modes = backend.capabilities.permission_modes
    assert PermissionMode.SAFE_EDIT in modes, f"{name} must support the default safe-edit tier"
    for mode in PermissionMode:
        if mode in modes:
            assert isinstance(backend.map_permission(mode), list)
        else:
            with pytest.raises(ValueError):
                backend.map_permission(mode)


# --- permission_fidelity honesty (#40) -------------------------------------------------------


#: Built-in backends that install a safe-edit restriction beyond the worktree.
_ENFORCED_DENIES = frozenset({"cursor", "opencode", "codex"})
#: Built-in backends where Marshal cannot promise a deny layer (worktree is the boundary).
_BOUNDARY_ONLY = frozenset({"command-code", "goose", "antigravity", "claude-code"})


def test_capabilities_default_permission_fidelity_is_boundary_only() -> None:
    # Invariant: unknown/dummy adapters fail honest — never claim enforcement by accident.
    assert Capabilities().permission_fidelity is PermissionFidelity.BOUNDARY_ONLY


@pytest.mark.parametrize(
    ("backend_fidelity", "permission", "expected"),
    [
        (PermissionFidelity.ENFORCED_DENIES, PermissionMode.SAFE_EDIT, PermissionFidelity.ENFORCED_DENIES),
        (PermissionFidelity.ENFORCED_DENIES, PermissionMode.READ_ONLY, PermissionFidelity.ENFORCED_DENIES),
        (PermissionFidelity.ENFORCED_DENIES, PermissionMode.YOLO, PermissionFidelity.UNRESTRICTED),
        (PermissionFidelity.BOUNDARY_ONLY, PermissionMode.SAFE_EDIT, PermissionFidelity.BOUNDARY_ONLY),
        (PermissionFidelity.BOUNDARY_ONLY, PermissionMode.READ_ONLY, PermissionFidelity.BOUNDARY_ONLY),
        (PermissionFidelity.BOUNDARY_ONLY, PermissionMode.YOLO, PermissionFidelity.UNRESTRICTED),
    ],
)
def test_resolve_permission_fidelity(
    backend_fidelity: PermissionFidelity,
    permission: PermissionMode,
    expected: PermissionFidelity,
) -> None:
    # #178: client fidelity is the (backend, permission) pair; yolo is never enforced-denies.
    assert resolve_permission_fidelity(backend_fidelity, permission) is expected


@pytest.mark.parametrize("name", sorted(_BACKENDS))
def test_built_in_backend_permission_fidelity(name: str) -> None:
    # Invariant: every built-in adapter declares an explicit fidelity; the matrix matches #40.
    fidelity = _BACKENDS[name].capabilities.permission_fidelity
    if name in _ENFORCED_DENIES:
        assert fidelity is PermissionFidelity.ENFORCED_DENIES
    elif name in _BOUNDARY_ONLY:
        assert fidelity is PermissionFidelity.BOUNDARY_ONLY
    else:
        raise AssertionError(f"backend {name!r} missing from fidelity matrix")
    # Every registered backend must appear in exactly one bucket.
    assert name in _ENFORCED_DENIES | _BOUNDARY_ONLY


def test_fidelity_matrix_covers_every_registered_backend() -> None:
    assert set(_BACKENDS) == _ENFORCED_DENIES | _BOUNDARY_ONLY
    assert not (_ENFORCED_DENIES & _BOUNDARY_ONLY)


# --- the headless prompting footgun ----------------------------------------------------------

# Tokens that would (re)enable an interactive approval/prompt; any of these deadlocks a
# stdin-less run. None appear in the current tables - this guards against a regression.
_PROMPTING_NEEDLES = (
    "ask-for-approval",
    "on-request",
    "on-failure",
    "--interactive",
    "--ask",
    "--prompt",
    "--confirm",
    "untrusted",
)


@pytest.mark.parametrize("name", sorted(_BACKENDS))
def test_no_permission_mode_maps_to_a_prompting_flag(name: str) -> None:
    # Invariant (CLAUDE.md, first rule): never use a prompting permission mode - it deadlocks
    # headless. Every supported mode must map to non-prompting argv.
    backend = _BACKENDS[name]
    for mode in backend.capabilities.permission_modes:
        for tok in backend.map_permission(mode):
            assert tok != "-i", f"{name}:{mode.value} maps to interactive flag {tok!r}"
            low = tok.lower()
            for needle in _PROMPTING_NEEDLES:
                assert needle not in low, f"{name}:{mode.value} maps to prompting flag {tok!r}"


# --- backend is a per-call parameter, never a public-surface name ----------------------------


def test_backend_name_is_never_encoded_in_a_public_surface_name() -> None:
    # Invariant: "backend is a per-call parameter, never encoded in tool/skill names." Holds
    # across the MCP tool surface, the CLI subcommands, and the published Skills.
    backends = backend_names()
    tools = _decorated_tool_names(_PKG / "interfaces" / "mcp_server.py")
    subcommands = _subcommand_names(_PKG / "interfaces" / "cli" / "parser.py")
    skills = [p.name for p in (_REPO_ROOT / "skills").iterdir() if p.is_dir() and p.name[0] != "."]
    # Guard against a vacuous pass if the source shape ever changes under the AST walk.
    assert len(tools) >= 10, tools
    assert len(subcommands) >= 5, subcommands
    assert len(skills) >= 3, skills
    for surface in (*tools, *subcommands, *skills):
        for backend in backends:
            assert backend not in surface.lower(), f"backend {backend!r} leaked into {surface!r}"


# --- usage honesty ---------------------------------------------------------------------------


def test_usage_record_defaults_to_unavailable_source() -> None:
    # Invariant: never present an estimate (or native) as ground truth. Absent data is
    # explicitly "unavailable", never a silent zero-cost native record.
    assert UsageRecord(backend="x").source is UsageSource.UNAVAILABLE


def test_usage_source_taxonomy_is_closed() -> None:
    # Lock the provenance vocabulary so a new source can't be added without being labelled.
    assert {s.value for s in UsageSource} == {
        "native",
        "admin-api",
        "unavailable",
    }


# --- the safe run() loop (the cornerstone) ---------------------------------------------------


def test_run_loop_closes_stdin_owns_a_group_times_out_and_kills() -> None:
    # Invariant: base.run() is the single chokepoint that defends the headless footguns. It must
    # close stdin, start its own session/group, enforce opts.timeout_s, and kill the whole group
    # on timeout. Asserted on source so a refactor that drops any of these trips here.
    src = inspect.getsource(CodingAgentBackend.run)
    assert "stdin=subprocess.DEVNULL" in src
    assert "start_new_session=True" in src
    assert "communicate(timeout=opts.timeout_s)" in src
    assert "_kill_process_group" in src
    # ordering: spawn the process before the timed wait; kill in the timeout branch.
    assert src.index("subprocess.Popen") < src.index("communicate(timeout=")
    assert src.index("TimeoutExpired") < src.index("_kill_process_group")


# --- status comparisons always go through RunStatus (never raw string literals) ---------------


_RUNSTATUS_LITERALS: frozenset[str] = frozenset(
    {s.value for s in __import__("marshal_engine.core.types", fromlist=["RunStatus"]).RunStatus}
)

# A status string literal (e.g. `== "running"`) is the smell: it bypasses the enum's single
# source of truth and survives a rename of RunStatus.RUNNING silently. The two safe forms
# are `rec.status == RunStatus.RUNNING.value` and `rec.status in ("a", "b", ...)` (the
# latter is fine because it enumerates the values explicitly). We allow the literal-tuple
# form via the special-cased "in a tuple" exemption below; everything else trips.
_ALLOWED_SAFE_SOURCES = (
    "RunStatus.",
    # `status == "error"` is allowed in catch-all error branches; the test below still flags
    # the value pair against the enum so a renamed status literal is caught.
)


def _enum_status_string_literals(path: Path) -> list[tuple[int, str, str]]:
    """Every `== <literal>` / `!= <literal>` / `in (<literals>,)` against a status string."""
    tree = ast.parse(path.read_text(encoding="utf-8"))
    out: list[tuple[int, str, str]] = []
    # Walk Compare nodes; capture both `x == "lit"` and `x in ("lit", "lit")` patterns.
    for node in ast.walk(tree):
        if isinstance(node, ast.Compare):
            for op, comp in zip(node.ops, node.comparators, strict=True):
                if isinstance(op, (ast.Eq, ast.NotEq)) and isinstance(comp, ast.Constant) and isinstance(comp.value, str):
                    if comp.value in _RUNSTATUS_LITERALS:
                        out.append((node.lineno, op.__class__.__name__, comp.value))
    return out


def test_engine_status_comparisons_go_through_runstatus() -> None:
    # H5 invariant: a status check that bypasses RunStatus (e.g. `rec.status == "running"`)
    # silently survives a rename of RunStatus.RUNNING, breaking the cancel-wins invariant's
    # comparison sites. The codebase must use `RunStatus.RUNNING.value` (or a tuple of those
    # values, which is allowed because it's still enumerating the canonical set). A bare
    # string-literal comparison trips here so the offending site can be fixed.
    offenders: list[str] = []
    for src_path in _PKG.rglob("*.py"):
        # Skip type re-exports: only the engine modules that own state transitions.
        rel = src_path.relative_to(_PKG)
        if rel.parts[0] in {"__pycache__", "data"}:
            continue
        for lineno, op, lit in _enum_status_string_literals(src_path):
            offenders.append(f"{rel}:{lineno}  {op} {lit!r}")
    assert not offenders, (
        "bare string-literal RunStatus comparisons (use RunStatus.X.value):\n  "
        + "\n  ".join(offenders)
    )


def test_reported_version_matches_pyproject() -> None:
    """`marshal --version` must equal [project].version - one source of truth, no drift.

    A hardcoded __version__ silently disagrees with the published wheel, and that wrong number
    then lands in every bug report.
    """
    import tomllib
    from pathlib import Path

    import marshal_engine

    pyproject = Path(__file__).resolve().parent.parent / "pyproject.toml"
    declared = tomllib.loads(pyproject.read_text())["project"]["version"]
    assert marshal_engine.__version__ == declared


def test_plugin_manifests_match_pyproject_version() -> None:
    """The Claude Code plugin manifests must not drift from [project].version.

    Same failure mode as a hardcoded __version__: the plugin advertises one version while the
    package it installs is another, and the mismatch is invisible until a user reports it.
    """
    import json
    import tomllib
    from pathlib import Path

    root = Path(__file__).resolve().parent.parent
    declared = tomllib.loads((root / "pyproject.toml").read_text())["project"]["version"]

    plugin = json.loads((root / ".claude-plugin" / "plugin.json").read_text())
    assert plugin["version"] == declared

    market = json.loads((root / ".claude-plugin" / "marketplace.json").read_text())
    for entry in market["plugins"]:
        assert entry["version"] == declared, f"{entry['name']} drifted from pyproject"


def test_plugin_manifests_list_every_backend() -> None:
    """Every registered backend must appear in the plugin descriptions.

    A backend missing from the description is a capability users never discover.
    """
    import json
    from pathlib import Path

    from marshal_engine.orchestration.registry import backend_names

    root = Path(__file__).resolve().parent.parent

    # Only the DESCRIPTION fields count. Searching the whole serialized manifest would let
    # `keywords` satisfy the assertion while the user-facing text went stale - a test that passes
    # for the wrong reason is worse than no test.
    descriptions: list[str] = []
    plugin = json.loads((root / ".claude-plugin" / "plugin.json").read_text())
    descriptions.append(plugin["description"])
    market = json.loads((root / ".claude-plugin" / "marketplace.json").read_text())
    descriptions.extend(entry["description"] for entry in market["plugins"])

    for name in backend_names():
        token = name.replace("-", " ").replace("_", " ")
        for desc in descriptions:
            low = desc.lower()
            assert token.lower() in low or name.lower() in low, (
                f"backend {name!r} is missing from a plugin manifest description"
            )


def test_marketplace_manifest_has_a_description() -> None:
    """`claude plugin validate --strict` fails without one, which blocks registry submission."""
    import json
    from pathlib import Path

    root = Path(__file__).resolve().parent.parent
    market = json.loads((root / ".claude-plugin" / "marketplace.json").read_text())
    assert market.get("description", "").strip(), "marketplace.json needs a description"
