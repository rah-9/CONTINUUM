"""Database schema: invariants enforced by PostgreSQL itself (defense in depth)."""

import re

import psycopg
import pytest

from continuum.core.state import (
    ActionStatus,
    ArtifactState,
    AttemptStatus,
    MilestoneStatus,
    RunStatus,
)
from continuum.core.store import Store


def _exec(store, sql, params=()):
    with store.connection() as conn:
        try:
            conn.execute(sql.encode(), params)
            conn.commit()
        except Exception:
            conn.rollback()
            raise


def _run(lab):
    result = lab.runtime.start(lab.definition.spec)
    return result.run_id


def test_ensure_schema_migrates_only_when_needed(schema, clock):
    from continuum.core.ids import SequentialIds
    from continuum.core.store import Store
    from tests.conftest import DSN

    fresh = Store(DSN, schema=schema + "_fresh", clock=clock, ids=SequentialIds("f"))
    try:
        assert fresh.ensure_schema() is True   # missing -> created
        assert fresh.ensure_schema() is False  # current -> untouched
    finally:
        fresh.close()
        with psycopg.connect(DSN, autocommit=True) as conn:
            conn.execute(f'DROP SCHEMA IF EXISTS "{schema}_fresh" CASCADE'.encode())


def test_migrate_is_idempotent_and_versioned(store):
    store.migrate()
    store.migrate()
    with store.connection() as conn:
        row = conn.execute(b"SELECT value FROM schema_meta WHERE key = 'schema_version'"
                           ).fetchone()
    assert row["value"] == Store.SCHEMA_VERSION


@pytest.mark.parametrize(("constraint", "enum"), [
    ("runs_status_valid", RunStatus), ("milestones_status_valid", MilestoneStatus),
    ("actions_status_valid", ActionStatus), ("attempts_status_valid", AttemptStatus),
    ("artifacts_state_valid", ArtifactState)])
def test_status_checks_match_python_enums(store, constraint, enum):
    with store.connection() as conn:
        row = conn.execute(
            b"SELECT pg_get_constraintdef(c.oid) AS def FROM pg_constraint c "
            b"JOIN pg_namespace n ON n.oid = c.connamespace "
            b"WHERE c.conname = %s AND n.nspname = %s", (constraint, store.schema)).fetchone()
    allowed = set(re.findall(r"'([a-z_]+)'::text", row["def"]))
    assert allowed == {e.value for e in enum}


@pytest.mark.parametrize("statement", [
    "UPDATE runs SET status = 'exploded' WHERE run_id = %s",
    "UPDATE runs SET finished_at = NULL WHERE run_id = %s",
    "UPDATE milestones SET evidence_ref = NULL WHERE run_id = %s AND status = 'verified'",
    "UPDATE actions SET outcome_ref = NULL WHERE run_id = %s AND status = 'confirmed'",
    "UPDATE actions SET args_sha256 = 'md5:x' WHERE run_id = %s",
    "UPDATE action_attempts SET finished_at = NULL WHERE action_id IN "
    "(SELECT action_id FROM actions WHERE run_id = %s)",
    "UPDATE usage_totals SET model_calls = -1 WHERE run_id = %s",
])
def test_database_rejects_invariant_violations(make_lab, statement):
    lab = make_lab("inventory_upload_v1")
    run_id = _run(lab)
    with pytest.raises(psycopg.errors.CheckViolation):
        _exec(lab.store, statement, (run_id,))


def test_event_log_cannot_be_truncated(make_lab):
    lab = make_lab("db_update_selected")
    _run(lab)
    with pytest.raises(psycopg.errors.RaiseException):
        _exec(lab.store, "TRUNCATE events CASCADE")


def test_lookup_indexes_exist(store):
    with store.connection() as conn:
        names = {r["indexname"] for r in conn.execute(
            b"SELECT indexname FROM pg_indexes WHERE schemaname = %s", (store.schema,))}
    assert {"idx_actions_run_status", "idx_model_sessions_run", "idx_artifacts_run",
            "idx_artifacts_action", "idx_attempts_started"} <= names
