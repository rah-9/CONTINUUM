# Continuum: project journal

This document records **what we decided**, **what we built** and **what we plan to do next**. It is the narrative companion to the normative spec (`00`–`07`). The decision log (`DECISIONS.md`) holds the detailed rationale, and `CHANGELOG.md` lists each change.

## 1. The core idea we are building toward

An AI agent doing multi-step work, such as "normalize this CSV, upload it, notify ops", loses two things when something breaks. It loses **progress**: work is redone or thrown away. It also loses **certainty about side effects**: did the upload or the email actually happen?

Continuum makes both durable and model-independent:

- **Progress is runtime-owned.** Milestones count as done only when a deterministic verifier says so, and the evidence is recorded. Chat history and model claims are never authoritative.
- **Every external effect is ledgered.** One business intent maps to one logical action with a stable identity. A crash, a timeout or a different model can never turn it into a second effect.
- **Uncertainty is explicit.** A write whose result is unknown is recorded as `unknown`, not as failed. Continuum proves the outcome or asks a human **before** any new write.
- **Models are replaceable.** After a failure, a new model resumes from a deterministic packet built from verified state, not from the old transcript.

Every feature is judged by one question: *does it keep valid work and prevent unsafe repeats across failures and model switches, with evidence?*

## 2. Decisions we made

| # | Decision | Why |
|---|---|---|
| 1 | **PostgreSQL instead of SQLite**, with every other technology unchanged | Owner directive. It also gives row locks, typed `JSONB`/`NUMERIC`/`TIMESTAMPTZ` columns, and a database-enforced append-only event log. Still single-host for v1 (D-001). |
| 2 | **One function maps events to projection rows**, used for both live writes and rebuilds | Rebuild equivalence (I2) cannot drift. `projection_diff()` checks it on every resume (D-003). |
| 3 | **Write identity uses the focus milestone**, the first eligible unverified one | Deterministic after crashes and handoffs. The model cannot pick or invent identities (D-004). |
| 4 | **Arguments are bound and hashed before dispatch**, including content hashes | "Same path, different bytes" becomes an `intent_conflict`. Secrets in arguments are rejected (D-005). |
| 5 | **A negative read never proves absence** | Lagging stores would otherwise cause duplicates. Without a race-safe contract, the run pauses for a human (docs/04 §6). |
| 6 | **Confirmed effects are never re-sent** | If drift would require re-sending, the run stops with `drift_requires_remediation` instead of looping (D-014). |
| 7 | **Leases with epochs, plus a background heartbeat** | Two controllers can never both write. Long tool or model calls do not silently lose ownership (D-015). |
| 8 | **Traces contain identifiers, not payloads** | Logs are safe to ship. Full diagnostics stay in the event log and redacted blobs (D-016). |
| 9 | **The lab is an honest experiment, not a demo** | Same tools, permissions, budgets and scorer for the baseline. Pauses count as non-completion. We report clean completion alongside verified completion (D-012, D-018). |
| 10 | **Deterministic fakes first, live providers opt-in** | Safety properties are proven cheaply and reproducibly. Live calls cost money and need verified profiles. |

## 3. What we have done

### Phase 1: v0.1.0 MVP ([PR #1](https://github.com/rah-9/CONTINUUM/pull/1), merged)

- **Spec 0.3.** Every document now specifies PostgreSQL consistently, and the DDL in docs/03 is kept equal to `schema.sql` by a test.
- **Runtime:**
  - event store with atomic event+projection transactions and fencing
  - leases
  - action ledger and state machine
  - single dispatch gateway with dedupe, conflict, permission and approval checks
  - reconciler implementing the docs/04 decision table
  - verifiers with fingerprints and cascading invalidation
  - deterministic continuation packet
  - aggregate budgets
  - model handoff with retry and backoff
- **Interfaces:** SDK, CLI, loopback-only HTTP, and a stdio MCP facade (`run_task`, `get_status`).
- **Continuum Lab:**
  - ground-truth mock services in deduped, observable and opaque modes, with scripted faults
  - 15 tasks
  - B0 baseline
  - fault injector, matrix runner, Wilson/bootstrap metrics and report
- **Quality:**
  - unit/property, integration, contract and chaos tests (every crash boundary, real-process lease races, hard kill, stale owner)
  - CI on PostgreSQL 16
- **Bugs caught during the build:**
  - mock commits were lost on error paths
  - manifest retention did not mirror the connector
  - in-flight dedupe was not modeled
  - an existing write could retry while another effect was unresolved

### Phase 2: v0.2.0 hardening and observability (this change)

- **Structured JSON tracing** of events, model calls and tool dispatches. This was a PRD requirement that was still missing.
- **Read-only timeline and trace export** (`inspect`, `continuum trace`, `GET /runs/{id}/trace`).
- **Lease heartbeat**, proven by a test where a slow tool outlives the lease TTL. Without the heartbeat, the stale owner stops safely.
- **Explicit drift-remediation stop.** Previously the run wasted model calls until every model was exhausted.
- **Demo tasks** showing the core idea in two commands (ack-loss recovery, and an opaque send that needs a human).
- **Clean-completion metric** in the lab.
- **Registry identity fix.** Runs from tasks that share a definition now resume against the correct binding.

## 4. Evidence so far

Results are from the deterministic fake-model safety matrix: 15 tasks × 6 faults × 3 connector modes × 2 seeds, 912 paired cells. They test **modeled** safety properties only. They are not claims about live providers. The full report is in `lab-reports/`.

LAB_RESULTS_PLACEHOLDER

**How to read it.**

- Continuum's safety properties hold in every cell:
  - zero committed duplicates
  - zero false completions
  - zero policy bypasses
  - every ambiguous action resolved or escalated
  - all verified progress preserved after crashes
- The cost is that Continuum **pauses for a human** when the connector cannot prove an outcome (opaque services, lagging reads). The baseline "completes" those runs, but often by committing a duplicate.

## 5. What we plan next

These are ordered by value to the core idea. Each item should become its own branch and PR under the docs/06 workflow.

1. **Live provider validation.** Fill verified profiles for three providers in `config/models.yaml`. Run `CONTINUUM_LIVE=1 pytest -m live`, then run the live clean subset and the interrupted subset from docs/07 §6 within a fixed budget, and publish the results, including negative ones.
2. **Real connector contract tests.** Before trusting any real object store, database or messaging API, write a contract test proving its idempotency retention and query semantics. The manifest must mirror the tested contract.
3. **Human remediation workflow** for `drift_requires_remediation`: a reviewed "supersede operation" path that issues a new, explicit operation version instead of overwriting history. The spec must be amended first.
4. **B1 checkpoint-only baseline** (docs/07 stretch). Needed before any claim against checkpoint frameworks.
5. **Packet token accounting** with each provider's own tokenizer, replacing the 4-characters-per-token estimate.
6. **Optional OpenTelemetry export** from the JSON trace (post-MVP per the PRD).
7. **Out of scope until evidence justifies it:** multi-host deployment, a dashboard and remote MCP tool discovery.

## 6. How we work

- One narrow change per branch. Contract changes update every affected doc in the same PR, plus `CHANGELOG.md`.
- Ledger, state and recovery behaviour change only after a failing test.
- `make check` (ruff, mypy `--strict`, all tests) must pass locally. CI repeats it against PostgreSQL plus a lab smoke run. Merge only on green.
- Results are reported honestly: unknown cost is never zero, pauses are never successes, and fake-model results are never presented as live evidence.
