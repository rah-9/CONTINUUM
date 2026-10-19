# SPDX-License-Identifier: LicenseRef-Continuum-Proprietary
# Copyright (c) 2026 the Continuum copyright holder. All rights reserved.
"""Every lab task completes cleanly (no faults) with exactly the intended effects."""

import pytest

from continuum.core.state import RunStatus
from continuum_lab.tasks.catalog import CATALOG
from continuum_lab.tasks.definition import score_ground_truth

AUTO_TASKS = sorted(t for t in CATALOG if t != "notify_with_approval")


@pytest.mark.parametrize("task_id", AUTO_TASKS)
def test_task_completes_without_faults(make_lab, task_id):
    lab = make_lab(task_id)
    result = lab.runtime.start(lab.definition.spec)
    assert result.status == RunStatus.COMPLETED, lab.runtime.status(result.run_id)
    score = score_ground_truth(lab.env, lab.definition.expected)
    assert score.final_state_correct
    assert score.committed_duplicates == 0
    assert score.detail["unintended_targets"] == 0
    assert lab.store.projection_diff(result.run_id) == []


def test_approval_task_pauses_then_completes_after_human_approval(make_lab):
    lab = make_lab("notify_with_approval")
    result = lab.runtime.start(lab.definition.spec)
    assert result.status == RunStatus.PAUSED_FOR_APPROVAL
    assert lab.env.messages.ground_truth() == []  # nothing sent before approval
    [action_id] = result.awaiting_approval
    lab.runtime.resolve_action(result.run_id, action_id, "approve", actor="ops-lead",
                               reason="content reviewed")
    resumed = lab.new_runtime().resume(result.run_id)
    assert resumed.status == RunStatus.COMPLETED, lab.runtime.status(result.run_id)
    assert len(lab.env.messages.ground_truth()) == 1
