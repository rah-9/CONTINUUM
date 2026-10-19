# SPDX-License-Identifier: LicenseRef-Continuum-Proprietary
# Copyright (c) 2026 the Continuum copyright holder. All rights reserved.
"""Identifier generation. Deterministic generator for tests and replays."""

from __future__ import annotations

import uuid
from typing import Protocol


class IdGenerator(Protocol):
    def new(self, prefix: str) -> str: ...


class RandomIds:
    def new(self, prefix: str) -> str:
        return f"{prefix}_{uuid.uuid4().hex}"


class SequentialIds:
    """Deterministic, collision-free within one generator instance."""

    def __init__(self, namespace: str = "t") -> None:
        self._namespace = namespace
        self._counter = 0

    def new(self, prefix: str) -> str:
        self._counter += 1
        return f"{prefix}_{self._namespace}{self._counter:06d}"
