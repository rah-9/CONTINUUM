# Changelog

## 0.7.0 — 2026-10-14

Measured, then improved (D-034). All numbers are on a 20,000-file git repository with 200 changed and untracked files.

### Changed
- **The MCP coding tools return a compact brief by default.** `coding_resume`, `coding_start` and `coding_brief` return the Markdown brief plus the task ids, about 1.3k tokens. `detail="full"` returns every field as JSON. Previously the full JSON *and* the same content as Markdown were sent on every handoff (about 8.3k tokens for a 30-note session).
- **The brief is bounded however long the session runs.** It keeps the newest 12 decisions, 10 context notes, 10 blockers, 5 next steps and 5 checkpoints. Notes are clipped to 500 characters (300 in Markdown) and file lists are capped. Every omission is counted (`agent_notes.omitted`), and the journal keeps everything. The full JSON stays under about 8k tokens; a test doubles the session length and asserts the brief does not grow.

### Added
- **`verified_facts.commits_since_last_checkpoint`**, observed from git: the next model sees exactly which commits were made since the last checkpoint. When history was rewritten the value is `None` (unknown), never a guess.

### Performance
- **Fingerprint:** about 2× faster (120–170 ms → about 70 ms), down from 7 to 5 git processes. Untracked files now come from the single `git status` call, and the index is listed only when `.gitmodules` exists. Coding operations are 35–55% faster end to end; for example `resume` went from 132 to 83 ms and `run_check` from 270 to 168 ms. A non-functional test pins the git-call count.

## 0.6.3 — 2026-10-14

This release adds a portability pass and a second full-codebase audit. The portability pass ran the built wheel in a clean environment, launched like an IDE does. The audit found 17 new defects, and the portability pass found 4 more. All are fixed, each with a regression test written first (`tests/integration/test_pass2_hardening.py`, `tests/integration/test_portability.py`; D-033).

### Fixed: any agent, any IDE, any OS, local and cloud
- **Windows.**
  - The CLI crashed with `UnicodeEncodeError` when an agent read it through a pipe (cp1252). Output is now always UTF-8.
  - Checks like `npm test` failed (`npm.cmd` cannot start without a shell); commands are now resolved via PATH/PATHEXT.
  - The lab's mock services imported POSIX-only `fcntl`.
  - Config was read with the ANSI code page; it is now UTF-8 everywhere.
- **IDE launch directory.** State and config defaulted to the server's working directory (Claude Desktop uses `/`). They now default to `~/.continuum`.
- **Cloud.** `setup.sh` used system `pip`, which PEP 668 images refuse and which needs Python 3.12. It now uses `uv tool --python 3.12` and links `continuum` onto PATH.
- **Configs.**
  - Shipped IDE configs had `/absolute/path` placeholders.
  - The Claude Code project config now falls back to the local DSN.
  - Configs added for Windsurf, Gemini CLI and Cline.

### Fixed: correctness and safety
- **Model-controlled input crashed the step loop instead of being rejected.**
  - A path escape in `csv.normalize` `source` or `store.put` `artifact_ref`.
  - NUL characters in tool arguments.

  Both are now `ActionRejected` (`path_not_permitted`, `invalid_args`).
- **NUL characters** in coding notes, goals or binary check output made every journal write fail. They are stored as U+FFFD.
- **Fingerprint blind spots** (fail-open):
  - edits inside a dirty git submodule (now fingerprinted recursively)
  - untracked files with non-UTF-8 names (hashed as raw bytes; such names, or a symlink target, also crashed observation)
- **Projection/replay equivalence (I2).** Float-noise costs (`3.0000000000000004e-06`) never matched the `NUMERIC(20,10)` row, so every resume rebuilt the projection and `inspect` reported it inconsistent. Costs are now quantized to the column's precision.
- **A human `abort` on an ended run** rewrote it from cancelled to failed. Terminal runs are never rewritten.
- **Checks.**
  - Checks received `CONTINUUM_*` credentials. Agent-written test code could reach the journal's database and register checks.
  - Database URL passwords were not redacted from check output.
  - On timeout only the direct child was killed, so grandchildren kept changing the tree. On Windows, captured pipes could hang. Each check now runs in its own process group, with output written to a bounded file.
- **Lease identity.** Default owner ids could collide between containers with the same hostname and pid, which defeated fencing. They now include a per-process random nonce.
- **Budgets.** Active time skipped recovery work and truncated long iterations to the 60 s cap. It is now recorded around every model call and dispatch batch, and at the end of each operation.
- **Crashes.**
  - Undecodable or duplicate task files took down every command; they are now reported by `doctor`.
  - A mixed-case `CONTINUUM_DB_SCHEMA` broke `db-init`; schema names are validated.
  - Bad `--log-level` values and unexpected errors produced raw CLI tracebacks; they are now masked.
  - A missing `[live]` extra crashed runs; it is now a `ModelError`.
- **Verifier messages** were stored unredacted and unbounded, and shown to the model. They are now redacted and limited to 500 characters.

## 0.6.2 — 2026-10-14

The full-codebase audit, continued into the core runtime and interfaces, found 9 issues. All are fixed, each with a regression test written first (`tests/integration/test_core_hardening.py`, D-032).

### Fixed
- **A transient read error permanently failed runs (high).** On resume or finalize, a verifier `ERROR` (for example a `store.head` timeout) was treated like `FAIL`. It invalidated the milestone and could end the run with `drift_requires_remediation`. Now an error records `VerifierError`, invalidates nothing, and pauses in `awaiting_reconciliation`. The next resume re-observes.
- **Concurrent operations in one process shared a lease (high).** Two HTTP requests in one `continuum serve` used the same controller id, so a second resume, resolve or cancel re-entered the live lease with the same epoch. Its `release` then expired the lease under the running driver. Runs are now also claimed per process for each operation, so a concurrent operation gets `already_owned`.
- **DNS rebinding against the loopback HTTP API (medium).** Requests whose `Host` or `Origin` is not loopback are refused.
- **Unknown model alias (medium).** It is rejected before any run is created (start) or any lease is taken (resume), and HTTP returns 422 instead of 500. Previously an orphan run was left `running`.
- **Raw tracebacks from interfaces (medium).**
  - Malformed task files are reported (`continuum doctor` → `invalid_files`) instead of breaking every command.
  - Bad factories and a broken `models.yaml` are typed `invalid_spec` errors.
  - Read-only inspection never loads live models.
  - MCP masks unexpected errors (`code: internal`), and `list_runs` tolerates an unreadable run.
  - The CLI reports `database_unavailable` (bounded by `CONTINUUM_DB_TIMEOUT_SECONDS`, default 10).
- **Costs (medium).** A model-registry cost of 0 is now unknown, never "free". A configured price snapshot replaces LiteLLM's generic estimate.
- **Secrets in rejection reasons (low).** Schema and path errors quoting argument values are redacted before they are persisted.
- **Malformed provider responses (low)** become a typed `ModelError`, so the run hands off instead of crashing.
- **Blob references (low).** They are validated as `blob:sha256:<64 hex>`. A human receipt (`receipt:123`) is never read as a blob, and a nonexistent blob is refused as evidence.

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
