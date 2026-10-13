"""Lease acquisition, takeover epochs and fencing (R3, U6, I6)."""

import pytest

from continuum.core.errors import AlreadyOwnedError


def test_live_lease_cannot_be_stolen(make_lab):
    lab = make_lab("inventory_upload_v1")
    ctx = lab.open_run()
    leases = lab.runtime.leases
    with pytest.raises(AlreadyOwnedError):
        leases.acquire(ctx.run_id, "ctl-2")
    assert leases.is_active(ctx.lease)


def test_renewal_keeps_epoch_and_takeover_increments_it(make_lab):
    lab = make_lab("inventory_upload_v1")
    ctx = lab.open_run()
    leases = lab.runtime.leases
    same = leases.acquire(ctx.run_id, "ctl-1")
    assert same.epoch == ctx.lease.epoch
    renewed = leases.renew(ctx.lease)
    assert renewed.epoch == ctx.lease.epoch
    lab.clock.advance(leases.ttl_seconds + 1)
    assert not leases.is_active(renewed)
    taken = leases.acquire(ctx.run_id, "ctl-2")
    assert taken.epoch == ctx.lease.epoch + 1
    with pytest.raises(AlreadyOwnedError):
        leases.renew(renewed)


def test_release_then_reacquire_by_same_owner_still_bumps_epoch(make_lab):
    lab = make_lab("inventory_upload_v1")
    ctx = lab.open_run()
    leases = lab.runtime.leases
    leases.release(ctx.lease)
    again = leases.acquire(ctx.run_id, "ctl-1")
    assert again.epoch == ctx.lease.epoch + 1
