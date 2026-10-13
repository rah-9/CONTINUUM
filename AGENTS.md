# AGENTS.md — Continuum coding-agent contract

Read `docs/00-README.md`, then `docs/01-PRD.md` and the specification(s) relevant to your assigned task. This project is **not** a general agent framework; implement only the scoped continuity runtime. If documents conflict, identify the exact conflict, propose the smallest consistent amendment, and update all affected documents before modifying semantics.

## Non-negotiable invariants

1. **Runtime-owned state:** chat history is not authoritative progress; a model cannot verify milestones or approve its own side effects.
2. **Single tool dispatch path:** adapters and step loop never call tools; all execution (including verifier/reconciler read probes) passes through trusted gateway with schema, allowlist and ledger checks. Fresh read-only calls do not get cached as duplicate writes.
3. **Atomic persistence:** for every durable state transition, append an event **and** apply its projection within the same PostgreSQL transaction; historical events are immutable (enforced by a trigger).
4. **One business intent, one logical action:** `(run_id, operation_key)` uniquely binds immutable arguments, action ID and (when supported) idempotency key. The model's call ID is never operation identity.
5. **No blind retries:** ambiguous writes are unknown, not failed. Auto-retry requires tested idempotency within retention or proven-safe non-commit. “Not found” from a lagging query is not absence proof.
6. **Lease fencing:** check active lease+epoch for state mutations and before dispatch. A stale worker response must not overwrite newer state; previously in-flight external effects still need reconciliation.
7. **Evidence-based progress:** milestone verifier, input/dependency fingerprint and referenced artifacts must agree. Drift invalidates the milestone and downstream dependents.
8. **Safe handoff:** no new side-effecting action while an unknown effect remains unresolved. Packet carries valid evidence and budgets, not unverified prior model claims as facts.
9. **No silent model degradation:** reject unsupported capabilities and oversize packets rather than dropping essential state or policy.
10. **No live replay:** diagnostic replay invokes no provider and no real tool. `resume` is the only actual continuation mechanism.

## Workflow rules

- Python 3.12+, Pydantic v2, PostgreSQL via psycopg 3, pytest, hypothesis, ruff, mypy. Prefer few dependencies and local-first development.
- One vertical task per branch/PR; write failing core tests before changing ledger, state or recovery behavior.
- Use deterministic fake model and trusted mocks by default. Live calls are explicitly opt-in and must respect budgets.
- Keep API keys out of git, event payloads, packets and test artifacts. Mask errors and enforce workspace path containment.
- No dashboard, Redis, distributed orchestrator, vector DB, arbitrary code runner, production hosting, remote MCP tool discovery or IDE/browser plugins in v1. The only v1 MCP surface is the local stdio server for pre-registered task delegation and read-only inspection; never add human-decision tools (approve/confirm/retry/abort) to it.
- Any change to state, events, action identity, verification or packet rules requires matching doc updates and a short entry in `docs/CHANGELOG.md`. Dependency additions require `docs/DECISIONS.md` rationale.
- Do not solve ambiguity by inventing business semantics. Ask for or document a specific decision; do not implement potentially unsafe guesses.

## Definition of done

- Acceptance tests pass, including the failure/restart path. `ruff`, `mypy` and relevant unit/integration/contract checks pass.
- New state transitions have positive and negative tests; unknown-action handling includes commit-then-drop and eventual-read-lag cases.
- No unauthorized or duplicate logical write is possible in included deterministic fixtures; human risk overrides are explicitly recorded.
- No stale projection, wrong evidence fingerprint, silent usage-zero substitution or real tool call from replay.
- Changed docs stay mutually consistent; summary states exact tests run and remaining limitations.
