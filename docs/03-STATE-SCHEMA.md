# Durable state and data schema

**Backend for v1: PostgreSQL only** (14+, one database on the local host). All persistent business state is reconstructed from the per-run event log. Event insertion and projection updates occur **inside the same PostgreSQL transaction**; a separate lease table manages ephemeral controller ownership.

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

## 3. PostgreSQL DDL (canonical)

The executable copy is `src/continuum/core/schema.sql`; `tests/unit/test_docs_sync.py` fails if the two differ.

```sql
-- Continuum durable schema (PostgreSQL 14+). Canonical DDL; see docs/03-STATE-SCHEMA.md.
-- Idempotent: safe to execute on every start. Schema migrations are out of scope for v1.

CREATE TABLE IF NOT EXISTS runs (
  run_id            TEXT PRIMARY KEY,
  task_id           TEXT NOT NULL,
  spec_json         JSONB NOT NULL,              -- canonical immutable snapshot
  status            TEXT NOT NULL,
  active_session_id TEXT,
  last_event_seq    BIGINT NOT NULL DEFAULT 0,
  created_at        TIMESTAMPTZ NOT NULL,
  updated_at        TIMESTAMPTZ NOT NULL,
  finished_at       TIMESTAMPTZ,
  failure_code      TEXT
);

CREATE TABLE IF NOT EXISTS events (
  run_id         TEXT NOT NULL REFERENCES runs(run_id),
  seq            BIGINT NOT NULL,
  event_id       TEXT NOT NULL UNIQUE,
  schema_version INTEGER NOT NULL,
  type           TEXT NOT NULL,
  occurred_at    TIMESTAMPTZ NOT NULL,
  actor          TEXT NOT NULL,
  payload_json   JSONB NOT NULL,
  PRIMARY KEY (run_id, seq)
);

-- Historical events are immutable.
CREATE OR REPLACE FUNCTION continuum_events_immutable() RETURNS trigger AS $$
BEGIN
  RAISE EXCEPTION 'events are append-only';
END;
$$ LANGUAGE plpgsql;

DROP TRIGGER IF EXISTS events_immutable ON events;
CREATE TRIGGER events_immutable BEFORE UPDATE OR DELETE ON events
  FOR EACH ROW EXECUTE FUNCTION continuum_events_immutable();

-- Ownership metadata: intentionally NOT reconstructed from historical events.
CREATE TABLE IF NOT EXISTS run_leases (
  run_id          TEXT PRIMARY KEY REFERENCES runs(run_id),
  owner_id        TEXT NOT NULL,
  epoch           BIGINT NOT NULL,
  expires_at_unix DOUBLE PRECISION NOT NULL   -- fractional unix seconds (DB time)
);

CREATE TABLE IF NOT EXISTS model_sessions (
  session_id    TEXT PRIMARY KEY,
  run_id        TEXT NOT NULL REFERENCES runs(run_id),
  model_id      TEXT NOT NULL,
  profile_json  JSONB NOT NULL,
  packet_sha256 TEXT NOT NULL,
  started_at    TIMESTAMPTZ NOT NULL,
  ended_at      TIMESTAMPTZ,
  finish_reason TEXT
);

CREATE TABLE IF NOT EXISTS milestones (
  run_id            TEXT NOT NULL REFERENCES runs(run_id),
  milestone_id      TEXT NOT NULL,
  status            TEXT NOT NULL,
  verifier_id       TEXT NOT NULL,
  verifier_version  TEXT NOT NULL,
  input_fingerprint TEXT,
  evidence_ref      TEXT,
  verified_at       TIMESTAMPTZ,
  PRIMARY KEY (run_id, milestone_id)
);

CREATE TABLE IF NOT EXISTS actions (
  action_id          TEXT PRIMARY KEY,          -- generated once by the runtime
  run_id             TEXT NOT NULL REFERENCES runs(run_id),
  milestone_id       TEXT,
  operation_key      TEXT NOT NULL,             -- trusted business intent identity
  tool_name          TEXT NOT NULL,
  manifest_version   TEXT NOT NULL,
  args_json          JSONB NOT NULL,            -- redacted; secrets passed out-of-band
  args_sha256        TEXT NOT NULL,             -- canonical execution intent digest
  idempotency_key    TEXT,                      -- null when unsupported
  status             TEXT NOT NULL,
  created_at         TIMESTAMPTZ NOT NULL,
  updated_at         TIMESTAMPTZ NOT NULL,
  outcome_ref        TEXT,
  reconciliation_ref TEXT,
  UNIQUE (run_id, operation_key)
);

CREATE INDEX IF NOT EXISTS idx_actions_run_status ON actions(run_id, status);

CREATE TABLE IF NOT EXISTS action_attempts (
  action_id            TEXT NOT NULL REFERENCES actions(action_id),
  attempt_no           INTEGER NOT NULL,
  status               TEXT NOT NULL,
  dispatch_owner_epoch BIGINT NOT NULL,
  started_at           TIMESTAMPTZ NOT NULL,
  finished_at          TIMESTAMPTZ,
  result_ref           TEXT,
  error_code           TEXT,
  PRIMARY KEY (action_id, attempt_no)
);

CREATE TABLE IF NOT EXISTS artifacts (
  artifact_id         TEXT PRIMARY KEY,
  run_id              TEXT NOT NULL REFERENCES runs(run_id),
  relative_path       TEXT NOT NULL,
  sha256              TEXT NOT NULL,
  byte_size           BIGINT NOT NULL,
  producing_action_id TEXT REFERENCES actions(action_id),
  created_at          TIMESTAMPTZ NOT NULL,
  state               TEXT NOT NULL             -- present | drifted | missing
);

CREATE TABLE IF NOT EXISTS approvals (
  action_id  TEXT PRIMARY KEY REFERENCES actions(action_id),
  decision   TEXT,                              -- approved | rejected | NULL
  actor_id   TEXT,
  decided_at TIMESTAMPTZ,
  reason     TEXT
);

CREATE TABLE IF NOT EXISTS usage_totals (
  run_id             TEXT PRIMARY KEY REFERENCES runs(run_id),
  model_calls        INTEGER NOT NULL DEFAULT 0,
  tokens_reported    BIGINT NOT NULL DEFAULT 0,
  cost_known_usd     NUMERIC(20, 10) NOT NULL DEFAULT 0,   -- exact decimal, never float
  has_unpriced_usage BOOLEAN NOT NULL DEFAULT FALSE,
  recoveries         INTEGER NOT NULL DEFAULT 0,
  active_elapsed_ms  BIGINT NOT NULL DEFAULT 0
);

-- ---------------------------------------------------------------------------
-- Integrity constraints and indexes (schema version 2).
-- Defense in depth: the database itself rejects states that would violate the
-- core invariants, even if application code regresses. Each statement is
-- idempotent, so existing databases are upgraded by re-running db-init.
-- ---------------------------------------------------------------------------

CREATE TABLE IF NOT EXISTS schema_meta (
  key   TEXT PRIMARY KEY,
  value TEXT NOT NULL
);
INSERT INTO schema_meta (key, value) VALUES ('schema_version', '6')
  ON CONFLICT (key) DO UPDATE SET value = EXCLUDED.value;

-- TRUNCATE would bypass the row-level immutability trigger.
DROP TRIGGER IF EXISTS events_no_truncate ON events;
CREATE TRIGGER events_no_truncate BEFORE TRUNCATE ON events
  FOR EACH STATEMENT EXECUTE FUNCTION continuum_events_immutable();

CREATE OR REPLACE FUNCTION continuum_add_constraint(tbl TEXT, name TEXT, def TEXT)
RETURNS void AS $$
BEGIN
  EXECUTE format('ALTER TABLE %I ADD CONSTRAINT %I %s', tbl, name, def);
EXCEPTION WHEN duplicate_object OR duplicate_table THEN
  NULL;  -- already present
END;
$$ LANGUAGE plpgsql;

SELECT continuum_add_constraint('runs', 'runs_status_valid', $c$CHECK (status IN (
  'created', 'running', 'recovering', 'paused_for_approval', 'awaiting_reconciliation',
  'completed', 'failed', 'cancelled'))$c$);
SELECT continuum_add_constraint('runs', 'runs_seq_nonnegative', 'CHECK (last_event_seq >= 0)');
SELECT continuum_add_constraint('runs', 'runs_terminal_finished', $c$CHECK (
  (status IN ('completed', 'failed', 'cancelled')) = (finished_at IS NOT NULL))$c$);
SELECT continuum_add_constraint('events', 'events_seq_positive',
  'CHECK (seq > 0 AND schema_version >= 1)');
SELECT continuum_add_constraint('run_leases', 'run_leases_epoch_positive', 'CHECK (epoch >= 1)');
SELECT continuum_add_constraint('milestones', 'milestones_status_valid',
  $c$CHECK (status IN ('pending', 'verified', 'invalidated'))$c$);
-- I3: a verified milestone always carries its fingerprint and evidence.
SELECT continuum_add_constraint('milestones', 'milestones_verified_has_evidence', $c$CHECK (
  status <> 'verified' OR (input_fingerprint IS NOT NULL AND evidence_ref IS NOT NULL
                           AND verified_at IS NOT NULL))$c$);
SELECT continuum_add_constraint('actions', 'actions_status_valid', $c$CHECK (status IN (
  'proposed', 'awaiting_approval', 'authorized', 'in_flight', 'confirmed', 'failed_clean',
  'unknown', 'needs_human', 'rejected', 'skipped'))$c$);
SELECT continuum_add_constraint('actions', 'actions_confirmed_has_outcome',
  $c$CHECK (status <> 'confirmed' OR outcome_ref IS NOT NULL)$c$);
SELECT continuum_add_constraint('actions', 'actions_args_digest',
  $c$CHECK (args_sha256 LIKE 'sha256:%')$c$);
SELECT continuum_add_constraint('action_attempts', 'attempts_status_valid',
  $c$CHECK (status IN ('started', 'confirmed', 'failed_clean', 'ambiguous'))$c$);
SELECT continuum_add_constraint('action_attempts', 'attempts_numbers_positive',
  'CHECK (attempt_no >= 1 AND dispatch_owner_epoch >= 1)');
-- An attempt is finished exactly when it has left the started state.
SELECT continuum_add_constraint('action_attempts', 'attempts_finished_iff_terminal',
  $c$CHECK ((status = 'started') = (finished_at IS NULL))$c$);
SELECT continuum_add_constraint('artifacts', 'artifacts_state_valid',
  $c$CHECK (state IN ('present', 'drifted', 'missing'))$c$);
SELECT continuum_add_constraint('artifacts', 'artifacts_hash_and_size',
  $c$CHECK (sha256 ~ '^[0-9a-f]{64}$' AND byte_size >= 0)$c$);
SELECT continuum_add_constraint('approvals', 'approvals_decision_valid',
  $c$CHECK (decision IS NULL OR decision IN ('approved', 'rejected'))$c$);
SELECT continuum_add_constraint('usage_totals', 'usage_nonnegative', $c$CHECK (
  model_calls >= 0 AND tokens_reported >= 0 AND cost_known_usd >= 0 AND recoveries >= 0
  AND active_elapsed_ms >= 0)$c$);

-- Leases use fractional seconds (rounding to whole seconds shortened leases).
ALTER TABLE run_leases ALTER COLUMN expires_at_unix TYPE DOUBLE PRECISION;

CREATE INDEX IF NOT EXISTS idx_model_sessions_run ON model_sessions(run_id);
CREATE INDEX IF NOT EXISTS idx_artifacts_run ON artifacts(run_id);
CREATE INDEX IF NOT EXISTS idx_artifacts_action ON artifacts(producing_action_id);
-- Recovery scans for attempts that started but never recorded an outcome.
CREATE INDEX IF NOT EXISTS idx_attempts_started ON action_attempts(action_id)
  WHERE status = 'started';

-- ---------------------------------------------------------------------------
-- Coding-session continuity (schema versions 3-4, docs/02 s10).
-- A coding agent's work in a repository survives model switches and new chats:
-- an append-only journal per session, plus checks that only a human registers.
-- ---------------------------------------------------------------------------

CREATE TABLE IF NOT EXISTS coding_sessions (
  session_id TEXT PRIMARY KEY,
  repo_root  TEXT NOT NULL,                   -- absolute, resolved path
  goal       TEXT NOT NULL,
  status     TEXT NOT NULL,                   -- active | closed
  last_seq   BIGINT NOT NULL DEFAULT 0,
  created_at TIMESTAMPTZ NOT NULL,
  updated_at TIMESTAMPTZ NOT NULL
);

CREATE TABLE IF NOT EXISTS coding_events (
  session_id   TEXT NOT NULL REFERENCES coding_sessions(session_id),
  seq          BIGINT NOT NULL,
  type         TEXT NOT NULL,
  actor        TEXT NOT NULL,                 -- agent label (self-reported) or human:<id>
  payload_json JSONB NOT NULL,
  occurred_at  TIMESTAMPTZ NOT NULL,
  PRIMARY KEY (session_id, seq)
);

-- Schema v5: history stays append-only, with one audited exception. A human-requested
-- erasure (`continuum coding forget`) deletes a whole session inside a transaction that
-- set continuum.erasure = 'on' locally and records a content-free tombstone. Updates are
-- never allowed; deletes outside such a transaction are rejected.
CREATE OR REPLACE FUNCTION continuum_coding_events_guard() RETURNS trigger AS $$
BEGIN
  IF TG_OP = 'DELETE' AND current_setting('continuum.erasure', true) = 'on' THEN
    RETURN OLD;
  END IF;
  RAISE EXCEPTION 'coding events are append-only (erasure only via continuum coding forget)';
END;
$$ LANGUAGE plpgsql;
DROP TRIGGER IF EXISTS coding_events_immutable ON coding_events;
CREATE TRIGGER coding_events_immutable BEFORE UPDATE OR DELETE ON coding_events
  FOR EACH ROW EXECUTE FUNCTION continuum_coding_events_guard();
DROP TRIGGER IF EXISTS coding_events_no_truncate ON coding_events;
CREATE TRIGGER coding_events_no_truncate BEFORE TRUNCATE ON coding_events
  FOR EACH STATEMENT EXECUTE FUNCTION continuum_events_immutable();

-- Verification commands are registered by a human operator, never by an agent.
CREATE TABLE IF NOT EXISTS coding_checks (
  repo_root       TEXT NOT NULL,
  name            TEXT NOT NULL,
  argv_json       JSONB NOT NULL,             -- executed without a shell
  timeout_seconds INTEGER NOT NULL,
  registered_by   TEXT NOT NULL,
  registered_at   TIMESTAMPTZ NOT NULL,
  PRIMARY KEY (repo_root, name)
);

SELECT continuum_add_constraint('coding_sessions', 'coding_sessions_status_valid',
  $c$CHECK (status IN ('active', 'closed'))$c$);
-- Schema v4 added ChatCaptured: replace the v3 list (an older constraint would reject it).
ALTER TABLE coding_events DROP CONSTRAINT IF EXISTS coding_events_type_valid;
SELECT continuum_add_constraint('coding_events', 'coding_events_type_valid', $c$CHECK (type IN (
  'SessionStarted', 'AgentJoined', 'TaskAdded', 'TaskStatusChanged', 'NoteRecorded',
  'CheckpointCaptured', 'CheckRan', 'ChatCaptured', 'SessionClosed'))$c$);
SELECT continuum_add_constraint('coding_events', 'coding_events_seq_positive',
  'CHECK (seq > 0)');
SELECT continuum_add_constraint('coding_checks', 'coding_checks_timeout_positive',
  'CHECK (timeout_seconds BETWEEN 1 AND 3600)');

CREATE INDEX IF NOT EXISTS idx_coding_sessions_repo ON coding_sessions(repo_root, status);

-- Schema v4: the same repository at another path (another machine, a cloud container,
-- a re-cloned checkout) is recognised by its normalized remote URL, never by guessing.
ALTER TABLE coding_sessions ADD COLUMN IF NOT EXISTS repo_key TEXT;  -- NULL: no remote
ALTER TABLE coding_checks ADD COLUMN IF NOT EXISTS repo_key TEXT;
CREATE INDEX IF NOT EXISTS idx_coding_sessions_key ON coding_sessions(repo_key, status);
CREATE INDEX IF NOT EXISTS idx_coding_checks_key ON coding_checks(repo_key, name);

-- Schema v5: what was erased, by whom, when and why. No content: the session id, a digest
-- of the repository path, and how many events were removed. Append-only.
CREATE TABLE IF NOT EXISTS coding_erasures (
  erasure_id     TEXT PRIMARY KEY,
  session_id     TEXT NOT NULL,
  repo_digest    TEXT NOT NULL,
  events_erased  BIGINT NOT NULL,
  erased_by      TEXT NOT NULL,
  reason         TEXT NOT NULL,
  erased_at      TIMESTAMPTZ NOT NULL
);
DROP TRIGGER IF EXISTS coding_erasures_immutable ON coding_erasures;
CREATE TRIGGER coding_erasures_immutable BEFORE UPDATE OR DELETE ON coding_erasures
  FOR EACH ROW EXECUTE FUNCTION continuum_events_immutable();
-- Captured chat messages are deduplicated by their key (re-imports are idempotent).
CREATE INDEX IF NOT EXISTS idx_coding_events_capture_key ON coding_events
  (session_id, (payload_json->>'key')) WHERE type = 'ChatCaptured';

-- Schema v5: one database, many people (D-040). Each person connects as their own
-- PostgreSQL role; a session, a check and a tombstone belong to the role that created them,
-- and row-level security shows every other role only its own rows. Events are visible
-- exactly when their session is. The schema owner (the operator who ran db-init) is not
-- subject to RLS and administers everyone; superusers always bypass RLS.
ALTER TABLE coding_sessions ADD COLUMN IF NOT EXISTS owner_role TEXT NOT NULL
  DEFAULT current_user;
ALTER TABLE coding_checks ADD COLUMN IF NOT EXISTS owner_role TEXT NOT NULL
  DEFAULT current_user;
ALTER TABLE coding_erasures ADD COLUMN IF NOT EXISTS owner_role TEXT NOT NULL
  DEFAULT current_user;
CREATE INDEX IF NOT EXISTS idx_coding_sessions_owner ON coding_sessions(owner_role, status);
-- Two people may register a check with the same name for the same path.
DO $$
BEGIN
  IF NOT EXISTS (
      SELECT 1 FROM pg_constraint c JOIN pg_attribute a
        ON a.attrelid = c.conrelid AND a.attnum = ANY(c.conkey)
      WHERE c.conrelid = 'coding_checks'::regclass AND c.contype = 'p'
        AND a.attname = 'owner_role') THEN
    ALTER TABLE coding_checks DROP CONSTRAINT IF EXISTS coding_checks_pkey;
    ALTER TABLE coding_checks ADD CONSTRAINT coding_checks_pkey
      PRIMARY KEY (owner_role, repo_root, name);
  END IF;
END;
$$;

ALTER TABLE coding_sessions ENABLE ROW LEVEL SECURITY;
ALTER TABLE coding_events ENABLE ROW LEVEL SECURITY;
ALTER TABLE coding_checks ENABLE ROW LEVEL SECURITY;
ALTER TABLE coding_erasures ENABLE ROW LEVEL SECURITY;
DROP POLICY IF EXISTS coding_sessions_own ON coding_sessions;
CREATE POLICY coding_sessions_own ON coding_sessions
  USING (owner_role = current_user) WITH CHECK (owner_role = current_user);
DROP POLICY IF EXISTS coding_checks_own ON coding_checks;
CREATE POLICY coding_checks_own ON coding_checks
  USING (owner_role = current_user) WITH CHECK (owner_role = current_user);
DROP POLICY IF EXISTS coding_erasures_own ON coding_erasures;
CREATE POLICY coding_erasures_own ON coding_erasures
  USING (owner_role = current_user) WITH CHECK (owner_role = current_user);
-- The subquery is itself filtered by the sessions policy.
DROP POLICY IF EXISTS coding_events_own ON coding_events;
CREATE POLICY coding_events_own ON coding_events
  USING (EXISTS (SELECT 1 FROM coding_sessions s WHERE s.session_id = coding_events.session_id))
  WITH CHECK (EXISTS (SELECT 1 FROM coding_sessions s
                      WHERE s.session_id = coding_events.session_id));


-- Schema v6: everything the agent did (D-042). Tool calls as the app reported them, what
-- each changed in the working copy, compaction; full outputs and diffs as content-addressed
-- blobs owned by the session (same row-level security and human-only erasure as events).
ALTER TABLE coding_events DROP CONSTRAINT IF EXISTS coding_events_type_valid;
SELECT continuum_add_constraint('coding_events', 'coding_events_type_valid', $c$CHECK (type IN (
  'SessionStarted', 'AgentJoined', 'TaskAdded', 'TaskStatusChanged', 'NoteRecorded',
  'CheckpointCaptured', 'CheckRan', 'ChatCaptured', 'SessionClosed', 'ToolCallStarted',
  'ToolCallFinished', 'ToolCallUnresolved', 'ToolCallResolved', 'FileImpactObserved',
  'CompactionObserved'))$c$);
CREATE INDEX IF NOT EXISTS idx_coding_events_call_key ON coding_events
  (session_id, (payload_json->>'call_key'), type) WHERE payload_json ? 'call_key';

CREATE TABLE IF NOT EXISTS coding_blobs (
  session_id    TEXT NOT NULL REFERENCES coding_sessions(session_id),
  digest        TEXT NOT NULL,                -- sha256 of the stored (redacted) content
  media_type    TEXT NOT NULL,                -- text/plain | text/x-diff | application/json
  chars         BIGINT NOT NULL,              -- length of the redacted original
  content       TEXT NOT NULL,                -- head and tail when longer than the cap
  truncated     BOOLEAN NOT NULL DEFAULT false,
  created_at    TIMESTAMPTZ NOT NULL,
  PRIMARY KEY (session_id, digest)
);
DROP TRIGGER IF EXISTS coding_blobs_immutable ON coding_blobs;
CREATE TRIGGER coding_blobs_immutable BEFORE UPDATE OR DELETE ON coding_blobs
  FOR EACH ROW EXECUTE FUNCTION continuum_coding_events_guard();
DROP TRIGGER IF EXISTS coding_blobs_no_truncate ON coding_blobs;
CREATE TRIGGER coding_blobs_no_truncate BEFORE TRUNCATE ON coding_blobs
  FOR EACH STATEMENT EXECUTE FUNCTION continuum_events_immutable();
ALTER TABLE coding_blobs ENABLE ROW LEVEL SECURITY;
DROP POLICY IF EXISTS coding_blobs_own ON coding_blobs;
CREATE POLICY coding_blobs_own ON coding_blobs
  USING (EXISTS (SELECT 1 FROM coding_sessions s WHERE s.session_id = coding_blobs.session_id))
  WITH CHECK (EXISTS (SELECT 1 FROM coding_sessions s
                      WHERE s.session_id = coding_blobs.session_id));

-- Full-text recall over everything remembered (D-043): every string in an event, and the
-- first 64 KiB of every blob (a tsvector is bounded; recall still finds the blob's event).
ALTER TABLE coding_events ADD COLUMN IF NOT EXISTS search tsvector
  GENERATED ALWAYS AS (jsonb_to_tsvector('english'::regconfig, payload_json, '["string"]'))
  STORED;
CREATE INDEX IF NOT EXISTS idx_coding_events_search ON coding_events USING gin (search);
ALTER TABLE coding_blobs ADD COLUMN IF NOT EXISTS search tsvector
  GENERATED ALWAYS AS (to_tsvector('english'::regconfig, left(content, 65536))) STORED;
CREATE INDEX IF NOT EXISTS idx_coding_blobs_search ON coding_blobs USING gin (search);
```

