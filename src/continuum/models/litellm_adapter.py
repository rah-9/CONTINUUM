# SPDX-License-Identifier: LicenseRef-Continuum-Proprietary
# Copyright (c) 2026 the Continuum copyright holder. All rights reserved.
"""Thin LiteLLM transport for live providers (opt-in; may incur cost).

One adapter covers several provider configurations. Exact model IDs and
pricing snapshots come from ``config/models.yaml``, never from code.
"""

from __future__ import annotations

import json
import re
from typing import Any

from continuum.models.base import (
    ErrorCategory,
    Message,
    ModelError,
    ModelResponse,
    ToolProposal,
    Usage,
    estimate_tokens,
)

_UNSAFE = re.compile(r"[^a-zA-Z0-9_-]")


def provider_tool_name(name: str) -> str:
    """Provider-safe function name. OpenAI and Anthropic require ``^[a-zA-Z0-9_-]{1,64}$``;
    Continuum tool names use dots (``store.put``), so they are mapped reversibly."""
    return _UNSAFE.sub("_", name)[:64]


class ToolNames:
    """Bijective mapping between Continuum tool names and provider-safe names."""

    def __init__(self, names: list[str]) -> None:
        self.to_provider: dict[str, str] = {}
        self.from_provider: dict[str, str] = {}
        for name in names:
            safe = provider_tool_name(name)
            if safe in self.from_provider and self.from_provider[safe] != name:
                raise ModelError("unsupported_capability",
                                 f"tool names {name!r} and {self.from_provider[safe]!r} "
                                 "collide after provider-safe mapping")
            self.to_provider[name] = safe
            self.from_provider[safe] = name

    @classmethod
    def from_tools(cls, tools: list[dict[str, Any]]) -> ToolNames:
        return cls([t["function"]["name"] for t in tools if "function" in t])

    def forward(self, name: str) -> str:
        return self.to_provider.get(name, provider_tool_name(name))

    def backward(self, name: str) -> str:
        return self.from_provider.get(name, name)

    def tools(self, tools: list[dict[str, Any]]) -> list[dict[str, Any]]:
        return [{**t, "function": {**t["function"], "name": self.forward(t["function"]["name"])}}
                if "function" in t else t for t in tools]


def _to_provider(messages: list[Message], names: ToolNames) -> list[dict[str, Any]]:
    """Provider-format history. Calls without a provider id get generated ids, and the
    tool results that answer them (in order) reuse exactly those ids, because every
    provider rejects a result whose id does not match a preceding call."""
    out: list[dict[str, Any]] = []
    unanswered: list[str] = []  # generated ids awaiting their tool result, in order
    for i, m in enumerate(messages):
        item: dict[str, Any] = {"role": m.role, "content": m.content}
        if m.role == "tool":
            call_id = m.reply_to_call_id
            if call_id is None and unanswered:
                call_id = unanswered.pop(0)
            elif call_id in unanswered:
                unanswered.remove(call_id)
            item["tool_call_id"] = call_id or f"call_{i}"
            item["content"] = m.content or ""
        if m.tool_proposals:
            calls = []
            for j, p in enumerate(m.tool_proposals):
                call_id = p.provider_call_id or f"call_{i}_{j}"
                if p.provider_call_id is None:
                    unanswered.append(call_id)
                calls.append({"id": call_id, "type": "function",
                              "function": {"name": names.forward(p.tool_name),
                                           "arguments": json.dumps(p.arguments)}})
            item["tool_calls"] = calls
        out.append(item)
    return out


def _category(exc: Exception) -> ErrorCategory:
    name = type(exc).__name__
    mapping: dict[str, ErrorCategory] = {
        "RateLimitError": "rate_limited",
        "ServiceUnavailableError": "provider_unavailable",
        "InternalServerError": "provider_unavailable",
        "APIConnectionError": "provider_unavailable",
        "Timeout": "timeout",
        "ContextWindowExceededError": "context_overflow",
        "BadRequestError": "bad_request",
        "UnsupportedParamsError": "unsupported_capability",
    }
    return mapping.get(name, "unknown")


