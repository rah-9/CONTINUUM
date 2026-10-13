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
  expires_at_unix BIGINT NOT NULL
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
