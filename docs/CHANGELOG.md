# Changelog

## 0.6.1 — 2026-10-14

A full-codebase audit of the coding-continuity layer found 23 issues, all fixed with regression tests (`tests/integration/test_coding_hardening.py`, D-031).

### Fixed
- **Verification could fail open.**
  - A git command that failed or timed out was hashed as empty, so a dirty tree could match a clean one. Observation now raises `observation_failed`.
  - External diff drivers and textconv could hide edits. Diffs are now raw.
  - In a repo with no commits, staged changes were invisible.
  - `src/build/`-style source directories were excluded at any depth.
  - Untracked symlinks were followed outside the repo.
- **Atomicity:** `coding_start` now writes the session row, `SessionStarted` and the initial tasks in one transaction, after validating every task. Invalid tasks no longer leave orphan sessions or turn `"pytest"` into checks `p, t, s, …`.
- **Evidence race:** `coding_complete_task` is refused if the journal moved on, or the code changed, between evaluation and commit.
- **Closed sessions** no longer run a check's command.
- **Crashes:** a non-executable check, or one whose argv names a directory, is recorded as a failed run (exit 126), not a raw exception.
- **Claims vs facts:**
  - checkpoint summaries moved from `verified_facts` to `agent_notes`
  - agent text is flattened in the Markdown brief, so it cannot forge headings or "verified" lines
  - agents cannot record actions as `human:`
  - agent labels are redacted
- **Containment:** a coding repository is never `/` or the home directory itself, and stays inside `CONTINUUM_CODING_ROOTS` when that is set.
- **Schema:** older code never downgrades a newer schema (`schema_too_new`). Concurrent first-use migrations are serialized by an advisory lock.
- **CLI:** `--` is special only for `coding checks add`, so `note … -- "-x text"` works again.
- Rename entries are parsed correctly in `changed_files`. Timestamps are compared as datetimes. A check that modifies the tree is flagged (`modified_tree`). Briefs observe the repository once and index check runs in a single pass.

## 0.6.0 — 2026-10-14

Spec 0.5.

### Added
- **Coding-session continuity** (D-028, D-029; docs/01 R13/U8, docs/02 §10, docs/03 schema v3). Coding work carries over when you switch models, versions, vendors or chats:
  - an append-only coding journal per repository
  - code state observed by Continuum
  - tasks verified only by human-registered checks passing on the current code with an unchanged definition
  - a handoff brief that separates verified facts from agent claims
  - tamper evidence for check definitions
- MCP: 11 `coding_*` tools (no check-registration or human-decision tool). Server instructions tell agents to call `coding_resume` first.
- CLI: `continuum coding …` for humans (checks, sessions, brief, close) and for shell-only agents (resume, note, checkpoint, check, task).
- `integrations/AGENT_RULES.md` (drop-in rules for AGENTS.md/CLAUDE.md), the Claude Code project `.mcp.json`, the cloud setup script, and `docs/CODING-HANDOFF.md`. README quickstart.
- Automatic schema migration on start (`ensure_schema`, D-030).

### Fixed
- Running a check that wrote `__pycache__` changed the code fingerprint and made the result unusable as evidence.
- `coding checks add … --actor X -- cmd` lost `--actor` to argparse `REMAINDER`.
- The CLI failed with a missing table on a database created by an older version.

## 0.5.0 — 2026-10-13

### Added
- **Schema v2** (D-025):
  - CHECK constraints enforce status validity, I3 (verified ⇒ evidence), confirmed ⇒ outcome, attempt and run consistency, well-formed digests and non-negative usage
  - `TRUNCATE` on `events` is blocked
  - lookup indexes
  - `schema_meta` version
  - idempotent upgrade through `db-init`
- **Verification** (D-026):
  - requirements traceability matrix enforced by a test
  - Hypothesis stateful tests across connector modes
  - non-functional tests (NF1–NF5)
  - CI runs every layer
- `docs/OPERATIONS.md` explains how Continuum runs (no always-on server; PostgreSQL is the only service), how MCP connects over stdio, configuration and the schema design. `docs/VERIFICATION.md` covers the strategy, the defects found and the results.

### Fixed
- **Lease expiry could arrive up to one second early.** DB time was cast with `::bigint`, which rounds to nearest. Leases now use fractional seconds (`DOUBLE PRECISION`, migrated idempotently). This was found when a chaos test failed under load, then reproduced deterministically before fixing (D-027).
- Starting a run with an existing id now raises a typed `run_exists` error instead of a raw database error.

## 0.4.0 — 2026-10-13

### Fixed
- **Live runs on OpenAI and Anthropic would have been rejected:** tool names such as `store.put` violate their `^[a-zA-Z0-9_-]{1,64}$` rule. Names are now mapped bijectively and back. Generated tool-call ids are now paired with their results (D-023).

### Added
- **Model switching across versions, vendors and chats** (D-024):
  - resume a run on any `provider/model-id` (e.g. `anthropic/claude-opus-5-5`) without editing config
  - `profile: auto` in `config/models.yaml`
  - registry profiles accepted only when complete; zero price means unknown
  - LiteLLM `drop_params` for model-family parameter differences
