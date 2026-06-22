"""Marshal - orchestration engine for a fleet of headless coding agents.

Import package is `marshal_engine` (the name `marshal` is a stdlib builtin and cannot be
used as a top-level package). The installed CLI command is `marshal`.
"""

from __future__ import annotations

from .backends.base import CodingAgentBackend
from .types import (
    AgentResult,
    Capabilities,
    PermissionMode,
    RunOpts,
    RunStatus,
    TaskSpec,
    UsageRecord,
    UsageSource,
)

__version__ = "0.0.1"

__all__ = [
    "CodingAgentBackend",
    "AgentResult",
    "Capabilities",
    "PermissionMode",
    "RunOpts",
    "RunStatus",
    "TaskSpec",
    "UsageRecord",
    "UsageSource",
    "__version__",
]
