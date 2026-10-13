import asyncio
from types import SimpleNamespace

import pytest

from continuum.core.errors import UnsupportedCapabilityError
from continuum.models.base import CapabilityProfile, ModelError, Usage
from continuum.models.fake import ScriptedModelAdapter, fake_profile, text_response
from continuum.models.litellm_adapter import LiteLLMAdapter, _category


def _raw(tool_calls=None, content=None, usage=None, finish="stop"):
    msg = SimpleNamespace(content=content, tool_calls=tool_calls)
    return SimpleNamespace(choices=[SimpleNamespace(message=msg, finish_reason=finish)],
                           usage=usage)


def _call(cid, name, args):
    return SimpleNamespace(id=cid, function=SimpleNamespace(name=name, arguments=args))


def test_normalizes_multiple_tool_proposals_with_correlation_ids():
    raw = _raw([_call("c1", "file.read", '{"path": "a"}'), _call("c2", "store.head", "{}")])
    resp = LiteLLMAdapter("x")._normalize(raw)
    assert [p.provider_call_id for p in resp.message.tool_proposals] == ["c1", "c2"]
    assert resp.finish_reason == "tool_calls"
    assert resp.usage.source == "unknown" and resp.usage.cost_usd is None


@pytest.mark.parametrize("args", ["{not json", "[1, 2]"])
def test_malformed_tool_arguments_are_typed_errors_not_messages(args):
    with pytest.raises(ModelError) as exc:
        LiteLLMAdapter("x")._normalize(_raw([_call("c1", "file.read", args)]))
    assert exc.value.category == "invalid_tool_output"


@pytest.mark.parametrize(("name", "category"), [
    ("RateLimitError", "rate_limited"), ("ServiceUnavailableError", "provider_unavailable"),
    ("Timeout", "timeout"), ("ContextWindowExceededError", "context_overflow"),
    ("WeirdError", "unknown")])
def test_provider_errors_map_to_taxonomy(name, category):
    assert _category(type(name, (Exception,), {})()) == category


def test_retryable_categories():
    assert ModelError("rate_limited").retryable
    assert not ModelError("bad_request").retryable


def test_usage_rejects_negative_cost_and_keeps_unknown():
    with pytest.raises(ValueError):
        Usage(cost_usd="-1")
    assert Usage().source == "unknown"


def test_placeholder_or_toolless_profiles_are_rejected():
    with pytest.raises(UnsupportedCapabilityError):
        CapabilityProfile(alias="A", model_id="provider_a/exact-version",
                          context_window_tokens=0, max_output_tokens=0,
                          supports_native_tools=True).check_deployable()
    with pytest.raises(UnsupportedCapabilityError):
        fake_profile("A", supports_native_tools=False).check_deployable()
    fake_profile("A").check_deployable()


def test_scripted_adapter_returns_responses_then_raises_injected_errors():
    adapter = ScriptedModelAdapter([text_response("hi"), ModelError("timeout")])
    first = asyncio.run(adapter.complete(messages=[], tools=[], max_output_tokens=10,
                                         timeout_seconds=1))
    assert first.message.content == "hi"
    with pytest.raises(ModelError):
        asyncio.run(adapter.complete(messages=[], tools=[], max_output_tokens=10,
                                     timeout_seconds=1))
