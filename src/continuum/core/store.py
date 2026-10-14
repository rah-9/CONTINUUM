"""PostgreSQL event store with transactional projections (docs/03 section 4).

Rules enforced here:

* One database transaction appends each event **and** applies its projection
  row changes (R2, invariant I1). The run row is locked ``FOR NO KEY UPDATE``
  so ``(run_id, seq)`` is assigned gaplessly under concurrency.
* Every append after ``RunCreated`` is fenced: the caller's ``(owner_id,
  epoch)`` must equal the active, unexpired lease, checked inside the same
  transaction with the lease row share-locked (R3, I6).
* No provider or tool I/O ever happens inside :meth:`Store.run_tx`.
"""

from __future__ import annotations

from collections.abc import Iterator, Sequence
from contextlib import contextmanager
from dataclasses import dataclass
from datetime import datetime
from decimal import Decimal
from importlib import resources
from typing import Any

import psycopg
from psycopg import sql
from psycopg.rows import dict_row
from psycopg.types.json import Jsonb
from psycopg_pool import ConnectionPool
from pydantic import BaseModel, ConfigDict

from continuum.core.clock import Clock
from continuum.core.errors import ContinuumError, NotFoundError, StaleOwnerError
from continuum.core.events import EVENT_SCHEMA_VERSION, Event, EventType, NewEvent, parse_payload
from continuum.core.faults import FaultInjector, NoFaults
from continuum.core.ids import IdGenerator
from continuum.core.projections import PROJECTION_TABLES, ProjectionState, RowChange, project
from continuum.core.state import ActionStatus, MilestoneStatus, RunStatus, TaskSpec
from continuum.core.tracing import trace

JSON_COLUMNS = frozenset({"spec_json", "profile_json", "args_json", "payload_json"})
# Fractional seconds, never rounded: an integer cast rounds to nearest and could make a
# lease expire up to a second early (a 1s TTL could last ~0.1s).
LEASE_NOW_SQL = ("COALESCE(%(now)s::double precision, "
                 "extract(epoch from clock_timestamp())::double precision)")


@dataclass(frozen=True)
class Fence:
    """The writer's claimed controller identity."""

    owner_id: str
    epoch: int


# ---- typed row views -------------------------------------------------------------


class _Row(BaseModel):
    model_config = ConfigDict(frozen=True, extra="ignore")


class RunRow(_Row):
    run_id: str
    task_id: str
    spec_json: dict[str, Any]
    status: RunStatus
    active_session_id: str | None
    last_event_seq: int
    created_at: datetime
    updated_at: datetime
    finished_at: datetime | None
    failure_code: str | None

    @property
    def spec(self) -> TaskSpec:
        return TaskSpec.model_validate(self.spec_json)


class MilestoneRow(_Row):
    run_id: str
    milestone_id: str
    status: MilestoneStatus
    verifier_id: str
    verifier_version: str
    input_fingerprint: str | None
    evidence_ref: str | None
    verified_at: datetime | None


class ActionRow(_Row):
    action_id: str
    run_id: str
    milestone_id: str | None
    operation_key: str
    tool_name: str
    manifest_version: str
    args_json: dict[str, Any]
    args_sha256: str
    idempotency_key: str | None
    status: ActionStatus
    created_at: datetime
    updated_at: datetime
    outcome_ref: str | None
    reconciliation_ref: str | None


class AttemptRow(_Row):
    action_id: str
    attempt_no: int
    status: str
    dispatch_owner_epoch: int
    started_at: datetime
    finished_at: datetime | None
    result_ref: str | None
    error_code: str | None


class ArtifactRow(_Row):
    artifact_id: str
    run_id: str
    relative_path: str
    sha256: str
    byte_size: int
    producing_action_id: str | None
    created_at: datetime
    state: str


class UsageRow(_Row):
    run_id: str
    model_calls: int
    tokens_reported: int
    cost_known_usd: Decimal
    has_unpriced_usage: bool
    recoveries: int
    active_elapsed_ms: int


class LeaseRow(_Row):
    run_id: str
    owner_id: str
    epoch: int
    expires_at_unix: float


class ApprovalRow(_Row):
    action_id: str
    decision: str | None
    actor_id: str | None
    decided_at: datetime | None
    reason: str | None


# ---- queries -----------------------------------------------------------------------


