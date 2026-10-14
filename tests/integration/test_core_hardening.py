"""Audit regressions for the core runtime and interfaces (v0.6.2).

Each test reproduces a defect found by the full-codebase audit before it was fixed.
"""

import shutil
from pathlib import Path
from types import SimpleNamespace

import pytest
from fastapi.testclient import TestClient

from continuum.core.errors import AlreadyOwnedError, ContinuumError, UnsupportedCapabilityError
from continuum.core.faults import SimulatedCrash
from continuum.core.redaction import Redactor
from continuum.core.state import RunStatus
from continuum.interfaces.http_api import create_app
from continuum.interfaces.mcp_facade import McpFacade
from continuum.interfaces.sdk import Continuum
from continuum.interfaces.settings import Settings
from continuum.models.base import CapabilityProfile, Price, Usage
from continuum.models.litellm_adapter import LiteLLMAdapter
from continuum.tools.trusted.services import ServiceTimeout
from continuum_lab.fault_injection import CrashInjector, CrashRule, for_milestone
from tests.conftest import DSN

CONFIG = Path(__file__).resolve().parents[2] / "config"


def _settings(schema, tmp_path, config_dir=CONFIG):
    return Settings(database_url=DSN, db_schema=schema, home=tmp_path / "home",
                    config_dir=config_dir, owner_id="ctl-test", lease_ttl_seconds=30)


@pytest.fixture
def sdk(schema, tmp_path):
    with Continuum(_settings(schema, tmp_path)) as s:
        s.init_db()
        yield s


# ----- K1: a transient read error is not drift ----------------------------------------------

def test_k1_transient_verifier_error_on_resume_is_not_drift(make_lab):
    crash = CrashInjector(CrashRule("after_milestone_verified_commit", for_milestone("m2")))
    lab = make_lab("inventory_upload_v1", faults=crash)
    with pytest.raises(SimulatedCrash):
        lab.runtime.start(lab.definition.spec)
    with lab.store.read() as q:
        run_id = q.run_ids()[-1]
    lab.clock.advance(31)
    store_cls = type(lab.env.store)
    real_head = store_cls.head
    calls = {"n": 0}

    def flaky(self, object_path):
        calls["n"] += 1
        if calls["n"] == 1:
            raise ServiceTimeout("transient")
        return real_head(self, object_path)

    store_cls.head = flaky
    try:
        paused = lab.new_runtime().resume(run_id)
    finally:
        store_cls.head = real_head
    assert paused.status == RunStatus.AWAITING_RECONCILIATION
    with lab.store.read() as q:
        types = [e.type.value for e in q.events(run_id)]
    assert "VerifierError" in types and "MilestoneInvalidated" not in types
    lab.clock.advance(31)
    assert lab.new_runtime(owner_id="ctl-3").resume(run_id).status == RunStatus.COMPLETED


# ----- K2: one process never runs two operations on the same run ---------------------------

def test_k2_same_process_cannot_operate_twice_on_one_run(make_lab):
    lab = make_lab("inventory_upload_v1")
    seen: dict[str, object] = {}

    class Reenter:
        def checkpoint(self, boundary, **context):
            if boundary == "after_milestone_verified_commit" and "error" not in seen:
                try:
                    lab.runtime.cancel(context["run_id"], actor="op", reason="concurrent")
                    seen["error"] = None
                except AlreadyOwnedError as exc:
                    seen["error"] = exc

    lab.runtime.store.faults = Reenter()
    lab.runtime.verification.faults = Reenter()
    result = lab.runtime.start(lab.definition.spec)
    assert isinstance(seen["error"], AlreadyOwnedError)
    assert result.status == RunStatus.COMPLETED


# ----- K3: DNS rebinding -------------------------------------------------------------------

