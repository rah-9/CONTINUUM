# Continuum — specification index

**Spec version:** 0.4 (2026-10-13: PostgreSQL backend, D-001; MCP plugin surface, D-022)  
**Status:** v0.1.0 implementation in `src/`, `eval/` and `tests/`; see `CHANGELOG.md`

**Purpose:** keep multi-step agent progress in runtime-owned, model-independent state. After an interruption or model handoff, reuse *valid verified work* and reconcile any uncertain external effect **before** new side-effecting work.

This is a **single project**. Continuum Lab is its in-repository fault-injection/evaluation suite, not a separate product.

## Read in order

1. [01-PRD.md](01-PRD.md): problem, hard requirements, exclusions, acceptance
2. [02-ARCHITECTURE-AND-FLOWS.md](02-ARCHITECTURE-AND-FLOWS.md): component boundaries, runtime and recovery flows
3. [03-STATE-SCHEMA.md](03-STATE-SCHEMA.md): durable entities, PostgreSQL schema, invariants
4. [04-EVENT-LOG-AND-LEDGER.md](04-EVENT-LOG-AND-LEDGER.md): state transitions, identity, retry/reconciliation policy
5. [05-MODEL-ADAPTERS.md](05-MODEL-ADAPTERS.md): provider contract, capability limits and packet
6. [06-DEV-WORKFLOW.md](06-DEV-WORKFLOW.md): implementation sequence, repo structure, gates
7. [07-EVAL-PLAN.md](07-EVAL-PLAN.md): controlled tests, mocks, measurements
8. [../AGENTS.md](../AGENTS.md): binding coding-agent instructions

## Normative precedence

For any conflict: **04 governs effects/retries; 03 governs persisted state; 02 governs orchestration; 05 governs model boundaries; 01 governs scope and acceptance**. Do not resolve genuine contradictions silently—fix all affected documents before implementing.

## Core vocabulary

- **Task spec:** developer-defined goal, allowed tools, verifiable milestones, limits and business operation identities.
- **Run:** one execution across zero or more model sessions, identified by `run_id`.
- **Milestone:** a checkable intermediate outcome with dependencies and a deterministic verifier.
- **Evidence:** verifier-produced references, digests and observations (not a model's claim).
- **Logical action:** one intended tool-side effect identified by immutable `action_id` and stable `operation_key`. One action can have multiple physical **attempts**.
- **Unknown:** a dispatched action may or may not have committed. It is *not* equivalent to failure.
- **Reconciliation:** establish the actual effect from authoritative evidence; retry only under an explicitly safe contract, otherwise pause for human review.
- **Handoff:** restart model reasoning from a deterministic packet assembled from runtime state.
- **Continuum Lab:** the test harness under `eval/` in this same repository.

## Explicit guarantees and limits

- For tasks and tools meeting the declared contracts, Continuum **attempts to retain verified progress and avoid unsafe duplicate dispatches**. It does **not** guarantee exactly-once effects across arbitrary external services.
- PostgreSQL (14+) is the **only** durable backend in v1, for a local single-host runtime (one database, controllers on one machine). Multiple workers claiming one run are blocked by lease/epoch rules, but external side effects still require reconciliation.
- v1 has a Python SDK, CLI, minimal localhost HTTP surface and a **local stdio MCP server** for task delegation and read-only inspection (`list_tasks`, `run_task`, `resume_run`, `cancel_run`, `get_status`, `inspect_run`, `pending_decisions`, `list_runs`), so any MCP-capable coding agent can use Continuum as a plugin. Human decisions are never exposed through MCP. Distributed/multi-host deployment, arbitrary MCP tool ingestion, code execution, dashboards and production hosting are **post-MVP**.

## Repo layout note

This file and `01`–`07` live in `docs/`; `AGENTS.md` is at the repo root. `docs/DECISIONS.md` records implementation decisions and `docs/CHANGELOG.md` contract changes.