class Queries:
    """Read helpers bound to one connection (inside or outside a transaction)."""

    def __init__(self, conn: psycopg.Connection[dict[str, Any]]) -> None:
        self.conn = conn

    def _one(self, query: str, params: Sequence[Any]) -> dict[str, Any] | None:
        with self.conn.cursor() as cur:
            cur.execute(query, params)
            return cur.fetchone()

    def _all(self, query: str, params: Sequence[Any]) -> list[dict[str, Any]]:
        with self.conn.cursor() as cur:
            cur.execute(query, params)
            return cur.fetchall()

    def run(self, run_id: str) -> RunRow:
        row = self._one("SELECT * FROM runs WHERE run_id = %s", (run_id,))
        if row is None:
            raise NotFoundError(f"run {run_id} not found")
        return RunRow.model_validate(row)

    def run_exists(self, run_id: str) -> bool:
        return self._one("SELECT 1 AS x FROM runs WHERE run_id = %s", (run_id,)) is not None

    def milestones(self, run_id: str) -> list[MilestoneRow]:
        rows = self._all("SELECT * FROM milestones WHERE run_id = %s ORDER BY milestone_id",
                         (run_id,))
        return [MilestoneRow.model_validate(r) for r in rows]

    def milestone_statuses(self, run_id: str) -> dict[str, MilestoneStatus]:
        return {m.milestone_id: m.status for m in self.milestones(run_id)}

    def action(self, action_id: str) -> ActionRow:
        row = self._one("SELECT * FROM actions WHERE action_id = %s", (action_id,))
        if row is None:
            raise NotFoundError(f"action {action_id} not found")
        return ActionRow.model_validate(row)

    def action_by_key(self, run_id: str, operation_key: str) -> ActionRow | None:
        row = self._one("SELECT * FROM actions WHERE run_id = %s AND operation_key = %s",
                        (run_id, operation_key))
        return ActionRow.model_validate(row) if row else None

    def actions(self, run_id: str, statuses: Sequence[str] | None = None) -> list[ActionRow]:
        if statuses is None:
            rows = self._all("SELECT * FROM actions WHERE run_id = %s ORDER BY created_at, "
                             "action_id", (run_id,))
        else:
            rows = self._all("SELECT * FROM actions WHERE run_id = %s AND status = ANY(%s) "
                             "ORDER BY created_at, action_id", (run_id, list(statuses)))
        return [ActionRow.model_validate(r) for r in rows]

    def attempts(self, action_id: str) -> list[AttemptRow]:
        rows = self._all("SELECT * FROM action_attempts WHERE action_id = %s ORDER BY attempt_no",
                         (action_id,))
        return [AttemptRow.model_validate(r) for r in rows]

    def started_attempts(self, run_id: str) -> list[AttemptRow]:
        rows = self._all(
            "SELECT t.* FROM action_attempts t JOIN actions a USING (action_id) "
            "WHERE a.run_id = %s AND t.status = 'started' ORDER BY t.started_at, t.action_id",
            (run_id,))
        return [AttemptRow.model_validate(r) for r in rows]

    def artifacts(self, run_id: str) -> list[ArtifactRow]:
        rows = self._all("SELECT * FROM artifacts WHERE run_id = %s ORDER BY created_at, "
                         "artifact_id", (run_id,))
        return [ArtifactRow.model_validate(r) for r in rows]

    def approval(self, action_id: str) -> ApprovalRow | None:
        row = self._one("SELECT * FROM approvals WHERE action_id = %s", (action_id,))
        return ApprovalRow.model_validate(row) if row else None

    def usage(self, run_id: str) -> UsageRow:
        row = self._one("SELECT * FROM usage_totals WHERE run_id = %s", (run_id,))
        if row is None:
            raise NotFoundError(f"usage for run {run_id} not found")
        return UsageRow.model_validate(row)

    def lease(self, run_id: str) -> LeaseRow | None:
        row = self._one("SELECT * FROM run_leases WHERE run_id = %s", (run_id,))
        return LeaseRow.model_validate(row) if row else None

    def events(self, run_id: str, after_seq: int = 0) -> list[Event]:
        rows = self._all("SELECT * FROM events WHERE run_id = %s AND seq > %s ORDER BY seq",
                         (run_id, after_seq))
        return [
            Event(run_id=r["run_id"], seq=r["seq"], event_id=r["event_id"],
                  schema_version=r["schema_version"], type=EventType(r["type"]),
                  occurred_at=r["occurred_at"], actor=r["actor"], payload=r["payload_json"])
            for r in rows
        ]

    def run_ids(self) -> list[str]:
        return [r["run_id"] for r in self._all("SELECT run_id FROM runs ORDER BY created_at", ())]

    def projection_rows(self, run_id: str) -> ProjectionState:
        """Durable projection rows for one run in :class:`ProjectionState` form."""
        state = ProjectionState()
        selects = {
            "runs": "SELECT * FROM runs WHERE run_id = %s",
            "model_sessions": "SELECT * FROM model_sessions WHERE run_id = %s",
            "milestones": "SELECT * FROM milestones WHERE run_id = %s",
            "actions": "SELECT * FROM actions WHERE run_id = %s",
            "action_attempts": "SELECT t.* FROM action_attempts t JOIN actions a "
                               "USING (action_id) WHERE a.run_id = %s",
            "artifacts": "SELECT * FROM artifacts WHERE run_id = %s",
            "approvals": "SELECT p.* FROM approvals p JOIN actions a USING (action_id) "
                         "WHERE a.run_id = %s",
            "usage_totals": "SELECT * FROM usage_totals WHERE run_id = %s",
        }
        for table, query in selects.items():
            for row in self._all(query, (run_id,)):
                key = tuple(row[c] for c in PROJECTION_TABLES[table])
                state.tables[table][key] = dict(row)
        return state


