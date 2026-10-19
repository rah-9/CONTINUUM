# SPDX-License-Identifier: LicenseRef-Continuum-Proprietary
# Copyright (c) 2026 the Continuum copyright holder. All rights reserved.
"""``continuum coding setup``: make a repository continuous across apps in one command.

Writes only project files, idempotently (re-running changes nothing):

* ``AGENTS.md``: the agent rules between markers (Codex, Cursor, Gemini CLI, most agents
  read it). An existing marked block is replaced; the rest of the file is kept.
* ``CLAUDE.md``: a marked line importing ``@AGENTS.md`` (Claude Code reads CLAUDE.md),
  unless it already imports it.
* ``.claude/settings.json``: Claude Code hooks (capture the conversation; show the brief
  at session start), merged with existing hooks.
* ``.mcp.json``: the ``continuum`` MCP server for Claude Code, if absent.

User-level files (``~/.codex/config.toml``) are never edited: the snippet is returned
for the user to add. No database access is needed.
"""

from __future__ import annotations

import json
from importlib import resources
from pathlib import Path
from typing import Any

BEGIN = "<!-- continuum:begin (managed by `continuum coding setup`) -->"
END = "<!-- continuum:end -->"
HOOK_COMMAND = "continuum hook claude-code"
CLAUDE_HOOK_EVENTS = ("SessionStart", "UserPromptSubmit", "PreToolUse", "PostToolUse",
                      "PostToolUseFailure", "SubagentStop", "PreCompact", "PostCompact",
                      "Stop", "SessionEnd")
TOOL_EVENTS = frozenset({"PreToolUse", "PostToolUse", "PostToolUseFailure"})
HOOK_TIMEOUT = 15

CODEX_SNIPPET = """\
# ~/.codex/config.toml  (Windows: %USERPROFILE%\\.codex\\config.toml)
# `notify` must be a top-level key: put it above any [table].
notify = ["continuum", "hook", "codex"]

[mcp_servers.continuum]
command = "continuum"
args = ["mcp"]
"""


def agent_rules() -> str:
    return resources.files("continuum.coding").joinpath("agent_rules.md").read_text(
        encoding="utf-8")


def _managed(existing: str, block: str) -> str:
    """Replace the managed block, or append it; the rest of the file is untouched."""
    body = f"{BEGIN}\n{block.strip()}\n{END}\n"
    if BEGIN in existing and END in existing:
        head, rest = existing.split(BEGIN, 1)
        tail = rest.split(END, 1)[1].lstrip("\n")
        return head + body + tail
    sep = "" if not existing or existing.endswith("\n\n") else (
        "\n" if existing.endswith("\n") else "\n\n")
    return existing + sep + body


def _write(path: Path, text: str, dry_run: bool) -> str:
    old = path.read_text(encoding="utf-8") if path.exists() else None
    if old == text:
        return "unchanged"
    if not dry_run:
        path.parent.mkdir(parents=True, exist_ok=True)
        path.write_text(text, encoding="utf-8")
    return "created" if old is None else "updated"


def _claude_settings(existing: dict[str, Any]) -> dict[str, Any]:
    hooks = existing.setdefault("hooks", {})
    for event in CLAUDE_HOOK_EVENTS:
        groups = hooks.setdefault(event, [])
        present = any(HOOK_COMMAND in str(h.get("command", ""))
                      for g in groups if isinstance(g, dict)
                      for h in g.get("hooks", []) if isinstance(h, dict))
        if not present:
            group: dict[str, Any] = {"hooks": [{"type": "command", "command": HOOK_COMMAND,
                                                "timeout": HOOK_TIMEOUT}]}
            if event in TOOL_EVENTS:     # every tool, including MCP tools, skills, subagents
                group = {"matcher": "*", **group}
            groups.append(group)
    return existing


def _load_json(path: Path) -> dict[str, Any]:
    if not path.exists():
        return {}
    data = json.loads(path.read_text(encoding="utf-8") or "{}")
    if not isinstance(data, dict):
        raise ValueError(f"{path} is not a JSON object; fix it or remove it first")
    return data


def setup(repo: str, *, dry_run: bool = False) -> dict[str, Any]:
    root = Path(repo).expanduser().resolve()
    if not root.is_dir():
        raise ValueError(f"{repo} is not a directory")
    files: dict[str, str] = {}

    agents = root / "AGENTS.md"
    old = agents.read_text(encoding="utf-8") if agents.exists() else ""
    files["AGENTS.md"] = _write(agents, _managed(old, agent_rules()), dry_run)

    claude = root / "CLAUDE.md"
    old = claude.read_text(encoding="utf-8") if claude.exists() else ""
    if "@AGENTS.md" in old and BEGIN not in old:
        files["CLAUDE.md"] = "unchanged (already imports AGENTS.md)"
    else:
        files["CLAUDE.md"] = _write(claude, _managed(
            old, "Continuum rules for every model and chat: @AGENTS.md"), dry_run)

    settings_path = root / ".claude" / "settings.json"
    settings = _claude_settings(_load_json(settings_path))
    files[".claude/settings.json"] = _write(
        settings_path, json.dumps(settings, indent=2) + "\n", dry_run)

    mcp_path = root / ".mcp.json"
    mcp = _load_json(mcp_path)
    mcp.setdefault("mcpServers", {}).setdefault(
        "continuum", {"command": "continuum", "args": ["mcp"]})
    files[".mcp.json"] = _write(mcp_path, json.dumps(mcp, indent=2) + "\n", dry_run)

    return {
        "repo": str(root), "dry_run": dry_run, "files": files,
        "codex": {"add_to_user_config": CODEX_SNIPPET,
                  "why": "Codex reads AGENTS.md (written above); the MCP server and the "
                         "notify hook live in the user's own config, which setup never edits"},
        "next": ["a human registers checks: continuum coding checks add . tests --actor "
                 "<you> -- <test command>",
                 "commit AGENTS.md, CLAUDE.md, .claude/settings.json and .mcp.json so every "
                 "checkout (and cloud container) gets the same behaviour"],
    }
