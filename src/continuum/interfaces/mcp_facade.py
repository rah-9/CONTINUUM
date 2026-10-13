"""Local stdio MCP server: Continuum as a plugin for any MCP-capable coding agent.

Any agent that speaks MCP (Claude Code, Cursor, VS Code, Codex, Claude Desktop,
...) can delegate pre-registered tasks to Continuum and inspect them. The
surface is deliberately limited (docs/01 R10, docs/02 s8):

* delegation: ``list_tasks``, ``run_task``, ``resume_run``, ``cancel_run``
* read-only inspection: ``get_status``, ``inspect_run``, ``pending_decisions``,
  ``list_runs``

**Human decisions are never exposed.** Approving, confirming or retrying an
uncertain side effect must come from a person (CLI/HTTP), never from a model
acting through MCP; ``pending_decisions`` tells the agent exactly what to ask
its user to do. Arbitrary task specs, tool execution and remote MCP discovery
are not offered.
"""

from __future__ import annotations

from collections.abc import Callable
from dataclasses import asdict
from typing import Any

from continuum.core.errors import ContinuumError
from continuum.core.runtime import RunResult
from continuum.interfaces.sdk import Continuum

SERVER_INSTRUCTIONS = """Continuum runs pre-registered multi-step tasks with durable, verified
progress and an external-effect ledger. Use run_task to delegate a task, then get_status /
inspect_run to follow it. A run may stop in `awaiting_reconciliation` or
`paused_for_approval`: call pending_decisions and ask your user to make the decision with the
command it returns — you must not decide on their behalf. After they decide, call resume_run.
resume_run with model_alias continues the same run on a different model from verified state."""

MCP_ACTOR = "mcp-agent"


def _result(r: RunResult) -> dict[str, Any]:
    data = asdict(r)
    data["status"] = r.status.value
    if r.needs_human or r.awaiting_approval:
        data["next_step"] = ("A human decision is required: call pending_decisions and ask "
                             "your user to run the returned command, then resume_run.")
    return data


class McpFacade:
    """Transport-independent facade logic (unit-testable without stdio)."""

    def __init__(self, sdk: Continuum) -> None:
        self.sdk = sdk

    @staticmethod
    def _guard(fn: Callable[[], dict[str, Any]]) -> dict[str, Any]:
        try:
            return fn()
        except ContinuumError as exc:
            return {"error": str(exc), "code": exc.code}

    def list_tasks(self) -> dict[str, Any]:
        tasks = self.sdk.tasks()
        out: dict[str, Any] = {"tasks": tasks}
        if not tasks:
            out["hint"] = ("no tasks registered; set CONTINUUM_CONFIG_DIR to a directory "
                           "containing tasks/*.yaml")
        return out

    def run_task(self, task_id: str, model_alias: str | None = None) -> dict[str, Any]:
        if task_id not in self.sdk.registry.task_ids():
            return {"error": f"task {task_id!r} is not pre-registered",
                    "available": self.sdk.registry.task_ids()}
        return self._guard(lambda: _result(self.sdk.start(task_id, model_alias=model_alias)))

    def resume_run(self, run_id: str, model_alias: str | None = None) -> dict[str, Any]:
        return self._guard(lambda: _result(self.sdk.resume(run_id, model_alias=model_alias)))

    def cancel_run(self, run_id: str, reason: str) -> dict[str, Any]:
        return self._guard(lambda: _result(self.sdk.cancel(run_id, actor=MCP_ACTOR,
                                                           reason=reason)))

    def get_status(self, run_id: str) -> dict[str, Any]:
        return self._guard(lambda: self.sdk.status(run_id))

    def inspect_run(self, run_id: str) -> dict[str, Any]:
        def view() -> dict[str, Any]:
            report = self.sdk.inspect(run_id)
            return {"run_id": run_id, "replayed_status": report["replayed_status"],
                    "timeline": report["timeline"],
                    "projection_consistent": not report["projection_diff"]}
        return self._guard(view)

    def pending_decisions(self, run_id: str) -> dict[str, Any]:
        def view() -> dict[str, Any]:
            items = self.sdk.pending_decisions(run_id)
            return {"run_id": run_id, "decisions": items,
                    "note": "Only a human may make these decisions. Ask your user to run the "
                            "command shown; do not attempt it yourself."}
        return self._guard(view)

    def list_runs(self, limit: int = 20) -> dict[str, Any]:
        runs = self.sdk.runs()[-max(1, min(limit, 200)):]
        return {"runs": [self.sdk.status(r) | {"actions": None} for r in reversed(runs)]}


def run_stdio(sdk: Continuum) -> None:
    from mcp.server.mcpserver import MCPServer  # mcp>=2 (FastMCP was renamed)

    sdk.init_db()  # idempotent; lets a freshly installed plugin work immediately
    facade = McpFacade(sdk)
    server: MCPServer[Any] = MCPServer("continuum", instructions=SERVER_INSTRUCTIONS)

    @server.tool()
    def list_tasks() -> dict[str, Any]:
        """List the pre-registered tasks this Continuum installation can run."""
        return facade.list_tasks()

    @server.tool()
    def run_task(task_id: str, model_alias: str | None = None) -> dict[str, Any]:
        """Run a pre-registered task until it completes or pauses for a human decision.
        Optionally choose the model alias (e.g. "B") to start with."""
        return facade.run_task(task_id, model_alias)

    @server.tool()
    def resume_run(run_id: str, model_alias: str | None = None) -> dict[str, Any]:
        """Resume a run under lease and ledger safeguards. Pass model_alias (a configured
        alias, or a provider/model-id such as "openai/gpt-6.1") to continue the same run on
        a different model or version; verified work is never redone."""
        return facade.resume_run(run_id, model_alias)

    @server.tool()
    def cancel_run(run_id: str, reason: str) -> dict[str, Any]:
        """Stop new side effects for a run (already-sent calls cannot be rolled back)."""
        return facade.cancel_run(run_id, reason)

    @server.tool()
    def get_status(run_id: str) -> dict[str, Any]:
        """Durable status of a run: milestones, actions, usage and lease."""
        return facade.get_status(run_id)

    @server.tool()
    def inspect_run(run_id: str) -> dict[str, Any]:
        """Read-only timeline of model decisions, effects, reconciliation and milestones."""
        return facade.inspect_run(run_id)

    @server.tool()
    def pending_decisions(run_id: str) -> dict[str, Any]:
        """Decisions only a human may make for this run, and the exact command to make them."""
        return facade.pending_decisions(run_id)

    @server.tool()
    def list_runs(limit: int = 20) -> dict[str, Any]:
        """Most recent runs with their status."""
        return facade.list_runs(limit)

    server.run("stdio")
