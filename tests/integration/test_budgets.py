# SPDX-License-Identifier: LicenseRef-Continuum-Proprietary
# Copyright (c) 2026 the Continuum copyright holder. All rights reserved.
"""Aggregate hard budgets (R9)."""

from decimal import Decimal

import pytest

from continuum.core.faults import SimulatedCrash
from continuum.core.state import Budgets, RunStatus
from continuum.models.base import ModelError
from continuum_lab.fault_injection import CrashInjector, CrashRule
from continuum_lab.harness import ModelFaults


def _spec(lab, **budget):
    base = lab.definition.spec.budgets.model_dump()
    return lab.definition.spec.model_copy(update={"budgets": Budgets(**{**base, **budget})})


def test_model_call_budget_is_enforced_across_handoffs(make_lab):
    faults = ModelFaults(fail_always={"A": ModelError("provider_unavailable")})
    lab = make_lab("csv_normalize_categories", model_faults=faults)
    result = lab.runtime.start(_spec(lab, max_model_calls=4))
    assert result.status == RunStatus.FAILED
    assert result.failure_code == "budget_exceeded"
    with lab.store.read() as q:
        assert q.usage(result.run_id).model_calls == 4


def test_cost_budget(make_lab):
    lab = make_lab("csv_normalize_categories")
    result = lab.runtime.start(_spec(lab, max_cost_usd=Decimal("0.001")))
    assert result.failure_code == "budget_exceeded"


def test_recovery_budget_and_no_silent_reset(make_lab):
    crash = CrashInjector(CrashRule("after_milestone_verified_commit"))
    lab = make_lab("csv_normalize_categories", faults=crash)
    with pytest.raises(SimulatedCrash):
        lab.runtime.start(_spec(lab, max_recoveries=0))
    with lab.store.read() as q:
        run_id = q.run_ids()[-1]
    lab.clock.advance(31)
    result = lab.new_runtime().resume(run_id)
    assert result.status == RunStatus.FAILED
    assert result.failure_code == "budget_exceeded"


def test_downtime_does_not_count_as_active_time(make_lab):
    crash = CrashInjector(CrashRule("after_milestone_verified_commit"))
    lab = make_lab("csv_normalize_categories", faults=crash)
    with pytest.raises(SimulatedCrash):
        lab.runtime.start(_spec(lab, max_active_seconds=5))
    with lab.store.read() as q:
        run_id = q.run_ids()[-1]
    lab.clock.advance(3600)  # an hour of process downtime
    result = lab.new_runtime().resume(run_id)
    assert result.status == RunStatus.COMPLETED
