# SPDX-License-Identifier: LicenseRef-Continuum-Proprietary
# Copyright (c) 2026 the Continuum copyright holder. All rights reserved.
import os

import pytest

from continuum.core.errors import PathContainmentError
from continuum.core.evidence import Workspace, input_fingerprint


@pytest.fixture
def ws(tmp_path):
    w = Workspace(tmp_path, "run_1")
    w.init()
    return w


@pytest.mark.parametrize("bad", ["../x", "/etc/passwd", "artifacts/../../x", ""])
def test_path_traversal_rejected(ws, bad):
    with pytest.raises(PathContainmentError):
        ws.resolve(bad)


def test_symlink_escape_rejected(ws, tmp_path):
    (tmp_path / "outside").mkdir()
    os.symlink(tmp_path / "outside", ws.base / "artifacts" / "link")
    with pytest.raises(PathContainmentError):
        ws.resolve("artifacts/link/file")


def test_inputs_are_immutable(ws):
    with pytest.raises(PathContainmentError):
        ws.write_bytes("inputs/x.csv", b"data")


def test_atomic_write_and_hash(ws):
    sha = ws.write_bytes("artifacts/a.txt", b"hello")
    assert ws.hash("artifacts/a.txt") == sha
    assert not list((ws.base / "artifacts").glob(".*tmp"))


def test_blob_roundtrip_and_tamper_detection(ws):
    ref = ws.put_blob({"b": 2, "a": 1})
    assert ws.put_blob({"a": 1, "b": 2}) == ref  # content-addressed
    assert ws.get_blob(ref) == {"a": 1, "b": 2}
    path = next((ws.base / "blobs").rglob("*.json"))
    path.write_text('{"a":9}')
    with pytest.raises(ValueError, match="does not match"):
        ws.get_blob(ref)


def test_fingerprint_changes_with_any_input():
    base = dict(input_artifact_hashes={"a": "1"}, external_input_versions={},
                dependency_evidence_refs={"m1": "e1"}, verifier_id="v", verifier_version="1",
                relevant_task_spec_fields={"id": "m2"})
    fp = input_fingerprint(**base)
    for key, value in [("input_artifact_hashes", {"a": "2"}),
                       ("dependency_evidence_refs", {"m1": "e2"}),
                       ("verifier_version", "2"),
                       ("external_input_versions", {"obj": 3})]:
        assert input_fingerprint(**{**base, key: value}) != fp
