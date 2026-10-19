# SPDX-License-Identifier: LicenseRef-Continuum-Proprietary
# Copyright (c) 2026 the Continuum copyright holder. All rights reserved.
"""Service client contracts that trusted tools call.

Concrete clients (Continuum Lab mocks, or future real connectors) implement
these protocols. Clients signal outcome certainty through exception type:
:class:`ServiceRejected` means *definitively no effect*; anything else
(including :class:`ServiceTimeout`) is treated as ambiguous for writes.
"""

from __future__ import annotations

from typing import Any, Literal, Protocol

ServiceMode = Literal["deduped", "observable", "opaque"]


class ServiceRejected(Exception):
    """The service refused the request before any effect (e.g. validation 4xx)."""

    def __init__(self, code: str, message: str = "") -> None:
        super().__init__(message or code)
        self.code = code


class ServiceTimeout(Exception):
    """No response received; the request may or may not have committed."""


class ObjectStoreClient(Protocol):
    mode: ServiceMode

    def put(self, object_path: str, content: bytes, *, idempotency_key: str | None
            ) -> dict[str, Any]: ...

    def head(self, object_path: str) -> dict[str, Any]: ...


class DatabaseClient(Protocol):
    mode: ServiceMode

    def update(self, record_id: str, fields: dict[str, Any], *, idempotency_key: str | None
               ) -> dict[str, Any]: ...

    def get(self, record_id: str) -> dict[str, Any]: ...


class MessageClient(Protocol):
    mode: ServiceMode

    def send(self, recipient: str, slot: str, body: str, *, idempotency_key: str | None
             ) -> dict[str, Any]: ...

    def query(self, recipient: str, slot: str) -> dict[str, Any]: ...
