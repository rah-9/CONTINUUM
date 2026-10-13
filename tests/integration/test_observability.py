"""Structured JSON tracing, timeline replay and trace export (NFR tracing, R11, I8)."""

import io
import json

import pytest

from continuum.core.state import RunStatus
from continuum.core.tracing import configure_logging


@pytest.fixture
def log_stream():
    stream = io.StringIO()
    configure_logging("INFO", stream)
    yield stream
    configure_logging("WARNING")


def test_json_trace_lines_cover_events_tools_and_models(make_lab, log_stream, monkeypatch):
    monkeypatch.setenv("SHOP_API_TOKEN", "tok-should-never-appear-123")
    lab = make_lab("upload_and_notify")
    result = lab.runtime.start(lab.definition.spec)
    assert result.status == RunStatus.COMPLETED
    lines = [json.loads(line) for line in log_stream.getvalue().splitlines()]
    kinds = {line["event"] for line in lines}
    assert {"event_committed", "tool_dispatch", "model_call"} <= kinds
    dispatch = next(line for line in lines if line["event"] == "tool_dispatch")
    assert dispatch["duration_ms"] >= 0 and dispatch["outcome"] == "ok"
    with lab.store.read() as q:
        assert sum(1 for line in lines if line["event"] == "event_committed") == \
            q.run(result.run_id).last_event_seq
    assert "tok-should-never-appear-123" not in log_stream.getvalue()
    assert "arguments" not in log_stream.getvalue()  # IDs and statuses only, no payloads


def test_timeline_and_trace_export_are_read_only(make_lab):
    lab = make_lab("inventory_upload_v1", store_mode="observable")
    lab.env.store.script("put", "commit_then_drop")
    result = lab.runtime.start(lab.definition.spec)
    with lab.store.read() as q:
        seq = q.run(result.run_id).last_event_seq
    calls = sum(a.calls for a in lab.adapters.values())
    requests = len(lab.env.store.attempts_log())

    report = lab.new_runtime().inspect(result.run_id)
    kinds = [step["kind"] for step in report["timeline"]]
    assert "model_decision" in kinds and "reconciliation" in kinds
    decision = next(s for s in report["timeline"]
                    if s["kind"] == "model_decision" and s["proposals"])
    assert decision["proposals"][0]["tool"] == "csv.normalize"
    outcomes = [s["result"] for s in report["timeline"] if s["kind"] == "outcome"]
    assert outcomes == ["ActionAttemptConfirmed", "ActionAttemptAmbiguous"]  # writes only

    trace = lab.new_runtime().trace_export(result.run_id)
    assert [r["seq"] for r in trace] == list(range(1, seq + 1))
    finished = next(r for r in trace if r["type"] == "ModelCallFinished")
    assert "response_ref" in finished["blobs"]  # model decision inlined for replay
    with lab.store.read() as q:
        assert q.run(result.run_id).last_event_seq == seq
    assert sum(a.calls for a in lab.adapters.values()) == calls
    assert len(lab.env.store.attempts_log()) == requests