- Contract tests against OpenAI and Anthropic request rules:
  - OpenAI → Anthropic mid-run
  - GPT-5.6 → 6.1 across chats
  - Claude Opus 4.8 → 5.5 by id
  - three-vendor round trip
- Cross-chat MCP test: separate server processes share and resume the same run.
- `docs/MODEL-SWITCHING.md`.

## 0.3.0 — 2026-10-13

Spec 0.4.

### Added
- **MCP plugin for coding agents.** The stdio server now has 8 tools: `list_tasks`, `run_task`, `resume_run`, `cancel_run`, `get_status`, `inspect_run`, `pending_decisions` and `list_runs`. It also gives agents server instructions and creates its tables automatically on start. Human decisions are never exposed (D-022).
- `integrations/` covers the following, with an end-to-end stdio test:
  - a Claude Code plugin with a skill, plus `.claude-plugin/marketplace.json`
  - configs for Cursor, VS Code, OpenAI Codex CLI and Claude Desktop
- `continuum doctor` checks the database, schema, registered tasks and live model profiles. `Continuum.pending_decisions` is a new SDK method.
- **Seamless handoff** (D-020, D-021):
  - capability-aware fallback; a context overflow prefers a larger window
  - handoff provenance in the packet
  - provider-tokenizer packet sizing (`LiteLLMAdapter.count_tokens`)
  - cost estimated from the price snapshot when the provider doesn't report it

### Changed
- `runtime.py` is split into `journal`, `budgets`, `verification` and `sessions` components (D-019). Behaviour is unchanged. `run_coro` moves to `continuum.models.base`.
- Spec 0.4: 00/01/02/05/06 and AGENTS.md describe the MCP surface and the handoff selection rules. The docs/05 packet example adds `handoff`.

## 0.2.0 — 2026-10-13

### Added
- Structured JSON tracing (`continuum.core.tracing`) covering committed events, model calls and tool dispatches (ids, statuses and durations only). The CLI flag `--log-level INFO` streams it to stderr.
- Read-only `inspect` timeline and `trace` export (`continuum trace`, `GET /runs/{id}/trace`, `Continuum.trace`): events as JSON lines, with redacted blobs inlined.
- Background lease heartbeat (`LeaseKeeper`; SDK interval is TTL/3), so a long model or tool call keeps ownership.
- Demo tasks `demo_ack_loss_recovery` and `demo_opaque_needs_human`, plus a `script` option for lab task bindings.
- Lab metric `clean_completion` (completed with no duplicate or unintended effect), with a paired difference.
- Project journal: `docs/PROJECT-JOURNAL.md`.

### Changed
- Drift that would require re-sending confirmed operations now stops the run at once with `failure_code = drift_requires_remediation` (docs/02 §7, docs/03 §5). Previously it looped until models were exhausted.
- The registered task id is stamped onto the run's spec, so resume always finds the binding that started the run.

## 0.1.0 — 2026-10-13

First implementation of the v1 MVP (spec 0.3).

### Spec changes (0.2 → 0.3)

- **Durable backend: PostgreSQL instead of SQLite** (`DECISIONS.md` D-001). Amended 00, 01, 02, 03, 04 and 06, plus `AGENTS.md`. docs/03 embeds the executable DDL, and `tests/unit/test_docs_sync.py` keeps it in sync with `src/continuum/core/schema.sql`.
- docs/05 packet example now shows the implemented fields: `builder_version`, optional `pending_milestones[].description` (removed first under compaction), the full `remaining_budget` including `cost_has_unpriced_usage`, and labelled `data_refs`.

### Added

- `continuum.core`: typed task spec and state enums; typed event payloads; a PostgreSQL store with atomic event+projection transactions and fencing; leases with epochs; action identity and state machine; the reconciler; evidence, workspace and blobs; secret redaction; the deterministic packet builder; and the run controller (start, resume, resolve, cancel, inspect).
- `continuum.tools`: trusted manifests, registry and the single dispatch gateway. Trusted adapters: `file.read`, `csv.normalize`, `report.write`, `store.put`/`store.head`, `db.update`/`db.get`, `message.send`/`message.query`.
- `continuum.verify`: verifier registry and built-in verifiers.
- `continuum.models`: provider-neutral contract and error taxonomy, deterministic fake adapters, and the LiteLLM adapter.
- `continuum.interfaces`: SDK, CLI (`continuum`), loopback FastAPI app and the stdio MCP facade.
- `continuum_lab` (`eval/`): ground-truth mock services (deduped, observable and opaque modes, with scripted faults), 15 tasks, the plan-following fake agent, the B0 baseline, fault injection, the matrix runner (`continuum-lab`), metrics (Wilson and paired bootstrap) and the Markdown report.
- Tests: unit and property, integration, contract (live tests opt-in) and chaos (every crash boundary; real-process lease races and hard kills).
- CI workflow with a PostgreSQL service, ruff, mypy `--strict`, all test layers and a lab smoke run.
