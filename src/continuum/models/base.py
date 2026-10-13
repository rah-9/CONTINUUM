"""Provider-neutral model contract (docs/05 sections 2-3)."""

from __future__ import annotations

import asyncio
import concurrent.futures
from collections.abc import Callable, Coroutine
from decimal import Decimal
from typing import Any, Literal, Protocol

from pydantic import BaseModel, ConfigDict, Field, model_validator

from continuum.core.errors import UnsupportedCapabilityError


class ToolProposal(BaseModel):
    provider_call_id: str | None = None  # correlation only, NOT operation identity
    tool_name: str
    arguments: dict[str, Any]


class Message(BaseModel):
    role: Literal["system", "user", "assistant", "tool"]
    content: str | None = None
    tool_proposals: list[ToolProposal] = Field(default_factory=list)
    reply_to_call_id: str | None = None


class Usage(BaseModel):
    input_tokens: int | None = Field(default=None, ge=0)
    output_tokens: int | None = Field(default=None, ge=0)
    cost_usd: str | None = None          # Decimal encoded as text
    source: Literal["reported", "estimated", "unknown"] = "unknown"

    @model_validator(mode="after")
    def _cost(self) -> Usage:
        if self.cost_usd is not None and Decimal(self.cost_usd) < 0:
            raise ValueError("cost must not be negative")
        return self


class ModelResponse(BaseModel):
    message: Message
    usage: Usage
    finish_reason: Literal["stop", "tool_calls", "length"]


ErrorCategory = Literal["rate_limited", "provider_unavailable", "timeout", "bad_request",
                        "context_overflow", "invalid_tool_output", "unsupported_capability",
                        "unknown"]

RETRYABLE: frozenset[str] = frozenset({"rate_limited", "provider_unavailable", "timeout"})


class ModelError(Exception):
    def __init__(self, category: ErrorCategory, message: str = "", *,
                 retry_after: float | None = None, usage: Usage | None = None) -> None:
        super().__init__(message or category)
        self.category: ErrorCategory = category
        self.retry_after = retry_after
        self.usage = usage or Usage()

    @property
    def retryable(self) -> bool:
        return self.category in RETRYABLE


class ModelAdapter(Protocol):
    async def complete(
        self,
        *,
        messages: list[Message],
        tools: list[dict[str, Any]],
        max_output_tokens: int,
        timeout_seconds: float,
    ) -> ModelResponse: ...


class Price(BaseModel):
    model_config = ConfigDict(frozen=True)
    input_per_million_usd: Decimal | None = None
    output_per_million_usd: Decimal | None = None
    as_of: str | None = None


class CapabilityProfile(BaseModel):
    """Snapshot per model session. Placeholder zero values reject deployment."""

    model_config = ConfigDict(frozen=True, extra="forbid")
    alias: str
    model_id: str
    context_window_tokens: int = Field(ge=0)
    max_output_tokens: int = Field(ge=0)
    supports_native_tools: bool
    supports_json_schema: bool = False
    supports_tool_call_ids: bool = True
    price: Price = Field(default_factory=Price)
    profile_source: str = "provider_docs_and_contract_tests"

    def check_deployable(self) -> None:
        if self.profile_source == "registry_incomplete":
            raise UnsupportedCapabilityError(
                f"profile {self.alias} ({self.model_id}): model registry entry is incomplete; "
                "add a verified profile to config/models.yaml")
        if self.context_window_tokens <= 0 or self.max_output_tokens <= 0:
            raise UnsupportedCapabilityError(
                f"profile {self.alias} has placeholder limits; fill verified values first")
        if not self.supports_native_tools:
            raise UnsupportedCapabilityError(
                f"model {self.model_id} has no native tool-calling contract (unsupported in v1)")
        if self.model_id.startswith("provider_") or "exact-version" in self.model_id:
            raise UnsupportedCapabilityError(f"profile {self.alias} has a placeholder model_id")


class ModelEndpoint(BaseModel):
    """A routed model: adapter instance plus its capability profile."""

    model_config = ConfigDict(arbitrary_types_allowed=True, frozen=True)
    profile: CapabilityProfile
    adapter: Any  # ModelAdapter; Any because Protocols are not pydantic-validated


class ModelRouter:
    """Aliases -> endpoints. An optional resolver adds endpoints on demand for model ids
    that are not configured (e.g. ``anthropic/claude-opus-5-5`` after an upgrade)."""

    def __init__(self, endpoints: dict[str, ModelEndpoint],
                 resolver: Callable[[str], ModelEndpoint] | None = None) -> None:
        self._endpoints = dict(endpoints)
        self._resolver = resolver

    def get(self, alias: str) -> ModelEndpoint:
        if alias not in self._endpoints:
            if self._resolver is None or "/" not in alias:
                raise UnsupportedCapabilityError(
                    f"model {alias!r} is not configured; use a configured alias or a "
                    "provider/model-id known to the model registry")
            self._endpoints[alias] = self._resolver(alias)
        return self._endpoints[alias]

    def aliases(self) -> list[str]:
        return list(self._endpoints)


def run_coro[T](coro: Coroutine[Any, Any, T]) -> T:
    """Run an adapter coroutine from sync code, even inside a running event loop."""
    try:
        asyncio.get_running_loop()
    except RuntimeError:
        return asyncio.run(coro)
    with concurrent.futures.ThreadPoolExecutor(max_workers=1) as pool:
        return pool.submit(asyncio.run, coro).result()


def estimate_tokens(text: str) -> int:
    """Conservative character-based estimate (about 4 chars per token)."""
    return (len(text) + 3) // 4
