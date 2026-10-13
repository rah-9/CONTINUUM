"""Opt-in live provider contract tests. These call real APIs and may cost money.

Run with ``CONTINUUM_LIVE=1 pytest -m live`` after filling verified profiles in
``config/models.yaml`` and exporting provider API keys.
"""

import os
from pathlib import Path

import pytest

from continuum.core.state import RunStatus
from continuum.interfaces.registry import load_live_router

pytestmark = [pytest.mark.live,
              pytest.mark.skipif(os.environ.get("CONTINUUM_LIVE") != "1",
                                 reason="live provider tests are opt-in (CONTINUUM_LIVE=1)")]

CONFIG = Path(__file__).resolve().parents[2] / "config" / "models.yaml"


@pytest.mark.parametrize("alias", ["A", "B", "C"])
def test_live_profile_is_deployable(alias):
    load_live_router(CONFIG).get(alias).profile.check_deployable()


@pytest.mark.parametrize("alias", ["A", "B"])
def test_live_provider_completes_demo_task(make_lab, alias):
    lab = make_lab("inventory_upload_v1")
    router = load_live_router(CONFIG)
    result = lab.new_runtime(owner_id="ctl-live", router=router).start(
        lab.definition.spec, model_alias=alias)
    assert result.status == RunStatus.COMPLETED
