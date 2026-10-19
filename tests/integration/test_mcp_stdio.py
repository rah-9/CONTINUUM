# SPDX-License-Identifier: LicenseRef-Continuum-Proprietary
# Copyright (c) 2026 the Continuum copyright holder. All rights reserved.
"""Continuum as an MCP plugin: real stdio protocol round-trips against `continuum mcp`."""

import asyncio
import json
import os
import shutil
import subprocess
import sys
from pathlib import Path

import pytest
from mcp import ClientSession, StdioServerParameters
from mcp.client.stdio import stdio_client

from tests.conftest import DSN

ROOT = Path(__file__).resolve().parents[2]
EXPECTED_TOOLS = {"list_tasks", "run_task", "resume_run", "cancel_run", "get_status",
                  "inspect_run", "pending_decisions", "list_runs",
                  "coding_resume", "coding_start", "coding_brief", "coding_add_task",
                  "coding_set_task_status", "coding_complete_task", "coding_note",
                  "coding_checkpoint", "coding_run_check", "coding_sessions", "coding_close",
                  "coding_recall"}


@pytest.fixture
def server_env(schema, tmp_path):
    return {**os.environ, "CONTINUUM_DATABASE_URL": DSN, "CONTINUUM_DB_SCHEMA": schema,
            "CONTINUUM_HOME": str(tmp_path / "home"),
            "CONTINUUM_CONFIG_DIR": str(ROOT / "config")}


def _continuum() -> str:
    exe = shutil.which("continuum", path=str(Path(sys.executable).parent))
    assert exe, "continuum console script not installed"
    return exe


def _payload(result) -> dict:
    structured = getattr(result, "structured_content", None) or getattr(
        result, "structuredContent", None)
    if isinstance(structured, dict):
        return structured.get("result", structured) if set(structured) == {"result"} \
            else structured
    return json.loads(result.content[0].text)


async def _session_flow(env: dict) -> dict:
    params = StdioServerParameters(command=_continuum(), args=["mcp"], env=env)
    async with stdio_client(params) as (read, write), ClientSession(read, write) as session:
        await session.initialize()
        tools = {t.name for t in (await session.list_tools()).tools}
        tasks = _payload(await session.call_tool("list_tasks", {}))
        paused = _payload(await session.call_tool(
            "run_task", {"task_id": "demo_opaque_needs_human"}))
        pending = _payload(await session.call_tool(
            "pending_decisions", {"run_id": paused["run_id"]}))
        # The human (not the agent) makes the decision through the CLI.
        action_id = pending["decisions"][0]["action_id"]
        subprocess.run([_continuum(), "resolve", paused["run_id"], action_id, "confirmed",
                        "--actor", "operator", "--reason", "seen in provider console",
                        "--evidence", "receipt:42"], env=env, check=True,
                       capture_output=True)
        resumed = _payload(await session.call_tool(
            "resume_run", {"run_id": paused["run_id"], "model_alias": "B"}))
        timeline = _payload(await session.call_tool("inspect_run",
                                                    {"run_id": paused["run_id"]}))
        unknown = _payload(await session.call_tool("run_task", {"task_id": "rm_rf_slash"}))
    return {"tools": tools, "tasks": tasks, "paused": paused, "pending": pending,
            "resumed": resumed, "timeline": timeline, "unknown": unknown}


def test_mcp_plugin_end_to_end_over_stdio(server_env):
    out = asyncio.run(_session_flow(server_env))
    assert out["tools"] == EXPECTED_TOOLS
    assert not any("resolve" in t or "approve" in t or "register" in t
                   for t in out["tools"])
    assert "demo_opaque_needs_human" in {t["task_id"] for t in out["tasks"]["tasks"]}
    assert out["paused"]["status"] == "awaiting_reconciliation"
    assert "next_step" in out["paused"]
    decision = out["pending"]["decisions"][0]
    assert decision["decisions"] == ["confirmed", "abort", "retry_at_risk"]
    assert decision["how"].startswith("continuum resolve ")
    assert out["resumed"]["status"] == "completed"
    assert out["timeline"]["projection_consistent"] is True
    assert "error" in out["unknown"]


async def _chat_one(env: dict) -> str:
    """First chat: delegate a task; it pauses. The chat (and its agent) then ends."""
    params = StdioServerParameters(command=_continuum(), args=["mcp"], env=env)
    async with stdio_client(params) as (read, write), ClientSession(read, write) as session:
        await session.initialize()
        paused = _payload(await session.call_tool(
            "run_task", {"task_id": "demo_opaque_needs_human"}))
        assert paused["status"] == "awaiting_reconciliation"
        return paused["run_id"]