**Time budget:** `max_active_seconds` counts execution time while the runtime is actively processing a run, not manual approval/reconciliation pauses or process downtime. Record bounded active-time increments via `RunActiveTimeRecorded` events and `usage_totals.active_elapsed_ms`; cap each increment using a monotonic clock while the process is alive. Active time includes recovery on resume (reconciliation, re-verification, authorized retries). Increments are recorded after every model call and every batch of tool dispatches, and when the operation ends, so the cap (which guards against suspend gaps) never truncates real work. Wall-clock time is reported as a separate evaluation metric.

**Schema clarifications:** `JSONB` stores canonical JSON; money is exact `NUMERIC` (never floating point); `has_unpriced_usage` is not zero cost. Timestamps are `TIMESTAMPTZ` taken from the event's `occurred_at`. A trigger rejects `UPDATE`/`DELETE` on `events`. Each deployment may use its own schema (`search_path`), which is how tests isolate runs. Exact unknown token counts are not fabricated. A table column containing redacted tool args is **not** sufficient to re-execute an action: immutable execution params must also be recoverable from trusted nonsecret sources; secret values are injected from environment at dispatch time. Artifacts are referenced by content hash; raw blobs reside in a confined run workspace. Input snapshots are immutable in that workspace.

**No extra `schema.py` event store model:** Python Pydantic models mirror the JSON/DDL contracts without maintaining a third source of truth. All event payloads include schema version. Schema migrations are out of scope until a second version is needed.

