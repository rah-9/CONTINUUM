# Event log, action identity and recovery protocol

This is the **normative document for all side-effecting actions**. Avoid interpreting “idempotent,” “queryable,” or “reversible” as proof of exactly-once execution.

## 1. Event storage

Each committed event has `{schema_version, event_id, run_id, seq, type, occurred_at, actor, payload}`. `(run_id, seq)` is assigned under a SQLite write transaction. Append event + update projection **atomically** as specified in 03. Hash chains and a distributed event bus are **not** required in v1. Never log raw secrets or oversized provider responses; store safe redacted references instead. `ModelCallFinished` records a reference to the sanitized **normalized model response** (including tool proposals), while attempt events reference sanitized tool results; this makes diagnostic replay possible without new provider or tool calls.

**Events used in v1** (payloads include IDs, evidence refs, reason and version as applicable):

- Run: `RunCreated`, `RunStarted`, `RunStatusChanged`, `RunCompleted`, `RunFailed`, `RunCancelled`.
- Model: `ModelSessionStarted`, `ModelCallStarted`, `ModelCallFinished`, `ModelCallFailed`, `ModelSwitched`, `ModelSessionEnded`.
- Action: `ActionProposed`, `ActionProposalDeduped`, `ActionRejected`, `ApprovalRequested`, `ApprovalDecided`, `ActionAuthorized`, `ActionAttemptStarted`, `ActionAttemptConfirmed`, `ActionAttemptFailedClean`, `ActionAttemptAmbiguous`, `ActionReconciled`, `ActionNeedsHuman`, `ActionSkipped`.
- Verification/artifact: `ArtifactRecorded`, `ArtifactDrift`, `VerificationStarted`, `MilestoneVerified`, `MilestoneFailed`, `MilestoneInvalidated`, `VerifierError`.
- Runtime: `RecoveryStarted`, `RunActiveTimeRecorded`, `BudgetExceeded`, `FaultInjected` (the latter only in test configurations).

Ensure events encode enough values to recreate all durable business projections and exact historical timestamps. Usage/cost events record provider reports **and whether values are estimated or unknown**.

## 2. Logical action identity — most important rule

A **logical action** is one intended business operation; an **attempt** is one physical dispatch. They are not the same entity.

1. The trusted tool manifest defines `operation_key_fields` (e.g. destination object key, record ID, recipient + approved message slot). The runtime derives `operation_key` from `run_id`, `milestone_id`, manifest/tool identity and those canonical fields. It never trusts the provider's tool-call ID or a free-form model `step_index`.
2. In one DB transaction, `UNIQUE(run_id, operation_key)` finds or creates the immutable `action_id`. Same identity and same args digest -> reuse action state/result. Same identity and **different** args digest -> `intent_conflict`, no dispatch.
3. An explicit, trusted task-defined slot/version distinguishes two intentionally distinct operations with otherwise identical arguments. The model cannot create a new slot spontaneously.
4. The idempotency key (if supported) is `sha256("continuum:v1|" + run_id + "|" + action_id)`; it is bound to immutable args and stable across provider changes and attempts. Persist it at authorization, rather than recompute from a model proposal.
5. A confirmed existing action gives the new model the recorded outcome/reference; it is **never re-sent** just because another model proposes it again.

This prevents exact operation-identity duplicates, **not** arbitrary semantic duplicates if task/manifests identify intent incorrectly. Connector contracts must be tested.

**Read-only exception:** each fresh read (including an authorized verifier or reconciler query) gets a runtime-generated, unique read-action identity and a logged attempt. It may be repeated after interruption; do **not** return cached old read results by the write-action deduplication path. Read permissions and provenance still go through the gateway.

## 3. State machines

```text
logical action:
 proposed -> rejected
 proposed -> awaiting_approval -> rejected | authorized
 proposed -> authorized -> in_flight
 in_flight -> confirmed | failed_clean | unknown
 failed_clean -> authorized                  (retryable, effect guaranteed absent)
 unknown -> confirmed                       (authoritative positive check / dedupe ack)
 unknown -> authorized                      (ONLY proven-safe absence + recorded evidence)
 unknown -> in_flight                       (ONLY documented idempotent same-key retry)
 unknown -> needs_human -> confirmed | authorized | skipped

physical attempt:
 started -> confirmed | failed_clean | ambiguous
```

Every `in_flight` state must have a committed `ActionAttemptStarted` event with an `(action_id, attempt_no, owner_epoch)` row before invoking a tool. **If a process dies before the outcome event**, mark the attempt ambiguous and the action unknown at takeover, even if it may not actually have been sent. Never classify an unknown write as `failed_clean` based on a client-side timeout.

`unknown -> authorized` requires positive **proof of safe non-commit**, not merely a eventually consistent `NOT FOUND`. `needs_human -> authorized` for an at-risk retry is an **explicit operator override** logged with actor/reason; it is excluded from any automated exactly-once claim. A human claim of success still requires the milestone verifier.

