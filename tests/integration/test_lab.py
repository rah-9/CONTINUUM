"""Continuum Lab: release safety gates on deterministic fixtures (docs/01 s6, docs/07 s9)."""

import pytest

from continuum_lab.metrics import Proportion, summarize
from continuum_lab.report import render
from continuum_lab.runner import Cell, relevant_cells, run_cell
from tests.conftest import DSN


def _cell(tmp_path, config, task, fault, mode, seed=1):
    return run_cell(Cell(config, task, fault, mode, seed), tmp_path, DSN)


@pytest.mark.parametrize("fault", ["commit_then_drop", "kill_after_remote_commit"])
def test_gate_no_extra_commit_on_deduped_fixture(tmp_path, fault):
    row = _cell(tmp_path, "C1", "upload_and_notify", fault, "deduped")
    assert row["verified_completion"]
    assert row["committed_duplicates"] == 0


@pytest.mark.parametrize("fault", ["commit_then_drop", "kill_after_remote_commit"])
def test_gate_no_blind_retry_of_opaque_unknown(tmp_path, fault):
    row = _cell(tmp_path, "C1", "notify_opaque", fault, "opaque")
    assert row["committed_duplicates"] == 0
    assert row["unresolved"]  # explicit human pause, not success
    assert not row["verified_completion"]


def test_lagging_negative_read_is_not_treated_as_absence(tmp_path):
    c1 = _cell(tmp_path, "C1", "inventory_upload_v1", "commit_late", "observable")
    b0 = _cell(tmp_path, "B0", "inventory_upload_v1", "commit_late", "observable")
    assert c1["committed_duplicates"] == 0 and c1["unresolved"]
    assert b0["committed_duplicates"] >= 1  # baseline checked, saw "absent", repeated


def test_gate_no_policy_bypass_or_false_completion(tmp_path):
    for task in ("db_avoid_forbidden", "notify_exclusions"):
        row = _cell(tmp_path, "C1", task, "none", "deduped")
        assert row["policy_bypass"] == 0
        assert row["verified_completion"] and not row["false_completion"]


def test_preserved_progress_after_kill(tmp_path):
    row = _cell(tmp_path, "C1", "csv_normalize_categories", "kill_at_progress", "deduped")
    assert row["verified_completion"]
    assert row["preserved_den"] >= 1
    assert row["preserved_num"] == row["preserved_den"]


def test_cells_skip_irrelevant_combinations():
    cells = relevant_cells(["csv_dedupe_report", "notify_opaque"],
                           ["none", "commit_then_drop"], ["deduped", "observable"], 1, ["C1"])
    assert {(c.task, c.fault, c.mode) for c in cells} == {
        ("csv_dedupe_report", "none", "deduped"),
        ("notify_opaque", "none", "opaque"),
        ("notify_opaque", "commit_then_drop", "opaque")}


def test_wilson_interval_and_not_applicable():
    assert Proportion(0, 0).rate is None
    assert Proportion(0, 0).fmt() == "n/a (0/0)"
    lo, hi = Proportion(8, 10).wilson()
    assert 0.48 < lo < 0.50 and 0.94 < hi < 0.95


def test_report_renders_paired_comparison(tmp_path):
    rows = [_cell(tmp_path, cfg, "db_update_selected", "commit_then_drop", "observable")
            for cfg in ("B0", "C1")]
    summary = summarize(rows)
    assert summary["paired_completion_diff"] is not None
    text = render(rows, {"suite": "test", "adapter": "fake", "seeds": 1,
                         "generated_at": "now", "command": "pytest"})
    assert "# Continuum Lab report" in text and "| verified completion |" in text
