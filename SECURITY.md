# Security

Continuum is proprietary software. This page explains how to report a vulnerability, which
versions receive fixes, and what Continuum protects, and how.

## Reporting a vulnerability

- Report privately to the copyright holder, through the support channel named in your license agreement.
- Never use a public issue, chat or forum.
- Include the version (`python -c "import continuum; print(continuum.__version__)"`), the operating system, the steps to reproduce, and what an attacker gains.
- Do not include real secrets or personal data. Redact them, or describe their shape.

Reports are acknowledged within 3 business days. A fix or mitigation for a confirmed issue
is targeted within 30 days, sooner for issues that expose captured chat or credentials.

## Supported versions

| Version | Security fixes |
|---|---|
| 1.0.x (including release candidates) | yes |
| 0.9.x and older | no: upgrade (see [docs/UPGRADING.md](docs/UPGRADING.md)) |

## What Continuum protects, and how

| Concern | Control | Where |
|---|---|---|
| API keys and secrets | Read from the environment only. Never written to config, events, packets, briefs or logs. Known secret values, URL credentials and common token formats are redacted before anything is stored, including captured chat and recorded commands. | `core/redaction.py`, D-016 |
| Captured chat (personal data) | Stored only in your PostgreSQL. Each person's sessions are visible only to their own database role (row-level security). A human erases a session completely with `continuum coding forget`; retention is automated with `--older-than-days`. | D-039, D-040, docs/OPERATIONS.md |
| History tampering | Events are append-only, enforced by database triggers. UPDATE and TRUNCATE are always refused; DELETE only inside an audited erasure, which leaves a content-free tombstone. | `core/schema.sql` |
| A model acting beyond its authority | Models cannot verify their own work, register or change checks, approve side effects or erase memory. Those are human-only CLI commands, never MCP tools. Checks run without a shell, from argv a human registered. | AGENTS.md invariants, docs/02 §10 |
| Prompt injection through captured text | Captured and agent-written text is flattened to single lines in the brief, so it cannot forge the brief's headings or rules. Agent statements are labelled as claims. | `coding/brief.py` |
| Network exposure | The MCP server speaks stdio only. The optional HTTP API binds to loopback and is unauthenticated by design: never expose it. | docs/OPERATIONS.md |
| Hooks breaking the user's app | Hooks always exit 0, have timeouts, and log masked errors locally. | `coding/hooks.py` |
| Workspace escape | Runtime tool paths are contained in the run's workspace. | `core/evidence.py`, `tools/gateway.py` |

## Operator responsibilities

- Give each person their own PostgreSQL login, never a superuser one: superusers bypass row-level security.
- Use TLS (`sslmode=require` or stronger) when the database is not on the same host.
- Set backup and WAL-archive retention to match your erasure policy: erased data remains in backups until they expire.
- Keep `CONTINUUM_DATABASE_URL` and provider keys in a secret store or environment, not in files committed to a repository.

---

_Continuum is proprietary and confidential. © 2026 the Continuum copyright holder. All rights reserved. See [LICENSE](LICENSE)._
