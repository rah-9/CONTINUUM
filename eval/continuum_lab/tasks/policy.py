# SPDX-License-Identifier: LicenseRef-Continuum-Proprietary
# Copyright (c) 2026 the Continuum copyright holder. All rights reserved.
"""Deterministic plan-following fake agent, shared by Continuum (C1) and baseline (B0).

The same policy runs under both configurations; only the context differs:
C1 sees the runtime's continuation packet, B0 sees its prior transcript.
A step counts as done when the context shows it:

* C1: its milestone is verified, or its op key is in ``confirmed_actions``;
* both: a tool reply for its call id has status ok/deduped (or rejected for
  expected-reject probes) in the current context.

With ``check_before_repeat`` (the fair baseline), a write whose earlier call
has no reply, or an error reply (e.g. timeout), is first checked with its read
tool, and skipped if the check shows the effect exists.
"""

from __future__ import annotations

import json
from typing import Any

from continuum.models.base import Message, ModelResponse, ToolProposal, Usage
from continuum_lab.tasks.definition import PlanStep

PACKET_MARKER = "Continuation packet"
DONE_STATUSES = {"ok", "deduped"}


def _usage() -> Usage:
    return Usage(input_tokens=200, output_tokens=40, cost_usd="0.0004", source="estimated")


class PlanPolicy:
    def __init__(self, plan: list[PlanStep], *, check_before_repeat: bool = False) -> None:
        self.plan = plan
        self.check_before_repeat = check_before_repeat

    def __call__(self, messages: list[Message], tools: list[dict[str, Any]]) -> ModelResponse:
        verified: set[str] = set()
        confirmed: set[str] = set()
        replies: dict[str, dict[str, Any]] = {}
        proposed: set[str] = set()
        for m in messages:
            if m.role == "user" and m.content and PACKET_MARKER in m.content:
                packet = json.loads(m.content.split("\n", 1)[1])
                verified |= {v["id"] for v in packet["verified_milestones"]}
                confirmed |= {c["operation_key"] for c in packet["confirmed_actions"]}
            if m.role == "user" and m.content and m.content.startswith("Runtime: verified"):
                inside = m.content.split("[", 1)[1].split("]", 1)[0]
                verified |= {x.strip().strip("'\"") for x in inside.split(",") if x.strip()}
            if m.role == "assistant":
                proposed |= {p.provider_call_id or "" for p in m.tool_proposals}
            if m.role == "tool" and m.reply_to_call_id:
                try:
                    replies[m.reply_to_call_id] = json.loads(m.content or "{}")
                except json.JSONDecodeError:
                    replies[m.reply_to_call_id] = {}

        for i, step in enumerate(self.plan):
            call_id = f"s{i}"
            if step.milestone in verified or (step.op_key and step.op_key in confirmed):
                continue
            reply = replies.get(call_id)
            if reply is not None:
                if reply.get("status") in DONE_STATUSES:
                    continue
                if step.expect_reject and reply.get("status") in ("rejected", "conflict"):
                    continue
            check_id = f"c{i}"
            uncertain = reply is None or reply.get("status") in ("error", "unknown")
            if self.check_before_repeat and step.check and call_id in proposed and uncertain:
                check_reply = replies.get(check_id)
                if check_reply is None:
                    tool, args = step.check
                    return self._call(check_id, tool, args)
                if (check_reply.get("data") or {}).get("exists"):
                    continue  # baseline saw the earlier effect; do not repeat
                # Negative check: a plain loop has no ledger, so it retries the write.
            return self._call(call_id, step.tool, step.args)
        return ModelResponse(message=Message(role="assistant", content="All steps complete."),
                             usage=_usage(), finish_reason="stop")

    @staticmethod
    def _call(call_id: str, tool: str, args: dict[str, Any]) -> ModelResponse:
        return ModelResponse(
            message=Message(role="assistant", tool_proposals=[
                ToolProposal(provider_call_id=call_id, tool_name=tool, arguments=dict(args))]),
            usage=_usage(), finish_reason="tool_calls")
