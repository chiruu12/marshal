"""Marshal - orchestration engine for a fleet of headless coding agents.

Import package is `marshal_engine` (the name `marshal` is a stdlib builtin and cannot be
used as a top-level package). The installed CLI command is `marshal`.
"""

from __future__ import annotations

from importlib.metadata import PackageNotFoundError
from importlib.metadata import version as _pkg_version

from .backends.base import CodingAgentBackend
from .types import (
    AgentResult,
    Capabilities,
    PermissionFidelity,
    PermissionMode,
    RunOpts,
    RunStatus,
    TaskSpec,
    UsageRecord,
    UsageSource,
)

# Read from installed package metadata so there is ONE source of truth (pyproject's
# [project].version). A hardcoded literal here silently drifts: the wheel says one version and
# `marshal --version` says another, which then lands in every bug report.
try:
    __version__ = _pkg_version("MarshalFleet")
except PackageNotFoundError:  # running from a source tree that was never installed
    __version__ = "0.0.0+unknown"

__all__ = [
    "CodingAgentBackend",
    "AgentResult",
    "Capabilities",
    "PermissionFidelity",
    "PermissionMode",
    "RunOpts",
    "RunStatus",
    "TaskSpec",
    "UsageRecord",
    "UsageSource",
    "__version__",
]
