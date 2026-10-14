"""Secret redaction applied before anything is persisted or sent to a model.

Secrets come only from environment/config. Two layers: (1) values of known
secret environment variables are replaced wherever they appear; (2) mapping
keys that look like credentials have their values masked.
"""

from __future__ import annotations

import os
import re
from collections.abc import Iterable
from typing import Any

REDACTED = "[REDACTED]"
_SECRET_KEY = re.compile(r"(api[_-]?key|secret|token|password|passwd|authorization|"
                         r"cookie|credential|private[_-]?key)", re.IGNORECASE)
_SECRET_ENV = re.compile(r"(KEY|SECRET|TOKEN|PASSWORD|CREDENTIAL)", re.IGNORECASE)
_URL_PASSWORD = re.compile(r"[a-z][a-z0-9+.-]*://[^:/@\s]*:([^@\s]+)@", re.IGNORECASE)
_URL_CREDENTIAL = re.compile(r"([a-z][a-z0-9+.-]*://[^:/@\s]*:)[^@\s]+@", re.IGNORECASE)


def _looks_secret(value: str) -> bool:
    """At least 8 characters from at least two classes (lower, upper, digit, other)."""
    classes = sum((any(c.islower() for c in value), any(c.isupper() for c in value),
                   any(c.isdigit() for c in value), any(not c.isalnum() for c in value)))
    return len(value) >= 8 and classes >= 2


class Redactor:
    def __init__(self, secret_values: Iterable[str] = ()) -> None:
        self._values = sorted({v for v in secret_values if v and len(v) >= 6},
                              key=len, reverse=True)

    @classmethod
    def from_environment(cls, extra: Iterable[str] = ()) -> Redactor:
        values = [v for k, v in os.environ.items() if _SECRET_ENV.search(k)]
        # Passwords embedded in connection URLs (CONTINUUM_DATABASE_URL, *_DSN, ...) are
        # always hidden inside URLs (see ``text``). Bare occurrences are replaced only for
        # secret-looking passwords: a dictionary word such as the README's local
        # `continuum:continuum` would otherwise rewrite every path and command into
        # `[REDACTED]`, permanently, in every journal entry and brief.
        values += [m.group(1) for v in os.environ.values() for m in _URL_PASSWORD.finditer(v)
                   if _looks_secret(m.group(1))]
        return cls([*values, *extra])

    def text(self, value: str) -> str:
        for secret in self._values:
            value = value.replace(secret, REDACTED)
        return _URL_CREDENTIAL.sub(rf"\1{REDACTED}@", value)

    def __call__(self, value: Any) -> Any:
        if isinstance(value, str):
            return self.text(value)
        if isinstance(value, dict):
            return {k: (REDACTED if isinstance(k, str) and _SECRET_KEY.search(k)
                        and isinstance(v, str) else self(v))
                    for k, v in value.items()}
        if isinstance(value, list | tuple):
            return [self(v) for v in value]
        return value
