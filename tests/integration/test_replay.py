# SPDX-License-Identifier: LicenseRef-Continuum-Proprietary
# Copyright (c) 2026 the Continuum copyright holder. All rights reserved.
"""Diagnostic replay is side-effect free (R11, I8)."""

from continuum.core.state import RunStatus


def test_inspect_invokes_no_tool_or_model(make_lab):
    lab = make_lab("upload_and_notify")
    result = lab.runtime.start(lab.definition.spec)
    assert result.status == RunStatus.COMPLETED
    calls = {a: ad.calls for a, ad in lab.adapters.items()}
    service_requests = (len(lab.env.store.attempts_log()), len(lab.env.messages.attempts_log()))
    with lab.store.read() as q:
        seq = q.run(result.run_id).last_event_seq
    report = lab.new_runtime().inspect(result.run_id)
    assert report["replayed_status"] == "completed"
    assert report["projection_diff"] == []
    assert {a: ad.calls for a, ad in lab.adapters.items()} == calls
    assert (len(lab.env.store.attempts_log()),
            len(lab.env.messages.attempts_log())) == service_requests
    with lab.store.read() as q:
        assert q.run(result.run_id).last_event_seq == seq  # nothing appended
