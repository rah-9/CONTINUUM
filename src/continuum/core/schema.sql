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
INSERT INTO schema_meta (key, value) VALUES ('schema_version', '4')
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

DROP TRIGGER IF EXISTS coding_events_immutable ON coding_events;
CREATE TRIGGER coding_events_immutable BEFORE UPDATE OR DELETE ON coding_events
  FOR EACH ROW EXECUTE FUNCTION continuum_events_immutable();
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
-- Captured chat messages are deduplicated by their key (re-imports are idempotent).
CREATE INDEX IF NOT EXISTS idx_coding_events_capture_key ON coding_events
  (session_id, (payload_json->>'key')) WHERE type = 'ChatCaptured';