`confirmed` is terminal as an action outcome. A human `skipped` action does not imply its required milestone can complete.

## 4. Dispatch algorithm (must be implemented atomically where indicated)

```text
propose(tool, args, milestone):
  validate manifest/schema/permission and derive trusted operation_key
  TX (with active run lease epoch):
    lookup action by (run_id, operation_key)
    if same key and args differ: record rejection/conflict; STOP
    if existing confirmed: record ActionProposalDeduped; return saved outcome; STOP
    if existing unknown/needs_human/in_flight: STOP; reconcile or wait
    if new: create logical action and event
    if approval needed: request approval; STOP
    authorize action and record event
    create next attempt; record ActionAttemptStarted; commit TX
  verify lease still owned immediately before dispatch
  call trusted tool with persisted args + original idempotency key
  TX (same epoch, or record via recovery owner after takeover):
    persist confirmed / failed_clean / ambiguous outcome + projection
  if result commit fails or owner lost: new owner sees unresolved attempt
```

Attempt row and `ActionAttemptStarted` event are committed even if dispatch never happens. Such an action is conservative `unknown`. For read-only operations, retry is permitted subject to data version/freshness constraints.

## 5. Trusted tool manifest example

```yaml
name: store.put
version: "1"
effect: write
args_schema:
  type: object
  required: [object_path, artifact_ref]
  additionalProperties: false
  properties:
    object_path: {type: string}
    artifact_ref: {type: string}
operation_identity:
  milestone_scoped: true
  fields: [object_path]             # validated and normalized by trusted code
  # same path + different file content => intent_conflict
idempotency:
  supported: true
  key_location: header
  retention_seconds: 86400          # *only* if tested/documented by this service
reconciliation:
  method: authoritative_query
  check_tool: store.head
  positive_proof: [object_path, expected_sha256]
  negative_proof_is_safe: false
permissions:
  requires_approval: false
  allowed_path_prefix: "uploads/"
timeout_seconds: 20
```

`effect` means read/write; finer recovery behavior follows **trusted** `idempotency` and `reconciliation` metadata. Do not trust remote MCP annotation hints as authoritative evidence of idempotency. An actual object store's etag is not automatically a SHA-256; compare a stored SHA-256 field or verified content hash using a connector-specific procedure.

## 6. Reconciliation decision table

| Situation | Automatic action | Reason |
|---|---|---|
| Read-only request interrupted | Retry, if still authorized and freshness valid | No external write effect |
| Idempotent write, documented key+retention still valid | Retry **same action/key/args**, or check first | Duplicate suppression is provided by service contract |
| Authoritative query proves effect present | Mark confirmed with evidence | Positive result identifies the intended effect |
| Query says absent, but write may still commit or query lags | **Stay unknown / needs_human** | Negative observation does not prove absence |
| Query proves safe non-commit under explicit atomic/linearizable contract | Record safe-absence evidence; authorize new attempt | Only when connector test validates contract |
| Opaque or irreversible write with ambiguous dispatch | Pause for human | Cannot safely infer result |
| Clean rejection guaranteed before any effect | `failed_clean`, bounded retry permitted | No effect took place |
| Provider idempotency TTL expired after unknown attempt | Pause or positive-query reconcile | Original key may no longer suppress repeats |

**Not all idempotency claims are equal:** a repeated request can dedupe while an original request is still processing, or the service may have retention windows / specific error behavior. Manifest policy must mirror the actual connector's contract.

## 7. Crash, lease and event failures

- Write-ahead is **event and projection commit before dispatch**; it does not make remote execution transactional with SQLite.
- Only a lease owner holding the active fencing epoch may append ordinary run/action events. On crash recovery, a new owner atomically takes the expired lease and logs `RecoveryStarted`; incomplete attempts become ambiguous.
- Stale worker responses must never overwrite a new owner's state. They may still have committed at the target. Reconciliation is the way to establish truth, not fencing alone.
- Ensure action idempotency is respected even if `ActionAttemptStarted` was persisted, the tool committed, and the process crashed before `ActionAttemptConfirmed`.

## 8. Human decisions

`resolve_action(run_id, action_id, decision, evidence_ref, actor, reason)` accepts:

- `confirmed`: requires an external receipt/reference; action becomes confirmed with provenance `human_assisted`, milestone still independently verifies.
- `abort`: marks skipped and halts required work; not a successful verified completion.
- `retry_at_risk`: explicitly opts into an action attempt that may duplicate, with warning recorded; never counted as automatically safe recovery.

No implicit decision from model text. Protect local HTTP decision endpoint from arbitrary remote access; v1 is loopback only. On resume, process unresolved decisions **before** any new side-effecting action.
