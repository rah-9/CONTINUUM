# How Continuum runs

## Is it a server?

**No always-on Continuum server is needed.** The only long-running service is **PostgreSQL**, which holds all durable state. Continuum itself is a Python library, and every way of using it is a short-lived process that opens the database, does its work and exits:

| Entry point | Process lifetime | Started by |
|---|---|---|
| `continuum run/resume/status/resolve …` (CLI) | one command | you, a script, cron |
| `from continuum.interfaces.sdk import Continuum` (SDK) | your program | your code |
| `continuum mcp` (MCP server, stdio) | while the agent session is open | **your coding agent**, automatically |
| `continuum serve` (HTTP, optional) | until you stop it | you; loopback `127.0.0.1` only |

Because state lives only in PostgreSQL, these processes are interchangeable. A run started from Claude Code over MCP can be checked with the CLI, resolved by a human with the CLI, and resumed from Cursor or Codex. Any process can pick it up, and leases ensure that only one controller drives a run at a time.

```text
 Claude Code / Cursor / Codex / VS Code / Claude Desktop           you (terminal)
          │  spawns `continuum mcp` on startup                          │
          │  JSON-RPC over the process's stdin/stdout                   │ continuum CLI
          ▼                                                            ▼
 ┌────────────────────────────┐      ┌────────────────────────────────────────┐
 │ continuum mcp (stdio)      │      │ continuum resolve / status / resume …  │
 │  list_tasks, run_task, …   │      └───────────────────┬────────────────────┘
 └─────────────┬──────────────┘                          │
               │  SDK → Runtime (lease, gateway, ledger, verifiers, sessions)
               ▼                                         ▼
        ┌──────────────────────────────────────────────────────────┐
        │                PostgreSQL (the only server)                │
        │  events (append-only) + projections + leases + schema_meta │
        └──────────────────────────────────────────────────────────┘
               │ trusted tools (gateway only)      │ model calls (LiteLLM)
               ▼                                   ▼
     object store / database / messaging      OpenAI / Anthropic / Gemini / …
```

## How the MCP connection works

1. You register the server once in your agent. Examples:
   - `claude mcp add continuum -- continuum mcp`
   - `.cursor/mcp.json`
   - `~/.codex/config.toml`
   
   See `integrations/README.md` for every client.
2. When the agent starts, it **spawns `continuum mcp` as a child process**. The two talk MCP (JSON-RPC 2.0) over the child's stdin and stdout. No network port is opened. Logs go to stderr, so they never corrupt the protocol stream.
3. On startup the server runs `db-init` (idempotent), loads the pre-registered tasks from `CONTINUUM_CONFIG_DIR` and advertises 19 tools (8 for tasks, 11 for coding continuity) with instructions for the agent.
4. When the agent calls `run_task`, the server process runs the task's step loop. It acquires the run lease, calls models and trusted tools through the gateway, and verifies milestones. It returns when the run completes or pauses for a human.
5. When the agent session ends, the child process exits. The run's state remains in PostgreSQL.

In cloud agent containers, the same applies inside the container. Point `CONTINUUM_DATABASE_URL` at a PostgreSQL that outlives the container, set as a secret, and install Continuum in the environment's setup script (`integrations/cloud/setup.sh`). See `integrations/README.md` §4.

Human decisions never travel over MCP. For those, the agent calls `pending_decisions` and shows you the `continuum resolve …` command to run in your terminal.

## Configuration (environment only)

| Variable | Default | Notes |
|---|---|---|
| `CONTINUUM_DATABASE_URL` | `postgresql://continuum:continuum@localhost:5432/continuum` | |
| `CONTINUUM_DB_SCHEMA` | `public` | one schema per environment or test |
| `CONTINUUM_HOME` | `~/.continuum` | run workspaces, blobs, mock-service state (per user, so an IDE can start the server from any directory) |
| `CONTINUUM_CONFIG_DIR` | `./config` if it has `tasks/`, else `~/.continuum/config` | pre-registered tasks and `models.yaml` |
| `CONTINUUM_OWNER_ID` | `ctl-<host>-<pid>` | lease owner identity |
| `CONTINUUM_LEASE_TTL_SECONDS` | `30` | heartbeat renews every TTL/3 |
| Provider keys | none | e.g. `ANTHROPIC_API_KEY`, `OPENAI_API_KEY`; redacted everywhere |

Run `continuum doctor` to check the database, schema, tasks and model profiles.

`CONTINUUM_CODING_ROOTS` (optional, `:`-separated; `;` on Windows) restricts which directories coding sessions may observe. `CONTINUUM_DB_TIMEOUT_SECONDS` (default 10) bounds how long a command waits for the database before reporting `database_unavailable`.

## Database schema (designed for invariants)

All durable state lives in PostgreSQL. The schema is executable in `src/continuum/core/schema.sql` and mirrored in docs/03.

- **`events`**: an append-only log per run. `(run_id, seq)` is gapless. `UPDATE`, `DELETE` and `TRUNCATE` are rejected by triggers.
- **Projections**, rebuilt exactly from events:
  - `runs`: lifecycle
  - `milestones`: verified progress
  - `actions`: one row per business operation, `UNIQUE(run_id, operation_key)`
  - `action_attempts`: physical dispatches
  - `artifacts`
  - `approvals`
  - `model_sessions`
  - `usage_totals`
- **`run_leases`**: controller ownership, deliberately not event-sourced. The epoch increases on every takeover.
- **`schema_meta`**: schema version.
- **CHECK constraints** enforce the core invariants inside the database itself, even if application code regresses:
  - valid statuses
  - a verified milestone always has its fingerprint and evidence
  - a confirmed action always has an outcome
  - an attempt is finished exactly when it is no longer `started`
  - a terminal run always has `finished_at`
  - non-negative usage
- **Indexes** cover every hot lookup: actions by status, sessions and artifacts by run, and a partial index on unfinished attempts used by recovery.

Upgrades: re-running `continuum db-init` adds new constraints and indexes to an existing database idempotently.

## Running in production-like settings (v1 limits)

- One host, one database. Several controller processes on that host are safe; leases and epochs fence them.
- The HTTP API is loopback-only and unauthenticated by design. Do not expose it.
- Back up PostgreSQL normally. The event log is the source of truth, and projections can always be rebuilt from it (`inspect` reports any difference).
