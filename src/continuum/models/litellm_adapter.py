"""Thin LiteLLM transport for live providers (opt-in; may incur cost).

One adapter covers several provider configurations. Exact model IDs and
pricing snapshots come from ``config/models.yaml``, never from code.
"""

from __future__ import annotations

import json
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


def _to_provider(messages: list[Message]) -> list[dict[str, Any]]:
    out: list[dict[str, Any]] = []
    for m in messages:
        item: dict[str, Any] = {"role": m.role, "content": m.content}
        if m.role == "tool":
            item["tool_call_id"] = m.reply_to_call_id
        if m.tool_proposals:
            item["tool_calls"] = [
                {"id": p.provider_call_id, "type": "function",
                 "function": {"name": p.tool_name, "arguments": json.dumps(p.arguments)}}
                for p in m.tool_proposals]
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
        self.kwargs = completion_kwargs

    def count_tokens(self, text: str) -> int:
        """Provider tokenizer via LiteLLM; conservative estimate if unavailable."""
        try:
            import litellm

            return int(litellm.token_counter(model=self.model_id, text=text))
        except Exception:  # unknown tokenizer: never undercount silently
            return estimate_tokens(text)

    async def complete(self, *, messages: list[Message], tools: list[dict[str, Any]],
                       max_output_tokens: int, timeout_seconds: float) -> ModelResponse:
        import litellm

        try:
            raw = await litellm.acompletion(
                model=self.model_id, messages=_to_provider(messages), tools=tools or None,
                max_tokens=max_output_tokens, timeout=timeout_seconds, **self.kwargs)
        except Exception as exc:  # provider exceptions mapped to the typed taxonomy
            retry_after = getattr(exc, "retry_after", None)
            raise ModelError(_category(exc), str(exc)[:500], retry_after=retry_after) from exc
        return self._normalize(raw)

    def _normalize(self, raw: Any) -> ModelResponse:
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
                                          tool_name=call.function.name, arguments=args))
        usage = Usage()
        raw_usage = getattr(raw, "usage", None)
        if raw_usage is not None:
            usage = Usage(input_tokens=getattr(raw_usage, "prompt_tokens", None),
                          output_tokens=getattr(raw_usage, "completion_tokens", None),
                          source="reported")
            try:
                import litellm

                usage = usage.model_copy(update={
                    "cost_usd": str(litellm.completion_cost(completion_response=raw)),
                    "source": "estimated"})
            except Exception:
                usage = usage.model_copy(update={"cost_usd": None})
        reason = choice.finish_reason
        finish = "tool_calls" if proposals else ("length" if reason == "length" else "stop")
        return ModelResponse(
            message=Message(role="assistant", content=msg.content, tool_proposals=proposals),
            usage=usage, finish_reason=finish)
