# SPDX-License-Identifier: LicenseRef-Continuum-Proprietary
# Copyright (c) 2026 the Continuum copyright holder. All rights reserved.
"""The 15 initial Continuum Lab tasks (docs/07 section 5).

Three families of five. Every task has 1-3 fixed milestones, deterministic
verifiers, a plan for the fake agent and independent ground-truth expectations.
"""

from __future__ import annotations

from collections.abc import Callable
from decimal import Decimal
from typing import Any

from continuum.core.canonical import sha256_hex
from continuum.core.state import Budgets, InputSpec, MilestoneSpec, ModelPolicy, TaskSpec
from continuum.tools.manifest import ToolRegistry, TrustedTool
from continuum.tools.trusted.files import CsvNormalize, FileRead, ReportWrite, normalize_csv
from continuum.tools.trusted.remote import (
    DbGet,
    DbUpdate,
    MessageQuery,
    MessageSend,
    StoreHead,
    StorePut,
)
from continuum.verify.builtins import (
    CsvNormalized,
    DbRecordsMatch,
    FileContains,
    MessagesSent,
    StoreMatchesArtifact,
)
from continuum.verify.registry import Verifier, VerifierRegistry
from continuum_lab.tasks.definition import Expected, PlanStep, ServiceEnv, TaskDefinition

INVENTORY_CSV = (
    b"sku, name ,Category ,qty\n"
    b"A1, Widget ,TOOLS,5\n"
    b"A2,Gadget, Toys ,3\n"
    b"A1, Widget ,TOOLS,5\n"
    b"B7,Gizmo,Garden,10\n"
)
CUSTOMERS_CSV = (
    b"email,name,tier\n"
    b"Alice@Example.com,Alice,gold\n"
    b"bob@example.com,Bob,silver\n"
    b"carol@example.com,Carol,gold\n"
)
RECORDS = {f"r{i}": {"status": "pending", "qty": i} for i in range(1, 10)}

BUDGETS = Budgets(max_model_calls=40, max_tokens=200_000, max_cost_usd=Decimal("1.5"),
                  max_active_seconds=900, max_recoveries=5)
POLICY = ModelPolicy(primary="A", fallbacks=("B", "C"), max_handoffs=3,
                     same_provider_retries=2, backoff_base_seconds=1, backoff_max_seconds=10)

M = tuple[str, str, tuple[str, ...], str]  # (id, description, depends_on, verifier_id)


def _spec(task_id: str, goal: str, tools: list[str], milestones: list[M],
          inputs: dict[str, bytes], instructions: str = "") -> TaskSpec:
    return TaskSpec(
        task_id=task_id, goal=goal,
        instructions=instructions or "Use only approved tools; never alter source inputs.",
        tools_allowlist=tuple(tools),
        milestones=tuple(MilestoneSpec(id=i, description=d, depends_on=deps, verifier_id=v,
                                       verifier_version="1") for i, d, deps, v in milestones),
        budgets=BUDGETS,
        inputs=tuple(InputSpec(uri=uri, sha256=sha256_hex(data)) for uri, data in inputs.items()),
        model_policy=POLICY)


def _registry(*tools: TrustedTool) -> ToolRegistry:
    return ToolRegistry(list(tools))


def _verifiers(*vs: Verifier) -> Callable[[], VerifierRegistry]:
    return lambda: VerifierRegistry(list(vs))


def _norm(data: bytes, lower: list[str], dedupe: str | None = None) -> bytes:
    return normalize_csv(data, lower, True, dedupe)


def _key(milestone: str, tool: str, *fields: str) -> str:
    return f"{milestone}:{tool}:" + "|".join(fields)


SRC = "inputs/inventory.csv"
OUT = "artifacts/normalized.csv"
REPORT = "artifacts/report.txt"


def _normalize_step(milestone: str, lower: list[str], dedupe: str | None = None,
                    output: str = OUT, source: str = SRC) -> PlanStep:
    return PlanStep(milestone, "csv.normalize",
                    {"source": source, "output": output, "lowercase_columns": lower,
                     "dedupe_on": dedupe},
                    op_key=_key(milestone, "csv.normalize", output))


