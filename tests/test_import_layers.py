"""Import-graph layer guard — acyclic graph + a small deny-list of forbidden edges.

Invariant: layers import downward only. Lazy / function-local imports are still edges
(they were used to dodge cycles); TYPE_CHECKING-guarded imports count too. This test
parses the package with AST so those cannot hide.

Forbidden edges (regressions of the SOLID audit fixes):
  types → worktree, config → registry, eastrouter → __init__

Known soft edges (allowed, documented — not pretended away):
  worktree → config, doctor → teams, and ``__init__`` re-exports of public types.
"""

from __future__ import annotations

import ast
from collections import defaultdict
from pathlib import Path

import marshal_engine
from marshal_engine.config import KNOWN_BACKEND_NAMES
from marshal_engine.env import KNOWN_CREDENTIAL_ENV_VARS
from marshal_engine.registry import backend_names, default_backends

_PKG = Path(marshal_engine.__file__).resolve().parent
_PKG_NAME = "marshal_engine"

# Edges that must never reappear (lazy or module-level).
_FORBIDDEN: frozenset[tuple[str, str]] = frozenset(
    {
        ("types", "worktree"),
        ("config", "registry"),
        ("eastrouter", "__init__"),
        ("env", "registry"),  # latent cycle with backends.base → env
    }
)

# Soft edges that exist today; listed so a future layer matrix does not treat them as bugs.
# ``__init__`` may import public symbols (re-exports); nothing else should import ``__init__``.
_KNOWN_SOFT: frozenset[tuple[str, str]] = frozenset(
    {
        ("worktree", "config"),
        ("doctor", "teams"),
    }
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
    # ``from . import X`` executes the package ``__init__`` (binds submodule or attribute).
    return ["__init__"] if not base else [".".join(base)]


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


def test_known_soft_edges_still_present() -> None:
    # Documented soft edges should still exist — if one disappears, drop it from the allow-list.
    edges = _all_edges()
    missing = sorted(_KNOWN_SOFT - edges)
    assert not missing, f"known soft edges gone (update _KNOWN_SOFT): {missing}"


def test_forbidden_layer_edges_absent() -> None:
    # Invariant: the three SOLID-audit violations must not return (lazy imports count).
    edges = _all_edges()
    found = sorted(_FORBIDDEN & edges)
    assert not found, f"forbidden import edges reintroduced: {found}"


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
