"""Local workspace tools: ``file.read``, ``csv.normalize``, ``report.write``.

Local writes are atomic and deterministic for identical bound arguments, so a
same-action retry is naturally idempotent with no retention limit.
"""

from __future__ import annotations

import csv
import io
from typing import Any

from continuum.core.canonical import sha256_hex
from continuum.core.errors import PathContainmentError
from continuum.tools.manifest import (
    BindContext,
    ToolCleanFailure,
    ToolContext,
    ToolManifest,
    ToolResult,
    TrustedTool,
)

MAX_READ_CHARS = 20_000


def _local_write_manifest(name: str, description: str, schema: dict[str, Any],
                          identity: list[str], path_fields: list[str]) -> ToolManifest:
    return ToolManifest.model_validate({
        "name": name, "version": "1", "description": description, "effect": "write",
        "args_schema": schema,
        "operation_identity": {"milestone_scoped": True, "fields": identity},
        "idempotency": {"supported": True, "key_location": "none", "retention_seconds": None},
        "reconciliation": {"method": "none"},
        "permissions": {"allowed_path_prefix": "artifacts/", "path_fields": path_fields},
        "timeout_seconds": 10,
    })


class FileRead(TrustedTool):
    manifest = ToolManifest.model_validate({
        "name": "file.read", "version": "1", "effect": "read",
        "description": "Read a UTF-8 text file from the run workspace (inputs/ or artifacts/).",
        "args_schema": {"type": "object", "required": ["path"], "additionalProperties": False,
                        "properties": {"path": {"type": "string", "maxLength": 256}}},
        "reconciliation": {"method": "none"},
        "permissions": {"path_fields": ["path"]},
    })

    def invoke(self, args: dict[str, Any], ctx: ToolContext) -> ToolResult:
        try:
            data = ctx.workspace.read_bytes(args["path"])
        except (FileNotFoundError, PathContainmentError) as exc:
            raise ToolCleanFailure("not_found", str(exc)) from exc
        text = data.decode("utf-8", errors="replace")
        return ToolResult({"path": args["path"], "sha256": sha256_hex(data),
                           "content": text[:MAX_READ_CHARS],
                           "truncated": len(text) > MAX_READ_CHARS})


def normalize_csv(source: bytes, lowercase_columns: list[str], strip: bool,
                  dedupe_on: str | None) -> bytes:
    """Deterministic CSV normalization shared by the tool and its verifier."""
    reader = csv.DictReader(io.StringIO(source.decode("utf-8")))
    fieldnames = [f.strip() for f in (reader.fieldnames or [])]
    out = io.StringIO()
    writer = csv.DictWriter(out, fieldnames=fieldnames, lineterminator="\n")
    writer.writeheader()
    seen: set[str] = set()
    for raw in reader:
        row = {k.strip(): (v or "") for k, v in raw.items() if k is not None}
        if strip:
            row = {k: v.strip() for k, v in row.items()}
        for col in lowercase_columns:
            if col in row:
                row[col] = row[col].lower()
        if dedupe_on:
            key = row.get(dedupe_on, "")
            if key in seen:
                continue
            seen.add(key)
        writer.writerow(row)
    return out.getvalue().encode("utf-8")


class CsvNormalize(TrustedTool):
    manifest = _local_write_manifest(
        "csv.normalize",
        "Normalize a CSV (strip whitespace, lowercase columns, optional dedupe) into artifacts/.",
        {"type": "object", "required": ["source", "output"], "additionalProperties": False,
         "properties": {
             "source": {"type": "string", "maxLength": 256},
             "output": {"type": "string", "maxLength": 256},
             "lowercase_columns": {"type": "array", "items": {"type": "string"}, "default": []},
             "strip": {"type": "boolean", "default": True},
             "dedupe_on": {"type": ["string", "null"], "default": None}}},
        identity=["output"], path_fields=["output"])

    def bind(self, args: dict[str, Any], ctx: BindContext) -> dict[str, Any]:
        source_sha = ctx.workspace.hash(args["source"])
        if source_sha is None:
            raise ToolCleanFailure("source_missing", f"{args['source']} does not exist")
        return {"source": args["source"], "output": args["output"],
                "lowercase_columns": sorted(args.get("lowercase_columns", [])),
                "strip": args.get("strip", True), "dedupe_on": args.get("dedupe_on"),
                "source_sha256": source_sha}

    def invoke(self, args: dict[str, Any], ctx: ToolContext) -> ToolResult:
        source = ctx.workspace.read_bytes(args["source"])
        if sha256_hex(source) != args["source_sha256"]:
            raise ToolCleanFailure("source_changed", "source changed since authorization")
        data = normalize_csv(source, args["lowercase_columns"], args["strip"], args["dedupe_on"])
        sha = ctx.workspace.write_bytes(args["output"], data)
        rows = max(data.decode().count("\n") - 1, 0)
        return ToolResult({"output": args["output"], "sha256": sha, "rows": rows},
                          artifacts=[args["output"]])


class ReportWrite(TrustedTool):
    manifest = _local_write_manifest(
        "report.write", "Write a UTF-8 text report into artifacts/.",
        {"type": "object", "required": ["path", "content"], "additionalProperties": False,
         "properties": {"path": {"type": "string", "maxLength": 256},
                        "content": {"type": "string", "maxLength": 100_000}}},
        identity=["path"], path_fields=["path"])

    def invoke(self, args: dict[str, Any], ctx: ToolContext) -> ToolResult:
        sha = ctx.workspace.write_bytes(args["path"], args["content"].encode("utf-8"))
        return ToolResult({"path": args["path"], "sha256": sha}, artifacts=[args["path"]])
