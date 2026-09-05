"""The scope tool handlers share.

Previously these were closure variables inside `build_app`; passing them explicitly means a tool
group is a plain module that states what it needs instead of inheriting a nested scope.
"""

from __future__ import annotations

from collections.abc import Awaitable, Callable
from dataclasses import dataclass
from typing import Any, Protocol, TypeVar

from ..workspaces import WorkspaceRegistry

_T = TypeVar("_T")


class _Offload(Protocol):
    """Callback protocol, not a plain ``Callable``, so the wrapped call's return type survives.

    ``Callable[..., Awaitable[Any]]`` would erase it and every ``await offload(...)`` would come
    back as ``Any`` - silently unchecked at exactly the boundary the MCP layer serializes.
    """

    def __call__(self, fn: Callable[..., _T], /, *args: Any, **kwargs: Any) -> Awaitable[_T]: ...


@dataclass(frozen=True)
class ToolContext:
    """Shared helpers handed to each tool group's `register(app, ctx)`."""

    registry: WorkspaceRegistry
    #: Run a (possibly long, blocking) service call off the event loop.
    offload: _Offload
    #: Resolve a workspace service, offload the call, tag the payload with the workspace.
    ws_call: Callable[..., Awaitable[dict[str, Any]]]
    #: Resolve a run's OWNING workspace, offload the call, tag the payload.
    run_call: Callable[..., Awaitable[dict[str, Any]]]
    #: Stamp a result with the workspace it came from.
    tag: Callable[[dict[str, Any], str], dict[str, Any]]
    #: Build the exception for a refusal raised in a tool BODY (outside `offload`, which converts
    #: the engine's own). The SDK keeps a deliberate `ToolError`'s text and redacts everything
    #: else, and a refusal's text is the only thing telling the driver what to do instead - so it
    #: must not be raised as a bare `ValueError`. A callable, not a direct import, because `mcp`
    #: is an optional extra and these modules are imported whether or not it is installed.
    refuse: Callable[[str], Exception]
    #: Captured ONCE at construction: mid-session os.environ mutation must not widen authority.
    allow_mcp_registration: bool