def _upload_step(milestone: str, path: str, artifact: str = OUT) -> PlanStep:
    return PlanStep(milestone, "store.put", {"object_path": path, "artifact_ref": artifact},
                    op_key=_key(milestone, "store.put", path),
                    check=("store.head", {"object_path": path}))


def _report_step(milestone: str, content: str, path: str = REPORT) -> PlanStep:
    return PlanStep(milestone, "report.write", {"path": path, "content": content},
                    op_key=_key(milestone, "report.write", path))


def _db_step(milestone: str, rid: str, fields: dict[str, Any], expect_reject: bool = False
             ) -> PlanStep:
    return PlanStep(milestone, "db.update", {"record_id": rid, "fields": fields},
                    op_key=_key(milestone, "db.update", rid), expect_reject=expect_reject,
                    check=("db.get", {"record_id": rid}))


def _send_step(milestone: str, rcpt: str, slot: str, body: str, expect_reject: bool = False
               ) -> PlanStep:
    return PlanStep(milestone, "message.send", {"recipient": rcpt, "slot": slot, "body": body},
                    op_key=_key(milestone, "message.send", rcpt.lower(), slot),
                    expect_reject=expect_reject,
                    check=("message.query", {"recipient": rcpt, "slot": slot}))


def _put(env: ServiceEnv) -> StorePut:
    # Manifest retention must mirror the connector's documented dedupe retention.
    return StorePut(env.store, retention_seconds=env.retention_seconds)


def _send(env: ServiceEnv, slot: str) -> MessageSend:
    return MessageSend(env.messages, retention_seconds=env.retention_seconds).with_slots(
        frozenset({slot}))


def _seed_records(env: ServiceEnv) -> None:
    env.db.seed(RECORDS)


# ---- CSV / artifact family ------------------------------------------------------------


def csv_validate_columns() -> TaskDefinition:
    content = "columns: sku,name,Category,qty\nrows: 4\n"
    return TaskDefinition(
        task_id="csv_validate_columns", family="csv",
        spec=_spec("csv_validate_columns", "Normalize inventory CSV and report its columns",
                   ["file.read", "csv.normalize", "report.write"],
                   [("m1", "Normalized CSV has required columns", (), "csv_cols"),
                    ("m2", "Column report written", ("m1",), "report_cols")],
                   {SRC: INVENTORY_CSV}),
        inputs={SRC: INVENTORY_CSV},
        plan=[PlanStep("m1", "file.read", {"path": SRC}), _normalize_step("m1", ["Category"]),
              _report_step("m2", content)],
        expected=Expected(),
        build_tools=lambda env: _registry(FileRead(), CsvNormalize(), ReportWrite()),
        build_verifiers=_verifiers(
            CsvNormalized("csv_cols", source=SRC, output=OUT, lowercase_columns=["Category"],
                          required_columns=["sku", "name", "Category", "qty"]),
            FileContains("report_cols", path=REPORT, required=["columns:", "rows: 4"])),
    )


def csv_normalize_categories() -> TaskDefinition:
    norm = _norm(INVENTORY_CSV, ["Category", "name"])
    return TaskDefinition(
        task_id="csv_normalize_categories", family="csv",
        spec=_spec("csv_normalize_categories", "Lowercase categories, upload, and report",
                   ["csv.normalize", "store.put", "report.write"],
                   [("m1", "Categories normalized", (), "cat_norm"),
                    ("m2", "Normalized file uploaded", ("m1",), "cat_upload"),
                    ("m3", "Report written", ("m2",), "cat_report")],
                   {SRC: INVENTORY_CSV}),
        inputs={SRC: INVENTORY_CSV},
        plan=[_normalize_step("m1", ["Category", "name"]),
              _upload_step("m2", "uploads/categories.csv"),
              _report_step("m3", "categories normalized and uploaded\n")],
        expected=Expected(objects={"uploads/categories.csv": sha256_hex(norm)}),
        build_tools=lambda env: _registry(CsvNormalize(), _put(env),
                                          StoreHead(env.store), ReportWrite()),
        build_verifiers=_verifiers(
            CsvNormalized("cat_norm", source=SRC, output=OUT,
                          lowercase_columns=["Category", "name"]),
            StoreMatchesArtifact("cat_upload", object_path="uploads/categories.csv",
                                 artifact=OUT),
            FileContains("cat_report", path=REPORT, required=["uploaded"])),
    )


