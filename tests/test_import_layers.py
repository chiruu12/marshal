"""Import-graph layer guard — acyclic graph + a layer matrix.

Invariant: layers import strictly downward. Lazy / function-local imports are still edges
(they were used to dodge cycles); TYPE_CHECKING-guarded imports count too. This test parses
the package with AST so those cannot hide.

    core         value types and pure logic
    runtime      processes, git, disk        } siblings: neither may import the other
    accounting   usage, cost, budgets        }
    backends     one adapter per coding CLI
    orchestration the fleet loop and what sequences it
    interfaces   service, CLI, MCP, workspaces, doctor

A module may import its own package, or any package of strictly lower rank. This replaces the
old hand-maintained deny-list (``types → worktree``, ``config → registry``, ``env → registry``)
— every one of those is now a rank violation, caught by rule rather than by enumeration.
"""

from __future__ import annotations

import ast
from collections import defaultdict
from pathlib import Path

import marshal_engine
from marshal_engine.core.config import KNOWN_BACKEND_NAMES
from marshal_engine.orchestration.registry import backend_names, default_backends
from marshal_engine.runtime.env import KNOWN_CREDENTIAL_ENV_VARS

_PKG = Path(marshal_engine.__file__).resolve().parent
_PKG_NAME = "marshal_engine"

# Layer ranks. A module may import its own layer, or any layer of strictly lower rank.
# `runtime` and `accounting` share a rank deliberately: they are siblings, so neither may
# import the other, and the equal rank is what forbids it.
_RANK: dict[str, int] = {
    "core": 0,
    "runtime": 1,
    "accounting": 1,
    "backends": 2,
    "orchestration": 3,
    "interfaces": 4,
}

# Top-level modules kept as re-export shims for published import paths. They deliberately
# reach into a layer, so they are not ranked - `test_shims_only_reexport` covers them instead.
_SHIMS: frozenset[str] = frozenset(
    {"config", "service", "teams", "state", "workspaces", "cli"}
)


def _module_key(path: Path) -> str:
    """Map a source file to a graph node name (``__init__``, ``types``, ``backends.cursor``, …)."""
    rel = path.relative_to(_PKG)
    if rel.name == "__init__.py":
        if len(rel.parts) == 1:
            return "__init__"
        return ".".join(rel.parts[:-1])
    return ".".join(rel.with_suffix("").parts)


def _package_parts(mod: str) -> list[str]:
    """Package path parts that contain ``mod`` (file modules use their parent)."""
    if mod == "__init__":
        return []
    parts = mod.split(".")
    if _PKG.joinpath(*parts).is_dir():
        return parts
    return parts[:-1]


def _resolve_from_import(mod: str, node: ast.ImportFrom) -> list[str]:
    """Resolve relative/absolute ImportFrom targets to package-local module keys."""
    if node.level == 0:
        if node.module is None:
            return []
        if node.module == _PKG_NAME:
            return ["__init__"]
        prefix = _PKG_NAME + "."
        if node.module.startswith(prefix):
            return [node.module[len(prefix) :]]
        return []

    parts = _package_parts(mod)
    climb = node.level - 1
    if climb > len(parts):
        return []  # escaped the package
    base = parts[: len(parts) - climb]
    if node.module:
        return [".".join(base + node.module.split("."))]
    # ``from . import X`` executes the package ``__init__`` AND binds submodule ``X``. Attributing
    # it only to ``__init__`` would let a forbidden edge hide behind this spelling, which is the
    # one thing this test exists to prevent.
    pkg_key = "__init__" if not base else ".".join(base)
    subs = [".".join(base + [alias.name]) for alias in node.names]
    return [pkg_key, *subs]


def _edges_from_file(path: Path) -> set[tuple[str, str]]:
    """All import edges from this file, including nested / TYPE_CHECKING bodies."""
    mod = _module_key(path)
    tree = ast.parse(path.read_text(encoding="utf-8"), filename=str(path))
    edges: set[tuple[str, str]] = set()
    for node in ast.walk(tree):
        if isinstance(node, ast.Import):
            for alias in node.names:
                name = alias.name
                if name == _PKG_NAME:
                    edges.add((mod, "__init__"))
                elif name.startswith(_PKG_NAME + "."):
                    edges.add((mod, name[len(_PKG_NAME) + 1 :]))
        elif isinstance(node, ast.ImportFrom):
            for target in _resolve_from_import(mod, node):
                if target != mod:
                    edges.add((mod, target))
    return edges


def _all_edges() -> set[tuple[str, str]]:
    edges: set[tuple[str, str]] = set()
    for path in _PKG.rglob("*.py"):
        if "__pycache__" in path.parts:
            continue
        edges |= _edges_from_file(path)
    return edges