def test_k3_http_rejects_foreign_host_and_origin(sdk):
    client = TestClient(create_app(sdk), client=("127.0.0.1", 50000),
                        base_url="http://127.0.0.1:8765")
    assert client.get("/runs/x").status_code == 404
    assert client.get("/runs/x", headers={"host": "rebind.attacker.example"}).status_code == 403
    assert client.post("/runs", json={"task_id": "x"},
                       headers={"origin": "http://rebind.attacker.example"}).status_code == 403
    assert client.get("/runs/x", headers={"host": "localhost:8765",
                                          "origin": "http://localhost:8765"}).status_code == 404


# ----- K4: bad model alias -------------------------------------------------------------------

def test_k4_unknown_model_alias_is_rejected_before_any_run_is_created(sdk, make_lab):
    lab = make_lab("inventory_upload_v1")
    with pytest.raises(UnsupportedCapabilityError):
        lab.runtime.start(lab.definition.spec, model_alias="nope")
    with lab.store.read() as q:
        assert q.run_ids() == []
    client = TestClient(create_app(sdk), client=("127.0.0.1", 50000),
                        base_url="http://127.0.0.1:8765")
    bad = client.post("/runs", json={"task_id": "inventory_upload_v1", "model_alias": "nope"})
    assert bad.status_code == 422 and bad.json()["code"] == "unsupported_capability"
    assert sdk.runs() == []
    done = sdk.start("inventory_upload_v1")
    with pytest.raises(UnsupportedCapabilityError):
        sdk.resume(done.run_id, model_alias="nope")


# ----- K5: malformed configuration and unexpected errors never escape raw -----------------

def _config_with(tmp_path, tasks: dict[str, str], models: str | None = None) -> Path:
    cfg = tmp_path / "cfg"
    (cfg / "tasks").mkdir(parents=True)
    for name, body in tasks.items():
        (cfg / "tasks" / name).write_text(body)
    shutil.copy(CONFIG / "models.yaml", cfg / "models.yaml")
    if models is not None:
        (cfg / "models.yaml").write_text(models)
    return cfg


def test_k5_bad_task_files_are_reported_not_fatal(schema, tmp_path):
    good = (CONFIG / "tasks" / "inventory_upload_v1.yaml").read_text()
    cfg = _config_with(tmp_path, {"good.yaml": good, "empty.yaml": "",
                                  "noid.yaml": "description: x\n",
                                  "badfactory.yaml": "task_id: t1\nfactory: no_such_mod:f\n"})
    with Continuum(_settings(schema, tmp_path, cfg)) as s:
        s.init_db()
        assert [t["task_id"] for t in s.tasks()] == ["inventory_upload_v1", "t1"]
        assert sorted(p.name for p in s.registry.errors) == ["empty.yaml", "noid.yaml"]
        with pytest.raises(ContinuumError) as err:
            s.start("t1")
        assert err.value.code == "invalid_spec"
        facade = McpFacade(s)
        assert facade.run_task("t1")["code"] == "invalid_spec"


def test_k5_broken_models_yaml_does_not_break_read_only_inspection(schema, tmp_path):
    good = (CONFIG / "tasks" / "inventory_upload_v1.yaml").read_text().replace(
        "fake_models: true", "fake_models: false")
    cfg = _config_with(tmp_path, {"t.yaml": good}, models="- not a mapping\n")
    with Continuum(_settings(schema, tmp_path, cfg)) as s:
        s.init_db()
        with pytest.raises(ContinuumError) as err:
            s.start("inventory_upload_v1")
        assert err.value.code == "invalid_spec"


def test_k5_mcp_masks_unexpected_errors_and_list_runs_tolerates_bad_runs(sdk, monkeypatch):
    facade = McpFacade(sdk)
    done = sdk.start("inventory_upload_v1")

    def boom(run_id):
        raise RuntimeError("secret internals")

    monkeypatch.setattr(sdk, "status", boom)
    out = facade.get_status(done.run_id)
    assert out["code"] == "internal" and "secret internals" not in out["error"]
    listed = facade.list_runs()
    assert listed["runs"][0]["run_id"] == done.run_id and listed["runs"][0]["code"] == "internal"


