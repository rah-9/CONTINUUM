# Durable state and data schema

**Backend for v1: SQLite only.** All persistent business state is reconstructed from the per-run event log. Event insertion and projection updates occur **inside the same SQLite transaction**; a separate lease table manages ephemeral controller ownership.

## 1. State enums

```text
run_status       created | running | recovering | paused_for_approval |
                 awaiting_reconciliation | completed | failed | cancelled
milestone_status pending | verified | invalidated
logical_action   proposed | awaiting_approval | authorized | in_flight |
                 confirmed | failed_clean | unknown | needs_human |
                 rejected | skipped
attempt_status   started | confirmed | failed_clean | ambiguous
verifier_result  pass | fail | error
```

`confirmed` means a write attempt's result was established, **not** that its containing milestone passed. `failed_clean` requires a strong guarantee of no effect. `unknown`/`needs_human` block replacement writes.

Transitions are normative in `04-EVENT-LOG-AND-LEDGER.md`. `run_status=failed` is terminal for current policy; a new authorized run is necessary to increase exhausted hard budgets in v1.

## 2. Task spec v1 (canonical JSON persisted in RunCreated)

```json
{
  "spec_version": 1,
  "task_id": "inventory_upload_v1",
  "goal": "Normalize CSV, upload the validated file, generate a report",
  "instructions": "Use approved tools and do not alter source input",
  "tools_allowlist": ["file.read", "csv.normalize", "store.put", "report.write"],
  "milestones": [
    {"id": "m1", "description": "Normalized CSV valid", "depends_on": [],
     "verifier_id": "csv_normalized", "verifier_version": "1"},
    {"id": "m2", "description": "Uploaded object matches normalized CSV", "depends_on": ["m1"],
     "verifier_id": "store_matches_artifact", "verifier_version": "1"}
  ],
  "budgets": {"max_model_calls": 40, "max_tokens": 200000,
              "max_cost_usd": 1.5, "max_active_seconds": 900,
              "max_recoveries": 5},
  "inputs": [{"uri": "inputs/inventory.csv", "sha256": "<actual hash at creation>"}],
  "model_policy": {"primary": "A", "fallbacks": ["B", "C"]}
}
```

Registration of trusted tool manifests includes operation identity rules. Example for `store.put`: **trusted rule** `operation_key = "m2:store.put:" + normalized(args.object_path)`, with immutable payload `args_hash`. Multiple intentional effects to the same target require a task-defined unique slot or version. The model cannot invent a new operation identifier to bypass an existing action. Task specs are immutable within a run.

Validate unique milestone IDs, DAG acyclicity, known verifier and manifest versions, budget positivity, file path confinement, verified/snapshotted inputs and appropriate operation identity rules **before** run creation.

## 3. SQLite DDL (canonical)