async def _chat_two(env: dict, run_id: str) -> dict:
    """Second chat, new server process, possibly a different agent model: it finds the run
    from durable state alone, the human decides, and it resumes on another model."""
    params = StdioServerParameters(command=_continuum(), args=["mcp"], env=env)
    async with stdio_client(params) as (read, write), ClientSession(read, write) as session:
        await session.initialize()
        runs = _payload(await session.call_tool("list_runs", {"limit": 5}))["runs"]
        found = next(r for r in runs if r["run_id"] == run_id)
        pending = _payload(await session.call_tool("pending_decisions", {"run_id": run_id}))
        subprocess.run([_continuum(), "resolve", run_id,
                        pending["decisions"][0]["action_id"], "confirmed", "--actor",
                        "operator", "--reason", "verified", "--evidence", "receipt:7"],
                       env=env, check=True, capture_output=True)
        resumed = _payload(await session.call_tool("resume_run",
                                                   {"run_id": run_id, "model_alias": "C"}))
        return {"found": found, "resumed": resumed}


def test_runs_survive_across_separate_agent_chats(server_env):
    run_id = asyncio.run(_chat_one(server_env))
    out = asyncio.run(_chat_two(server_env, run_id))
    assert out["found"]["status"] == "awaiting_reconciliation"
    assert out["resumed"]["status"] == "completed"


def _git_repo(root: Path) -> Path:
    root.mkdir()
    (root / "calc.py").write_text("def mul(a, b):\n    raise NotImplementedError\n")
    for args in (["init", "-q"], ["add", "."],
                 ["-c", "user.email=e@x", "-c", "user.name=n", "commit", "-qm", "init"]):
        subprocess.run(["git", "-C", str(root), *args], check=True, capture_output=True)
    return root


async def _coding_chat(env: dict, calls) -> list:
    params = StdioServerParameters(command=_continuum(), args=["mcp"], env=env)
    results = []
    async with stdio_client(params) as (read, write), ClientSession(read, write) as session:
        await session.initialize()
        for name, arguments in calls:
            if callable(arguments):
                arguments = arguments(results)
            results.append(_payload(await session.call_tool(name, arguments)))
    return results


def test_coding_work_survives_model_switch_between_chats_over_mcp(server_env, tmp_path):
    repo = _git_repo(tmp_path / "calc")
    # A human registers the check (CLI only; there is no MCP tool for this).
    subprocess.run([_continuum(), "coding", "checks", "add", str(repo), "unit", "--actor",
                    "operator", "--", sys.executable, "-c",
                    "import calc; assert calc.mul(3, 4) == 12"],
                   env=server_env, check=True, capture_output=True)
    a1 = "claude-opus-4-8 (Claude Code)"

    def edit_then(name, args_fn):
        def build(results):
            (repo / "calc.py").write_text("def mul(a, b):\n    return a * b\n")
            return args_fn(results)
        return (name, build)

    sid = lambda r: r[0]["session_id"]  # noqa: E731
    tid = lambda r: r[0]["tasks"][0]["task_id"]  # noqa: E731
    chat1 = asyncio.run(_coding_chat(server_env, [
        ("coding_start", {"repo_path": str(repo), "goal": "implement mul", "agent": a1,
                          "tasks": [{"title": "implement mul", "checks": ["unit"]}]}),
        ("coding_note", lambda r: {"session_id": sid(r), "kind": "decision",
                                   "text": "integers only for now", "agent": a1}),
        edit_then("coding_checkpoint", lambda r: {"session_id": sid(r),
                                                  "summary": "mul implemented", "agent": a1}),
        ("coding_complete_task", lambda r: {"session_id": sid(r), "task_id": tid(r),
                                            "agent": a1}),
        ("coding_run_check", lambda r: {"session_id": sid(r), "name": "unit", "agent": a1}),
        ("coding_complete_task", lambda r: {"session_id": sid(r), "task_id": tid(r),
                                            "agent": a1}),
        ("coding_note", lambda r: {"session_id": sid(r), "kind": "next_step",
                                   "text": "add div() with zero handling", "agent": a1}),
    ]))
    assert chat1[3]["status"] == "refused"  # done without evidence is refused
    assert chat1[4]["passed"] is True
    assert chat1[5]["verified"] is True

    # The user switches model: a brand-new chat and server process, knowing only the repo.
    [small, brief] = asyncio.run(_coding_chat(server_env, [
        ("coding_resume", {"repo_path": str(repo), "agent": "claude-opus-5-5 (Claude Code)"}),
        ("coding_brief", lambda r: {"session_id": r[0]["session_id"], "detail": "full"}),
    ]))
    # default: compact (Markdown + ids), so the brief is not sent to the model twice
    assert set(small) == {"session_id", "status", "goal", "tasks", "markdown", "detail"}
    assert small["tasks"][0]["verified"] and not small["tasks"][0]["needs_reverification"]
    assert "integers only for now" in small["markdown"]
    assert "add div() with zero handling" in small["markdown"]
    assert brief["session_id"] == chat1[0]["session_id"]
    assert brief["tasks"][0]["verified"] and not brief["tasks"][0]["needs_reverification"]
    assert brief["agent_notes"]["decisions"][0]["text"] == "integers only for now"
    assert brief["agent_notes"]["next_steps"][-1]["text"] == "add div() with zero handling"
    assert [a["agent"] for a in brief["agents"]] == [a1, "claude-opus-5-5 (Claude Code)"]
    assert "Rules" in brief["markdown"]
