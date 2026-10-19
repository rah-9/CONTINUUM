# SPDX-License-Identifier: LicenseRef-Continuum-Proprietary
# Copyright (c) 2026 the Continuum copyright holder. All rights reserved.
"""Mock database: per-record field updates with an independent mutation log."""

from __future__ import annotations

from typing import Any

from continuum.core.canonical import digest
from continuum.tools.trusted.services import ServiceRejected
from continuum_lab.mock_services.base import MockService


class MockDatabase(MockService):
    kind = "db"

    def seed(self, records: dict[str, dict[str, Any]]) -> None:
        with self._txn() as s:
            for rid, fields in records.items():
                s["data"][rid] = {"fields": fields, "fields_sha256": digest(fields),
                                  "version": 0, "receipt_id": "seed"}

    def update(self, record_id: str, fields: dict[str, Any], *, idempotency_key: str | None
               ) -> dict[str, Any]:
        with self._txn() as s:
            known = record_id in s["data"]
        if not known:
            raise ServiceRejected("no_such_record", f"record {record_id} does not exist")
        receipt = self._write("update", record_id,
                              {"fields": fields, "fields_sha256": digest(fields)},
                              idempotency_key)
        return {**receipt, "record_id": record_id}

    def get(self, record_id: str) -> dict[str, Any]:
        item = self._read(record_id)
        if item is None:
            return {"exists": False, "record_id": record_id}
        return {"exists": True, "record_id": record_id, "fields": item["fields"],
                "fields_sha256": item["fields_sha256"], "version": item["version"]}
