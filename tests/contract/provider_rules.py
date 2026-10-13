"""Provider-faithful request validation and replies for switching tests.

These emulate the request rules real providers enforce (and reject with HTTP 400),
so cross-vendor/version switching is tested against the constraints that actually
break live runs, not a lenient stub:

* function names must match ``^[a-zA-Z0-9_-]{1,64}$`` (OpenAI and Anthropic);
* every assistant tool call has a non-empty id; every ``tool`` message answers an
  id from the immediately preceding assistant turn, and every call is answered
  before the next non-tool message;
* tool-call arguments are JSON strings; Anthropic ids must match ``^[a-zA-Z0-9_-]+$``.
"""

from __future__ import annotations

import json
import re
from types import SimpleNamespace
from typing import Any

from continuum.models.base import Message, ToolProposal
from continuum_lab.tasks.policy import PlanPolicy

NAME = re.compile(r"^[a-zA-Z0-9_-]{1,64}$")
ANTHROPIC_ID = re.compile(r"^[a-zA-Z0-9_-]+$")


class ProviderRejected(Exception):
    """What a provider's HTTP 400 would be."""


def validate(vendor: str, messages: list[dict[str, Any]], tools: list[dict[str, Any]] | None
             ) -> None:
    for t in tools or []:
        name = t["function"]["name"]
        if not NAME.match(name):
            raise ProviderRejected(f"{vendor}: invalid function name {name!r}")
    pending: set[str] = set()
    for m in messages:
        role = m["role"]
        if role not in ("system", "user", "assistant", "tool"):
            raise ProviderRejected(f"{vendor}: invalid role {role}")
        if role == "tool":
            if m.get("tool_call_id") not in pending:
                raise ProviderRejected(f"{vendor}: tool result for unknown id "
                                       f"{m.get('tool_call_id')!r}")
            if not isinstance(m.get("content"), str):
                raise ProviderRejected(f"{vendor}: tool content must be a string")
            pending.discard(m["tool_call_id"])
            continue
        if pending:
            raise ProviderRejected(f"{vendor}: tool calls {sorted(pending)} left unanswered")
        for call in m.get("tool_calls") or []:
            cid = call.get("id")
            if not cid or (vendor == "anthropic" and not ANTHROPIC_ID.match(cid)):
                raise ProviderRejected(f"{vendor}: invalid tool call id {cid!r}")
            if not NAME.match(call["function"]["name"]):
                raise ProviderRejected(f"{vendor}: invalid function name in history")
            json.loads(call["function"]["arguments"])
            pending.add(cid)


class StrictProviders:
    """A litellm.acompletion stand-in serving several vendors by model-id prefix."""

    PREFIX = {"openai": "call_", "anthropic": "toolu_", "gemini": "fc_"}

    def __init__(self, plan: list[Any], *, fail: dict[str, Exception] | None = None) -> None:
        self.policy = PlanPolicy(plan)
        self.fail = fail or {}
        self.calls: list[str] = []
        self.rejections: list[str] = []

    async def __call__(self, *, model: str, messages: list[dict[str, Any]],
                       tools: list[dict[str, Any]] | None = None, **kw: Any) -> Any:
        vendor = model.split("/", 1)[0]
        self.calls.append(model)
        if model in self.fail:
            raise self.fail[model]
        try:
            validate(vendor, messages, tools)
        except ProviderRejected as exc:
            self.rejections.append(str(exc))
            raise type("BadRequestError", (Exception,), {})(str(exc)) from exc
        prefix = self.PREFIX[vendor]
        safe = {t["function"]["name"] for t in tools or []}
        # Translate the provider-format history back for the deterministic agent.
        history = []
        for m in messages:
            props = [ToolProposal(provider_call_id=c["id"].split("_", 1)[1],
                                  tool_name=c["function"]["name"],
                                  arguments=json.loads(c["function"]["arguments"]))
                     for c in m.get("tool_calls") or []]
            reply = m.get("tool_call_id")
            history.append(Message(role=m["role"], content=m["content"], tool_proposals=props,
                                   reply_to_call_id=reply.split("_", 1)[1] if reply else None))
        decision = self.policy(history, tools or [])
        calls = []
        for p in decision.message.tool_proposals:
            name = re.sub(r"[^a-zA-Z0-9_-]", "_", p.tool_name)
            assert name in safe, f"agent chose {name} not offered to {vendor}"
            calls.append(SimpleNamespace(id=f"{prefix}{p.provider_call_id}",
                                         function=SimpleNamespace(
                                             name=name, arguments=json.dumps(p.arguments))))
        msg = SimpleNamespace(content=decision.message.content, tool_calls=calls or None)
        return SimpleNamespace(
            choices=[SimpleNamespace(message=msg, finish_reason="tool_calls" if calls
                                     else "stop")],
            usage=SimpleNamespace(prompt_tokens=300, completion_tokens=50))
