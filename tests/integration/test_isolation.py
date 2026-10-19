# SPDX-License-Identifier: LicenseRef-Continuum-Proprietary
# Copyright (c) 2026 the Continuum copyright holder. All rights reserved.
"""Several people, one database (D-040): each PostgreSQL role sees, changes and erases only
its own coding memory, enforced by row-level security, not by the client's good manners."""

import os
import subprocess
import sys
import uuid
from collections.abc import Iterator
from pathlib import Path

import psycopg
import pytest
from psycopg.conninfo import make_conninfo

from continuum.coding import access
from continuum.coding.service import CodingContinuity
from continuum.coding.transcript import Message
from continuum.core.errors import ContinuumError, NotFoundError
from continuum.core.ids import RandomIds
from continuum.core.store import Store
from tests.conftest import DSN


def _git_repo(path: Path) -> Path:
    path.mkdir(parents=True)
    subprocess.run(["git", "init", "-q", str(path)], check=True)
    return path


def _raw(people: "People", name: str) -> psycopg.Connection:
    return psycopg.connect(people.dsns[name],
                           options=f"-c search_path={people.op.store.schema}")


class People:
    def __init__(self, op: CodingContinuity, alice: CodingContinuity, bob: CodingContinuity,
                 roles: dict[str, str], dsns: dict[str, str]) -> None:
        self.op, self.alice, self.bob, self.roles, self.dsns = op, alice, bob, roles, dsns


@pytest.fixture
def people(store: Store, schema: str, clock) -> Iterator[People]:
    with psycopg.connect(DSN, autocommit=True) as conn:
        if not conn.execute(b"SELECT rolsuper OR rolcreaterole FROM pg_roles "
                            b"WHERE rolname = current_user").fetchone()[0]:
            pytest.skip("the test database user cannot create roles")
        tag = uuid.uuid4().hex[:8]
        roles = {"alice": f"alice_{tag}", "bob": f"bob_{tag}", "eve": f"eve_{tag}"}
        for r in roles.values():
            conn.execute(f"CREATE ROLE \"{r}\" LOGIN PASSWORD 'pw-{tag}'".encode())
    for name in ("alice", "bob"):
        access.grant(store, roles[name])
    dsns = {n: make_conninfo(DSN, user=r, password=f"pw-{tag}") for n, r in roles.items()}
    stores = {n: Store(dsns[n], schema=schema, clock=clock, ids=RandomIds())
              for n in ("alice", "bob")}

    def svc(s: Store) -> CodingContinuity:
        return CodingContinuity(s, clock=clock, ids=RandomIds())

    try:
        yield People(svc(store), svc(stores["alice"]), svc(stores["bob"]), roles, dsns)
    finally:
        for s in stores.values():
            s.close()
        with psycopg.connect(DSN, autocommit=True) as conn:
            for r in roles.values():
                conn.execute(f'DROP OWNED BY "{r}"'.encode())
                conn.execute(f'DROP ROLE "{r}"'.encode())


def test_each_person_sees_only_their_own_memory_in_the_same_repository(people, tmp_path):
    repo = str(_git_repo(tmp_path / "shared-checkout"))
    a = people.alice.capture(repo, [Message("user", "alice: the API must use OAuth only")],
                             source="cli")["session_id"]
    with pytest.raises(NotFoundError):              # same repo path, but not bob's session
        people.bob.latest_active(repo)
    b = people.bob.capture(repo, [Message("user", "bob: we migrate the billing tables")],
                           source="cli")["session_id"]
    assert a != b
    assert people.alice.latest_active(repo) == a and people.bob.latest_active(repo) == b
    assert "OAuth" in people.alice.brief(a)["markdown"]
    assert "OAuth" not in people.bob.brief(b)["markdown"]
    with pytest.raises(NotFoundError):
        people.bob.brief(a)
    assert people.bob.recall("OAuth", repo_path=repo, all_sessions=True)["total_matches"] == 0
    assert [s["session_id"] for s in people.bob.sessions()] == [b]
    with _raw(people, "bob") as conn:                    # raw SQL is narrowed too
        events = conn.execute(b"SELECT DISTINCT session_id FROM coding_events").fetchall()
        assert events == [(b,)]


def test_nobody_can_write_into_or_take_over_someone_elses_session(people, tmp_path):
    repo = str(_git_repo(tmp_path / "app"))
    a = people.alice.capture(repo, [Message("user", "alice's plan")], source="cli")["session_id"]
    b = people.bob.capture(repo, [Message("user", "bob's plan")], source="cli")["session_id"]
    with pytest.raises(NotFoundError):
        people.bob.note(a, "decision", "drop alice's tables", agent="m")
    with pytest.raises(NotFoundError):
        people.bob.close(a, "hijack", agent="m", human=True)
    with _raw(people, "bob") as conn:
        attempts = [
            (b"INSERT INTO coding_events (session_id, seq, type, actor, payload_json, "
             b"occurred_at) VALUES (%s, 99, 'NoteRecorded', 'x', '{}', now())", (a,)),
            (b"INSERT INTO coding_sessions (session_id, repo_root, goal, status, last_seq, "
             b"created_at, updated_at, owner_role) VALUES ('cs_fake', '/x', 'g', 'active', "
             b"0, now(), now(), %s)", (people.roles["alice"],)),
            (b"UPDATE coding_sessions SET owner_role = %s WHERE session_id = %s",
             (people.roles["alice"], b)),
        ]
        for statement, params in attempts:
            with pytest.raises(psycopg.errors.InsufficientPrivilege):
                conn.execute(statement, params)
            conn.rollback()
        # an UPDATE aimed at alice's row matches nothing: it is invisible
        assert conn.execute(b"UPDATE coding_sessions SET goal = 'x' WHERE session_id = %s",
                            (a,)).rowcount == 0
        conn.rollback()
        with pytest.raises(psycopg.errors.InsufficientPrivilege):   # runtime: operator's
            conn.execute(b"SELECT * FROM runs")
    assert "alice's plan" in people.alice.brief(a)["markdown"]