def test_k5_cli_reports_database_down_without_a_traceback(monkeypatch, capsys):
    from continuum.interfaces import cli

    monkeypatch.setenv("CONTINUUM_DATABASE_URL", "postgresql://nobody:x@127.0.0.1:1/none")
    monkeypatch.setenv("CONTINUUM_DB_TIMEOUT_SECONDS", "2")
    assert cli.main(["runs"]) == 2
    assert '"code": "database_unavailable"' in capsys.readouterr().out


# ----- K6: costs -------------------------------------------------------------------------

def _raw(prompt=1000, completion=1000):
    msg = SimpleNamespace(content="hi", tool_calls=None)
    return SimpleNamespace(choices=[SimpleNamespace(message=msg, finish_reason="stop")],
                           usage=SimpleNamespace(prompt_tokens=prompt,
                                                 completion_tokens=completion))


def test_k6_zero_registry_cost_is_unknown_and_configured_prices_win():
    usage = LiteLLMAdapter("ollama/llama3")._normalize(_raw()).usage
    assert usage.cost_usd is None  # registry price 0 means "unknown", never "free"
    from continuum.core.sessions import estimate_cost

    profile = CapabilityProfile(alias="a", model_id="m", context_window_tokens=1000,
                                max_output_tokens=100, supports_native_tools=True,
                                price=Price(input_per_million_usd="1",
                                            output_per_million_usd="2"))
    litellm_guess = Usage(input_tokens=1000, output_tokens=1000, cost_usd="0.5",
                          source="estimated")
    assert estimate_cost(profile, litellm_guess).cost_usd == "0.003"


# ----- K7: secrets in rejection reasons ----------------------------------------------------

def test_k7_rejection_reasons_are_redacted(make_lab):
    lab = make_lab("inventory_upload_v1")
    ctx = lab.open_run("run_s")
    secret = "sk-live-SUPERSECRET-0123456789"
    lab.runtime.gateway.redactor = Redactor([secret])
    lab.runtime.gateway.propose(ctx, "file.read", {"path": 1, "x": secret}, caller="model",
                                milestone_id="m1")
    lab.runtime.gateway.propose(ctx, "store.put", {"object_path": secret, "artifact_ref": "a"},
                                caller="model", milestone_id="m1")
    with lab.store.read() as q:
        reasons = [e.payload["reason"] for e in q.events("run_s")
                   if e.type.value == "ActionRejected"]
    assert len(reasons) == 2 and not any(secret in r for r in reasons)


# ----- K8: malformed provider responses ----------------------------------------------------

@pytest.mark.parametrize("raw", [
    SimpleNamespace(choices=[], usage=None),
    SimpleNamespace(choices=[SimpleNamespace(message=SimpleNamespace(
        content=None, tool_calls=[SimpleNamespace(id="c", function=None)]),
        finish_reason="tool_calls")], usage=None),
    SimpleNamespace(usage=None),
])
def test_k8_malformed_provider_response_is_a_model_error(raw):
    from continuum.models.base import ModelError

    with pytest.raises(ModelError) as err:
        LiteLLMAdapter("openai/gpt-6.1")._normalize(raw)
    assert err.value.category == "invalid_tool_output"


# ----- K9: blob references ---------------------------------------------------------------

def test_k9_blob_refs_are_validated_and_human_receipts_are_not_read_as_blobs(sdk, tmp_path):
    from continuum.core.evidence import Workspace

    ws = Workspace(tmp_path / "ws", "run_b")
    ws.init()
    with pytest.raises(ContinuumError):
        ws.get_blob("blob:sha256:../../etc/passwd")
    paused = sdk.start("demo_opaque_needs_human")
    action_id = paused.needs_human[0]
    with pytest.raises(ContinuumError):
        sdk.resolve_action(paused.run_id, action_id, "confirmed", actor="op", reason="seen",
                           evidence_ref="blob:sha256:" + "0" * 64)
    sdk.resolve_action(paused.run_id, action_id, "confirmed", actor="op", reason="seen",
                       evidence_ref="receipt:123")
    assert sdk.status(paused.run_id)["needs_human"] == []
    assert sdk.resume(paused.run_id).status == RunStatus.COMPLETED
