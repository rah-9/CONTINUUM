# SPDX-License-Identifier: LicenseRef-Continuum-Proprietary
# Copyright (c) 2026 the Continuum copyright holder. All rights reserved.
"""Switching models across vendors, versions and chats must just work (D-023).

Every test drives the real LiteLLM adapter against provider-faithful emulators
(``provider_rules``) that reject what real APIs reject.
"""

import pytest

from continuum.core.errors import UnsupportedCapabilityError
from continuum.core.faults import SimulatedCrash
from continuum.core.state import RunStatus
from continuum.models.base import ModelEndpoint, ModelRouter
from continuum.models.litellm_adapter import LiteLLMAdapter, ToolNames, _to_provider
from continuum.models.profiles import registry_profile
from continuum_lab.fault_injection import CrashInjector, CrashRule, for_milestone
from continuum_lab.tasks.catalog import CATALOG, get_task
from tests.contract.provider_rules import ProviderRejected, StrictProviders, validate

# Registry-shaped capability data, pinned so tests are deterministic.
INFO = {
    "openai/gpt-5.6": {"max_input_tokens": 272_000, "max_output_tokens": 128_000,
                       "supports_function_calling": True, "input_cost_per_token": 1.25e-6,
                       "output_cost_per_token": 1e-5},
    "openai/gpt-6.1": {"max_input_tokens": 400_000, "max_output_tokens": 128_000,
                       "supports_function_calling": True, "input_cost_per_token": 2e-6,
                       "output_cost_per_token": 1.6e-5},
    "anthropic/claude-opus-4-8": {"max_input_tokens": 200_000, "max_output_tokens": 64_000,
                                  "supports_function_calling": True,
                                  "input_cost_per_token": 5e-6, "output_cost_per_token": 2.5e-5},
    "anthropic/claude-opus-5-5": {"max_input_tokens": 1_000_000, "max_output_tokens": 128_000,
                                  "supports_function_calling": True,
                                  "input_cost_per_token": 4e-6, "output_cost_per_token": 2e-5},
}


def endpoint(model_id, alias=None):
    return ModelEndpoint(profile=registry_profile(model_id, alias, info=INFO[model_id]),
                         adapter=LiteLLMAdapter(model_id))


def router(mapping, *, resolver=True):
    return ModelRouter({a: endpoint(m, a) for a, m in mapping.items()},
                       resolver=endpoint if resolver else None)


@pytest.fixture
def providers(monkeypatch):
    import litellm

    def install(plan, **kw):
        stub = StrictProviders(plan, **kw)
        monkeypatch.setattr(litellm, "acompletion", stub)
        monkeypatch.setattr(litellm, "completion_cost",
                            lambda completion_response: (_ for _ in ()).throw(ValueError()))
        return stub
    return install


def _sessions(lab, run_id):
    with lab.store.read() as q:
        return [e.payload["model_id"] for e in q.events(run_id)
                if e.type.value == "ModelSessionStarted"]


# ---- the bug this suite was built to catch ------------------------------------------------

@pytest.mark.parametrize("vendor", ["openai", "anthropic"])
def test_every_catalog_tool_name_is_valid_for_every_vendor(vendor):
    allow = sorted({t for task_id in CATALOG for t in get_task(task_id).spec.tools_allowlist})
    names = ToolNames(allow)
    schemas = [{"type": "function", "function": {"name": n, "parameters": {}}}
               for n in names.to_provider]
    validate(vendor, [], names.tools(schemas))  # raises ProviderRejected if invalid
    assert {names.backward(names.forward(n)) for n in allow} == set(allow)
    with pytest.raises(ProviderRejected):
        validate(vendor, [], schemas)  # the raw dotted names are what used to be sent
    assert names.backward(names.forward("store.put")) == "store.put"


def test_colliding_tool_names_are_refused_not_silently_merged():
    from continuum.models.base import ModelError

    with pytest.raises(ModelError):
        ToolNames(["store.put", "store_put"])


def test_history_with_missing_call_ids_is_still_valid():
    from continuum.models.base import Message, ToolProposal

    names = ToolNames(["db.get"])
    msgs = _to_provider([
        Message(role="user", content="go"),
        Message(role="assistant", tool_proposals=[
            ToolProposal(tool_name="db.get", arguments={"record_id": "r1"}),
            ToolProposal(tool_name="db.get", arguments={"record_id": "r2"})]),
        Message(role="tool", content="{}"), Message(role="tool", content="{}")], names)
    validate("anthropic", msgs, names.tools(
        [{"type": "function", "function": {"name": "db.get", "parameters": {}}}]))
    assert [c["id"] for c in msgs[1]["tool_calls"]] == [msgs[2]["tool_call_id"],
                                                         msgs[3]["tool_call_id"]]


# ---- switching scenarios ------------------------------------------------------------------

def test_vendor_switch_mid_run_openai_to_anthropic(make_lab, providers):
    lab = make_lab("csv_normalize_categories")
    outage = type("ServiceUnavailableError", (Exception,), {})("503")
    stub = providers(lab.definition.plan)
    rt = lab.new_runtime(owner_id="ctl-1", router=router(
        {"A": "openai/gpt-5.6", "B": "anthropic/claude-opus-5-5", "C": "openai/gpt-6.1"}))

    calls = {"n": 0}
    original = stub.__call__

    async def flaky(**kw):  # OpenAI goes down after its first two turns
        calls["n"] += 1
        if kw["model"].startswith("openai/") and calls["n"] > 2:
            raise outage
        return await original(**kw)

    import litellm

    litellm.acompletion = flaky
    result = rt.start(lab.definition.spec)
    assert result.status == RunStatus.COMPLETED
    assert stub.rejections == []
    assert _sessions(lab, result.run_id) == ["openai/gpt-5.6", "anthropic/claude-opus-5-5"]
    assert lab.env.store.commits_for("uploads/categories.csv") == 1


