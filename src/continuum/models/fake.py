"""Deterministic fake model adapters for tests and Continuum Lab safety matrices."""

from __future__ import annotations

from collections.abc import Callable, Sequence
from typing import Any

from continuum.models.base import (
    CapabilityProfile,
    Message,
    ModelError,
    ModelResponse,
    Usage,
)

Policy = Callable[[list[Message], list[dict[str, Any]]], ModelResponse]


def fake_profile(alias: str, *, context_window_tokens: int = 128_000,
                 supports_native_tools: bool = True) -> CapabilityProfile:
    return CapabilityProfile(alias=alias, model_id=f"fake/{alias}",
                             context_window_tokens=context_window_tokens,
                             max_output_tokens=4096,
                             supports_native_tools=supports_native_tools,
                             profile_source="fake_adapter")


def text_response(text: str, usage: Usage | None = None) -> ModelResponse:
    return ModelResponse(message=Message(role="assistant", content=text),
                         usage=usage or Usage(input_tokens=10, output_tokens=5, cost_usd="0",
                                              source="reported"),
                         finish_reason="stop")


class FakeModelAdapter:
    """Calls a deterministic policy; can inject typed errors on chosen call numbers."""

    def __init__(self, policy: Policy, *, errors: dict[int, ModelError] | None = None,
                 fail_always: ModelError | None = None) -> None:
        self.policy = policy
        self.errors = errors or {}
        self.fail_always = fail_always
        self.calls = 0
        self.seen: list[list[Message]] = []

    async def complete(self, *, messages: list[Message], tools: list[dict[str, Any]],
                       max_output_tokens: int, timeout_seconds: float) -> ModelResponse:
        self.calls += 1
        self.seen.append(list(messages))
        if self.fail_always is not None:
            raise self.fail_always
        if self.calls in self.errors:
            raise self.errors[self.calls]
        return self.policy(messages, tools)


class ScriptedModelAdapter(FakeModelAdapter):
    """Returns a fixed sequence of responses, then a final stop message."""

    def __init__(self, responses: Sequence[ModelResponse | ModelError]) -> None:
        self._responses = list(responses)
        super().__init__(self._next)

    def _next(self, messages: list[Message], tools: list[dict[str, Any]]) -> ModelResponse:
        if not self._responses:
            return text_response("done")
        item = self._responses.pop(0)
        if isinstance(item, ModelError):
            raise item
        return item
