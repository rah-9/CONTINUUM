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
| Tool outputs and diffs | Stored whole (the operator's choice) after redaction, diffs included, as immutable blobs under the session's row-level security, erased with the session. Credential files (`.env*`, keys) are listed as changed, but their content is never stored. Snapshots of directories outside git are kept locally in `$CONTINUUM_HOME/snapshots/` (owner-only) and deleted by `forget`. Tool output is shown as quoted data that cannot close its quote, and it never enters the requirements; only the user's words can replace a requirement. | D-042, D-044, D-045 |
| Images and documents from chats and tools | Stored whole (the operator's choice) in `coding_media` under the session's row-level security, immutable, erased with the session. **Redaction cannot see inside an image**: a screenshot of a secret is stored as it is. Never interpreted by Continuum; shown to a model only on request. | D-046 |
| Working memory (model-written) | The session's stored, redacted messages and tool-call lines are sent to the model in `CONTINUUM_MEMORY_MODEL` (default: Claude Haiku through the person's own Claude Code login). A local model or `off` keeps chat content on the machine. The output is redacted again, its headings are demoted so it cannot forge the brief, and it is shown as a model's account; requirements, decisions and the unknown-outcome gate never come from it. | D-048, docs/OPERATIONS.md |
| Read-only probes | A lost `git push` is checked with `git ls-remote` only: arguments validated from the recorded command (no option injection), no shell, no credential prompt, 15 s timeout. A probe can only record that something happened, never that it did not. | D-047 |
| Repeating an effect of unknown outcome | A call that could act outside the repository and lost its result, or ended interrupted or timed out, blocks new external calls until a person records what happened. Each app's pre-tool hook refuses them in that app's own format (Claude Code, Codex, Gemini CLI, Cursor; exit code 2 elsewhere). | D-042, D-045, AGENTS.md invariants 5 and 8 |
| History tampering | Events are append-only, enforced by database triggers. UPDATE and TRUNCATE are always refused; DELETE only inside an audited erasure, which leaves a content-free tombstone. | `core/schema.sql` |
| A model acting beyond its authority | Models cannot verify their own work, register or change checks, approve side effects or erase memory. Those are human-only CLI commands, never MCP tools. Checks run without a shell, from argv a human registered. | AGENTS.md invariants, docs/02 §10 |
| Prompt injection through captured text | Captured and agent-written text is flattened to single lines in the brief, so it cannot forge the brief's headings or rules. Agent statements are labelled as claims. | `coding/brief.py` |
| Network exposure | The MCP server speaks stdio only. The optional HTTP API binds to loopback and is unauthenticated by design: never expose it. | docs/OPERATIONS.md |
| Hooks breaking the user's app | Hooks exit 0 except to refuse a call, have timeouts (a snapshot gives up after 5 s), answer Cursor's permission hooks with valid JSON, and log masked errors locally. | `coding/hooks.py` |
| Workspace escape | Runtime tool paths are contained in the run's workspace. | `core/evidence.py`, `tools/gateway.py` |

## Operator responsibilities

- Give each person their own PostgreSQL login, never a superuser one: superusers bypass row-level security.
- Use TLS (`sslmode=require` or stronger) when the database is not on the same host.
- Set backup and WAL-archive retention to match your erasure policy: erased data remains in backups until they expire.
- Choose where the working memory is computed: a hosted provider receives chat content; set `CONTINUUM_MEMORY_MODEL` to a local model or `off` if that is not allowed.
- Keep `CONTINUUM_DATABASE_URL` and provider keys in a secret store or environment, not in files committed to a repository.

---

_Continuum is proprietary and confidential. © 2026 the Continuum copyright holder. All rights reserved. See [LICENSE](LICENSE)._
