# SPDX-License-Identifier: LicenseRef-Continuum-Proprietary
# Copyright (c) 2026 the Continuum copyright holder. All rights reserved.
from continuum.core.state import RunStatus


def test_inventory_upload_happy_path(make_lab):
    lab = make_lab("inventory_upload_v1")
    result = lab.runtime.start(lab.definition.spec)
    assert result.status == RunStatus.COMPLETED, lab.runtime.status(result.run_id)
    assert lab.env.store.commits_for("uploads/inventory.csv") == 1
    assert lab.store.projection_diff(result.run_id) == []
