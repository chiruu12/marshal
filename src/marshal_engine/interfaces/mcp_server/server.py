
from __future__ import annotations

import os
import sys
from collections.abc import Callable
from pathlib import Path
from typing import Any

from pydantic import BaseModel

from ...core._version import __version__
from ...runtime.env import merge_user_path
from ..service import MarshalService
from ..workspaces import (
    DEFAULT_WORKSPACE,
    WorkspaceDef,
    WorkspaceRegistry,
    build_service_for,
)
from .context import ToolContext
from .schema import _ALLOW_MCP_REGISTRATION_ENV, _T

# Imported by module, not `from . import ...`: the latter executes this package's __init__,
# which imports this module - a cycle the layer guard rejects.
from .tools_inspect import register as _register_inspect
from .tools_integrate import register as _register_integrate
from .tools_recipes import register as _register_recipes
from .tools_runs import register as _register_runs
from .tools_workspaces import register as _register_workspaces


def build_service() -> MarshalService:
    """Build the single DEFAULT-workspace service from the environment (the legacy entry point).

    Retained for the library/test path and reused by the registry's default builder. Multi-workspace
    wiring goes through WorkspaceRegistry.from_env() in main().
    """
    repo = Path(os.environ.get("MARSHAL_REPO", "."))
    cfg_path = Path(os.environ.get("MARSHAL_CONFIG") or repo / "fleet.config.yaml")
    return build_service_for(
        WorkspaceDef(name=DEFAULT_WORKSPACE, path=repo, config_path=cfg_path),
        missing_config="legacy",
        config_warnings="plain",
    )


def build_app(target: WorkspaceRegistry | MarshalService) -> Any:
    """Construct the MCPServer app over a WorkspaceRegistry (backend AND workspace are per-call params).

    Accepts a bare MarshalService too (wrapped as a one-workspace registry) for the single-repo and
    test paths. Each tool is async and offloads its service call to a worker thread via anyio, so a
    blocking run() never holds the event loop - the driver can poll/cancel an in-flight run.
    """
    import anyio.to_thread
    from mcp.server.mcpserver import MCPServer

    registry = target if isinstance(target, WorkspaceRegistry) else WorkspaceRegistry.for_service(target)
    # Report our version in the initialize handshake. Without it every client sees an empty
    # string, so "which Marshal am I talking to?" is unanswerable from the client side - exactly
    # the question you ask first when a tool misbehaves.
    app = MCPServer("marshal", version=__version__)
    # Captured ONCE at construction: mid-session os.environ mutation must not widen authority.
    allow_mcp_registration = os.environ.get(_ALLOW_MCP_REGISTRATION_ENV) == "1"

    async def offload(fn: Callable[..., _T], *args: Any, **kwargs: Any) -> _T:
        """Run a (possibly long, blocking) service call off the event loop."""
        return await anyio.to_thread.run_sync(lambda: fn(*args, **kwargs))

    def tag(payload: dict[str, Any], workspace: str) -> dict[str, Any]:
        """Stamp a result with the workspace it came from, so the driver can route follow-ups."""
        return {**payload, "workspace": workspace}

    def dump_result(result: Any) -> dict[str, Any]:
        if isinstance(result, BaseModel):
            return result.model_dump(mode="json")
        assert isinstance(result, dict)
        return result

    async def ws_call(
        workspace: str | None,
        fn: Callable[..., _T],
        /,
        *args: Any,
        **kwargs: Any,
    ) -> dict[str, Any]:
        """Resolve a workspace service, offload ``fn(svc, *args, **kwargs)``, tag the JSON payload."""
        svc = await offload(registry.get, workspace)
        ws = workspace or DEFAULT_WORKSPACE
        result = await offload(fn, svc, *args, **kwargs)
        return tag(dump_result(result), ws)

    async def run_call(
        run_id: str,
        workspace: str | None,
        fn: Callable[..., _T],
        /,
        *args: Any,
        **kwargs: Any,
    ) -> dict[str, Any]:
        """Resolve a run's owning workspace, offload ``fn(svc, *args, **kwargs)``, tag the payload."""
        name, svc = await offload(registry.require_run, run_id, workspace)
        result = await offload(fn, svc, *args, **kwargs)
        return tag(dump_result(result), name)

    ctx = ToolContext(
        registry=registry,
        offload=offload,
        ws_call=ws_call,
        run_call=run_call,
        tag=tag,
        allow_mcp_registration=allow_mcp_registration,
    )
    # Registration order is the order tools appear to a driver listing them.
    _register_inspect(app, ctx)
    _register_runs(app, ctx)
    _register_integrate(app, ctx)
    _register_recipes(app, ctx)
    _register_workspaces(app, ctx)

    return app


def detach_from_host_terminal() -> bool:
    """Leave the host's session when we were spawned as a stdio server. Returns True if we did.

    A stdio MCP server is a child of its host, so by default it shares the host's process group and
    controlling terminal. Anything in that group that touches the terminal from outside the
    foreground group raises SIGTTIN/SIGTTOU, and those are delivered to the *whole group* - the host
    is suspended (ps STAT ``T``) with no crash, no exit code, and nothing on stderr. Marshal spawns
    a lot of children, including an interactive login shell that sources the user's rc files, so it
    is in a position to do this to its host. ``DETACHED_STDIO`` stops each child from inheriting a
    terminal; this stops *us* from holding one at all, which closes the class rather than the
    instances.

    Gated on stdin not being a tty, which is exactly the "a host spawned me over a pipe" case. Run
    by hand in a terminal we stay in the shell's job, so Ctrl-C still works. Failures are ignored:
    ``setsid`` raises EPERM when we are already a process-group leader, which means there is no
    host session to leave.
    """
    if not hasattr(os, "setsid"):
        return False
    try:
        # sys.stdin is None when stdin was closed outright, and isatty() raises on a detached
        # stream. Neither is a terminal, so both mean "detach"; neither may abort startup.
        on_a_terminal = sys.stdin is not None and sys.stdin.isatty()
    except (OSError, ValueError):
        on_a_terminal = False
    if on_a_terminal:
        return False
    try:
        os.setsid()
    except OSError:
        return False
    return True


def main() -> None:
    detach_from_host_terminal()
    # The MCP host (Claude Code, Cursor, ...) often spawns us with a stripped PATH that lacks the
    # user's zshrc-managed directories (Homebrew, ~/.local/bin, npm-global). Backend CLIs installed
    # there then look missing to shutil.which and `marshal doctor` falsely FAILs them. Augment PATH
    # from the user's login shell *before* the registry builds backends, so every tool sees the
    # real environment. No-op if PATH is already complete or MARSHAL_NO_PATH_FIX=1.
    merge_user_path()
    registry = WorkspaceRegistry.from_env()
    # Build the default workspace eagerly so the connect-time config message + warnings still fire at
    # startup (named workspaces build lazily on first touch).
    registry.get(DEFAULT_WORKSPACE)
    build_app(registry).run()


if __name__ == "__main__":
    main()
