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
                 INTEGRATIONS / "claude-code" / "project.mcp.json",
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


def test_agent_rules_and_skill_teach_the_coding_handoff_protocol():
    rules = (INTEGRATIONS / "AGENT_RULES.md").read_text()
    skill = (INTEGRATIONS / "claude-code" / "skills" / "continuum" / "SKILL.md").read_text()
    for text in (rules, skill):
        for tool in ("coding_resume", "coding_run_check", "coding_complete_task", "next_step"):
            assert tool in text
    assert "continuum coding resume" in rules  # CLI fallback for agents without MCP
    # the cloud config takes the database URL (a secret) from the environment, never inline
    project = _servers(INTEGRATIONS / "claude-code" / "project.mcp.json")["continuum"]
    assert project["env"]["CONTINUUM_DATABASE_URL"] == "${CONTINUUM_DATABASE_URL}"
    assert "postgresql://" not in (INTEGRATIONS / "cloud" / "setup.sh").read_text()
