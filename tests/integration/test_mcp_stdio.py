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
                  "inspect_run", "pending_decisions", "list_runs"}


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
    assert not any("resolve" in t or "approve" in t for t in out["tools"])
    assert "demo_opaque_needs_human" in {t["task_id"] for t in out["tasks"]["tasks"]}
    assert out["paused"]["status"] == "awaiting_reconciliation"
    assert "next_step" in out["paused"]
    decision = out["pending"]["decisions"][0]
    assert decision["decisions"] == ["confirmed", "abort", "retry_at_risk"]
    assert decision["how"].startswith("continuum resolve ")
    assert out["resumed"]["status"] == "completed"
    assert out["timeline"]["projection_consistent"] is True
    assert "error" in out["unknown"]