```sql
PRAGMA foreign_keys = ON;
PRAGMA journal_mode = WAL;
PRAGMA busy_timeout = 5000;

CREATE TABLE runs (
  run_id TEXT PRIMARY KEY,
  task_id TEXT NOT NULL,
  spec_json TEXT NOT NULL,               -- canonical immutable snapshot
  status TEXT NOT NULL,
  active_session_id TEXT,
  last_event_seq INTEGER NOT NULL DEFAULT 0,
  created_at TEXT NOT NULL,
  updated_at TEXT NOT NULL,
  finished_at TEXT,
  failure_code TEXT
);

CREATE TABLE events (
  run_id TEXT NOT NULL REFERENCES runs(run_id),
  seq INTEGER NOT NULL,
  event_id TEXT NOT NULL UNIQUE,
  schema_version INTEGER NOT NULL,
  type TEXT NOT NULL,
  occurred_at TEXT NOT NULL,
  actor TEXT NOT NULL,
  payload_json TEXT NOT NULL,
  PRIMARY KEY (run_id, seq)
);

-- Ownership metadata: intentionally NOT reconstructed from historical events.
CREATE TABLE run_leases (
  run_id TEXT PRIMARY KEY REFERENCES runs(run_id),
  owner_id TEXT NOT NULL,
  epoch INTEGER NOT NULL,
  expires_at_unix INTEGER NOT NULL
);

CREATE TABLE model_sessions (
  session_id TEXT PRIMARY KEY,
  run_id TEXT NOT NULL REFERENCES runs(run_id),
  model_id TEXT NOT NULL,
  profile_json TEXT NOT NULL,
  packet_sha256 TEXT NOT NULL,
  started_at TEXT NOT NULL,
  ended_at TEXT,
  finish_reason TEXT
);

CREATE TABLE milestones (
  run_id TEXT NOT NULL REFERENCES runs(run_id),
  milestone_id TEXT NOT NULL,
  status TEXT NOT NULL,
  verifier_id TEXT NOT NULL,
  verifier_version TEXT NOT NULL,
  input_fingerprint TEXT,
  evidence_ref TEXT,
  verified_at TEXT,
  PRIMARY KEY (run_id, milestone_id)
);

CREATE TABLE actions (
  action_id TEXT PRIMARY KEY,              -- generated once by the runtime
  run_id TEXT NOT NULL REFERENCES runs(run_id),
  milestone_id TEXT,
  operation_key TEXT NOT NULL,             -- trusted business intent identity
  tool_name TEXT NOT NULL,
  manifest_version TEXT NOT NULL,
  args_json TEXT NOT NULL,                -- redacted; secrets passed out-of-band
  args_sha256 TEXT NOT NULL,               -- canonical execution intent digest
  idempotency_key TEXT,                   -- null when unsupported
  status TEXT NOT NULL,
  created_at TEXT NOT NULL,
  updated_at TEXT NOT NULL,
  outcome_ref TEXT,
  reconciliation_ref TEXT,
  UNIQUE (run_id, operation_key)
);

CREATE INDEX idx_actions_run_status ON actions(run_id, status);

CREATE TABLE action_attempts (
  action_id TEXT NOT NULL REFERENCES actions(action_id),
  attempt_no INTEGER NOT NULL,
  status TEXT NOT NULL,
  dispatch_owner_epoch INTEGER NOT NULL,
  started_at TEXT NOT NULL,
  finished_at TEXT,
  result_ref TEXT,
  error_code TEXT,
  PRIMARY KEY (action_id, attempt_no)
);

CREATE TABLE artifacts (
  artifact_id TEXT PRIMARY KEY,
  run_id TEXT NOT NULL REFERENCES runs(run_id),
  relative_path TEXT NOT NULL,
  sha256 TEXT NOT NULL,
  byte_size INTEGER NOT NULL,
  producing_action_id TEXT REFERENCES actions(action_id),
  created_at TEXT NOT NULL,
  state TEXT NOT NULL                 -- present | drifted | missing
);

CREATE TABLE approvals (
  action_id TEXT PRIMARY KEY REFERENCES actions(action_id),
  decision TEXT,                    -- approved | rejected | NULL
  actor_id TEXT,
  decided_at TEXT,
  reason TEXT
);

CREATE TABLE usage_totals (
  run_id TEXT PRIMARY KEY REFERENCES runs(run_id),
  model_calls INTEGER NOT NULL DEFAULT 0,
  tokens_reported INTEGER NOT NULL DEFAULT 0,
  cost_known_usd TEXT NOT NULL DEFAULT '0',
  has_unpriced_usage INTEGER NOT NULL DEFAULT 0,
  recoveries INTEGER NOT NULL DEFAULT 0,
  active_elapsed_ms INTEGER NOT NULL DEFAULT 0
);
```

**Time budget:** `max_active_seconds` counts execution time while the runtime is actively processing a run, not manual approval/reconciliation pauses or process downtime. Record bounded active-time increments via `RunActiveTimeRecorded` events and `usage_totals.active_elapsed_ms`; cap each increment using a monotonic clock while the process is alive. Wall-clock time is reported as a separate evaluation metric.

