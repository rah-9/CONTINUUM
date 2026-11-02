# SPDX-License-Identifier: LicenseRef-Continuum-Proprietary
# Copyright (c) 2026 the Continuum copyright holder. All rights reserved.
"""A real 0.9 database (schema v4, with sessions, chat and checks) upgrades in place to
1.0 (v5): nothing is lost, the owner keeps everything, and the new features work on it."""

import subprocess
import uuid
from pathlib import Path

import psycopg
import pytest

from continuum.coding.service import CodingContinuity
from continuum.coding.transcript import Message
from continuum.core.clock import SystemClock
from continuum.core.ids import RandomIds
from continuum.core.store import Store
from tests.conftest import DSN

OLD_DDL = Path(__file__).resolve().parents[1] / "fixtures" / "schema_v4_continuum_0_9.sql"


@pytest.fixture
def old_schema():
    name = f"v4_{uuid.uuid4().hex[:10]}"
    with psycopg.connect(DSN, autocommit=True,
                         options=f"-c search_path={name}") as conn:
        conn.execute(f'CREATE SCHEMA "{name}"'.encode())
        conn.execute(OLD_DDL.read_bytes())
    yield name
    with psycopg.connect(DSN, autocommit=True) as conn:
        conn.execute(f'DROP SCHEMA IF EXISTS "{name}" CASCADE'.encode())


def test_a_0_9_database_upgrades_to_1_0_without_losing_anything(store, old_schema, tmp_path):
    repo = tmp_path / "app"
    subprocess.run(["git", "init", "-q", str(repo)], check=True)
    # Real 1.0 rows, written into the 0.9 tables column by column (0.9 had no owner_role).
    new = CodingContinuity(store, clock=SystemClock(), ids=RandomIds())
    sid = new.capture(str(repo), [Message("user", "Totals must round half-even."),
                                  Message("agent", "I chose Decimal for money.")],
                      source="claude-code", chat_id="c1")["session_id"]
    new.register_check(str(repo), "tests", ["pytest", "-q"], actor="alice")
    with psycopg.connect(DSN, autocommit=True) as conn:
        for table in ("coding_sessions", "coding_events", "coding_checks"):
            cols = [r[0] for r in conn.execute(
                b"SELECT column_name FROM information_schema.columns WHERE table_schema = %s "
                b"AND table_name = %s AND column_name NOT IN ('owner_role', 'search') "
                b"ORDER BY ordinal_position",
                (store.schema, table)).fetchall()]
            names = ", ".join(cols)
            conn.execute(f'INSERT INTO "{old_schema}".{table} ({names}) '
                         f'SELECT {names} FROM "{store.schema}".{table}'.encode())

    old = Store(DSN, schema=old_schema, clock=SystemClock(), ids=RandomIds())
    try:
        assert old.ensure_schema() is True          # v4 -> v5, once
        assert old.ensure_schema() is False
        coding = CodingContinuity(old, clock=SystemClock(), ids=RandomIds())
        assert coding.latest_active(str(repo)) == sid  # the migrating role owns old rows
        md = coding.brief(sid)["markdown"]
        assert "round half-even" in md and "Decimal" in md
        assert [c["argv"] for c in coding.checks(str(repo))] == [["pytest", "-q"]]
        coding.register_check(str(repo), "tests", ["pytest", "-x"], actor="alice")  # new key
        assert [c["argv"] for c in coding.checks(str(repo))] == [["pytest", "-x"]]
        coding.capture(str(repo), [Message("user", "And add a CSV export.")],
                       source="claude-code", chat_id="c1")
        assert "CSV export" in coding.brief(sid)["markdown"]
        assert coding.forget(sid, actor="op", reason="upgrade test")["events_erased"] >= 4
        with old.connection() as conn:
            version = conn.execute(b"SELECT value FROM schema_meta "
                                   b"WHERE key = 'schema_version'").fetchone()["value"]
            rls = conn.execute(b"SELECT relrowsecurity FROM pg_class "
                               b"WHERE oid = 'coding_sessions'::regclass").fetchone()
            conn.rollback()
        assert version == Store.SCHEMA_VERSION and rls["relrowsecurity"] is True
    finally:
        old.close()


def test_a_database_with_tool_calls_upgrades_again(store, tmp_path, monkeypatch):
    """Upgrading a database that already holds tool calls (rc2+) re-ran an old step that
    re-added the v4 list of event types and failed on those rows: found by upgrading a
    real session's database from v7 to v8."""
    monkeypatch.setenv("CONTINUUM_HOME", str(tmp_path / "home"))
    repo = tmp_path / "app"
    repo.mkdir()
    subprocess.run(["git", "init", "-q", str(repo)], check=True)
    coding = CodingContinuity(store, clock=SystemClock(), ids=RandomIds())
    coding.capture(str(repo), [Message("user", "Fix the export.")], source="claude-code")
    coding.record_activity(str(repo), [
        {"v": 1, "app": "claude-code", "chat": "c", "phase": "start", "call_key": "call:1",
         "tool": "Bash", "kind": "shell", "effect": "write", "input": {"command": "ls"}}],
        source="claude-code")
    with psycopg.connect(DSN, autocommit=True,
                         options=f"-c search_path={store.schema}") as conn:
        conn.execute(b"UPDATE schema_meta SET value = '7' WHERE key = 'schema_version'")
    assert store.ensure_schema() is True                  # v7 -> current, with tool calls
    with psycopg.connect(DSN, options=f"-c search_path={store.schema}") as conn:
        version = conn.execute(b"SELECT value FROM schema_meta WHERE key = "
                               b"'schema_version'").fetchone()[0]
    assert version == Store.SCHEMA_VERSION
