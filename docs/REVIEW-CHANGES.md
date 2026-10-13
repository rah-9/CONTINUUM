# Continuum specification review — v0.1 → v0.2

Reviewed: 2026-10-13. Scope: 8 docs + AGENTS.md. These are **revised specifications**, not implemented or runtime-tested code.

## Critical corrections

1. Split **logical action** (`actions`) from physical dispatch **attempts** (`action_attempts`). One stable action/operation key must survive model changes; prior `(run_id, key, attempt)` uniqueness did not prevent a new attempt from becoming a new logical effect.
2. Removed unsafe `logical_step_index` calculated from model proposals. Trusted operation identity comes from task/manifest-defined business keys; same key with changed args is a conflict.
3. Replaced blanket queryable-write rule "if absent, retry". A negative query may lag, and the original request may still commit. Safe absent proof needs a documented race-safe connector contract, else human review.
4. Made event insert and projection update **one SQLite transaction**. Kept lease metadata explicitly outside event sourcing; added lease epoch and takeover behavior.
5. Distinguished external action confirmation from independently **verified milestone** completion; added input/dependency/verifier version fingerprints and transitive invalidation.
6. Replaced the risky `live replay` feature with **diagnostic side-effect-free replay**; actual execution continues only through `resume` and ledger checks.
7. Made three independent providers a **tested configuration goal**, not three bespoke adapter implementations; one LiteLLM transport plus fake adapter is sufficient until contracts demand otherwise.
8. Removed unsupported claims of exact model-cost accounting, context truncation without loss, arbitrary "any model" compatibility, and exactly-once delivery against unsupported services.

## Scope reductions

- SQLite only for v1; PostgreSQL, Temporal/DBOS, distributed worker stack deferred.
- Trusted Python tools, fixed schemas and mocks only; no generic remote MCP discovery or arbitrary-code Docker sandbox in v1.
- SDK/CLI + localhost HTTP are core interfaces; a thin local stdio MCP facade exposing only pre-registered task delegation is retained from the original spec. Remote MCP tool discovery, browser/IDE integrations and dashboards are deferred.
- No OTel instrumentation requirement or tamper-evident hash chain for the MVP; structured trace JSON suffices.
- Removed code-editing benchmarks and mandatory prompt-injection task family; benchmark centers on CSV, database, object and notification side effects.
- Continuum Lab remains in `eval/` of the same codebase, not a separate project.

## Evaluation fixes

- Safety comparison distinguishes **committed duplicates** from **attempted unsafe repeats**; idempotency-enabled mocks may show zero commits for both baseline and Continuum.
- Corrected denominator choices; milestones invalidated for real input drift are not treated as lost progress.
- Faults at 25/50/75% are mapped to available milestone boundaries and report their actual fraction.
- Two live repeats are exploratory; no automatic statistical-significance claims.
- A plain retry/transcript baseline is required; checkpoint-only comparison is optional and must precede claims that Continuum beats checkpoint frameworks.

## Practical limitations retained

- Lease fencing cannot recall an external request already in flight.
- Wrong tool manifest semantics can invalidate idempotency guarantees.
- Human `retry_at_risk` may cause duplicates and must be disclosed.
- Deterministic fixtures prove only their modeled safety properties; real integrations need separate contract tests.

## External verification used during review

- SQLite transactions/one-writer isolation: https://www.sqlite.org/isolation.html
- MCP tool schema semantics and untrusted annotations: https://github.com/modelcontextprotocol/modelcontextprotocol/blob/main/docs/specification/2025-11-25/server/tools.mdx

All claims about external systems must be validated again when concrete production connectors are added.