# ---- transactions --------------------------------------------------------------------


class RunTx(Queries):
    """A fenced write transaction on one run. Use via :meth:`Store.run_tx`."""

    def __init__(self, conn: psycopg.Connection[dict[str, Any]], store: Store, run_id: str,
                 next_seq: int) -> None:
        super().__init__(conn)
        self._store = store
        self.run_id = run_id
        self._next_seq = next_seq
        self.appended: list[Event] = []

    def append(self, event: NewEvent) -> Event:
        parse_payload(event.type, event.payload)  # validate against the typed contract
        seq = self._next_seq
        occurred_at = self._store.clock.now()
        committed = Event(run_id=self.run_id, seq=seq, event_id=self._store.ids.new("evt"),
                          schema_version=EVENT_SCHEMA_VERSION, type=event.type,
                          occurred_at=occurred_at, actor=event.actor, payload=event.payload)
        changes = project(run_id=self.run_id, seq=seq, event_type=event.type,
                          occurred_at=occurred_at, payload=event.payload)
        # RunCreated inserts the runs row, which the event row references.
        for change in changes:
            _apply_sql(self.conn, change)
        with self.conn.cursor() as cur:
            cur.execute(
                "INSERT INTO events (run_id, seq, event_id, schema_version, type, occurred_at,"
                " actor, payload_json) VALUES (%s, %s, %s, %s, %s, %s, %s, %s)",
                (self.run_id, seq, committed.event_id, committed.schema_version,
                 committed.type.value, occurred_at, committed.actor, Jsonb(committed.payload)))
        self._next_seq += 1
        self.appended.append(committed)
        return committed

    def append_all(self, events: Sequence[NewEvent]) -> list[Event]:
        return [self.append(e) for e in events]


def _apply_sql(conn: psycopg.Connection[dict[str, Any]], change: RowChange) -> None:
    def adapt(column: str, value: Any) -> Any:
        return Jsonb(value) if column in JSON_COLUMNS and value is not None else value

    table = sql.Identifier(change.table)
    with conn.cursor() as cur:
        if change.op == "insert":
            row = {**change.key, **change.values}
            cols = list(row)
            cur.execute(
                sql.SQL("INSERT INTO {} ({}) VALUES ({})").format(
                    table,
                    sql.SQL(", ").join(sql.Identifier(c) for c in cols),
                    sql.SQL(", ").join(sql.Placeholder() for _ in cols)),
                [adapt(c, row[c]) for c in cols])
            return
        sets = [sql.SQL("{} = %s").format(sql.Identifier(c)) for c in change.values]
        sets += [sql.SQL("{0} = {0} + %s").format(sql.Identifier(c)) for c in change.increments]
        where = sql.SQL(" AND ").join(
            sql.SQL("{} = %s").format(sql.Identifier(c)) for c in change.key)
        params = [adapt(c, v) for c, v in change.values.items()]
        params += list(change.increments.values()) + list(change.key.values())
        cur.execute(sql.SQL("UPDATE {} SET {} WHERE {}").format(
            table, sql.SQL(", ").join(sets), where), params)
        if cur.rowcount != 1:
            raise RuntimeError(f"projection update matched {cur.rowcount} rows: {change}")


