
from __future__ import annotations

from typing import TYPE_CHECKING, Annotated, Any

from pydantic import Field

from ..scaffold import scaffold_fleet_config
from .context import ToolContext

if TYPE_CHECKING:  # the mcp SDK is an optional extra; only needed for typing here
    from mcp.server.mcpserver import MCPServer
from .schema import _ALLOW_MCP_REGISTRATION_ENV


def register(app: MCPServer, ctx: ToolContext) -> None:
    """Register this group's tools on ``app``."""
    registry = ctx.registry
    offload = ctx.offload
    allow_mcp_registration = ctx.allow_mcp_registration

    @app.tool()
    async def list_workspaces() -> list[dict[str, Any]]:
        """List the repos this server can target: name, path, config_path, configured, client_count,
        and which is the default. Pass a name as the `workspace` param on the other tools."""
        return await offload(registry.describe)

    @app.tool()
    async def add_workspace(
        name: Annotated[str, Field(description="Short name to register the repo under (letters, digits, ._-).")],
        path: Annotated[str, Field(description="Absolute path to the repo (an existing directory).")],
        scaffold: Annotated[
            bool, Field(description="Also drop a starter fleet.config.yaml if the repo has none.")
        ] = False,
    ) -> dict[str, Any]:
        """Register a repo as a workspace in the central registry (~/.marshal/workspaces.yaml) so it
        can be targeted by `workspace=`. Available immediately - no reconnect. The path must be an
        existing directory; a repo with no fleet.config.yaml registers with zero clients until one is
        added (pass scaffold=true to drop a starter in). Then call list_workspaces / list_clients.

        DISABLED BY DEFAULT: refuses unless the server was started with
        MARSHAL_ALLOW_MCP_WORKSPACE_REGISTRATION=1. On refusal, ask the operator to run
        `marshal workspace add <name> <path>` instead (hot-reloaded - no reconnect needed)."""
        if not allow_mcp_registration:
            raise ValueError(
                "MCP workspace registration is disabled by default. Ask the operator to register "
                "the repo with `marshal workspace add <name> <path>` (hot-reloaded, no reconnect "
                f"needed), or to restart the MCP server with {_ALLOW_MCP_REGISTRATION_ENV}=1 to "
                "allow registration through this tool."
            )

        def _do() -> dict[str, Any]:
            wdef = registry.add(name, path)  # writes the file this registry reads; hot-reloads in
            return {
                "name": wdef.name,
                "path": str(wdef.path),
                "config_path": str(wdef.config_path),
                "scaffolded": scaffold_fleet_config(wdef.path) if scaffold else False,
            }

        return await offload(_do)
