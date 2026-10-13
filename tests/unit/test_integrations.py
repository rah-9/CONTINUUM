"""Shipped agent integrations are well-formed and launch the stdio MCP server."""

import json
import tomllib
from pathlib import Path

ROOT = Path(__file__).resolve().parents[2]
INTEGRATIONS = ROOT / "integrations"


def _servers(path: Path) -> dict:
    data = json.loads(path.read_text())
    return data.get("mcpServers") or data["servers"]


def test_mcp_client_configs_launch_continuum_mcp():
    for path in [INTEGRATIONS / "claude-code" / ".mcp.json",
                 INTEGRATIONS / "cursor" / "mcp.json",
                 INTEGRATIONS / "vscode" / "mcp.json",
                 INTEGRATIONS / "claude-desktop" / "claude_desktop_config.json"]:
        server = _servers(path)["continuum"]
        assert server["command"] == "continuum" and server["args"] == ["mcp"], path
    codex = tomllib.loads((INTEGRATIONS / "codex" / "config.toml").read_text())
    assert codex["mcp_servers"]["continuum"]["args"] == ["mcp"]


def test_claude_code_plugin_and_marketplace():
    plugin = json.loads((INTEGRATIONS / "claude-code" / ".claude-plugin" /
                         "plugin.json").read_text())
    assert plugin["name"] == "continuum"
    market = json.loads((ROOT / ".claude-plugin" / "marketplace.json").read_text())
    [entry] = market["plugins"]
    assert (ROOT / entry["source"] / ".claude-plugin" / "plugin.json").is_file()
    skill = (INTEGRATIONS / "claude-code" / "skills" / "continuum" / "SKILL.md").read_text()
    assert skill.startswith("---\nname: continuum\ndescription: ")
    assert "do not decide for the user" in skill.lower()