class Store:
    """Connection pool plus fenced, atomic run transactions."""

    def __init__(self, dsn: str, *, schema: str = "public", clock: Clock, ids: IdGenerator,
                 faults: FaultInjector | None = None, lock_timeout_ms: int = 5000,
                 pool_size: int = 8) -> None:
        self.dsn = dsn
        self.schema = schema
        self.clock = clock
        self.ids = ids
        self.faults: FaultInjector = faults or NoFaults()
        options = f"-c search_path={schema} -c lock_timeout={lock_timeout_ms}"
        self.pool: ConnectionPool[psycopg.Connection[dict[str, Any]]] = ConnectionPool(
            dsn, min_size=1, max_size=pool_size, open=True,
            kwargs={"row_factory": dict_row, "options": options, "autocommit": False})

    def close(self) -> None:
        self.pool.close()

    SCHEMA_VERSION = "3"

    def ensure_schema(self) -> bool:
        """Migrate only if the schema is missing or older than this code. True if it ran.

        A database written by a *newer* Continuum is never downgraded: that is a typed
        ``schema_too_new`` error telling the operator to upgrade this installation."""
        try:
            with self.pool.connection() as conn:
                row = conn.execute(
                    b"SELECT value FROM schema_meta WHERE key = 'schema_version'").fetchone()
                conn.rollback()
        except psycopg.errors.UndefinedTable:
            row = None
        if row is not None:
            stored, ours = int(row["value"]), int(self.SCHEMA_VERSION)
            if stored == ours:
                return False
            if stored > ours:
                raise ContinuumError(
                    f"database schema v{stored} is newer than this Continuum (v{ours}); "
                    "upgrade continuum", code="schema_too_new")
        self.migrate()
        return True

    def migrate(self) -> None:
        """Idempotent DDL, serialized across processes by an advisory lock so concurrent
        first-use starts (an MCP server and a CLI command) cannot race on CREATE."""
        ddl = resources.files("continuum.core").joinpath("schema.sql").read_text()
        with self.pool.connection() as conn:
            try:
                conn.execute(b"SELECT pg_advisory_xact_lock(hashtext('continuum:migrate'))")
                conn.execute(sql.SQL("CREATE SCHEMA IF NOT EXISTS {}").format(
                    sql.Identifier(self.schema)))
                conn.execute(ddl.encode())
                conn.commit()
            except BaseException:
                conn.rollback()
                raise

    @contextmanager
    def read(self) -> Iterator[Queries]:
        with self.pool.connection() as conn:
            yield Queries(conn)
            conn.rollback()

    @contextmanager
    def connection(self) -> Iterator[psycopg.Connection[dict[str, Any]]]:
        with self.pool.connection() as conn:
            yield conn

    def lease_now(self) -> float | None:
        return self.clock.lease_time()

    @contextmanager
    def run_tx(self, run_id: str, fence: Fence | None, *, create: bool = False
               ) -> Iterator[RunTx]:
        """Open a fenced transaction. Commits on clean exit, rolls back otherwise.

        ``create=True`` is used only for ``RunCreated``: the run must not exist
        and no fence applies yet (the lease is acquired immediately after).
        """
        with self.pool.connection() as conn:
            try:
                if create:
                    tx = RunTx(conn, self, run_id, next_seq=1)
                else:
                    row = Queries(conn)._one(
                        "SELECT last_event_seq FROM runs WHERE run_id = %s FOR NO KEY UPDATE",
                        (run_id,))
                    if row is None:
                        raise NotFoundError(f"run {run_id} not found")
                    if fence is None:
                        raise StaleOwnerError("writes to an existing run require a lease fence")
                    self._check_fence(conn, run_id, fence)
                    tx = RunTx(conn, self, run_id, next_seq=row["last_event_seq"] + 1)
                yield tx
                self.faults.checkpoint("before_event_commit", run_id=run_id,
                                       event_types=[e.type.value for e in tx.appended])
                conn.commit()
            except BaseException:
                conn.rollback()
                raise
        for e in tx.appended:
            trace("event_committed", run_id=run_id, seq=e.seq, type=e.type.value,
                  actor=e.actor)
        self.faults.checkpoint("after_event_commit", run_id=run_id,
                               event_types=[e.type.value for e in tx.appended])

    def _check_fence(self, conn: psycopg.Connection[dict[str, Any]], run_id: str,
                     fence: Fence) -> None:
        with conn.cursor() as cur:
            cur.execute(
                f"SELECT owner_id, epoch, expires_at_unix > {LEASE_NOW_SQL} AS active "
                "FROM run_leases WHERE run_id = %(run_id)s FOR SHARE",
                {"run_id": run_id, "now": self.lease_now()})
            lease = cur.fetchone()
        if lease is None or not lease["active"]:
            raise StaleOwnerError(f"no active lease for run {run_id}")
        if lease["owner_id"] != fence.owner_id or lease["epoch"] != fence.epoch:
            raise StaleOwnerError(
                f"stale owner {fence.owner_id}@{fence.epoch}; active is "
                f"{lease['owner_id']}@{lease['epoch']}")

    def append(self, run_id: str, fence: Fence, events: Sequence[NewEvent]) -> list[Event]:
        with self.run_tx(run_id, fence) as tx:
            return tx.append_all(events)

    # ---- rebuild / verification (I2) -------------------------------------------------

    def replay_projection(self, run_id: str) -> ProjectionState:
        """Replay committed events into a fresh in-memory projection. No I/O besides reads."""
        with self.read() as q:
            events = q.events(run_id)
        return replay_events(events)

    def projection_diff(self, run_id: str) -> list[str]:
        """Differences between durable projections and a replay of the event log."""
        replayed = self.replay_projection(run_id)
        with self.read() as q:
            durable = q.projection_rows(run_id)
        return diff_projections(replayed, durable)

    def rebuild_projection(self, run_id: str, fence: Fence) -> None:
        """Replace this run's projection rows with a replay of its events, atomically."""
        replayed = self.replay_projection(run_id)
        with self.run_tx(run_id, fence) as tx:
            conn = tx.conn
            for stmt in (
                "DELETE FROM approvals WHERE action_id IN "
                "(SELECT action_id FROM actions WHERE run_id = %s)",
                "DELETE FROM action_attempts WHERE action_id IN "
                "(SELECT action_id FROM actions WHERE run_id = %s)",
                "DELETE FROM artifacts WHERE run_id = %s",
                "DELETE FROM actions WHERE run_id = %s",
                "DELETE FROM milestones WHERE run_id = %s",
                "DELETE FROM model_sessions WHERE run_id = %s",
                "DELETE FROM usage_totals WHERE run_id = %s",
            ):
                conn.execute(stmt.encode(), (run_id,))
            run_row = next(iter(replayed.tables["runs"].values()))
            _apply_sql(conn, RowChange("runs", {"run_id": run_id}, "update",
                                       {k: v for k, v in run_row.items() if k != "run_id"}))
            for table in ("milestones", "usage_totals", "model_sessions", "actions",
                          "action_attempts", "artifacts", "approvals"):
                key_cols = PROJECTION_TABLES[table]
                for row in replayed.tables[table].values():
                    key = {c: row[c] for c in key_cols}
                    values = {c: v for c, v in row.items() if c not in key_cols}
                    _apply_sql(conn, RowChange(table, key, "insert", values))


def replay_events(events: Sequence[Event]) -> ProjectionState:
    state = ProjectionState()
    for expected_seq, event in enumerate(events, start=1):
        if event.seq != expected_seq:
            raise ValueError(f"event sequence gap: expected {expected_seq}, got {event.seq}")
        for change in project(run_id=event.run_id, seq=event.seq, event_type=event.type,
                              occurred_at=event.occurred_at, payload=event.payload):
            state.apply(change)
    return state


def diff_projections(expected: ProjectionState, actual: ProjectionState) -> list[str]:
    diffs: list[str] = []
    for table in PROJECTION_TABLES:
        exp, act = expected.tables[table], actual.tables[table]
        for key in sorted(set(exp) | set(act), key=repr):
            if key not in act:
                diffs.append(f"{table}{key}: missing durable row")
            elif key not in exp:
                diffs.append(f"{table}{key}: durable row not produced by events")
            else:
                for col in sorted(set(exp[key]) | set(act[key])):
                    if exp[key].get(col) != act[key].get(col):
                        diffs.append(f"{table}{key}.{col}: replay={exp[key].get(col)!r} "
                                     f"durable={act[key].get(col)!r}")
    return diffs
