"""Shared fixture for the export-feature handoff trial (hidden from the agents).

A real coding session with a note and a check run whose output contains a "customer"
marker, in its own throwaway schema. ``TRIAL_PROJ_SRC`` points at the project under test.
"""

from __future__ import annotations

import os
import subprocess
import sys
import uuid
from collections.abc import Iterator
from typing import Any

import psycopg
import pytest

from continuum.coding.service import CodingContinuity
from continuum.core.clock import SystemClock
from continuum.core.ids import RandomIds
from continuum.core.store import Store

DSN = os.environ.get("TRIAL_DATABASE_URL",
                     "postgresql://continuum:continuum@localhost:5432/continuum_test")
PROJ_SRC = os.environ["TRIAL_PROJ_SRC"]


@pytest.fixture
def session(tmp_path: Any) -> Iterator[tuple[Store, str, str]]:
    schema = "trial_" + uuid.uuid4().hex[:10]
    store = Store(DSN, schema=schema, clock=SystemClock(), ids=RandomIds())
    store.migrate()
    repo = tmp_path / "r"
    repo.mkdir()
    subprocess.run(["git", "init", "-q", str(repo)], check=True)
    (repo / "a.py").write_text("x = 1\n")
    cc = CodingContinuity(store, clock=SystemClock(), ids=RandomIds())
    sid = cc.start(str(repo), "goal", agent="m", tasks=[{"title": "t"}])["session_id"]
    cc.note(sid, "decision", "use sqlite", agent="m")
    cc.register_check(str(repo), "c", [sys.executable, "-c", "print('CUSTOMER-SECRET-ROW 4711')"],
                      actor="op", timeout_seconds=30)
    cc.run_check(sid, "c", agent="m")
    yield store, sid, schema
    store.close()
    with psycopg.connect(DSN, autocommit=True) as conn:
        conn.execute(f'DROP SCHEMA "{schema}" CASCADE'.encode())


def cli(schema: str, *args: str) -> subprocess.CompletedProcess[str]:
    """Run the project's CLI from its source tree against the trial schema."""
    env = {**os.environ, "CONTINUUM_DATABASE_URL": DSN, "CONTINUUM_DB_SCHEMA": schema,
           "PYTHONPATH": PROJ_SRC}
    code = "import sys; from continuum.interfaces.cli import main; sys.exit(main())"
    return subprocess.run([sys.executable, "-c", code, *args],
                          env=env, capture_output=True,
                          text=True, check=False)
