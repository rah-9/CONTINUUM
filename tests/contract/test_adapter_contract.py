# SPDX-License-Identifier: LicenseRef-Continuum-Proprietary
# Copyright (c) 2026 the Continuum copyright holder. All rights reserved.
"""Provider adapter contract through the full runtime, with a stubbed LiteLLM transport.

Provider-format tool calls (JSON-string arguments, provider call IDs) must be
normalized and validated before reaching the gateway; provider errors must map
to the typed taxonomy and drive retry/handoff, never a fake success.
"""

import json
from types import SimpleNamespace

import pytest

from continuum.core.state import RunStatus
from continuum.models.base import ModelEndpoint, ModelRouter
from continuum.models.fake import fake_profile
from continuum.models.litellm_adapter import LiteLLMAdapter
from continuum_lab.tasks.policy import PlanPolicy


def _provider_reply(policy_response, *, mangle=False):
    calls = [SimpleNamespace(id=f"prov-{p.provider_call_id}", function=SimpleNamespace(
        name=p.tool_name, arguments="{broken" if mangle else json.dumps(p.arguments)))
        for p in policy_response.message.tool_proposals]
    msg = SimpleNamespace(content=policy_response.message.content, tool_calls=calls or None)
    usage = SimpleNamespace(prompt_tokens=120, completion_tokens=30)
    return SimpleNamespace(choices=[SimpleNamespace(message=msg, finish_reason="stop")],
                           usage=usage)


class FakeProvider:
    """Emulates litellm.acompletion for a provider following the task plan."""

    def __init__(self, plan, *, mangle_first=0, raise_for=None):
        self.policy = PlanPolicy(plan)
        self.mangle_first = mangle_first
        self.raise_for = raise_for or {}
        self.calls = []

    async def __call__(self, *, model, messages, tools, **kw):
        from continuum.models.base import Message, ToolProposal

        self.calls.append(model)
        if model in self.raise_for:
            raise self.raise_for[model]
        normalized = []
        for m in messages:  # reverse the adapter's provider mapping for the policy
            props = [ToolProposal(provider_call_id=c["id"].removeprefix("prov-"),
                                  tool_name=c["function"]["name"],
                                  arguments=json.loads(c["function"]["arguments"]))
                     for c in m.get("tool_calls") or []]
            reply = m.get("tool_call_id")
            normalized.append(Message(role=m["role"], content=m["content"],
                                      tool_proposals=props,
                                      reply_to_call_id=reply.removeprefix("prov-")
                                      if reply else None))
        mangle = self.mangle_first > 0
        self.mangle_first -= 1
        return _provider_reply(self.policy(normalized, tools), mangle=mangle)


def _router(models):
    return ModelRouter({a: ModelEndpoint(profile=fake_profile(a).model_copy(
        update={"model_id": m}), adapter=LiteLLMAdapter(m)) for a, m in models.items()})


@pytest.fixture
def stub_litellm(monkeypatch):
    import litellm

    def install(provider):
        monkeypatch.setattr(litellm, "acompletion", provider)
        monkeypatch.setattr(litellm, "completion_cost",
                            lambda completion_response: 0.0005)
    return install


def test_two_provider_configurations_complete_through_litellm(make_lab, stub_litellm):
    lab = make_lab("upload_and_notify")
    provider = FakeProvider(lab.definition.plan)
    stub_litellm(provider)
    router = _router({"A": "prov-a/model-1", "B": "prov-b/model-2", "C": "prov-c/model-3"})
    result = lab.new_runtime(owner_id="ctl-1", router=router).start(lab.definition.spec)
    assert result.status == RunStatus.COMPLETED
    with lab.store.read() as q:
        usage = q.usage(result.run_id)
    assert usage.tokens_reported > 0
    assert usage.cost_known_usd > 0


def test_malformed_provider_tool_json_never_reaches_gateway(make_lab, stub_litellm):
    lab = make_lab("db_update_selected")
    provider = FakeProvider(lab.definition.plan, mangle_first=2)
    stub_litellm(provider)
    router = _router({"A": "prov-a/m", "B": "prov-b/m", "C": "prov-c/m"})
    result = lab.new_runtime(owner_id="ctl-1", router=router).start(lab.definition.spec)
    assert result.status == RunStatus.COMPLETED
    assert provider.calls[:2] == ["prov-a/m", "prov-a/m"]  # threshold then handoff
    assert "prov-b/m" in provider.calls
    assert lab.env.db.commits_for("r1") == 1


def test_provider_outage_maps_to_typed_error_and_hands_off(make_lab, stub_litellm):
    lab = make_lab("db_update_selected")
    outage = type("ServiceUnavailableError", (Exception,), {})("503")
    provider = FakeProvider(lab.definition.plan, raise_for={"prov-a/m": outage})
    stub_litellm(provider)
    router = _router({"A": "prov-a/m", "B": "prov-b/m", "C": "prov-c/m"})
    result = lab.new_runtime(owner_id="ctl-1", router=router).start(lab.definition.spec)
    assert result.status == RunStatus.COMPLETED
    with lab.store.read() as q:
        failed = [e for e in q.events(result.run_id) if e.type.value == "ModelCallFailed"]
    assert {e.payload["error_category"] for e in failed} == {"provider_unavailable"}
    assert len(failed) == 3  # first call + 2 bounded same-provider retries
