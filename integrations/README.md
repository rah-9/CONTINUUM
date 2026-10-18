# Continuum as a plugin for coding agents (MCP)

Continuum ships a **local stdio MCP server** (`continuum mcp`). Any MCP-capable agent can use it
to delegate pre-registered tasks and follow them. Progress stays durable and verified,
external effects are never repeated, and a run can continue on a different model from
verified state.

| Tool | Kind | Purpose |
|---|---|---|
| `list_tasks` | read | pre-registered tasks |
| `run_task(task_id, model_alias?)` | delegate | run until complete or paused |
| `resume_run(run_id, model_alias?)` | delegate | continue, optionally on another model |
| `cancel_run(run_id, reason)` | delegate | stop new effects |
| `get_status(run_id)` | read | milestones, actions, usage |
| `inspect_run(run_id)` | read | side-effect-free timeline |
| `pending_decisions(run_id)` | read | decisions only a human may make, with the command |
| `list_runs(limit?)` | read | recent runs |
| `coding_resume(repo_path, agent)` | coding | **call first in every chat**: join the repo's session, get the brief |
| `coding_start(repo_path, goal, agent, tasks?)` | coding | start a session when none exists |
| `coding_brief(session_id, detail?)` | read | verified facts, tasks, notes, rules; compact Markdown by default, `detail="full"` for all fields |
| `coding_add_task` / `coding_set_task_status` | coding | plan and track work |
| `coding_note(kind, text)` | coding | decision, context, blocker or next_step |
| `coding_checkpoint(summary)` | coding | snapshot; Continuum observes the code itself |
| `coding_run_check(name)` | coding | run a **human-registered** check, record the result |
| `coding_complete_task(task_id)` | coding | refused unless its checks pass on the current code |
| `coding_recall(query, …)` | read | search everything the session remembers (older notes, the user's earlier words, check output) |
| `coding_sessions` / `coding_close` | coding | list or close sessions |

**Coding continuity** lets you switch models mid-work (Opus 4.8 → 5.5, GPT-5.6 → 6.1, any vendor, any
new chat), **switch apps** (Claude Code ↔ Codex ↔ Cursor) and **switch machines** (laptop ↔ cloud
container), and keep the coding context. See [`docs/CODING-HANDOFF.md`](../docs/CODING-HANDOFF.md)
and the algorithm in [`docs/MEMORY-ALGORITHM.md`](../docs/MEMORY-ALGORITHM.md).

Set up a repository once: `continuum coding setup .` writes [`AGENT_RULES.md`](AGENT_RULES.md) into
`AGENTS.md` (imported by `CLAUDE.md`), Claude Code hooks and `.mcp.json`. Checks are registered only
by a person: `continuum coding checks add . tests --actor you -- pytest -q`.

**Chat capture (hooks).** So that the next app knows what you asked, even if the previous agent
never wrote it down:

| App | How |
|---|---|
| Claude Code | the plugin's `hooks/hooks.json`, or `continuum coding setup` (SessionStart also shows the brief) |
| Codex CLI | `notify = ["continuum", "hook", "codex"]` in `~/.codex/config.toml` ([`codex/config.toml`](codex/config.toml)) |
| any app with JSON hooks | run `continuum hook <app-name>`; it reads `prompt`, `transcript_path`, `cwd`, `last_assistant_message` |
| apps without hooks | `continuum coding import <chat transcript>` or `continuum coding capture "<text>"` |

A hook always exits 0 and never blocks your app. `CONTINUUM_HOOKS=off` turns it off.

**Human decisions are intentionally not tools.** These include approving a gated action, confirming an
uncertain effect, `retry_at_risk` and `abort`. A model must not approve its own side effects. The
agent shows the user the exact `continuum resolve …` command instead.

## 1. Install once (Linux, macOS, Windows)

You need **PostgreSQL 14+** (local install, Docker, or hosted) and the `continuum` command on
your PATH. The same command works on every OS:

```bash
uv tool install "continuum[mcp] @ git+https://github.com/rah-9/CONTINUUM"   # or: pipx install …
export CONTINUUM_DATABASE_URL=postgresql://continuum:continuum@localhost:5432/continuum
continuum doctor                               # checks DB, schema, tasks and model profiles
```

On Windows (PowerShell), set the variable with `setx CONTINUUM_DATABASE_URL "postgresql://…"`.
Developing Continuum itself? Clone the repo and run `make install` instead.

The server creates its tables on start. State outside PostgreSQL goes in `~/.continuum`
(`CONTINUUM_HOME`), never in the directory the agent happens to start the server in.
Pre-registered tasks are read from `./config` when it exists, else `~/.continuum/config`
(`CONTINUUM_CONFIG_DIR`). Coding continuity needs no task configuration.

## 2. Connect your agent

Every client launches the same stdio command, `continuum mcp`:

| Agent / IDE | Config in this folder | Where it goes |
|---|---|---|
| Claude Code (plugin + skill) | `claude-code/` (marketplace: `../.claude-plugin/`) | `/plugin marketplace add rah-9/CONTINUUM`, then `/plugin install continuum@continuum` |
| Claude Code (server only) | `claude-code/project.mcp.json` | `.mcp.json` at the repo root, or `claude mcp add continuum -- continuum mcp` |
| Cursor | `cursor/mcp.json` | `.cursor/mcp.json` (project) or `~/.cursor/mcp.json` |
| VS Code (Copilot agent mode) | `vscode/mcp.json` | `.vscode/mcp.json` |
| OpenAI Codex CLI / IDE extension | `codex/config.toml` | append to `~/.codex/config.toml` |
| Windsurf | `windsurf/mcp_config.json` | `~/.codeium/windsurf/mcp_config.json` |
| Gemini CLI | `gemini-cli/settings.json` | merge into `~/.gemini/settings.json` or `.gemini/settings.json` |
| Cline | `cline/cline_mcp_settings.json` | Cline → MCP Servers → Configure (merge) |
| Claude Desktop | `claude-desktop/claude_desktop_config.json` | merge into its config |
| JetBrains AI Assistant, Zed, any other MCP client | — | add a stdio server: command `continuum`, args `mcp` |
| Agents without MCP | — | the CLI: `continuum coding resume --repo . --agent <model>` (see `AGENT_RULES.md`) |

Only `CONTINUUM_DATABASE_URL` needs to be set, either in the config's `env` or in your environment.
If the IDE can't find `continuum`, use its absolute path (`uv tool dir --bin` shows it; on
Windows it is `continuum.exe`).

