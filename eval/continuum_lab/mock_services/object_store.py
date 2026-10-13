"""Mock object store: put/head with exact content SHA-256 (not an ETag)."""

from __future__ import annotations

from typing import Any

from continuum.core.canonical import sha256_hex
from continuum_lab.mock_services.base import MockService


class MockObjectStore(MockService):
    kind = "obj"

    def put(self, object_path: str, content: bytes, *, idempotency_key: str | None
            ) -> dict[str, Any]:
        receipt = self._write("put", object_path,
                              {"content_sha256": sha256_hex(content), "size": len(content)},
                              idempotency_key)
        return {**receipt, "object_path": object_path, "content_sha256": sha256_hex(content)}

    def head(self, object_path: str) -> dict[str, Any]:
        item = self._read(object_path)
        if item is None:
            return {"exists": False, "object_path": object_path}
        return {"exists": True, "object_path": object_path,
                "content_sha256": item["content_sha256"], "size": item["size"],
                "version": item["version"]}
