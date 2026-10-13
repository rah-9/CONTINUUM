"""Thin local stdio MCP facade: ``run_task`` and ``get_status`` only (R10).

Delegates pre-registered task specs; it does not expose tools, discover remote
MCP servers or accept arbitrary task specs.
"""

from __future__ import annotations

from dataclasses import asdict
from typing import Any

from continuum.core.errors import ContinuumError
from continuum.interfaces.sdk import Continuum


class McpFacade:
    """Transport-independent facade logic (unit-testable without stdio)."""

    def __init__(self, sdk: Continuum) -> None:
        self.sdk = sdk

    def run_task(self, task_id: str) -> dict[str, Any]:
        if task_id not in self.sdk.registry.task_ids():
            return {"error": f"task {task_id!r} is not pre-registered",
                    "available": self.sdk.registry.task_ids()}
        try:
            result = self.sdk.start(task_id)
        except ContinuumError as exc:
            return {"error": str(exc), "code": exc.code}
        data = asdict(result)
        data["status"] = result.status.value
        return data

    def get_status(self, run_id: str) -> dict[str, Any]:
        try:
            return self.sdk.status(run_id)
        except ContinuumError as exc:
            return {"error": str(exc), "code": exc.code}


def run_stdio(sdk: Continuum) -> None:
    from mcp.server.mcpserver import MCPServer  # mcp>=2 (FastMCP was renamed)

    facade = McpFacade(sdk)
    server: MCPServer[Any] = MCPServer("continuum")

    @server.tool()
    def run_task(task_id: str) -> dict[str, Any]:
        """Run a pre-registered Continuum task to completion or an explicit pause."""
        return facade.run_task(task_id)

    @server.tool()
    def get_status(run_id: str) -> dict[str, Any]:
        """Get durable status (milestones, actions, usage) of a Continuum run."""
        return facade.get_status(run_id)

    server.run("stdio")
