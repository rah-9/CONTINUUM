"""Remote-effect tools backed by service clients: object store, database, messages.

Each write tool's manifest mirrors the connector's *actual* contract mode:

* ``deduped``    - idempotency key suppresses repeat commits within retention;
                   authoritative positive query available.
* ``observable`` - no dedupe; authoritative positive query; negative reads may lag.
* ``opaque``     - no dedupe and no trustworthy query: ambiguity needs a human.
"""

from __future__ import annotations

from typing import Any

from continuum.core.canonical import digest, sha256_hex
from continuum.tools.manifest import (
    BindContext,
    ToolAmbiguousFailure,
    ToolCleanFailure,
    ToolContext,
    ToolManifest,
    ToolResult,
    TrustedTool,
)
from continuum.tools.trusted.services import (
    DatabaseClient,
    MessageClient,
    ObjectStoreClient,
    ServiceMode,
    ServiceRejected,
    ServiceTimeout,
)


def _contract(mode: ServiceMode, retention_seconds: int | None, check_tool: str,
              check_args: list[str], positive_proof: list[str]) -> dict[str, Any]:
    if mode == "deduped":
        return {
            "idempotency": {"supported": True, "key_location": "header",
                            "retention_seconds": retention_seconds},
            "reconciliation": {"method": "authoritative_query", "check_tool": check_tool,
                               "check_args": check_args, "positive_proof": positive_proof,
                               "negative_proof_is_safe": False}}
    if mode == "observable":
        return {
            "idempotency": {"supported": False},
            "reconciliation": {"method": "authoritative_query", "check_tool": check_tool,
                               "check_args": check_args, "positive_proof": positive_proof,
                               "negative_proof_is_safe": False}}
    return {"idempotency": {"supported": False}, "reconciliation": {"method": "opaque"}}


def _call(fn: Any, *args: Any, **kwargs: Any) -> dict[str, Any]:
    try:
        result: dict[str, Any] = fn(*args, **kwargs)
        return result
    except ServiceRejected as exc:
        raise ToolCleanFailure(exc.code, str(exc)) from exc
    except ServiceTimeout as exc:
        raise ToolAmbiguousFailure("timeout", str(exc) or "no response") from exc


# ---- object store -----------------------------------------------------------------


class StorePut(TrustedTool):
    def __init__(self, client: ObjectStoreClient, *, retention_seconds: int | None = 86_400,
                 requires_approval: bool = False) -> None:
        self.client = client
        self.manifest = ToolManifest.model_validate({
            "name": "store.put", "version": "1", "effect": "write",
            "description": "Upload a workspace artifact to the object store at object_path.",
            "args_schema": {
                "type": "object", "required": ["object_path", "artifact_ref"],
                "additionalProperties": False,
                "properties": {"object_path": {"type": "string", "maxLength": 256},
                               "artifact_ref": {"type": "string", "maxLength": 256}}},
            "operation_identity": {"milestone_scoped": True, "fields": ["object_path"]},
            **_contract(client.mode, retention_seconds, "store.head", ["object_path"],
                        ["object_path", "content_sha256"]),
            "permissions": {"requires_approval": requires_approval,
                            "allowed_path_prefix": "uploads/", "path_fields": ["object_path"]},
            "timeout_seconds": 20,
        })

    def bind(self, args: dict[str, Any], ctx: BindContext) -> dict[str, Any]:
        sha = ctx.workspace.hash(args["artifact_ref"])
        if sha is None:
            raise ToolCleanFailure("artifact_missing", f"{args['artifact_ref']} does not exist")
        # Same path + different content => different args digest => intent_conflict.
        return {"object_path": args["object_path"], "artifact_ref": args["artifact_ref"],
                "content_sha256": sha}

    def normalize_identity(self, field_name: str, value: Any) -> str:
        return str(value).strip().strip("/")

    def invoke(self, args: dict[str, Any], ctx: ToolContext) -> ToolResult:
        content = ctx.workspace.read_bytes(args["artifact_ref"])
        if sha256_hex(content) != args["content_sha256"]:
            raise ToolCleanFailure("artifact_changed", "artifact bytes changed since binding")
        receipt = _call(self.client.put, args["object_path"], content,
                        idempotency_key=ctx.idempotency_key)
        return ToolResult(receipt)


class StoreHead(TrustedTool):
    def __init__(self, client: ObjectStoreClient) -> None:
        self.client = client
        self.manifest = ToolManifest.model_validate({
            "name": "store.head", "version": "1", "effect": "read",
            "description": "Look up object metadata (exists, content_sha256) by object_path.",
            "args_schema": {"type": "object", "required": ["object_path"],
                            "additionalProperties": False,
                            "properties": {"object_path": {"type": "string",
                                                           "maxLength": 256}}},
            "reconciliation": {"method": "none"},
        })

    def invoke(self, args: dict[str, Any], ctx: ToolContext) -> ToolResult:
        return ToolResult(_call(self.client.head, args["object_path"]))


# ---- database ----------------------------------------------------------------------


