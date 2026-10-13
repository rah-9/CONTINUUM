"""Mock messaging: immutable sends per (recipient, slot); query shows latest send."""

from __future__ import annotations

from typing import Any

from continuum.core.canonical import sha256_hex
from continuum_lab.mock_services.base import MockService


def _target(recipient: str, slot: str) -> str:
    return f"{recipient.strip().lower()}/{slot}"


class MockMessages(MockService):
    kind = "msg"

    def send(self, recipient: str, slot: str, body: str, *, idempotency_key: str | None
             ) -> dict[str, Any]:
        receipt = self._write("send", _target(recipient, slot),
                              {"recipient": recipient, "slot": slot,
                               "body_sha256": sha256_hex(body)}, idempotency_key)
        return {**receipt, "message_id": receipt["receipt_id"]}

    def query(self, recipient: str, slot: str) -> dict[str, Any]:
        item = self._read(_target(recipient, slot))
        if item is None:
            return {"exists": False, "recipient": recipient, "slot": slot}
        return {"exists": True, "recipient": item["recipient"], "slot": item["slot"],
                "body_sha256": item["body_sha256"], "message_id": item["receipt_id"]}
