"""B0: plain tool loop + retries + transcript handoff (docs/07 section 2).

Same task objective, allowed tools, provider routing, endpoint behaviour,
permissions (schema/allowlist/path checks), budgets and independent scorer as
Continuum. Differences (disclosed in reports): no durable action ledger, no
verification-gated progress, no reconciliation; on restart or model switch it
continues from its persisted visible transcript (length-capped). It sends no
idempotency keys (a plain loop has no stable action identity), and it is told
to verify before repeating writes, with read tools available to do so.
"""

from __future__ import annotations

import json
from collections.abc import Callable
from dataclasses import dataclass, field
from pathlib import Path
from typing import Any

import jsonschema

from continuum.core.canonical import canonical_json
from continuum.core.clock import Clock
from continuum.core.evidence import Workspace
from continuum.core.faults import FaultInjector, NoFaults
from continuum.core.runtime import run_coro
from continuum.models.base import Message, ModelError, ModelRouter, ToolProposal
from continuum.tools.manifest import (
    BindContext,
    ToolAmbiguousFailure,
    ToolCleanFailure,
    ToolContext,
    ToolRegistry,
)
from continuum_lab.tasks.definition import TaskDefinition

BASELINE_PROMPT = """You are an agent completing a task with the provided tools.
Before repeating any write whose earlier outcome is unclear (error or timeout), use a
read tool to check whether it already happened. Reply without tool calls when done."""


@dataclass
class BaselineResult:
    finished: bool
    reason: str
    model_calls: int = 0
    tokens: int = 0
    cost_usd: float = 0.0
    unpriced: bool = False
    handoffs: int = 0
    tool_calls: list[dict[str, Any]] = field(default_factory=list)