## 4. Atomic transactions and run ownership

- In one transaction, lock the run row (`SELECT ... FOR NO KEY UPDATE`) for event sequence assignment, then append the event, apply its projection change and update `runs.last_event_seq`; commit or roll back all together. `lock_timeout` bounds waiting. Never append an event without applying its projection; no provider/tool I/O inside transaction.
- On restart, replay ordered events from RunCreated into a *fresh in-memory projection* and compare with durable projections. Runtime can rebuild projections in one transaction if damaged. Some projection fields (such as timestamps) must use the original event values, not `now()` during rebuild.
- Claim lease atomically if absent, expired or already owned by the caller; increment epoch on **every takeover**. Use fractional database time (`extract(epoch from clock_timestamp())`, never rounded to whole seconds) to avoid process clock differences; the fence check share-locks the lease row inside the append transaction so a concurrent takeover cannot interleave. Each worker writes only with `(owner_id, epoch)` matching active unexpired lease.
- Heartbeat periodically (a background renewal at about a third of the TTL keeps ownership through long model or tool calls; it only extends the same `(owner, epoch)` and can never take over); before any tool dispatch verify active ownership. If the old lease expires during an external request, takeover must classify its started attempt as ambiguous. Database lease fencing limits *local controllers*; it cannot revoke an external HTTP request.
- Concurrent `resume` calls: at most one succeeds as owner; all others receive `already_owned` and do nothing. Do not silently steal a live lease.

