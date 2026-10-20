# SPDX-License-Identifier: LicenseRef-Continuum-Proprietary
# Copyright (c) 2026 the Continuum copyright holder. All rights reserved.
"""Several people, one database (D-040). Operator commands, CLI only, never MCP.

Each person connects with their own PostgreSQL role (``CONTINUUM_DATABASE_URL`` with their
own user). Row-level security on the coding tables shows each role only its own sessions,
events, checks and tombstones. The schema owner (who ran ``db-init``) administers: grants
and revokes access, reassigns sessions when a person's role changes, runs retention for
everyone. Creating roles and passwords stays with the database administrator.
"""

from __future__ import annotations

import re
from typing import Any

import psycopg
from psycopg import sql

from continuum.core.errors import ContinuumError, NotFoundError, SpecValidationError
from continuum.core.store import Store

_ROLE = re.compile(r"[A-Za-z_][A-Za-z0-9_.@-]{0,62}")

# What a person needs: their own coding rows (RLS narrows every statement to them) and
# the schema version. No runtime (``continuum run``) tables: those stay the operator's.
_GRANTS: tuple[tuple[str, tuple[str, ...]], ...] = (
    ("SELECT", ("schema_meta",)),
    ("SELECT, INSERT, UPDATE, DELETE", ("coding_sessions", "coding_checks")),
    ("SELECT, INSERT, DELETE", ("coding_events",)),   # the trigger still forbids UPDATE
    ("SELECT, INSERT", ("coding_erasures",)),
    ("SELECT, INSERT, DELETE", ("coding_blobs", "coding_media")),  # immutable: no UPDATE
)
_TABLES = ("schema_meta", "coding_sessions", "coding_checks", "coding_events",
           "coding_erasures", "coding_blobs", "coding_media")


class NotOperatorError(ContinuumError):
    code = "not_operator"


def _role(name: str) -> str:
    if not _ROLE.fullmatch(name):
        raise SpecValidationError(f"invalid role name {name!r}")
    return name


def _operator(conn: psycopg.Connection[dict[str, Any]], schema: str) -> None:
    row = conn.execute(
        b"SELECT pg_has_role(current_user, n.nspowner, 'USAGE') AS ok, current_user AS me "
        b"FROM pg_namespace n WHERE n.nspname = %s", (schema,)).fetchone()
    if row is None:
        raise NotFoundError(f"schema {schema} does not exist; run continuum db-init")
    if not row["ok"]:
        raise NotOperatorError(
            f"role {row['me']} does not own schema {schema}; only the operator who ran db-init "
            "can manage access")


def _existing_role(conn: psycopg.Connection[dict[str, Any]], role: str) -> None:
    if conn.execute(b"SELECT 1 FROM pg_roles WHERE rolname = %s", (role,)).fetchone() is None:
        raise NotFoundError(f"PostgreSQL role {role} does not exist; ask the database "
                            f"administrator to create it (CREATE ROLE ... LOGIN)")


def grant(store: Store, role: str) -> dict[str, Any]:
    role = _role(role)
    with store.connection() as conn:
        try:
            _operator(conn, store.schema)
            _existing_role(conn, role)
            schema, who = sql.Identifier(store.schema), sql.Identifier(role)
            conn.execute(sql.SQL("GRANT USAGE ON SCHEMA {} TO {}").format(schema, who))
            for privileges, tables in _GRANTS:
                conn.execute(sql.SQL("GRANT " + privileges + " ON {} TO {}").format(
                    sql.SQL(", ").join(sql.Identifier(t) for t in tables), who))
            conn.commit()
        except BaseException:
            conn.rollback()
            raise
    return {"role": role, "granted": True, "sees": "only its own coding sessions"}


def revoke(store: Store, role: str) -> dict[str, Any]:
    """Remove access. The person's sessions stay (erase them with ``coding forget``)."""
    role = _role(role)
    with store.connection() as conn:
        try:
            _operator(conn, store.schema)
            _existing_role(conn, role)
            who = sql.Identifier(role)
            conn.execute(sql.SQL("REVOKE ALL ON {} FROM {}").format(
                sql.SQL(", ").join(sql.Identifier(t) for t in _TABLES), who))
            conn.execute(sql.SQL("REVOKE USAGE ON SCHEMA {} FROM {}").format(
                sql.Identifier(store.schema), who))
            conn.commit()
        except BaseException:
            conn.rollback()
            raise
    return {"role": role, "granted": False}


def reassign(store: Store, old: str, new: str) -> dict[str, Any]:
    """Move sessions and checks to another role (a person's login changed). Tombstones
    keep their owner: they are history."""
    old, new = _role(old), _role(new)
    with store.connection() as conn:
        try:
            _operator(conn, store.schema)
            _existing_role(conn, new)
            sessions = conn.execute(
                b"UPDATE coding_sessions SET owner_role = %s WHERE owner_role = %s",
                (new, old)).rowcount
            checks = conn.execute(
                b"UPDATE coding_checks SET owner_role = %s WHERE owner_role = %s "
                b"AND NOT EXISTS (SELECT 1 FROM coding_checks c WHERE c.owner_role = %s "
                b"AND c.repo_root = coding_checks.repo_root AND c.name = coding_checks.name)",
                (new, old, new)).rowcount
            conn.commit()
        except BaseException:
            conn.rollback()
            raise
    return {"from": old, "to": new, "sessions": sessions, "checks": checks}


def users(store: Store) -> list[dict[str, Any]]:
    """Every role with sessions or access, with its session counts (no content)."""
    with store.connection() as conn:
        try:
            _operator(conn, store.schema)
            rows = conn.execute(
                b"SELECT owner_role AS role, count(*) AS sessions, "
                b"count(*) FILTER (WHERE status = 'active') AS active, "
                b"max(updated_at) AS last_write FROM coding_sessions GROUP BY owner_role")
            found = {r["role"]: {**r, "has_access": None} for r in rows.fetchall()}
            for r in conn.execute(
                    b"SELECT r.rolname AS role FROM pg_roles r WHERE "
                    b"has_table_privilege(r.oid, 'coding_sessions', 'INSERT') "
                    b"AND NOT r.rolsuper").fetchall():
                found.setdefault(r["role"], {"role": r["role"], "sessions": 0, "active": 0,
                                             "last_write": None})["has_access"] = True
            conn.rollback()
        except BaseException:
            conn.rollback()
            raise
    return [{**v, "has_access": bool(v["has_access"]),
             "last_write": None if v["last_write"] is None else v["last_write"].isoformat()}
            for _, v in sorted(found.items())]