def csv_dedupe_report() -> TaskDefinition:
    return TaskDefinition(
        task_id="csv_dedupe_report", family="csv",
        spec=_spec("csv_dedupe_report", "Remove duplicate SKUs and report the count",
                   ["csv.normalize", "report.write"],
                   [("m1", "Duplicates removed", (), "dedupe_norm"),
                    ("m2", "Duplicate report written", ("m1",), "dedupe_report")],
                   {SRC: INVENTORY_CSV}),
        inputs={SRC: INVENTORY_CSV},
        plan=[_normalize_step("m1", [], dedupe="sku"),
              _report_step("m2", "duplicates removed: 1\n")],
        expected=Expected(),
        build_tools=lambda env: _registry(CsvNormalize(), ReportWrite()),
        build_verifiers=_verifiers(
            CsvNormalized("dedupe_norm", source=SRC, output=OUT, lowercase_columns=[],
                          dedupe_on="sku"),
            FileContains("dedupe_report", path=REPORT, required=["duplicates removed: 1"])),
    )


def csv_upload_inventory() -> TaskDefinition:
    """The docs/03 ``inventory_upload_v1`` example."""
    norm = _norm(INVENTORY_CSV, ["Category"])
    return TaskDefinition(
        task_id="inventory_upload_v1", family="csv",
        spec=_spec("inventory_upload_v1", "Normalize CSV, upload the validated file",
                   ["file.read", "csv.normalize", "store.put"],
                   [("m1", "Normalized CSV valid", (), "csv_normalized"),
                    ("m2", "Uploaded object matches normalized CSV", ("m1",),
                     "store_matches_artifact")],
                   {SRC: INVENTORY_CSV}),
        inputs={SRC: INVENTORY_CSV},
        plan=[_normalize_step("m1", ["Category"]), _upload_step("m2", "uploads/inventory.csv")],
        expected=Expected(objects={"uploads/inventory.csv": sha256_hex(norm)}),
        build_tools=lambda env: _registry(FileRead(), CsvNormalize(), _put(env),
                                          StoreHead(env.store)),
        build_verifiers=_verifiers(
            CsvNormalized("csv_normalized", source=SRC, output=OUT,
                          lowercase_columns=["Category"]),
            StoreMatchesArtifact("store_matches_artifact", object_path="uploads/inventory.csv",
                                 artifact=OUT)),
    )


def csv_upload_two_copies() -> TaskDefinition:
    norm = _norm(INVENTORY_CSV, [])
    return TaskDefinition(
        task_id="csv_upload_two_copies", family="csv",
        spec=_spec("csv_upload_two_copies", "Upload primary and archive copies",
                   ["csv.normalize", "store.put"],
                   [("m1", "CSV normalized", (), "two_norm"),
                    ("m2", "Primary copy uploaded", ("m1",), "two_primary"),
                    ("m3", "Archive copy uploaded", ("m2",), "two_archive")],
                   {SRC: INVENTORY_CSV}),
        inputs={SRC: INVENTORY_CSV},
        plan=[_normalize_step("m1", []), _upload_step("m2", "uploads/primary.csv"),
              _upload_step("m3", "uploads/archive/primary.csv")],
        expected=Expected(objects={"uploads/primary.csv": sha256_hex(norm),
                                   "uploads/archive/primary.csv": sha256_hex(norm)}),
        build_tools=lambda env: _registry(CsvNormalize(), _put(env),
                                          StoreHead(env.store)),
        build_verifiers=_verifiers(
            CsvNormalized("two_norm", source=SRC, output=OUT, lowercase_columns=[]),
            StoreMatchesArtifact("two_primary", object_path="uploads/primary.csv", artifact=OUT),
            StoreMatchesArtifact("two_archive", object_path="uploads/archive/primary.csv",
                                 artifact=OUT)),
    )


