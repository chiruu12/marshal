"""Backend registry - construct adapters by name (used by the CLI / MCP layer).

The Fleet itself takes injected backends for testability; this registry supplies the real ones.
"""

from __future__ import annotations

from collections.abc import Callable

from .backends.antigravity import AntigravityBackend
from .backends.base import CodingAgentBackend
from .backends.claude_code import ClaudeCodeBackend
from .backends.codex import CodexBackend
from .backends.command_code import CommandCodeBackend
from .backends.cursor import CursorBackend
from .backends.goose import GooseBackend
from .backends.opencode import OpenCodeBackend

_FACTORIES: dict[str, Callable[[], CodingAgentBackend]] = {
    "cursor": CursorBackend,
    "opencode": OpenCodeBackend,
    "codex": CodexBackend,
    "command-code": CommandCodeBackend,
    "antigravity": AntigravityBackend,
    "claude-code": ClaudeCodeBackend,
    "goose": GooseBackend,
}


def backend_names() -> list[str]:
    return list(_FACTORIES)


def make_backend(name: str) -> CodingAgentBackend:
    if name not in _FACTORIES:
        raise ValueError(f"unknown backend {name!r}; known: {', '.join(_FACTORIES)}")
    return _FACTORIES[name]()


def default_backends() -> dict[str, CodingAgentBackend]:
    return {name: factory() for name, factory in _FACTORIES.items()}
