"""Structured JSON local tracing (docs/01 section 5).

One JSON object per line on the ``continuum`` logger hierarchy. Callers pass
only IDs, types, statuses and durations, never payloads or arguments, so
secrets cannot reach logs. Full diagnostic data lives in the event log and in
redacted blobs; see :mod:`continuum.core.replay` for trace export.
"""

from __future__ import annotations

import json
import logging
import sys
import time
from collections.abc import Iterator
from contextlib import contextmanager
from datetime import UTC, datetime
from typing import IO, Any

LOGGER = logging.getLogger("continuum.trace")


class JsonFormatter(logging.Formatter):
    def format(self, record: logging.LogRecord) -> str:
        body: dict[str, Any] = {
            "ts": datetime.fromtimestamp(record.created, UTC).isoformat(timespec="milliseconds"),
            "level": record.levelname.lower(),
            "logger": record.name,
            "event": record.getMessage(),
        }
        fields = getattr(record, "fields", None)
        if isinstance(fields, dict):
            body.update(fields)
        if record.exc_info:
            body["exception"] = self.formatException(record.exc_info)
        return json.dumps(body, default=str, sort_keys=True)


def configure_logging(level: str = "INFO", stream: IO[str] | None = None) -> None:
    """Route the ``continuum`` loggers to JSON lines (stderr by default). Idempotent."""
    root = logging.getLogger("continuum")
    root.setLevel(level.upper())
    for handler in list(root.handlers):
        if getattr(handler, "_continuum_json", False):
            root.removeHandler(handler)
    handler = logging.StreamHandler(stream or sys.stderr)
    handler.setFormatter(JsonFormatter())
    handler._continuum_json = True  # type: ignore[attr-defined]
    root.addHandler(handler)
    root.propagate = False


def trace(event: str, **fields: Any) -> None:
    LOGGER.info(event, extra={"fields": fields})


@contextmanager
def span(event: str, **fields: Any) -> Iterator[dict[str, Any]]:
    """Time a block; the yielded dict can be extended with outcome fields."""
    extra: dict[str, Any] = {}
    started = time.perf_counter()
    try:
        yield extra
    except BaseException as exc:
        extra.setdefault("outcome", f"exception:{type(exc).__name__}")
        raise
    finally:
        duration_ms = round((time.perf_counter() - started) * 1000, 3)
        trace(event, duration_ms=duration_ms, **fields, **extra)
