"""Controller lease and fencing epoch per run (docs/03 section 4, R3, I6).

* Acquisition is a single atomic upsert: it succeeds only if no lease exists,
  the existing lease expired, or the caller already owns it.
* The epoch increases on **every** takeover (any acquisition that is not a
  renewal of the caller's own live lease), including re-acquisition after
  release, so a stale writer from an earlier tenure can never match.
* Database time is used unless the clock supplies an explicit lease time
  (tests), so process clock skew cannot extend ownership.

A lease limits local controllers only; it cannot recall an in-flight request.
"""

from __future__ import annotations

import threading
from dataclasses import dataclass

from continuum.core.errors import AlreadyOwnedError
from continuum.core.store import LEASE_NOW_SQL, Fence, Store
from continuum.core.tracing import trace


@dataclass(frozen=True)
class Lease:
    run_id: str
    owner_id: str
    epoch: int
    expires_at_unix: int

    @property
    def fence(self) -> Fence:
        return Fence(self.owner_id, self.epoch)


class LeaseManager:
    def __init__(self, store: Store, *, ttl_seconds: int = 30) -> None:
        if ttl_seconds <= 0:
            raise ValueError("ttl_seconds must be positive")
        self.store = store
        self.ttl_seconds = ttl_seconds

    def acquire(self, run_id: str, owner_id: str) -> Lease:
        """Acquire or take over the run lease. Raises :class:`AlreadyOwnedError`."""
        params = {"run_id": run_id, "owner": owner_id, "ttl": self.ttl_seconds,
                  "now": self.store.lease_now()}
        with self.store.connection() as conn:
            row = conn.execute(
                f"""
                INSERT INTO run_leases AS l (run_id, owner_id, epoch, expires_at_unix)
                VALUES (%(run_id)s, %(owner)s, 1, {LEASE_NOW_SQL} + %(ttl)s)
                ON CONFLICT (run_id) DO UPDATE SET
                  owner_id = EXCLUDED.owner_id,
                  epoch = CASE
                    WHEN l.owner_id = EXCLUDED.owner_id AND l.expires_at_unix > {LEASE_NOW_SQL}
                    THEN l.epoch ELSE l.epoch + 1 END,
                  expires_at_unix = {LEASE_NOW_SQL} + %(ttl)s
                WHERE l.owner_id = EXCLUDED.owner_id OR l.expires_at_unix <= {LEASE_NOW_SQL}
                RETURNING run_id, owner_id, epoch, expires_at_unix
                """.encode(),
                params,
            ).fetchone()
            conn.commit()
        if row is None:
            raise AlreadyOwnedError(f"run {run_id} is owned by another live controller")
        lease = Lease(row["run_id"], row["owner_id"], row["epoch"], row["expires_at_unix"])
        self.store.faults.checkpoint("after_lease_acquired", run_id=run_id, epoch=lease.epoch)
        return lease

    def renew(self, lease: Lease) -> Lease:
        """Heartbeat. Raises :class:`AlreadyOwnedError` if ownership was lost."""
        with self.store.connection() as conn:
            row = conn.execute(
                f"""
                UPDATE run_leases SET expires_at_unix = {LEASE_NOW_SQL} + %(ttl)s
                WHERE run_id = %(run_id)s AND owner_id = %(owner)s AND epoch = %(epoch)s
                  AND expires_at_unix > {LEASE_NOW_SQL}
                RETURNING expires_at_unix
                """.encode(),
                {"run_id": lease.run_id, "owner": lease.owner_id, "epoch": lease.epoch,
                 "ttl": self.ttl_seconds, "now": self.store.lease_now()},
            ).fetchone()
            conn.commit()
        if row is None:
            raise AlreadyOwnedError(f"lease on {lease.run_id} lost by {lease.owner_id}")
        return Lease(lease.run_id, lease.owner_id, lease.epoch, row["expires_at_unix"])

    def is_active(self, lease: Lease) -> bool:
        """True if ``lease`` is still the active unexpired lease (pre-dispatch check)."""
        with self.store.connection() as conn:
            row = conn.execute(
                f"""
                SELECT 1 AS ok FROM run_leases
                WHERE run_id = %(run_id)s AND owner_id = %(owner)s AND epoch = %(epoch)s
                  AND expires_at_unix > {LEASE_NOW_SQL}
                """.encode(),
                {"run_id": lease.run_id, "owner": lease.owner_id, "epoch": lease.epoch,
                 "now": self.store.lease_now()},
            ).fetchone()
            conn.rollback()
        return row is not None

    def release(self, lease: Lease) -> None:
        """Expire the lease now if still held; the next acquisition bumps the epoch."""
        with self.store.connection() as conn:
            conn.execute(
                f"""
                UPDATE run_leases SET expires_at_unix = {LEASE_NOW_SQL}
                WHERE run_id = %(run_id)s AND owner_id = %(owner)s AND epoch = %(epoch)s
                """.encode(),
                {"run_id": lease.run_id, "owner": lease.owner_id, "epoch": lease.epoch,
                 "now": self.store.lease_now()},
            )
            conn.commit()


class LeaseKeeper:
    """Background heartbeat that keeps a lease alive during long model/tool calls.

    It only extends expiry for the same ``(owner, epoch)``; it can never take a
    lease over. If renewal fails, :attr:`lost` is set and the pre-dispatch and
    fenced-write checks stop this controller as usual.
    """

    def __init__(self, manager: LeaseManager, lease: Lease, interval_seconds: float) -> None:
        if interval_seconds <= 0:
            raise ValueError("interval_seconds must be positive")
        self.manager = manager
        self.lease = lease
        self.interval = interval_seconds
        self.lost = False
        self.renewals = 0
        self._stop = threading.Event()
        self._thread = threading.Thread(target=self._run, name=f"lease-{lease.run_id}",
                                        daemon=True)

    def _run(self) -> None:
        while not self._stop.wait(self.interval):
            try:
                self.lease = self.manager.renew(self.lease)
                self.renewals += 1
            except AlreadyOwnedError:
                self.lost = True
                trace("lease_lost", run_id=self.lease.run_id, owner_id=self.lease.owner_id,
                      epoch=self.lease.epoch)
                return
            except Exception as exc:  # transient DB error: retry on the next tick
                trace("lease_renew_error", run_id=self.lease.run_id, error=type(exc).__name__)

    def __enter__(self) -> LeaseKeeper:
        self._thread.start()
        return self

    def __exit__(self, *exc: object) -> None:
        self._stop.set()
        self._thread.join(timeout=self.interval + 5)
