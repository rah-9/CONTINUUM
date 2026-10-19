# SPDX-License-Identifier: LicenseRef-Continuum-Proprietary
# Copyright (c) 2026 the Continuum copyright holder. All rights reserved.
"""Hidden judge for the long-chat trial: one test per requirement the user stated over 30
messages. Run against a finished project: TRIAL_PROJ=<project dir> pytest judge_taskr.py

Requirements stated only in the chat, early, for work done *after* the switch (export):
  R11 header (message 8), R12 done yes/no (message 15), R13 --out/--force (message 21),
  R14 sorted by created_at (message 28).
Requirements implemented before the switch, which the next model must not break: R1-R10.
"""

import csv
import io
import json
import os
import re
import subprocess
import sys
import time
from pathlib import Path

import pytest

PROJ = Path(os.environ["TRIAL_PROJ"])
ID = re.compile(r"\b([0-9a-f]{4})\b")


@pytest.fixture
def run(tmp_path):
    home = tmp_path / "home"

    def call(*args, stdin=subprocess.DEVNULL):
        env = {**os.environ, "TASKR_HOME": str(home), "PYTHONPATH": str(PROJ)}
        env.pop("PYTHONSAFEPATH", None)
        return subprocess.run([sys.executable, "-m", "taskr", *args], cwd=tmp_path, env=env,
                              stdin=stdin, capture_output=True, text=True, timeout=60)

    call.home = home
    return call


def _id(run, title):
    out = run("list").stdout
    line = next(line for line in out.splitlines() if title in line)
    return ID.search(line.replace(title, "")).group(1)


def test_r1_data_file_under_taskr_home(run):
    assert run("add", "alpha").returncode == 0
    assert (run.home / "tasks.json").is_file()
    json.loads((run.home / "tasks.json").read_text())


def test_r2_ids_are_4_hex_and_never_reused(run):
    seen = set()
    for i in range(6):
        run("add", f"t{i}")
        tid = _id(run, f"t{i}")
        assert re.fullmatch(r"[0-9a-f]{4}", tid)
        assert tid not in seen
        seen.add(tid)
        assert run("rm", tid, "--yes").returncode == 0


def test_r3_list_marks_done_and_open(run):
    run("add", "open one")
    run("add", "closed one")
    assert run("done", _id(run, "closed one")).returncode == 0
    out = run("list").stdout
    lines = out.splitlines()
    assert any(line.lstrip().startswith("[x]") and "closed one" in line for line in lines)
    assert any(line.lstrip().startswith("[ ]") and "open one" in line for line in out.splitlines())


def test_r4_r5_r6_priority_default_low_and_sorting(run):
    run("add", "first-default")
    time.sleep(1.1)
    run("add", "second-normal", "--priority", "normal")
    run("add", "third-high", "--priority", "high")
    run("add", "fourth-default")
    titles = [t for line in run("list").stdout.splitlines()
              for t in ("first-default", "second-normal", "third-high", "fourth-default")
              if t in line]
    assert titles == ["third-high", "second-normal", "first-default", "fourth-default"]


def test_r7_rm_needs_confirmation_without_a_terminal(run):
    run("add", "keep me")
    tid = _id(run, "keep me")
    out = run("rm", tid)
    assert out.returncode == 2
    assert "keep me" in run("list").stdout


def test_r8_titles_are_trimmed_and_empty_rejected(run):
    assert run("add", "   ").returncode == 2
    run("add", "  padded  ")
    line = next(line for line in run("list").stdout.splitlines() if "padded" in line)
    assert "  padded" not in line and line.rstrip().endswith("padded")


def test_r9_errors_are_one_line_without_tracebacks(run):
    run("add", "x")
    out = run("done", "ffff" if "ffff" not in run("list").stdout else "eeee")
    assert out.returncode == 1
    assert out.stderr.startswith("error: ") and "Traceback" not in out.stderr + out.stdout


def test_r10_created_at_utc_z(run):
    run("add", "stamp")
    data = json.loads((run.home / "tasks.json").read_text())
    text = json.dumps(data)
    assert re.search(r"\d{4}-\d\d-\d\dT\d\d:\d\d:\d\dZ", text)


def test_edit_title(run):
    run("add", "old title")
    assert run("edit", _id(run, "old title"), "--title", "new title").returncode == 0
    assert "new title" in run("list").stdout and "old title" not in run("list").stdout


def _export_rows(run, *args):
    """The export as the user specified it: `export --out FILE` (message 21). Whether a bare
    `export` prints to stdout was never stated, so it is not judged."""
    target = run.home.parent / f"export-{time.monotonic_ns()}.csv"
    out = run("export", "--out", str(target), *args)
    assert out.returncode == 0, out.stderr
    return target.read_text()


def _two_tasks(run):
    run("add", "b-task", "--priority", "high")
    time.sleep(1.1)
    run("add", "a-task")
    run("done", _id(run, "a-task"))
    return list(csv.DictReader(io.StringIO(_export_rows(run))))


def test_r11_export_header(run):
    run("add", "h")
    assert _export_rows(run).splitlines()[0].strip() == "id,title,priority,done,created_at"


def test_r12_export_done_is_yes_no(run):
    rows = _two_tasks(run)
    assert {r["title"]: r["done"] for r in rows} == {"b-task": "no", "a-task": "yes"}


def test_r14_export_sorted_by_created_at(run):
    rows = _two_tasks(run)
    assert [r["title"] for r in rows] == ["b-task", "a-task"]
    assert rows[1]["priority"] == "low"


def test_r13_export_out_never_overwrites_without_force(run, tmp_path):
    run("add", "e")
    target = tmp_path / "out.csv"
    target.write_text("KEEP\n")
    out = run("export", "--out", str(target))
    assert out.returncode == 2 and target.read_text() == "KEEP\n"
    assert run("export", "--out", str(target), "--force").returncode == 0
    assert target.read_text().startswith("id,title,priority,done,created_at")
