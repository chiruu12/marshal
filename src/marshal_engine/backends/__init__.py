"""Backend adapters. Each backend subclasses CodingAgentBackend."""

from __future__ import annotations

from .base import CodingAgentBackend

# Adapters are imported by module path (e.g. marshal_engine.backends.codex.CodexBackend).
# A backend registry will be added when the fleet/MCP layer lands.

__all__ = ["CodingAgentBackend"]