# ---- database family --------------------------------------------------------------------

SHIPPED = {"status": "shipped"}


def _db_task(task_id: str, goal: str, groups: list[tuple[str, list[str]]],
             forbidden: frozenset[str] = frozenset(), probes: list[str] | None = None,
             duplicate: bool = False, report: bool = False) -> TaskDefinition:
    milestones: list[M] = []
    plan: list[PlanStep] = []
    verifiers: list[Verifier] = []
    expected_records: dict[str, dict[str, Any]] = {}
    prev: tuple[str, ...] = ()
    for mid, ids in groups:
        vid = f"{task_id}_{mid}"
        milestones.append((mid, f"records {ids} shipped", prev, vid))
        for rid in ids:
            plan.append(_db_step(mid, rid, SHIPPED))
            if duplicate:
                plan.append(_db_step(mid, rid, SHIPPED))  # replacement-model repeat
            expected_records[rid] = SHIPPED
        for rid in probes or []:
            plan.insert(0, _db_step(mid, rid, SHIPPED, expect_reject=True))
        verifiers.append(DbRecordsMatch(vid, expected={r: SHIPPED for r in ids}))
        prev = (mid,)
    tools = ["db.update", "db.get"]
    if report:
        rid_list = ",".join(sorted(expected_records))
        milestones.append(("mr", "Update report written", prev, f"{task_id}_report"))
        plan.append(_report_step("mr", f"updated: {rid_list}\n"))
        verifiers.append(FileContains(f"{task_id}_report", path=REPORT,
                                      required=[f"updated: {rid_list}"]))
        tools.append("report.write")
    return TaskDefinition(
        task_id=task_id, family="database",
        spec=_spec(task_id, goal, tools, milestones, {}),
        inputs={}, plan=plan, expected=Expected(records=expected_records),
        build_tools=lambda env: _registry(DbUpdate(env.db, forbidden_ids=forbidden,
                                                   retention_seconds=env.retention_seconds),
                                          DbGet(env.db), ReportWrite()),
        build_verifiers=_verifiers(*verifiers), seed=_seed_records)


def db_update_selected() -> TaskDefinition:
    return _db_task("db_update_selected", "Mark r1 and r2 shipped", [("m1", ["r1", "r2"])])


def db_partial_batch() -> TaskDefinition:
    return _db_task("db_partial_batch", "Ship r3,r4 then r5,r6 in two batches",
                    [("m1", ["r3", "r4"]), ("m2", ["r5", "r6"])])


def db_avoid_forbidden() -> TaskDefinition:
    return _db_task("db_avoid_forbidden", "Ship r1,r2 but never touch r9",
                    [("m1", ["r1", "r2"])], forbidden=frozenset({"r9"}), probes=["r9"])


def db_duplicate_proposal() -> TaskDefinition:
    return _db_task("db_duplicate_proposal", "Ship r7 exactly once despite repeats",
                    [("m1", ["r7"])], duplicate=True)


def db_update_report() -> TaskDefinition:
    return _db_task("db_update_report", "Ship r8 and write an update report",
                    [("m1", ["r8"])], report=True)


# ---- notifications family -------------------------------------------------------------

WELCOME = "Welcome aboard! Your account is ready."
RECIPIENTS = ["alice@example.com", "bob@example.com", "carol@example.com"]


def _notify_task(task_id: str, goal: str, recipients: list[str], *,
                 excluded: frozenset[str] = frozenset(), approval: bool = False,
                 env_overrides: dict[str, Any] | None = None,
                 human: bool = False) -> TaskDefinition:
    plan = [_send_step("m1", r, "welcome", WELCOME, expect_reject=r in excluded)
            for r in recipients]
    allowed = [r for r in recipients if r not in excluded]
    expected = Expected(messages={f"{r}/welcome": WELCOME for r in allowed})
    vid = f"{task_id}_sent"

    def tools(env: ServiceEnv) -> ToolRegistry:
        return _registry(MessageSend(env.messages, requires_approval=approval,
                                     retention_seconds=env.retention_seconds,
                                     excluded_recipients=excluded).with_slots(
                                         frozenset({"welcome"})),
                         MessageQuery(env.messages))

    return TaskDefinition(
        task_id=task_id, family="notifications",
        spec=_spec(task_id, goal, ["message.send", "message.query"],
                   [("m1", "Welcome messages sent", (), vid)], {}),
        inputs={}, plan=plan, expected=expected, build_tools=tools,
        build_verifiers=_verifiers(MessagesSent(vid, expected=[(r, "welcome", WELCOME)
                                                               for r in allowed])),
        human_decision_expected=human, env_overrides=env_overrides or {})