class DbUpdate(TrustedTool):
    def __init__(self, client: DatabaseClient, *, retention_seconds: int | None = 86_400,
                 forbidden_ids: frozenset[str] = frozenset()) -> None:
        self.client = client
        self.forbidden_ids = forbidden_ids
        self.manifest = ToolManifest.model_validate({
            "name": "db.update", "version": "1", "effect": "write",
            "description": "Set fields on one database record (by record_id).",
            "args_schema": {
                "type": "object", "required": ["record_id", "fields"],
                "additionalProperties": False,
                "properties": {"record_id": {"type": "string", "maxLength": 64},
                               "fields": {"type": "object", "maxProperties": 20}}},
            "operation_identity": {"milestone_scoped": True, "fields": ["record_id"]},
            **_contract(client.mode, retention_seconds, "db.get", ["record_id"],
                        ["record_id", "fields_sha256"]),
            "timeout_seconds": 20,
        })

    def bind(self, args: dict[str, Any], ctx: BindContext) -> dict[str, Any]:
        if args["record_id"] in self.forbidden_ids:
            raise ToolCleanFailure("forbidden_record", f"{args['record_id']} may not be changed")
        return {"record_id": args["record_id"], "fields": args["fields"],
                "fields_sha256": digest(args["fields"])}

    def invoke(self, args: dict[str, Any], ctx: ToolContext) -> ToolResult:
        return ToolResult(_call(self.client.update, args["record_id"], args["fields"],
                                idempotency_key=ctx.idempotency_key))


class DbGet(TrustedTool):
    def __init__(self, client: DatabaseClient) -> None:
        self.client = client
        self.manifest = ToolManifest.model_validate({
            "name": "db.get", "version": "1", "effect": "read",
            "description": "Read one database record by record_id.",
            "args_schema": {"type": "object", "required": ["record_id"],
                            "additionalProperties": False,
                            "properties": {"record_id": {"type": "string", "maxLength": 64}}},
            "reconciliation": {"method": "none"},
        })

    def invoke(self, args: dict[str, Any], ctx: ToolContext) -> ToolResult:
        return ToolResult(_call(self.client.get, args["record_id"]))


# ---- messages ----------------------------------------------------------------------


class MessageSend(TrustedTool):
    def __init__(self, client: MessageClient, *, retention_seconds: int | None = 86_400,
                 requires_approval: bool = False,
                 excluded_recipients: frozenset[str] = frozenset()) -> None:
        self.client = client
        self.excluded = excluded_recipients
        self.manifest = ToolManifest.model_validate({
            "name": "message.send", "version": "1", "effect": "write",
            "description": "Send a message to a recipient in an approved message slot.",
            "args_schema": {
                "type": "object", "required": ["recipient", "slot", "body"],
                "additionalProperties": False,
                "properties": {"recipient": {"type": "string", "maxLength": 128},
                               "slot": {"type": "string", "maxLength": 64},
                               "body": {"type": "string", "maxLength": 4000}}},
            # recipient + task-defined slot: the model cannot invent a new slot
            # to bypass an existing action (slots are validated in bind).
            "operation_identity": {"milestone_scoped": True, "fields": ["recipient", "slot"]},
            **_contract(client.mode, retention_seconds, "message.query",
                        ["recipient", "slot"], ["recipient", "slot", "body_sha256"]),
            "permissions": {"requires_approval": requires_approval},
            "timeout_seconds": 20,
        })
        self.allowed_slots: frozenset[str] | None = None

    def with_slots(self, slots: frozenset[str]) -> MessageSend:
        self.allowed_slots = slots
        return self

    def normalize_identity(self, field_name: str, value: Any) -> str:
        return str(value).strip().lower()

    def bind(self, args: dict[str, Any], ctx: BindContext) -> dict[str, Any]:
        recipient = args["recipient"].strip().lower()
        if recipient in self.excluded:
            raise ToolCleanFailure("excluded_recipient", f"{recipient} is on the exclusion list")
        if self.allowed_slots is not None and args["slot"] not in self.allowed_slots:
            raise ToolCleanFailure("unknown_slot", f"slot {args['slot']!r} is not task-defined")
        return {"recipient": recipient, "slot": args["slot"], "body": args["body"],
                "body_sha256": sha256_hex(args["body"])}

    def invoke(self, args: dict[str, Any], ctx: ToolContext) -> ToolResult:
        return ToolResult(_call(self.client.send, args["recipient"], args["slot"], args["body"],
                                idempotency_key=ctx.idempotency_key))


class MessageQuery(TrustedTool):
    def __init__(self, client: MessageClient) -> None:
        self.client = client
        self.manifest = ToolManifest.model_validate({
            "name": "message.query", "version": "1", "effect": "read",
            "description": "Query whether a message was sent to recipient in slot.",
            "args_schema": {"type": "object", "required": ["recipient", "slot"],
                            "additionalProperties": False,
                            "properties": {"recipient": {"type": "string", "maxLength": 128},
                                           "slot": {"type": "string", "maxLength": 64}}},
            "reconciliation": {"method": "none"},
        })

    def invoke(self, args: dict[str, Any], ctx: ToolContext) -> ToolResult:
        return ToolResult(_call(self.client.query, args["recipient"].strip().lower(),
                                args["slot"]))
