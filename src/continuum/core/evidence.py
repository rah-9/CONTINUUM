"""Run workspace, content-addressed blobs and milestone fingerprints.

* All workspace paths are confined to the run directory (no traversal, no
  symlink escape); writes are atomic (temp file + fsync + ``os.replace``).
* Evidence, tool results and normalized model responses are stored by content
  hash as ``blob:sha256:<hex>`` references; raw blobs never enter events.
* ``input_fingerprint`` follows docs/03 section 5 exactly.
"""

from __future__ import annotations

import json
import os
import shutil
import tempfile
from pathlib import Path
from typing import Any

from continuum.core.canonical import canonical_json, digest, sha256_hex
from continuum.core.errors import PathContainmentError, SpecValidationError
from continuum.core.state import TaskSpec

BLOB_PREFIX = "blob:sha256:"


def sha256_file(path: Path) -> str:
    import hashlib

    h = hashlib.sha256()
    with path.open("rb") as fh:
        for chunk in iter(lambda: fh.read(1 << 16), b""):
            h.update(chunk)
    return h.hexdigest()


def atomic_write_bytes(path: Path, data: bytes) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    fd, tmp = tempfile.mkstemp(dir=path.parent, prefix=f".{path.name}.", suffix=".tmp")
    try:
        with os.fdopen(fd, "wb") as fh:
            fh.write(data)
            fh.flush()
            os.fsync(fh.fileno())
        os.replace(tmp, path)
    except BaseException:
        Path(tmp).unlink(missing_ok=True)
        raise


class Workspace:
    """Per-run confined directory: ``inputs/`` (immutable snapshots), ``artifacts/``,
    and ``blobs/`` (content-addressed evidence and redacted results)."""

    def __init__(self, root: Path, run_id: str) -> None:
        self.base = (root / run_id).resolve()
        self.run_id = run_id

    def init(self) -> None:
        for sub in ("inputs", "artifacts", "blobs"):
            (self.base / sub).mkdir(parents=True, exist_ok=True)

    def resolve(self, relative: str) -> Path:
        if not relative or relative.startswith(("/", "\\")) or "\x00" in relative:
            raise PathContainmentError(f"path must be workspace-relative: {relative!r}")
        candidate = (self.base / relative).resolve()
        if candidate != self.base and self.base not in candidate.parents:
            raise PathContainmentError(f"path escapes workspace: {relative!r}")
        return candidate

    def relative(self, path: Path) -> str:
        return path.resolve().relative_to(self.base).as_posix()

    def read_bytes(self, relative: str) -> bytes:
        return self.resolve(relative).read_bytes()

    def write_bytes(self, relative: str, data: bytes) -> str:
        """Atomically write and return the sha256 hex of ``data``."""
        if relative.startswith("inputs/"):
            raise PathContainmentError("input snapshots are immutable")
        atomic_write_bytes(self.resolve(relative), data)
        return sha256_hex(data)

    def hash(self, relative: str) -> str | None:
        path = self.resolve(relative)
        return sha256_file(path) if path.is_file() else None

    def snapshot_inputs(self, spec: TaskSpec, source_dir: Path) -> None:
        """Copy declared inputs into ``inputs/`` and verify their declared hashes."""
        for item in spec.inputs:
            src = (source_dir / item.uri).resolve()
            if not src.is_file():
                raise SpecValidationError(f"input {item.uri} not found under {source_dir}")
            actual = sha256_file(src)
            if actual != item.sha256:
                raise SpecValidationError(
                    f"input {item.uri} hash {actual} does not match spec {item.sha256}")
            dest = self.resolve(item.uri if item.uri.startswith("inputs/")
                                else f"inputs/{item.uri}")
            dest.parent.mkdir(parents=True, exist_ok=True)
            if not dest.exists():
                shutil.copyfile(src, dest)
                dest.chmod(0o444)

    def input_path(self, uri: str) -> str:
        return uri if uri.startswith("inputs/") else f"inputs/{uri}"

    # ---- blobs ----------------------------------------------------------------------

    def put_blob(self, value: Any) -> str:
        data = canonical_json(value).encode("utf-8")
        hexdigest = sha256_hex(data)
        path = self.base / "blobs" / hexdigest[:2] / f"{hexdigest}.json"
        if not path.exists():
            atomic_write_bytes(path, data)
        return BLOB_PREFIX + hexdigest

    def get_blob(self, ref: str) -> Any:
        if not ref.startswith(BLOB_PREFIX):
            raise ValueError(f"not a blob reference: {ref}")
        hexdigest = ref.removeprefix(BLOB_PREFIX)
        path = self.base / "blobs" / hexdigest[:2] / f"{hexdigest}.json"
        data = path.read_bytes()
        if sha256_hex(data) != hexdigest:
            raise ValueError(f"blob {ref} content does not match its digest")
        return json.loads(data)


def input_fingerprint(
    *,
    input_artifact_hashes: dict[str, str],
    external_input_versions: dict[str, Any],
    dependency_evidence_refs: dict[str, str | None],
    verifier_id: str,
    verifier_version: str,
    relevant_task_spec_fields: dict[str, Any],
) -> str:
    return digest({
        "input_artifact_hashes": input_artifact_hashes,
        "external_input_versions": external_input_versions,
        "dependency_evidence_refs": dependency_evidence_refs,
        "verifier_id": verifier_id,
        "verifier_version": verifier_version,
        "relevant_task_spec_fields": relevant_task_spec_fields,
    })
