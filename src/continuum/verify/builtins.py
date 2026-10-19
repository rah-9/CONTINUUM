# SPDX-License-Identifier: LicenseRef-Continuum-Proprietary
# Copyright (c) 2026 the Continuum copyright holder. All rights reserved.
"""Built-in, parameterised deterministic verifiers.

Each instance is configured by a task definition and registered under its
``verifier_id``. None of them trust model text or a tool's success response
alone: they check artifact bytes and/or authoritative service observations.
"""

from __future__ import annotations

from typing import Any

from continuum.core.canonical import digest, sha256_hex
from continuum.core.state import VerifierResult
from continuum.tools.trusted.files import normalize_csv
from continuum.verify.registry import ObservationError, Verifier, VerifierContext, VerifierOutcome

PASS, FAIL = VerifierResult.PASS, VerifierResult.FAIL


class CsvNormalized(Verifier):
    """Artifact equals the deterministic normalization of the input snapshot."""

    def __init__(self, verifier_id: str, *, source: str, output: str,
                 lowercase_columns: list[str], dedupe_on: str | None = None,
                 required_columns: list[str] | None = None, version: str = "1") -> None:
        self.verifier_id, self.version = verifier_id, version
        self.source, self.output = source, output
        self.lowercase_columns = sorted(lowercase_columns)
        self.dedupe_on = dedupe_on
        self.required_columns = required_columns or []

    def verify(self, ctx: VerifierContext) -> VerifierOutcome:
        src, out = ctx.artifact(self.source), ctx.artifact(self.output)
        if src is None:
            return VerifierOutcome(FAIL, f"source {self.source} missing")
        if out is None:
            return VerifierOutcome(FAIL, f"{self.output} not produced yet",
                                   input_artifacts={self.source: src[1]})
        header = out[0].decode("utf-8").split("\n", 1)[0].split(",")
        missing = [c for c in self.required_columns if c not in header]
        inputs = {self.source: src[1], self.output: out[1]}
        if missing:
            return VerifierOutcome(FAIL, f"missing columns {missing}", input_artifacts=inputs)
        expected = normalize_csv(src[0], self.lowercase_columns, True, self.dedupe_on)
        if sha256_hex(expected) != out[1]:
            return VerifierOutcome(FAIL, "normalized output does not match expected content",
                                   input_artifacts=inputs)
        return VerifierOutcome(PASS, "normalized CSV valid", input_artifacts=inputs,
                               evidence={"rows": expected.decode().count("\n") - 1})


class FileContains(Verifier):
    """A workspace artifact exists and contains every required substring."""

    def __init__(self, verifier_id: str, *, path: str, required: list[str],
                 version: str = "1") -> None:
        self.verifier_id, self.version = verifier_id, version
        self.path, self.required = path, required

    def verify(self, ctx: VerifierContext) -> VerifierOutcome:
        art = ctx.artifact(self.path)
        if art is None:
            return VerifierOutcome(FAIL, f"{self.path} not written yet")
        text = art[0].decode("utf-8", errors="replace")
        missing = [s for s in self.required if s not in text]
        inputs = {self.path: art[1]}
        if missing:
            return VerifierOutcome(FAIL, f"report missing {missing}", input_artifacts=inputs)
        return VerifierOutcome(PASS, "report complete", input_artifacts=inputs)


def _observe(ctx: VerifierContext, tool: str, args: dict[str, Any]) -> dict[str, Any] | None:
    try:
        return ctx.observe(tool, args)
    except ObservationError:
        return None


class StoreMatchesArtifact(Verifier):
    """The object store (authoritative) holds exactly the artifact's bytes."""

    def __init__(self, verifier_id: str, *, object_path: str, artifact: str,
                 version: str = "1") -> None:
        self.verifier_id, self.version = verifier_id, version
        self.object_path, self.artifact_path = object_path, artifact

    def verify(self, ctx: VerifierContext) -> VerifierOutcome:
        art = ctx.artifact(self.artifact_path)
        if art is None:
            return VerifierOutcome(FAIL, f"{self.artifact_path} missing")
        inputs = {self.artifact_path: art[1]}
        obs = _observe(ctx, "store.head", {"object_path": self.object_path})
        if obs is None:
            return VerifierOutcome(VerifierResult.ERROR, "store observation failed",
                                   input_artifacts=inputs)
        if not obs.get("exists"):
            return VerifierOutcome(FAIL, f"{self.object_path} not in store",
                                   input_artifacts=inputs)
        if obs.get("content_sha256") != art[1]:
            return VerifierOutcome(FAIL, "stored object differs from artifact",
                                   input_artifacts=inputs)
        return VerifierOutcome(PASS, "uploaded object matches artifact", input_artifacts=inputs,
                               external_versions={self.object_path: obs.get("version")},
                               evidence={"store": obs})


class DbRecordsMatch(Verifier):
    """Each listed record holds exactly the expected field values."""

    def __init__(self, verifier_id: str, *, expected: dict[str, dict[str, Any]],
                 version: str = "1") -> None:
        self.verifier_id, self.version = verifier_id, version
        self.expected = expected

    def verify(self, ctx: VerifierContext) -> VerifierOutcome:
        versions: dict[str, Any] = {}
        for record_id, fields in sorted(self.expected.items()):
            obs = _observe(ctx, "db.get", {"record_id": record_id})
            if obs is None:
                return VerifierOutcome(VerifierResult.ERROR, f"db read {record_id} failed")
            if not obs.get("exists") or obs.get("fields_sha256") != digest(fields):
                return VerifierOutcome(FAIL, f"record {record_id} does not match")
            versions[record_id] = obs.get("version")
        return VerifierOutcome(PASS, "records match", external_versions=versions)


class MessagesSent(Verifier):
    """Each (recipient, slot) message exists with the exact expected body."""

    def __init__(self, verifier_id: str, *, expected: list[tuple[str, str, str]],
                 version: str = "1") -> None:
        self.verifier_id, self.version = verifier_id, version
        self.expected = expected

    def verify(self, ctx: VerifierContext) -> VerifierOutcome:
        versions: dict[str, Any] = {}
        for recipient, slot, body in self.expected:
            obs = _observe(ctx, "message.query", {"recipient": recipient, "slot": slot})
            if obs is None:
                return VerifierOutcome(VerifierResult.ERROR, f"query {recipient}/{slot} failed")
            if not obs.get("exists") or obs.get("body_sha256") != sha256_hex(body):
                return VerifierOutcome(FAIL, f"message {recipient}/{slot} not sent as expected")
            versions[f"{recipient}/{slot}"] = obs.get("message_id")
        return VerifierOutcome(PASS, "messages sent", external_versions=versions)