def test_version_upgrade_between_chats_gpt_5_6_to_6_1(make_lab, providers):
    """Chat 1 runs on gpt-5.6 and stops; the operator upgrades alias A to gpt-6.1;
    chat 2 (a new process) resumes the same run on the new version without redoing work."""
    crash = CrashInjector(CrashRule("after_milestone_verified_commit", for_milestone("m1")))
    lab = make_lab("csv_normalize_categories", faults=crash)
    stub = providers(lab.definition.plan)
    chat1 = lab.new_runtime(owner_id="chat-1", faults=crash,
                            router=router({"A": "openai/gpt-5.6", "B": "openai/gpt-5.6",
                                           "C": "openai/gpt-5.6"}))
    with pytest.raises(SimulatedCrash):
        chat1.start(lab.definition.spec)
    with lab.store.read() as q:
        run_id = q.run_ids()[-1]
    lab.clock.advance(31)
    chat2 = lab.new_runtime(owner_id="chat-2", router=router(
        {"A": "openai/gpt-6.1", "B": "openai/gpt-6.1", "C": "openai/gpt-6.1"}))
    result = chat2.resume(run_id)
    assert result.status == RunStatus.COMPLETED
    assert stub.rejections == []
    assert _sessions(lab, run_id) == ["openai/gpt-5.6", "openai/gpt-6.1"]
    with lab.store.read() as q:
        normalize = [a for a in q.actions(run_id) if a.tool_name == "csv.normalize"]
        assert len(normalize) == 1 and len(q.attempts(normalize[0].action_id)) == 1


def test_switch_to_newly_released_model_by_id_claude_4_8_to_5_5(make_lab, providers):
    """Resume a Claude Opus 4.8 run on Claude Opus 5.5 by model id — no config edit."""
    crash = CrashInjector(CrashRule("after_milestone_verified_commit", for_milestone("m2")))
    lab = make_lab("upload_and_notify", faults=crash)
    stub = providers(lab.definition.plan)
    configured = {"A": "anthropic/claude-opus-4-8", "B": "anthropic/claude-opus-4-8",
                  "C": "anthropic/claude-opus-4-8"}
    with pytest.raises(SimulatedCrash):
        lab.new_runtime(owner_id="chat-1", faults=crash,
                        router=router(configured)).start(lab.definition.spec)
    with lab.store.read() as q:
        run_id = q.run_ids()[-1]
    lab.clock.advance(31)
    result = lab.new_runtime(owner_id="chat-2", router=router(configured)).resume(
        run_id, model_alias="anthropic/claude-opus-5-5")
    assert result.status == RunStatus.COMPLETED
    assert stub.rejections == []
    assert _sessions(lab, run_id)[-1] == "anthropic/claude-opus-5-5"
    assert lab.env.store.commits_for("uploads/customers.csv") == 1
    assert len(lab.env.messages.ground_truth()) == 1
    with lab.store.read() as q:
        usage = q.usage(run_id)
    assert usage.cost_known_usd > 0  # estimated from the new model's price snapshot


def test_round_trip_across_three_vendors_keeps_one_effect(make_lab, providers):
    lab = make_lab("db_partial_batch")
    down = type("ServiceUnavailableError", (Exception,), {})
    stub = providers(lab.definition.plan,
                     fail={"openai/gpt-6.1": down("503"),
                           "anthropic/claude-opus-5-5": down("529")})
    rt = lab.new_runtime(owner_id="ctl-1", router=router(
        {"A": "openai/gpt-6.1", "B": "anthropic/claude-opus-5-5", "C": "openai/gpt-5.6"}))
    result = rt.start(lab.definition.spec)
    assert result.status == RunStatus.COMPLETED
    assert _sessions(lab, result.run_id) == ["openai/gpt-6.1", "anthropic/claude-opus-5-5",
                                             "openai/gpt-5.6"]
    assert stub.rejections == []
    assert all(lab.env.db.commits_for(r) == 1 for r in ("r3", "r4", "r5", "r6"))


# ---- profiles for new versions ----------------------------------------------------------

def test_incomplete_registry_entry_is_refused_with_guidance():
    unknown = {"max_input_tokens": None, "max_output_tokens": None,
               "supports_function_calling": None, "input_cost_per_token": 0}
    with pytest.raises(UnsupportedCapabilityError, match=r"config/models\.yaml"):
        registry_profile("openai/gpt-6.1", info=unknown)


def test_zero_registry_price_means_unknown_not_free():
    info = {**INFO["openai/gpt-6.1"], "input_cost_per_token": 0, "output_cost_per_token": 0}
    assert registry_profile("openai/gpt-6.1", info=info).price.input_per_million_usd is None


def test_unconfigured_alias_without_provider_prefix_is_rejected():
    with pytest.raises(UnsupportedCapabilityError, match="provider/model-id"):
        router({"A": "openai/gpt-5.6"}).get("gpt-6.1")