def _cycles(edges: set[tuple[str, str]]) -> list[list[str]]:
    """Return simple cycles in the directed import graph (node-name lists)."""
    graph: dict[str, set[str]] = defaultdict(set)
    nodes: set[str] = set()
    for a, b in edges:
        graph[a].add(b)
        nodes.add(a)
        nodes.add(b)

    cycles: list[list[str]] = []
    stack: list[str] = []
    on_path: set[str] = set()
    done: set[str] = set()
    seen_cycle_keys: set[tuple[str, ...]] = set()

    def dfs(n: str) -> None:
        if n in done:
            return
        if n in on_path:
            i = stack.index(n)
            body = stack[i:]
            rot = min(range(len(body)), key=lambda j: body[j:] + body[:j])
            key = tuple(body[rot:] + body[:rot])
            if key not in seen_cycle_keys:
                seen_cycle_keys.add(key)
                cycles.append([*key, key[0]])
            return
        on_path.add(n)
        stack.append(n)
        for nxt in sorted(graph.get(n, ())):
            dfs(nxt)
        stack.pop()
        on_path.remove(n)
        done.add(n)

    for n in sorted(nodes):
        dfs(n)
    return cycles


def test_known_backend_names_match_registry() -> None:
    # Invariant: config's static backend name set must not drift from the registry.
    assert KNOWN_BACKEND_NAMES == frozenset(backend_names())


def test_known_credential_env_vars_match_backends() -> None:
    # Invariant: env's static credential-name set must not drift from backend ClassVars.
    live: set[str] = set()
    for backend in default_backends().values():
        live.update(backend.credential_env_vars)
    assert KNOWN_CREDENTIAL_ENV_VARS == frozenset(live)


def _layer_of(mod: str) -> str | None:
    """The layer package a module lives in, or None for shims / ``__init__``."""
    head = mod.split(".")[0]
    return head if head in _RANK else None


def test_every_module_lives_in_a_layer() -> None:
    # A new top-level module must be placed in a layer, not dropped in the package root.
    stray = sorted(
        _module_key(p)
        for p in _PKG.glob("*.py")
        if p.name != "__init__.py" and p.stem not in _SHIMS
    )
    assert not stray, f"top-level modules must live in a layer package (or be a shim): {stray}"


def test_imports_point_downward() -> None:
    # Invariant: a layer may import its own layer or a strictly lower one. This subsumes the
    # old deny-list: types→worktree is core→runtime, config→registry is core→orchestration.
    violations = []
    for src, dst in sorted(_all_edges()):
        a, b = _layer_of(src), _layer_of(dst)
        if a is None or b is None or a == b:
            continue
        if _RANK[b] >= _RANK[a]:
            violations.append(f"{src} -> {dst}  ({a}[{_RANK[a]}] -> {b}[{_RANK[b]}])")
    assert not violations, "imports must point downward:\n  " + "\n  ".join(violations)


def test_shims_only_reexport() -> None:
    # A shim exists to preserve a published import path - it must never grow logic, or the
    # module would have two homes with different behaviour.
    for name in sorted(_SHIMS):
        path = _PKG / f"{name}.py"
        assert path.exists(), f"missing shim for published path marshal_engine.{name}"
        # `ast.Expr` is allowed ONLY when it is a docstring. A bare `ast.Expr` exclusion also
        # waved through every module-level call - a `print`, a registration hook, any import-time
        # side effect - which is exactly the "shim grew logic" case this test exists to catch.
        # Position matters: Python treats only the FIRST statement as the docstring. Accepting a
        # string expression anywhere would still wave through a bare string mid-module, which is a
        # statement with no effect - the same "shim grew a body" smell this test exists to catch.
        module_body = ast.parse(path.read_text(encoding="utf-8")).body

        def _is_docstring(i: int, n: ast.stmt) -> bool:
            return (
                i == 0
                and isinstance(n, ast.Expr)
                and isinstance(n.value, ast.Constant)
                and isinstance(n.value.value, str)
            )

        body = [
            n
            for i, n in enumerate(module_body)
            if not isinstance(n, (ast.Import, ast.ImportFrom)) and not _is_docstring(i, n)
        ]
        assert not body, f"shim {name}.py must only re-export, found: {body}"


def test_import_graph_is_acyclic() -> None:
    # Invariant: no import cycles, including ones that only close via function-local imports.
    edges = _all_edges()
    found = _cycles(edges)
    assert not found, "import cycles:\n  " + "\n  ".join(" -> ".join(c) for c in found)


def test_init_does_not_appear_as_import_target_outside_reexports() -> None:
    # Soft: ``__init__`` may import others (re-exports); nothing inside the package should
    # import ``__init__`` (that was the eastrouter violation). External users are out of scope.
    edges = _all_edges()
    offenders = sorted((a, b) for a, b in edges if b == "__init__" and a != "__init__")
    assert not offenders, f"package modules must not import __init__: {offenders}"
