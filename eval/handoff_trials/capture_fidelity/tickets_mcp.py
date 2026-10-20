# SPDX-License-Identifier: LicenseRef-Continuum-Proprietary
# Copyright (c) 2026 the Continuum copyright holder. All rights reserved.
"""A tiny MCP server standing in for an external ticket system (Trial 7). Creating a
ticket writes outside the repository, like a real Jira/GitHub/Linear MCP server."""

import json
import os
from pathlib import Path
from typing import Any

from mcp.server.mcpserver import MCPServer

STORE = Path(os.environ.get("TICKETS_FILE", "/tmp/tickets.json"))
server: MCPServer[Any] = MCPServer("tickets")


def _load() -> list[dict[str, Any]]:
    return json.loads(STORE.read_text()) if STORE.exists() else []


@server.tool()
def list_tickets() -> dict[str, Any]:
    """List open tickets."""
    return {"tickets": _load()}


@server.tool()
def create_ticket(title: str, body: str = "") -> dict[str, Any]:
    """Create a ticket in the team's tracker. Returns its id."""
    tickets = _load()
    ticket = {"id": f"TCK-{100 + len(tickets)}", "title": title, "body": body}
    tickets.append(ticket)
    STORE.write_text(json.dumps(tickets))
    return ticket


if __name__ == "__main__":
    server.run("stdio")