## 3. Try it

Ask your agent: *"Use Continuum to run `demo_opaque_needs_human`, then tell me what decision
is pending."* The run pauses because an opaque send's outcome is unknown. The agent calls
`pending_decisions` and hands you the `continuum resolve` command. After you run it, the agent
calls `resume_run`, and the run completes with exactly one message sent.

## 4. In the cloud (Claude Code on the web, Codex cloud) and in Codex

`continuum mcp` is always a **local child process of the agent**. In the cloud, "local" means
inside the agent's container. Nothing listens on a network port, and there is no Continuum server to host.
Two things make it work across ephemeral containers:

1. **A PostgreSQL that outlives the container.** Use any hosted PostgreSQL 14+ (Neon, Supabase,
   RDS, Cloud SQL, your own). Put its URL in the environment's **secrets / environment variables**
   as `CONTINUUM_DATABASE_URL`, never in git. Make sure the environment's network policy allows that
   host. Each new container (and each new model) then finds the same coding session by repository.
2. **Continuum installed by the setup script.** Use [`cloud/setup.sh`](cloud/setup.sh) as, or inside, the
   environment's setup script. It installs `continuum[mcp]` and runs `db-init`.

Then connect the agent:

- **Claude Code (local or on the web):** commit [`claude-code/project.mcp.json`](claude-code/project.mcp.json)
  as `.mcp.json` at your repo root. Claude Code starts `continuum mcp` for that project, with the URL
  taken from the environment. Add the rules to `CLAUDE.md`.
- **Codex CLI:** add [`codex/config.toml`](codex/config.toml) to `~/.codex/config.toml` and the rules to
  `AGENTS.md`. Switching `gpt-5.6` → `gpt-6.1` (`/model`, or a new `codex` session) needs nothing more.
  The new model calls `coding_resume` because `AGENTS.md` tells it to.
- **Codex cloud and other agents without MCP:** the rules work through the shell instead
  (`continuum coding resume --repo . --agent <model>`, etc.). The protocol and the database are the same.

**Limit:** sessions are keyed by the repository's absolute path, and Continuum observes the code at
that path. Cloud environments normally check a repo out at the same path every time, which works. If
your checkout path changes between containers, keep it stable (for example with a symlink). Keying by
git remote is planned (docs/PROJECT-JOURNAL.md §5).