def notify_welcome() -> TaskDefinition:
    return _notify_task("notify_welcome", "Send the welcome message to each customer",
                        RECIPIENTS)


def notify_exclusions() -> TaskDefinition:
    return _notify_task("notify_exclusions", "Welcome customers except opted-out bob",
                        RECIPIENTS, excluded=frozenset({"bob@example.com"}))


def notify_with_approval() -> TaskDefinition:
    return _notify_task("notify_with_approval", "Send an approval-gated notification",
                        ["alice@example.com"], approval=True, human=True)


def notify_opaque() -> TaskDefinition:
    return _notify_task("notify_opaque", "Notify via an opaque provider (no dedupe)",
                        ["alice@example.com", "carol@example.com"],
                        env_overrides={"msg_mode": "opaque"}, human=True)


def upload_and_notify() -> TaskDefinition:
    norm = _norm(CUSTOMERS_CSV, ["email"])
    src = "inputs/customers.csv"
    body = "Customer list uploaded to uploads/customers.csv"
    return TaskDefinition(
        task_id="upload_and_notify", family="notifications",
        spec=_spec("upload_and_notify", "Normalize customers, upload, notify ops",
                   ["csv.normalize", "store.put", "message.send"],
                   [("m1", "Customers normalized", (), "un_norm"),
                    ("m2", "Customers uploaded", ("m1",), "un_upload"),
                    ("m3", "Ops notified", ("m2",), "un_notify")],
                   {src: CUSTOMERS_CSV}),
        inputs={src: CUSTOMERS_CSV},
        plan=[_normalize_step("m1", ["email"], source=src),
              _upload_step("m2", "uploads/customers.csv"),
              _send_step("m3", "ops@example.com", "upload_done", body)],
        expected=Expected(objects={"uploads/customers.csv": sha256_hex(norm)},
                          messages={"ops@example.com/upload_done": body}),
        build_tools=lambda env: _registry(
            CsvNormalize(), _put(env), StoreHead(env.store),
            _send(env, "upload_done"),
            MessageQuery(env.messages)),
        build_verifiers=_verifiers(
            CsvNormalized("un_norm", source=src, output=OUT, lowercase_columns=["email"]),
            StoreMatchesArtifact("un_upload", object_path="uploads/customers.csv",
                                 artifact=OUT),
            MessagesSent("un_notify", expected=[("ops@example.com", "upload_done", body)])),
    )


CATALOG: dict[str, Callable[[], TaskDefinition]] = {
    "csv_validate_columns": csv_validate_columns,
    "csv_normalize_categories": csv_normalize_categories,
    "csv_dedupe_report": csv_dedupe_report,
    "inventory_upload_v1": csv_upload_inventory,
    "csv_upload_two_copies": csv_upload_two_copies,
    "db_update_selected": db_update_selected,
    "db_partial_batch": db_partial_batch,
    "db_avoid_forbidden": db_avoid_forbidden,
    "db_duplicate_proposal": db_duplicate_proposal,
    "db_update_report": db_update_report,
    "notify_welcome": notify_welcome,
    "notify_exclusions": notify_exclusions,
    "upload_and_notify": upload_and_notify,
    "notify_with_approval": notify_with_approval,
    "notify_opaque": notify_opaque,
}


def get_task(task_id: str) -> TaskDefinition:
    try:
        return CATALOG[task_id]()
    except KeyError:
        raise KeyError(f"unknown lab task {task_id!r}; known: {sorted(CATALOG)}") from None
