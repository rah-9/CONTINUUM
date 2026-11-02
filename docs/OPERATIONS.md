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
3. On startup the server runs `db-init` (idempotent), loads the pre-registered tasks from `CONTINUUM_CONFIG_DIR` and advertises 20 tools (8 for tasks, 12 for coding continuity) with instructions for the agent.
4. When the agent calls `run_task`, the server process runs the task's step loop. It acquires the run lease, calls models and trusted tools through the gateway, and verifies milestones. It returns when the run completes or pauses for a human.
5. When the agent calls `coding_resume`, the server observes the repository (five git calls, about 165 ms on a 20k-file repo), reads the session journal and returns a compact Markdown brief of about 1–3k tokens. No LLM is called; the only model cost of a handoff is the next model reading the brief.
6. Separately, the app's **hooks** run `continuum hook <app>` as short-lived processes: on each prompt and turn end they journal the conversation, and at session start (Claude Code) they print the brief into the new chat. A hook waits at most 3 s for the database (`CONTINUUM_DB_TIMEOUT_SECONDS` overrides it) and logs failures to `$CONTINUUM_HOME/hooks.log`. It exits 0; the one exception is refusing a tool call for an app whose JSON refusal format is unknown, where it exits 2 (D-045). `CONTINUUM_HOOKS=off` disables them.
7. When the agent session ends, the child process exits. The run's and the coding session's state remain in PostgreSQL.

In cloud agent containers, the same applies inside the container. Point `CONTINUUM_DATABASE_URL` at a PostgreSQL that outlives the container, set as a secret, and install Continuum in the environment's setup script (`integrations/cloud/setup.sh`). See `integrations/README.md` §4.

Human decisions never travel over MCP. For those, the agent calls `pending_decisions` and shows you the `continuum resolve …` command to run in your terminal.

## Configuration (environment only)

| Variable | Default | Notes |
|---|---|---|
| `CONTINUUM_DATABASE_URL` | `postgresql://continuum:continuum@localhost:5432/continuum` | |
| `CONTINUUM_DB_SCHEMA` | `public` | one schema per environment or test |
| `CONTINUUM_HOME` | `~/.continuum` | run workspaces, blobs, mock-service state (per user, so an IDE can start the server from any directory) |
| `CONTINUUM_CONFIG_DIR` | `./config` if it has `tasks/`, else `~/.continuum/config` | pre-registered tasks and `models.yaml` |
| `CONTINUUM_OWNER_ID` | `ctl-<host>-<pid>-<random>` | lease owner identity; if you set it, it must be unique per process, because an equal id is treated as the same controller |
| `CONTINUUM_HOOKS` | on | `off` disables `continuum hook` (chat capture and the session-start brief) |
| `CONTINUUM_LEASE_TTL_SECONDS` | `30` | heartbeat renews every TTL/3 |
| `CONTINUUM_MEMORY_MODEL` | `claude-cli:haiku` if `claude` is installed, else off | the model that keeps the working memory (D-048): `claude-cli:<model>`, `litellm:<provider/model>`, `<provider>/<model>` (e.g. `ollama/qwen3:4b`), `fake`, `off` |
| `CONTINUUM_MEMORY_API_BASE` | none | an OpenAI-compatible or self-hosted endpoint for a LiteLLM memory model |
| Provider keys | none | e.g. `ANTHROPIC_API_KEY`, `OPENAI_API_KEY`; redacted everywhere |

Run `continuum doctor` to check the database, schema, tasks and model profiles.

`CONTINUUM_CODING_ROOTS` (optional, `:`-separated; `;` on Windows) restricts which directories coding sessions may observe. `CONTINUUM_DB_TIMEOUT_SECONDS` (default 10) bounds how long a command waits for the database before reporting `database_unavailable`.

## Working memory (model-written, optional)

After each turn, a detached process updates the session's working memory with the model in `CONTINUUM_MEMORY_MODEL` (D-048). It never blocks a hook. `continuum coding memory` prints it; `--update` runs an update now and reports `updated`, `current`, `busy`, `failed` or `off`. Failures are logged to `hooks.log` under `CONTINUUM_HOME` and leave the previous memory.

- **What is sent:** the session's messages and one line per tool call (redacted, as stored) go to the configured provider. Use a local model (`ollama/<model>`, or `CONTINUUM_MEMORY_API_BASE`) or `off` when chat content must not leave the machine.
- **Cost:** one call per new part of up to ~25k tokens. Catching up this project's own 255k-token session took 12–15 Haiku calls, about 20 minutes and $0.30; an ordinary turn is one small call. With `claude-cli`, calls count against the person's Claude Code plan; a plan limit makes the update fail cleanly until it resets.
- `litellm` providers need the `live` extra (`pip install 'continuum[live]'`) and their usual key variables.

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

## Hooks, the local spool, and unknown outcomes

- **Hooks never wait for the database** (D-042). Each one redacts the payload, writes it to `$CONTINUUM_HOME/spool/` (owner-only) and starts a background drainer.
  - The drainer writes the journal in order and deletes a file only after its transaction commits. One drainer runs at a time, under an OS file lock that the kernel releases if the drainer dies.
  - With the database down, files wait and are applied later. A payload that cannot be applied goes to `spool/failed/` and is logged in `hooks.log`.
  - Measured in real Claude Code, median: 99 ms before a tool call, 120 ms after (including the content snapshot).