## 5. Fingerprints and progress integrity

`milestone.input_fingerprint = SHA256(canonical_json({input_artifact_hashes, external_input_versions, dependency_evidence_refs, verifier_id, verifier_version, relevant_task_spec_fields}))`.

Verification success must supply an evidence reference pointing to read-only verifier output, not model prose. At resume, recheck source snapshots, artifact hash and dependency fingerprints. Invalidate the changed milestone **and transitive downstream dependents** with events. Already committed effects remain in the ledger; when redo is needed, their operation keys still prevent blind repeats. If desired new payload conflicts with an existing confirmed operation key, stop and require a distinct task/spec version or human-approved remediation workflow, not overwriting history. The runtime records this stop as `RunFailed(failure_code = "drift_requires_remediation")`.

**Coding sessions (schema v5).** `ChatCaptured.role` may be `action`: one of the agent's tool calls from the app's transcript, one line, with `failed` (D-041). Erasure (D-039) and per-role isolation with row-level security (D-040) are described in OPERATIONS.md. **Coding sessions (schema v3, extended in v4).** v4 adds `ChatCaptured` events (the user's words and the agent's last report, captured by app hooks; deduplicated through an index on `payload_json->>'key'`), `AgentJoined.repo_root` (set when a session moves to another checkout), and `repo_key` on sessions and checks (the normalized, credential-free remote URL that identifies the same repository at another path). The v3 event-type constraint is replaced on upgrade. **Coding sessions (schema v3).** `coding_sessions`, `coding_events` and `coding_checks` are separate from runs. `coding_events` is append-only (an immutability trigger, and TRUNCATE is blocked) with a gapless per-session `seq` assigned under the session row lock. A coding task is verified only by `CheckRan` evidence whose fingerprint equals the current observed `RepoState.fingerprint` and whose `argv_sha256` equals the digest of the check's current registered definition (docs/02 §10). `continuum` migrates older schemas automatically on start (`ensure_schema`). Migrations are serialized by an advisory lock, and a schema newer than the code is a `schema_too_new` error, never a downgrade.

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

---

_Continuum is proprietary and confidential. © 2026 the Continuum copyright holder. All rights reserved. See [LICENSE](../LICENSE)._