**Schema clarifications:** `TEXT` stores canonical JSON; money is decimal text rather than floating-point currency; `has_unpriced_usage` is not zero cost. Exact unknown token counts are not fabricated. A table column containing redacted tool args is **not** sufficient to re-execute an action: immutable execution params must also be recoverable from trusted nonsecret sources; secret values are injected from environment at dispatch time. Artifacts are referenced by content hash; raw blobs reside in a confined run workspace. Input snapshots are immutable in that workspace.

**No extra `schema.py` event store model:** Python Pydantic models mirror the JSON/DDL contracts without maintaining a third source of truth. All event payloads include schema version. Schema migrations are out of scope until a second version is needed.

## 4. Atomic transactions and run ownership

- Use `BEGIN IMMEDIATE` for event sequence assignment, event append, corresponding projection change, and `runs.last_event_seq` update; commit or roll back all together. Never append an event without applying its projection; no provider/tool I/O inside transaction.
- On restart, replay ordered events from RunCreated into a *fresh in-memory projection* and compare with durable projections. Runtime can rebuild projections in one transaction if damaged. Some projection fields (such as timestamps) must use the original event values, not `now()` during rebuild.
- Claim lease atomically if absent, expired or already owned by the caller; increment epoch on **every takeover**. Use database time (`unixepoch('now')`) to avoid process clock differences. Each worker writes only with `(owner_id, epoch)` matching active unexpired lease.
- Heartbeat periodically; before any tool dispatch verify active ownership. If the old lease expires during an external request, takeover must classify its started attempt as ambiguous. SQLite lease fencing limits *local controllers*; it cannot revoke an external HTTP request.
- Concurrent `resume` calls: at most one succeeds as owner; all others receive `already_owned` and do nothing. Do not silently steal a live lease.

## 5. Fingerprints and progress integrity

`milestone.input_fingerprint = SHA256(canonical_json({input_artifact_hashes, external_input_versions, dependency_evidence_refs, verifier_id, verifier_version, relevant_task_spec_fields}))`.

Verification success must supply an evidence reference pointing to read-only verifier output, not model prose. At resume, recheck source snapshots, artifact hash and dependency fingerprints. Invalidate the changed milestone **and transitive downstream dependents** with events. Already committed effects remain in the ledger; when redo is needed, their operation keys still prevent blind repeats. If desired new payload conflicts with an existing confirmed operation key, stop and require a distinct task/spec version or human-approved remediation workflow, not overwriting history.

## 6. Portable packet projection (example)

```json
{
  "run_id": "run_example",
  "goal": "Process input and upload report",
  "verified": [{"id": "m1", "evidence_ref": "evidence/m1.json", "fingerprint": "sha256:..."}],
  "pending": ["m2"],
  "artifacts": [{"path": "artifacts/normalized.csv", "sha256": "sha256:..."}],
  "confirmed_operations": ["m1:csv.normalize:input-01"],
  "unresolved_operations": [],
  "remaining_limits": {"model_calls": 14, "tokens_estimate": 80000}
}
```

Packet excludes untrusted model transcript; see `05-MODEL-ADAPTERS.md` for exact construction.

## 7. Required invariant tests

- **I1:** strictly increasing, gapless `(run_id, seq)` for committed events; event/projection atomicity.
- **I2:** projections rebuild exactly from committed events, excluding the ephemeral lease table.
- **I3:** only a passing versioned verifier with matching input fingerprint can mark milestone verified.
- **I4:** ambiguous/unfinished dispatched attempts block a write until contract-safe reconciliation.
- **I5:** `(run_id, operation_key)` maps to one immutable action ID and args hash across model changes; attempts preserve idempotency key.
- **I6:** competing controllers cannot both hold an unexpired lease or commit transitions under stale epochs.
- **I7:** drift invalidates affected milestone and dependency descendants, without deleting confirmed action history.
- **I8:** event inspection/replay never invokes real tools or models.
