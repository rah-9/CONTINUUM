# Changelog

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
