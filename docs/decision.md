# Agent decisions and coordination log

Shared, append-only communication for Codex, Claude, and any replacement agent working
on CONTINUUM through GitHub. Read this file before starting work and before handing off.
This is repository documentation under `docs/`, not a root-level chat file or application
asset. It does not replace `AGENTS.md`, the specifications, or runtime verification.

## Append-only rules

- Append new entries at the end. Never edit, delete, reorder, or rewrite earlier entries.
- Correct an earlier entry by appending a correction that names the entry it supersedes.
- Include a UTC timestamp, agent identity, task, decisions and reasons, changed files,
  exact checks and outcomes, blockers, and next steps.
- Distinguish user instructions, code observations, proposals, and unverified claims.
  A proposal from another agent is not user approval.
- Before changing shared files, read the latest GitHub version and append the intended
  scope. Check for another agent working on the same files.
- Synchronize before publishing. Preserve other agents' work and all log entries.
  If both agents append concurrently, retain both complete entries; do not force-push
  or replace the file with an older local copy. With the contents API, read the current
  blob SHA and supply it on update; on a conflict, read again and append to that content.
- Record a handoff when stopping, including unfinished work and its actual test status.
  Do not speak for the other agent or claim they read an entry without an acknowledgement.
- Do not put credentials, private transcripts, or unrelated personal information here.
- Continue using `docs/DECISIONS.md` for implementation rationale required by the existing
  engineering contract, and `docs/CHANGELOG.md` for contract changes. This lowercase,
  singular `docs/decision.md` is the user-requested inter-agent log.

## 2026-11-26 — Codex — User instructions and revised scope

**User instructions:** Codex and a separate Claude-powered agent will work on this same
project through GitHub. Decisions and communication must be appended to a file inside
the documentation directory. The user selected the name `decision.md` instead of
`chat.md`. The user may switch agents; the record must remain useful to successors.

**Superseded request:** The original request included deleting unnecessary GitHub
branches and preventing CI/CD triggers. The user subsequently stopped the branch-cleanup
work and narrowed this task to understanding the project and publishing this log.
No branch was deleted and no repository Actions setting or workflow was changed.

**Decision:** Publish only `docs/decision.md` on the existing default branch, `main`.
Use a documentation commit with `[skip ci]` so GitHub Actions workflows triggered by
`push` or `pull_request` can skip it. This does not disable CI/CD globally or guarantee
that third-party integrations honor the marker. Do not resume branch cleanup without
a new user request.

**Coordination status:** This establishes the shared channel. No acknowledgement from
Claude has been received in this session.

## 2026-11-26 — Codex — Project orientation and handoff

**Review scope:** Inspected the tracked repository layout, agent contract, product and
architecture specifications, dependency/build configuration, core execution and recovery
paths, coding continuity and memory paths, interface surfaces, integration instructions,
and the test organization. Module and function inventories were used to navigate the
larger codebase. This orientation is not an exhaustive correctness or security audit
of every function; future tasks still require reading the affected implementation.

**Project:** CONTINUUM is a Python 3.12+ continuity runtime, currently version
`1.0.0rc5` in `pyproject.toml`. PostgreSQL is its only durable backend. The package is
proprietary; maintain its existing license and distribution restrictions.

**Code map:**

| Area | Responsibility |
| --- | --- |
| `src/continuum/core/` | Typed task/run state; immutable events and transactional projections; ownership leases and fencing; logical-action ledger; recovery, verification, budgets, model sessions and handoff packets. |
| `src/continuum/tools/` | Trusted manifests and the single dispatch gateway; schema/allowlist/path checks, operation identity, deduplication, approval and unresolved-effect gates; local and service-backed adapters. |
| `src/continuum/verify/` | Deterministic milestone verifiers and their registry; inspect artifact content and independent observations. |
| `src/continuum/models/` | Provider-neutral messages, usage and capabilities; deterministic fake model; LiteLLM transport and profile resolution. |
| `src/continuum/coding/service.py` | Coding-session journal, tasks, notes, checkpoints, captured conversation/activity, registered checks, recall, media, memory, retention and erasure. |
| `src/continuum/coding/hooks.py`, `activity.py`, `transcript.py`, `spool.py`, `recorder.py` | Normalize app hooks/transcripts, spool capture without waiting for the database, deduplicate recorded calls, preserve redacted outputs and observed file impact. Unknown external outcomes block new external calls. |
| `src/continuum/coding/repo.py`, `snapshot.py`, `reconcile.py` | Observe repository identity/content and file changes; reconcile supported unknown pushes through read-only positive remote evidence. |
| `src/continuum/coding/brief.py`, `memory.py`, `digest.py`, `semantic.py`, `embed.py`, `working_memory.py` | Build bounded handoff context; consolidate requirements; extract journal-backed digest; optionally organize by local embeddings or maintain a labelled model-written memory. |
| `src/continuum/interfaces/` | Python SDK, CLI, loopback HTTP and local stdio MCP. MCP exposes registered task delegation and coding continuity, but not human approvals or check registration. |
| `integrations/` | Agent configuration, hooks, Claude Code commands/skill and shared agent rules. |
| `config/` | Model routing and pre-registered task definitions. |
| `eval/` | Continuum Lab, independent mock-service ground truth, fault injection, baseline comparisons and memory/handoff experiments. |
| `tests/` | Unit, integration, contract, property, chaos and nonfunctional coverage; requirement traceability. |

**Runtime flow:** A registered task starts under a lease. The controller requests model
proposals, but only the trusted gateway dispatches tools. Dispatch intent is persisted
before invocation; the outcome is recorded afterward. Recovery classifies unfinished
attempts as unknown, checks evidence drift, and reconciles uncertain effects before
continuing writes. Verifiers confer milestone completion; model prose does not.

**Coding handoff flow:** App hooks and transcript imports capture user messages, agent
reports and tool calls. Repository content and changes are independently observed.
Session events and stored outputs support recall and bounded briefs. A task is verified
only with its registered checks passing on the current content fingerprint and current
check definitions; committing identical content alone does not invalidate that fingerprint.
Across checkouts, automatic session matching requires the normalized remote and branch.
A GitHub Markdown log is separate from this PostgreSQL-backed session machinery.

**Constraints to preserve:**

- Read `AGENTS.md`, then `docs/00-README.md` and the relevant specifications.
- Persist each durable runtime event and its projection atomically.
- Keep business operation identity stable across retries and provider changes.
- Treat ambiguous writes as unknown; a negative read alone is not absence proof.
- Preserve lease fencing, evidence fingerprints, and aggregate budgets.
- Diagnostic replay must invoke neither providers nor real tools.
- Keep claims distinct from verified facts; do not add human-decision MCP tools.
- This task changes documentation only; no runtime behavior or dependencies changed.

**Validation:** `.venv/bin/pytest tests/unit -q` completed with
**208 passed in 10.08s**. No integration, chaos, live-provider, or full-suite result is
claimed for this orientation.

**Next agent:** Read this log and the engineering contract, synchronize the checkout
with GitHub, and append your identity and intended task scope. There is no feature
implementation task assigned by this session. Branch cleanup remains stopped.
