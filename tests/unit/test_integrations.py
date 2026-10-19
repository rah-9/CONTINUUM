# SPDX-License-Identifier: LicenseRef-Continuum-Proprietary
# Copyright (c) 2026 the Continuum copyright holder. All rights reserved.
"""Shipped agent integrations are well-formed and launch the stdio MCP server."""

import json
import tomllib
from pathlib import Path

import pytest

ROOT = Path(__file__).resolve().parents[2]
INTEGRATIONS = ROOT / "integrations"


def _servers(path: Path) -> dict:
    data = json.loads(path.read_text())
    return data.get("mcpServers") or data["servers"]


def test_mcp_client_configs_launch_continuum_mcp():
    configs = sorted(INTEGRATIONS.glob("*/*.json")) + sorted(INTEGRATIONS.glob("*/.mcp.json"))
    assert {p.parent.name for p in configs} >= {
        "claude-code", "cursor", "vscode", "claude-desktop", "windsurf", "gemini-cli", "cline"}
    for path in configs:
        server = _servers(path)["continuum"]
        assert server["command"] == "continuum" and server["args"] == ["mcp"], path
        # copied verbatim, a config must work: no placeholder paths to forget to edit
        assert "/absolute/path" not in path.read_text(), path
    codex_text = (INTEGRATIONS / "codex" / "config.toml").read_text()
    codex = tomllib.loads(codex_text)
    assert codex["mcp_servers"]["continuum"]["args"] == ["mcp"]
    assert "/absolute/path" not in codex_text


def _real_bash() -> str | None:
    """bash, but not Windows' WSL launcher (System32\\bash.exe), which has no distro on CI."""
    import os
    import shutil

    if os.name != "nt":
        return shutil.which("bash")
    git = shutil.which("git")
    candidates = [Path(git).resolve().parents[1] / "bin" / "bash.exe"] if git else []
    candidates.append(Path(os.environ.get("PROGRAMFILES", r"C:\Program Files"))
                      / "Git" / "bin" / "bash.exe")
    return next((str(c) for c in candidates if c.is_file()), None)


def test_cloud_setup_script_is_valid_bash():
    import subprocess

    script = INTEGRATIONS / "cloud" / "setup.sh"
    bash = _real_bash()
    if bash is None:
        pytest.skip("no bash on this machine (the script runs in Linux containers)")
    subprocess.run([bash, "-n", str(script)], check=True)
    text = script.read_text()
    assert "uv tool install" in text and "--python 3.12" in text  # PEP 668 / old pythons


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
    # cloud: the secret from the environment; local: falls back to the local default
    assert project["env"]["CONTINUUM_DATABASE_URL"].startswith("${CONTINUUM_DATABASE_URL:-")
    assert "postgresql://" not in (INTEGRATIONS / "cloud" / "setup.sh").read_text()


def test_apps_journal_the_chat_through_the_hook_command():
    """Switching apps (Claude Code <-> Codex) needs both to journal the conversation."""
    hooks = json.loads((INTEGRATIONS / "claude-code" / "hooks" / "hooks.json").read_text())
    from continuum.coding.install import CLAUDE_HOOK_EVENTS, TOOL_EVENTS

    assert set(hooks["hooks"]) == set(CLAUDE_HOOK_EVENTS)    # plugin and setup agree
    for event in CLAUDE_HOOK_EVENTS:
        commands = [h["command"] for g in hooks["hooks"][event] for h in g["hooks"]]
        assert commands == ["continuum hook claude-code"], event
        if event in TOOL_EVENTS:                             # every tool, MCP included
            assert [g.get("matcher") for g in hooks["hooks"][event]] == ["*"], event
    codex = tomllib.loads((INTEGRATIONS / "codex" / "config.toml").read_text())
    assert codex["notify"] == ["continuum", "hook", "codex"]


def test_packaged_agent_rules_match_the_published_rules():
    packaged = ROOT / "src" / "continuum" / "coding" / "agent_rules.md"
    assert packaged.read_text() == (INTEGRATIONS / "AGENT_RULES.md").read_text()
