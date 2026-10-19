# SPDX-License-Identifier: LicenseRef-Continuum-Proprietary
# Copyright (c) 2026 the Continuum copyright holder. All rights reserved.
"""Check e1 (Part 1): the export function. Registered as a human check."""

from continuum.coding.export import export_session
from trial_fixture import session  # noqa: F401  (pytest fixture)


def test_export_function(session):  # noqa: F811
    store, sid, _ = session
    rows = export_session(store, sid)
    assert [r["seq"] for r in rows] == sorted(r["seq"] for r in rows)
    assert len(rows) >= 4
    assert all({"seq", "type", "actor", "at", "payload"} <= set(r) for r in rows)
    assert all(r["at"].endswith("Z") for r in rows)
    ran = [r for r in rows if r["type"] == "CheckRan"]
    assert ran
    assert "output_tail" not in ran[0]["payload"]
    assert "output_sha256" in ran[0]["payload"]
    assert "CUSTOMER-SECRET-ROW" not in repr(rows)
