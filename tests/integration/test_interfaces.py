"""SDK, CLI, loopback HTTP API and MCP facade over pre-registered tasks (R10)."""

import json
from pathlib import Path

import pytest
from fastapi.testclient import TestClient

from continuum.core.errors import SpecValidationError
from continuum.interfaces import cli
from continuum.interfaces.http_api import create_app, serve
from continuum.interfaces.mcp_facade import McpFacade
from continuum.interfaces.sdk import Continuum
from continuum.interfaces.settings import Settings
from tests.conftest import DSN

CONFIG = Path(__file__).resolve().parents[2] / "config"


@pytest.fixture
def settings(schema, tmp_path):
    return Settings(database_url=DSN, db_schema=schema, home=tmp_path / "home",
                    config_dir=CONFIG, owner_id="ctl-test", lease_ttl_seconds=30)


@pytest.fixture
def sdk(settings):
    with Continuum(settings) as s:
        s.init_db()
        yield s


def test_sdk_lifecycle_with_approval(sdk):
    assert "inventory_upload_v1" in [t["task_id"] for t in sdk.tasks()]
    done = sdk.start("inventory_upload_v1")
    assert done.status.value == "completed"
    assert sdk.status(done.run_id)["milestones"][1]["status"] == "verified"
    assert sdk.inspect(done.run_id)["projection_diff"] == []

    paused = sdk.start("notify_with_approval")
    assert paused.status.value == "paused_for_approval"
    sdk.resolve_action(paused.run_id, paused.awaiting_approval[0], "approve", actor="lead",
                       reason="ok")
    assert sdk.resume(paused.run_id).status.value == "completed"


def test_unregistered_task_is_rejected(sdk):
    from continuum.core.errors import NotFoundError

    with pytest.raises(NotFoundError):
        sdk.start("arbitrary_task_from_the_internet")


def test_http_api_endpoints(sdk):
    client = TestClient(create_app(sdk), client=("127.0.0.1", 50000),
                        base_url="http://127.0.0.1:8765")
    created = client.post("/runs", json={"task_id": "notify_with_approval"})
    assert created.status_code == 201
    body = created.json()
    assert body["status"] == "paused_for_approval"
    run_id, action_id = body["run_id"], body["awaiting_approval"][0]
    assert client.get(f"/runs/{run_id}").json()["status"] == "paused_for_approval"
    bad = client.post(f"/runs/{run_id}/actions/{action_id}/resolve",
                      json={"decision": "confirmed", "actor": "lead", "reason": "x"})
    assert bad.status_code == 409  # not awaiting reconciliation; awaiting approval
    ok = client.post(f"/runs/{run_id}/actions/{action_id}/resolve",
                     json={"decision": "approve", "actor": "lead", "reason": "reviewed"})
    assert ok.status_code == 200
    assert client.post(f"/runs/{run_id}/resume", json={}).json()["status"] == "completed"
    assert client.get("/runs/nope").status_code == 404


def test_http_rejects_non_loopback_clients_and_binds(sdk):
    remote = TestClient(create_app(sdk), client=("203.0.113.9", 50000))
    assert remote.get("/runs/x").status_code == 403
    with pytest.raises(SpecValidationError):
        serve(sdk, host="0.0.0.0")


def test_mcp_facade_only_delegates_registered_tasks(sdk):
    facade = McpFacade(sdk)
    assert "error" in facade.run_task("not_registered")
    result = facade.run_task("db_update_selected")
    assert result["status"] == "completed"
    assert facade.get_status(result["run_id"])["status"] == "completed"
    assert "error" in facade.get_status("missing")


def test_cli_json_output(settings, monkeypatch, capsys):
    monkeypatch.setenv("CONTINUUM_DATABASE_URL", settings.database_url)
    monkeypatch.setenv("CONTINUUM_DB_SCHEMA", settings.db_schema)
    monkeypatch.setenv("CONTINUUM_HOME", str(settings.home))
    monkeypatch.setenv("CONTINUUM_CONFIG_DIR", str(settings.config_dir))
    assert cli.main(["db-init"]) == 0
    assert cli.main(["run", "csv_dedupe_report"]) == 0
    out = capsys.readouterr().out
    run = json.loads(out[out.index("{", out.index("}") + 1):])
    assert run["status"] == "completed"
    assert cli.main(["status", run["run_id"]]) == 0
    assert cli.main(["status", "missing-run"]) == 2


def test_demo_tasks_show_recovery_and_human_pause(sdk):
    done = sdk.start("demo_ack_loss_recovery")
    assert done.status.value == "completed"
    status = sdk.status(done.run_id)
    assert status["task_id"] == "demo_ack_loss_recovery"
    trace = sdk.trace(done.run_id)
    assert any(r["type"] == "ActionAttemptAmbiguous" for r in trace)
    assert any(r["type"] == "ActionReconciled" for r in trace)

    paused = sdk.start("demo_opaque_needs_human")
    assert paused.status.value == "awaiting_reconciliation"
    [action_id] = paused.needs_human
    sdk.resolve_action(paused.run_id, action_id, "confirmed", actor="ops",
                       reason="provider console shows delivery", evidence_ref="receipt:1")
    assert sdk.resume(paused.run_id).status.value == "completed"


def test_http_trace_endpoint(sdk):
    client = TestClient(create_app(sdk), client=("127.0.0.1", 50000),
                        base_url="http://127.0.0.1:8765")
    run_id = client.post("/runs", json={"task_id": "db_update_selected"}).json()["run_id"]
    trace = client.get(f"/runs/{run_id}/trace").json()
    assert trace[0]["type"] == "RunCreated"


def test_mcp_facade_delegation_and_read_only_views(sdk):
    facade = McpFacade(sdk)
    assert any(t["task_id"] == "db_update_selected" for t in facade.list_tasks()["tasks"])
    paused = facade.run_task("notify_with_approval")
    assert paused["status"] == "paused_for_approval"
    decisions = facade.pending_decisions(paused["run_id"])["decisions"]
    assert decisions[0]["decisions"] == ["approve", "reject"]
    cancelled = facade.cancel_run(paused["run_id"], reason="user changed their mind")
    assert cancelled["status"] == "cancelled"
    runs = facade.list_runs(5)["runs"]
    assert runs[0]["run_id"] == paused["run_id"]
    assert not hasattr(facade, "resolve_action")  # human decisions are never agent tools


def test_doctor_reports_installation_health(sdk):
    report = sdk.doctor()
    assert report["database"]["ok"] and report["schema"]["ok"] and report["tasks"]["ok"]
    assert report["live_models"]["A"]["deployable"] is False  # placeholders are rejected
    assert report["ok"] is True
