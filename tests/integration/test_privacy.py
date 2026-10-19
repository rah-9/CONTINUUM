# SPDX-License-Identifier: LicenseRef-Continuum-Proprietary
# Copyright (c) 2026 the Continuum copyright holder. All rights reserved.
"""Erasure and retention (D-039): a person can remove a session completely; nothing else
can remove or change history; every erasure leaves a content-free record."""

import json
import os
import subprocess
import sys
from pathlib import Path

import psycopg
import pytest

from continuum.coding.service import CodingContinuity
from continuum.coding.transcript import Message
from continuum.core.errors import NotFoundError
from continuum.core.ids import RandomIds
from tests.conftest import DSN

SECRET_WORDS = "the customer is Acme Bank, account 4711"


def _git_repo(path: Path) -> Path:
    path.mkdir(parents=True)
    subprocess.run(["git", "init", "-q", str(path)], check=True)
    return path


@pytest.fixture
def coding(store, clock):
    return CodingContinuity(store, clock=clock, ids=RandomIds())


def _rows(store, sql: str) -> list[tuple]:
    with psycopg.connect(DSN) as conn:
        return conn.execute(sql.replace("{s}", f'"{store.schema}"').encode()).fetchall()


def test_forget_erases_everything_and_leaves_a_content_free_tombstone(coding, store, tmp_path):
    repo = _git_repo(tmp_path / "app")
    coding.capture(str(repo), [Message("user", SECRET_WORDS), Message("agent", "noted")],
                   source="claude-code")
    sid = coding.latest_active(str(repo))
    coding.note(sid, "decision", "store " + SECRET_WORDS, agent="m")
    keep = coding.capture(str(_git_repo(tmp_path / "other")),
                          [Message("user", "unrelated work")], source="cli")["session_id"]

    out = coding.forget(sid, actor="alice", reason="client asked to delete their data")
    assert out["events_erased"] >= 4
    with pytest.raises(NotFoundError):
        coding.brief(sid)
    dump = json.dumps(_rows(store, "SELECT * FROM {s}.coding_events"), default=str)
    assert "Acme" not in dump and sid not in dump
    assert coding.brief(keep)["session_id"] == keep            # other sessions untouched
    [tomb] = coding.erasures()
    assert tomb["session_id"] == sid and tomb["erased_by"] == "human:alice"
    assert tomb["events_erased"] == out["events_erased"]
    assert "Acme" not in json.dumps(tomb, default=str) and str(repo) not in json.dumps(tomb)


def test_history_still_cannot_be_changed_or_deleted_outside_an_erasure(coding, store, tmp_path):
    repo = _git_repo(tmp_path / "app")
    sid = coding.capture(str(repo), [Message("user", "hello world")],
                         source="cli")["session_id"]
    with psycopg.connect(DSN) as conn:
        for sql in (f'UPDATE "{store.schema}".coding_events SET actor = %s',
                    f'DELETE FROM "{store.schema}".coding_events WHERE session_id = %s'):
            with pytest.raises(psycopg.errors.RaiseException, match="append-only"):
                conn.execute(sql.encode(), ("x" if "UPDATE" in sql else sid,))
            conn.rollback()
        # the erasure switch is transaction-local: it cannot leak into a later statement
        conn.execute(b"SELECT set_config('continuum.erasure', 'on', true)")
        conn.commit()
        with pytest.raises(psycopg.errors.RaiseException):
            conn.execute(f'DELETE FROM "{store.schema}".coding_events'.encode())
        conn.rollback()
        with pytest.raises(psycopg.errors.RaiseException):     # updates: never
            conn.execute(b"SELECT set_config('continuum.erasure', 'on', true)")
            conn.execute(f'UPDATE "{store.schema}".coding_events SET actor = %s'.encode(),
                         ("x",))
        conn.rollback()
    coding.forget(sid, actor="op", reason="test")
    with psycopg.connect(DSN) as conn, pytest.raises(psycopg.errors.RaiseException):
        conn.execute(f'DELETE FROM "{store.schema}".coding_erasures'.encode())


def test_retention_erases_only_old_closed_sessions(coding, store, clock, tmp_path):
    old_closed = coding.capture(str(_git_repo(tmp_path / "a")), [Message("user", "old")],
                                source="cli")["session_id"]
    coding.close(old_closed, "done", agent="op", human=True)
    old_active = coding.capture(str(_git_repo(tmp_path / "b")), [Message("user", "old b")],
                                source="cli")["session_id"]
    clock.advance(100 * 86400)
    recent_closed = coding.capture(str(_git_repo(tmp_path / "c")), [Message("user", "new")],
                                   source="cli")["session_id"]
    coding.close(recent_closed, "done", agent="op", human=True)

    out = coding.forget_older_than(90, actor="ops-bot-owner", reason="90-day retention")
    assert out["sessions_erased"] == 1
    ids = {r[0] for r in _rows(store, "SELECT session_id FROM {s}.coding_sessions")}
    assert ids == {old_active, recent_closed}
    out = coding.forget_older_than(90, actor="owner", reason="incl. active",
                                   include_active=True)
    assert out["sessions_erased"] == 1
    assert len(coding.erasures()) == 2


def test_erasure_is_human_only_and_confirmed(schema, tmp_path):
    exe = str(Path(sys.executable).parent / "continuum")
    env = {**os.environ, "CONTINUUM_DATABASE_URL": DSN, "CONTINUUM_DB_SCHEMA": schema,
           "CONTINUUM_HOME": str(tmp_path / "home")}
    out = subprocess.run([exe, "coding", "forget", "cs_x", "--actor", "a", "--reason", "r"],
                         env=env, capture_output=True, text=True)
    assert out.returncode == 2 and "confirmation_required" in out.stdout
    from continuum.interfaces import mcp_facade

    assert not any("forget" in name or "erase" in name for name in dir(mcp_facade.McpFacade))
