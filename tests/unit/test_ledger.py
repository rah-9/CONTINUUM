# SPDX-License-Identifier: LicenseRef-Continuum-Proprietary
# Copyright (c) 2026 the Continuum copyright holder. All rights reserved.
import pytest
from hypothesis import given
from hypothesis import strategies as st

from continuum.core.errors import InvalidTransitionError
from continuum.core.ledger import (
    ACTION_TRANSITIONS,
    assert_transition,
    idempotency_key,
    operation_key,
)
from continuum.core.state import ActionStatus as S


@pytest.mark.parametrize(("current", "target"), [
    (S.PROPOSED, S.AUTHORIZED), (S.AUTHORIZED, S.IN_FLIGHT), (S.IN_FLIGHT, S.UNKNOWN),
    (S.UNKNOWN, S.CONFIRMED), (S.UNKNOWN, S.NEEDS_HUMAN), (S.UNKNOWN, S.IN_FLIGHT),
    (S.NEEDS_HUMAN, S.SKIPPED), (S.FAILED_CLEAN, S.AUTHORIZED),
])
def test_allowed_transitions(current, target):
    assert_transition(current, target)


@pytest.mark.parametrize(("current", "target"), [
    (S.UNKNOWN, S.FAILED_CLEAN),   # a timeout is never a clean failure
    (S.CONFIRMED, S.IN_FLIGHT),    # confirmed is terminal
    (S.PROPOSED, S.IN_FLIGHT),     # must be authorized first
    (S.NEEDS_HUMAN, S.IN_FLIGHT),  # human must authorize explicitly
    (S.SKIPPED, S.AUTHORIZED),
])
def test_forbidden_transitions(current, target):
    with pytest.raises(InvalidTransitionError):
        assert_transition(current, target)


def test_every_status_has_a_rule():
    assert set(ACTION_TRANSITIONS) == set(S)


def test_operation_key_uses_trusted_fields_only():
    def norm(f, v):
        return str(v).strip()
    key = operation_key(tool_name="store.put", milestone_id="m2", milestone_scoped=True,
                        fields=["object_path"],
                        args={"object_path": " uploads/a ", "artifact_ref": "x"},
                        normalize=norm)
    assert key == "m2:store.put:uploads/a"
    with pytest.raises(ValueError):
        operation_key(tool_name="store.put", milestone_id=None, milestone_scoped=True,
                      fields=["object_path"], args={"object_path": "a"}, normalize=norm)


@given(st.text(min_size=1), st.text(min_size=1))
def test_idempotency_key_is_stable_and_bound_to_identity(run_id, action_id):
    assert idempotency_key(run_id, action_id) == idempotency_key(run_id, action_id)
    assert idempotency_key(run_id, action_id) != idempotency_key(run_id, action_id + "x")
