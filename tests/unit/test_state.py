# SPDX-License-Identifier: LicenseRef-Continuum-Proprietary
# Copyright (c) 2026 the Continuum copyright holder. All rights reserved.
from decimal import Decimal

import pytest
from pydantic import ValidationError

from continuum.core.errors import SpecValidationError
from continuum.core.state import MilestoneStatus, focus_milestone, parse_task_spec


def spec_dict(**overrides):
    base = {
        "task_id": "t", "goal": "g", "tools_allowlist": ["file.read"],
        "milestones": [
            {"id": "m1", "description": "a", "depends_on": [], "verifier_id": "v",
             "verifier_version": "1"},
            {"id": "m2", "description": "b", "depends_on": ["m1"], "verifier_id": "v",
             "verifier_version": "1"},
            {"id": "m3", "description": "c", "depends_on": ["m2"], "verifier_id": "v",
             "verifier_version": "1"},
        ],
        "budgets": {"max_model_calls": 5, "max_tokens": 100, "max_cost_usd": "1",
                    "max_active_seconds": 10, "max_recoveries": 1},
        "model_policy": {"primary": "A"},
    }
    base.update(overrides)
    return base


def test_valid_spec_parses_and_is_frozen():
    spec = parse_task_spec(spec_dict())
    assert spec.budgets.max_cost_usd == Decimal("1")
    with pytest.raises(ValidationError):
        spec.goal = "x"  # type: ignore[misc]


@pytest.mark.parametrize(("milestones", "message"), [
    ([{"id": "m1", "description": "a", "depends_on": ["m2"], "verifier_id": "v",
       "verifier_version": "1"},
      {"id": "m2", "description": "b", "depends_on": ["m1"], "verifier_id": "v",
       "verifier_version": "1"}], "cycle"),
    ([{"id": "m1", "description": "a", "depends_on": ["zz"], "verifier_id": "v",
       "verifier_version": "1"}], "unknown"),
    ([{"id": "m1", "description": "a", "depends_on": [], "verifier_id": "v",
       "verifier_version": "1"}] * 2, "unique"),
    ([], "at least one"),
])
def test_invalid_milestone_graphs_rejected(milestones, message):
    with pytest.raises(SpecValidationError, match=message):
        parse_task_spec(spec_dict(milestones=milestones))


def test_non_positive_budget_rejected():
    with pytest.raises(SpecValidationError):
        parse_task_spec(spec_dict(budgets={"max_model_calls": 0, "max_tokens": 1,
                                           "max_cost_usd": "1", "max_active_seconds": 1,
                                           "max_recoveries": 0}))


def test_input_paths_must_be_confined():
    with pytest.raises(SpecValidationError, match="workspace-relative"):
        parse_task_spec(spec_dict(inputs=[{"uri": "../etc/passwd", "sha256": "0" * 64}]))


def test_focus_is_first_eligible_unverified_and_descendants_are_transitive():
    spec = parse_task_spec(spec_dict())
    V, P = MilestoneStatus.VERIFIED, MilestoneStatus.PENDING
    assert focus_milestone(spec, {"m1": P, "m2": P, "m3": P}) == "m1"
    assert focus_milestone(spec, {"m1": V, "m2": P, "m3": P}) == "m2"
    assert focus_milestone(spec, {"m1": V, "m2": V, "m3": V}) is None
    assert spec.descendants("m1") == ["m2", "m3"]
    assert spec.descendants("m3") == []