class BaselineRunner:
    def __init__(self, definition: TaskDefinition, *, tools: ToolRegistry, router: ModelRouter,
                 workspace: Workspace, transcript_path: Path, clock: Clock,
                 faults: FaultInjector | None = None, transcript_cap: int = 60,
                 approver: Callable[[str, dict[str, Any]], bool] | None = None) -> None:
        self.definition = definition
        self.spec = definition.spec
        self.tools = tools
        self.router = router
        self.workspace = workspace
        self.transcript_path = transcript_path
        self.clock = clock
        self.faults: FaultInjector = faults or NoFaults()
        self.transcript_cap = transcript_cap
        self.approver = approver
        self.state_path = transcript_path.with_suffix(".state.json")

    # ---- persistence (the baseline's only durable memory) ------------------------------

    def _load(self) -> tuple[list[Message], dict[str, Any]]:
        if self.transcript_path.exists():
            raw = json.loads(self.transcript_path.read_text())
            state = json.loads(self.state_path.read_text())
            return [Message.model_validate(m) for m in raw], state
        goal = (f"Task: {self.spec.goal}\nInstructions: {self.spec.instructions}\n"
                "Milestones:\n" + "\n".join(f"- {m.id}: {m.description}"
                                             for m in self.spec.milestones))
        return ([Message(role="system", content=BASELINE_PROMPT),
                 Message(role="user", content=goal)],
                {"alias_index": 0, "model_calls": 0, "tokens": 0, "cost": 0.0,
                 "unpriced": False, "handoffs": 0})

    def _save(self, messages: list[Message], state: dict[str, Any]) -> None:
        self.transcript_path.parent.mkdir(parents=True, exist_ok=True)
        self.transcript_path.write_text(canonical_json([m.model_dump() for m in messages]))
        self.state_path.write_text(canonical_json(state))

    # ---- loop -------------------------------------------------------------------------------

    def run(self, *, handoff: bool = False) -> BaselineResult:
        """Run (or continue after a restart). ``handoff`` switches to the next model,
        passing the persisted visible transcript."""
        messages, state = self._load()
        if handoff:
            state["alias_index"] += 1
            state["handoffs"] += 1
        aliases = [self.spec.model_policy.primary, *self.spec.model_policy.fallbacks]
        budget = self.spec.budgets
        schemas = [t.manifest.provider_schema() for n in self.spec.tools_allowlist
                   if (t := self.tools.get(n)) is not None]
        result = BaselineResult(finished=False, reason="")
        retries = 0
        while True:
            if state["model_calls"] >= budget.max_model_calls:
                result.reason = "budget_exceeded"
                break
            if state["alias_index"] >= len(aliases):
                result.reason = "models_exhausted"
                break
            endpoint = self.router.get(aliases[state["alias_index"]])
            state["model_calls"] += 1
            window = [messages[0], messages[1], *messages[2:][-self.transcript_cap:]]
            try:
                response = run_coro(endpoint.adapter.complete(
                    messages=window, tools=schemas, max_output_tokens=2048,
                    timeout_seconds=60))
            except ModelError as exc:
                state["unpriced"] = True
                if exc.retryable and retries < self.spec.model_policy.same_provider_retries:
                    retries += 1
                    self.clock.sleep(exc.retry_after or 1)
                    continue
                retries = 0
                state["alias_index"] += 1  # transcript handoff to the next model
                state["handoffs"] += 1
                self._save(messages, state)
                continue
            usage = response.usage
            state["tokens"] += (usage.input_tokens or 0) + (usage.output_tokens or 0)
            if usage.cost_usd is None:
                state["unpriced"] = True
            else:
                state["cost"] += float(usage.cost_usd)
            messages.append(response.message)
            self._save(messages, state)
            if not response.message.tool_proposals:
                result.finished = True
                result.reason = "agent_reported_done"
                break
            for proposal in response.message.tool_proposals:
                reply = self._execute(proposal, result)
                messages.append(Message(role="tool", reply_to_call_id=proposal.provider_call_id,
                                        content=canonical_json(reply)))
                self._save(messages, state)
        result.model_calls = state["model_calls"]
        result.tokens = state["tokens"]
        result.cost_usd = state["cost"]
        result.unpriced = state["unpriced"]
        result.handoffs = state["handoffs"]
        return result

    def _execute(self, proposal: ToolProposal, result: BaselineResult) -> dict[str, Any]:
        name, args = proposal.tool_name, proposal.arguments
        tool = self.tools.get(name)
        if tool is None or name not in self.spec.tools_allowlist:
            return {"status": "rejected", "message": f"tool {name} not allowed"}
        m = tool.manifest
        try:
            m.validate_args(args)
            prefix = m.permissions.allowed_path_prefix
            for f in m.permissions.path_fields:
                value = str(args.get(f, ""))
                if ".." in value.split("/") or (prefix and not value.startswith(prefix)):
                    return {"status": "rejected", "message": f"{f} not permitted"}
            bound = tool.bind(args, BindContext(self.workspace.run_id, None, self.workspace))
        except (jsonschema.ValidationError, ToolCleanFailure) as exc:
            return {"status": "rejected", "message": str(exc)}
        ctx = ToolContext(run_id=self.workspace.run_id, action_id="b0", attempt_no=1,
                          milestone_id=None, idempotency_key=None, workspace=self.workspace,
                          timeout_seconds=m.timeout_seconds)
        if m.permissions.requires_approval and (
                self.approver is None or not self.approver(name, bound)):
            return {"status": "rejected", "message": "approval denied"}
        self.faults.checkpoint("before_dispatch", tool=name)
        result.tool_calls.append({"tool": name, "args": bound})
        try:
            out = tool.invoke(bound, ctx)
        except ToolCleanFailure as exc:
            return {"status": "rejected", "message": str(exc)}
        except (ToolAmbiguousFailure, Exception) as exc:
            self.faults.checkpoint("after_response_receipt", tool=name)
            return {"status": "error", "message": f"{exc}; the operation may have completed"}
        self.faults.checkpoint("after_response_receipt", tool=name)
        return {"status": "ok", "data": out.data}