class LiteLLMAdapter:
    def __init__(self, model_id: str, **completion_kwargs: Any) -> None:
        self.model_id = model_id
        # drop_params: let LiteLLM drop/translate parameters a model family does not
        # accept (e.g. reasoning models and max_tokens vs max_completion_tokens), so
        # switching model versions does not need code changes.
        self.kwargs = {"drop_params": True, **completion_kwargs}

    def count_tokens(self, text: str) -> int:
        """Provider tokenizer via LiteLLM; conservative estimate if unavailable."""
        try:
            import litellm

            return int(litellm.token_counter(model=self.model_id, text=text))
        except Exception:  # unknown tokenizer: never undercount silently
            return estimate_tokens(text)

    async def complete(self, *, messages: list[Message], tools: list[dict[str, Any]],
                       max_output_tokens: int, timeout_seconds: float) -> ModelResponse:
        try:
            import litellm
        except ImportError as exc:  # the [live] extra is not installed
            raise ModelError("unsupported_capability",
                             "live models need `pip install continuum[live]`") from exc
        try:
            names = ToolNames.from_tools(tools)
            raw = await litellm.acompletion(
                model=self.model_id, messages=_to_provider(messages, names),
                tools=names.tools(tools) or None, max_tokens=max_output_tokens,
                timeout=timeout_seconds, **self.kwargs)
        except Exception as exc:  # provider exceptions mapped to the typed taxonomy
            retry_after = getattr(exc, "retry_after", None)
            raise ModelError(_category(exc), str(exc)[:500], retry_after=retry_after) from exc
        return self._normalize(raw, names)

    def _normalize(self, raw: Any, names: ToolNames | None = None) -> ModelResponse:
        """Provider response -> ModelResponse; any unexpected shape is a typed ModelError
        (e.g. empty ``choices`` after a content filter), never a crash of the step loop."""
        try:
            return self._normalize_unchecked(raw, names or ToolNames([]))
        except ModelError:
            raise
        except Exception as exc:
            raise ModelError("invalid_tool_output",
                             f"malformed provider response: {type(exc).__name__}") from exc

    def _normalize_unchecked(self, raw: Any, names: ToolNames) -> ModelResponse:
        choice = raw.choices[0]
        msg = choice.message
        proposals: list[ToolProposal] = []
        for call in getattr(msg, "tool_calls", None) or []:
            try:
                args = json.loads(call.function.arguments or "{}")
            except (json.JSONDecodeError, TypeError) as exc:
                raise ModelError("invalid_tool_output", "tool arguments are not JSON") from exc
            if not isinstance(args, dict):
                raise ModelError("invalid_tool_output", "tool arguments must be an object")
            proposals.append(ToolProposal(provider_call_id=call.id,
                                          tool_name=names.backward(call.function.name),
                                          arguments=args))
        usage = Usage()
        raw_usage = getattr(raw, "usage", None)
        if raw_usage is not None:
            usage = Usage(input_tokens=getattr(raw_usage, "prompt_tokens", None),
                          output_tokens=getattr(raw_usage, "completion_tokens", None),
                          source="reported")
            try:
                import litellm

                cost = litellm.completion_cost(completion_response=raw)
                # a registry price of 0 means "unknown", never "free" (docs/05)
                usage = usage.model_copy(update={
                    "cost_usd": str(cost) if cost and cost > 0 else None,
                    "source": "estimated"})
            except Exception:
                usage = usage.model_copy(update={"cost_usd": None})
        reason = choice.finish_reason
        finish = "tool_calls" if proposals else ("length" if reason == "length" else "stop")
        return ModelResponse(
            message=Message(role="assistant", content=msg.content, tool_proposals=proposals),
            usage=usage, finish_reason=finish)