def test_checks_are_per_person(people, tmp_path):
    repo = str(_git_repo(tmp_path / "app"))
    people.alice.register_check(repo, "tests", ["pytest", "-q"], actor="alice")
    people.bob.register_check(repo, "tests", ["make", "test"], actor="bob")
    assert [c["argv"] for c in people.alice.checks(repo)] == [["pytest", "-q"]]
    assert [c["argv"] for c in people.bob.checks(repo)] == [["make", "test"]]
    assert people.op.checks(repo) == []                # the operator's own: none


def test_erasure_is_per_person_and_the_operator_administers_everyone(people, clock, tmp_path):
    repo = str(_git_repo(tmp_path / "app"))
    a = people.alice.capture(repo, [Message("user", "alice")], source="cli")["session_id"]
    b = people.bob.capture(repo, [Message("user", "bob")], source="cli")["session_id"]
    with pytest.raises(NotFoundError):
        people.bob.forget(a, actor="bob", reason="not mine")
    people.bob.forget(b, actor="bob", reason="mine")
    assert [t["session_id"] for t in people.bob.erasures()] == [b]
    assert people.alice.erasures() == []
    # the operator sees every tombstone, and the erased person keeps theirs
    people.op.forget(a, actor="operator", reason="client request")
    assert {t["session_id"] for t in people.op.erasures()} == {a, b}
    assert [t["session_id"] for t in people.alice.erasures()] == [a]
    # retention by the operator covers everyone
    c = people.alice.capture(repo, [Message("user", "again")], source="cli")["session_id"]
    people.alice.close(c, "done", agent="alice", human=True)
    clock.advance(40 * 86400)
    assert people.op.forget_older_than(30, actor="op", reason="30 days")["sessions_erased"] == 1


def test_the_operator_never_joins_someone_elses_session_by_accident(people, tmp_path):
    repo = str(_git_repo(tmp_path / "app"))
    a = people.alice.capture(repo, [Message("user", "alice")], source="cli")["session_id"]
    with pytest.raises(NotFoundError):
        people.op.latest_active(repo)
    assert people.op.sessions() == []
    assert people.op.brief(a)["session_id"] == a       # explicit id: the operator may look
    users = {u["role"]: u for u in access.users(people.op.store)}
    assert users[people.roles["alice"]]["sessions"] == 1
    assert users[people.roles["bob"]]["has_access"] is True
    moved = access.reassign(people.op.store, people.roles["alice"], people.roles["bob"])
    assert moved["sessions"] == 1
    assert people.bob.latest_active(repo) == a
    with pytest.raises(NotFoundError):
        people.alice.latest_active(repo)


def test_access_is_managed_only_by_the_operator(people, schema):
    with pytest.raises(access.NotOperatorError):
        access.grant(Store(people.dsns["alice"], schema=schema, clock=people.op.clock,
                           ids=RandomIds()), people.roles["eve"])
    eve = Store(people.dsns["eve"], schema=schema, clock=people.op.clock, ids=RandomIds())
    try:
        with pytest.raises(ContinuumError, match="continuum admin grant") as no:
            eve.ensure_schema()
        assert no.value.code == "no_access"
        access.grant(people.op.store, people.roles["eve"])
        assert CodingContinuity(eve, clock=people.op.clock, ids=RandomIds()).sessions() == []
        access.revoke(people.op.store, people.roles["eve"])
        with pytest.raises(ContinuumError, match="no access"):
            eve.ensure_schema()
    finally:
        eve.close()
    with pytest.raises(NotFoundError):
        access.grant(people.op.store, "no_such_role_x")


def test_admin_cli(people, schema, tmp_path):
    exe = str(Path(sys.executable).parent / "continuum")
    env = {**os.environ, "CONTINUUM_DATABASE_URL": DSN, "CONTINUUM_DB_SCHEMA": schema,
           "CONTINUUM_HOME": str(tmp_path / "home")}
    out = subprocess.run([exe, "admin", "grant", people.roles["eve"]], env=env,
                         capture_output=True, text=True)
    assert out.returncode == 0, out.stdout + out.stderr
    out = subprocess.run([exe, "admin", "users"], env=env, capture_output=True, text=True)
    assert people.roles["eve"] in out.stdout
    as_alice = {**env, "CONTINUUM_DATABASE_URL": people.dsns["alice"]}
    out = subprocess.run([exe, "admin", "revoke", people.roles["eve"]], env=as_alice,
                         capture_output=True, text=True)
    assert out.returncode == 2 and "not_operator" in out.stdout