- **What gets snapshotted:** before and after every call that can write files (edits, shell commands), the hook stores the working copy's content as a git tree in the repository's own object store. Unreferenced objects are pruned by `git gc` as usual. A snapshot gives up after 5 s; the call is still journaled, without a diff.
  - **Outside git**, the same is kept in a private store, `$CONTINUUM_HOME/snapshots/<hash>.git`, one per directory. The directory itself is never written. Directories with more than 20,000 files, files over 10 MB, the home directory and filesystem roots are not snapshotted. `continuum coding forget` deletes the store on the machine where it runs; on other machines, delete `$CONTINUUM_HOME/snapshots/` there.
  - **Credential files** (`.env*`, `*.pem`, `*.key`, `id_rsa*`, `credentials*`, `secrets.*`) are listed as changed, but their content is never stored in a diff.
- **Unknown outcomes.** A call that could change something outside the repository blocks new external calls when it has no recorded result, or when it ended interrupted, timed out or disconnected (D-045). The app's pre-tool hook refuses them in its own format; local work continues. The refusal names the exact `resolve` command. A late result lifts the block at once. After checking what really happened:

  ```bash
  continuum coding unresolved SESSION                  # what is open
  continuum coding resolve SESSION CALL --outcome applied|not_applied|accepted_unknown \
      --actor alice --reason "the push never reached origin"
  ```
- **Drill-down:** `continuum coding show SESSION --seq N` (or `--blob ID`) prints any `[#N]` from the brief, or a stored output or diff. `continuum coding media SESSION ID --out FILE` saves an `[image #ID]` (a screenshot from the chat or a tool).
- **Lost pushes proven automatically** (D-047): after a turn, each unresolved external `git push` is checked with `git ls-remote` (read-only, no credential prompt, 15 s timeout). When the remote shows the pushed commit, it is recorded as applied by `reconciler:git` with the evidence and the block lifts. If the remote is unreachable or shows another commit, nothing is recorded and a person decides as before.
- **Images and long messages** (D-046): images and documents in chats and tool results are kept in `coding_media` (up to 10 MB each; larger ones are described, not stored). Messages of any length are kept whole (a blob beyond 16,000 characters).

## Data retention and erasure (coding sessions)

Coding sessions hold what people typed, and a client may ask for it to be deleted. History
is append-only, so erasure is a separate, deliberate, human-only operation (D-039):

```bash
# one session (prints confirmation_required without --yes)
continuum coding forget cs_123 --actor alice --reason "client request #42" --yes
# retention: every closed session untouched for 90 days
continuum coding forget --older-than-days 90 --actor ops --reason "90-day retention" --yes
# what was erased, when, by whom (no content)
continuum coding erasures
```

- **What is removed:** every event of the session, its stored outputs and diffs, and its row. Nothing of its content is left in Continuum's tables.
- **What is kept:** a tombstone in `coding_erasures` with the session id, a hash of the repository path, the event count, the person and the reason (redacted). Tombstones are immutable.
- **How the guard works:** the `coding_events` trigger refuses every UPDATE and DELETE unless the transaction has set `continuum.erasure = on`, which only `forget` does, inside the same transaction that writes the tombstone. The setting is transaction-local. TRUNCATE is always refused.
- **Retention** erases closed sessions only, unless `--include-active` is given. Schedule it with cron or a task scheduler.
- **Not covered:** PostgreSQL backups and WAL archives keep their copies until they expire; set their retention to match. Runtime run events (`continuum run`) are not personal chat data and are not erased by this command.
- **Never an MCP tool.** No model can erase memory.

## Several people, one database (isolation)

Each person connects with their own PostgreSQL role. Row-level security on the coding tables
shows every role only its own sessions, events, checks and erasure records: by the database,
not by the client (D-040).

```bash
# database administrator: one login per person
psql -c "CREATE ROLE alice LOGIN PASSWORD '...'"
# operator (the role that ran `continuum db-init` and owns the schema)
continuum admin grant alice      # alice may now use Continuum; she sees only her own
continuum admin users            # roles, session counts, access (no content)
continuum admin revoke alice     # access removed; her sessions stay until erased
continuum admin reassign alice alice_new   # her login changed: move her sessions
```

- **Each person** sets `CONTINUUM_DATABASE_URL` with their own user. Two people working in the same repository path get separate sessions; neither can read, write, close or erase the other's.
- **The operator** (schema owner) is not restricted by RLS: `continuum coding brief SESSION` works for any session, retention (`coding forget --older-than-days`) covers everyone, and `coding erasures` lists every tombstone. Lookups by repository still return only the operator's own sessions, so the operator's own coding never joins someone else's session.
- **Superusers bypass RLS.** Never give people a superuser login. Roles without a grant get `no_access` naming the command that fixes it.
- **Scope:** coding memory. The runtime tables (`continuum run`) remain the operator's; people are not granted them.
- **Upgrading** an existing database assigns its existing sessions to the role that runs the migration.

## Running in production-like settings (v1 limits)

- One host, one database (shared by several people with their own roles, above). Several controller processes on that host are safe; leases and epochs fence them.
- The HTTP API is loopback-only and unauthenticated by design. Do not expose it.
- Back up PostgreSQL normally. The event log is the source of truth, and projections can always be rebuilt from it (`inspect` reports any difference).

---

_Continuum is proprietary and confidential. © 2026 the Continuum copyright holder. All rights reserved. See [LICENSE](../LICENSE)._
